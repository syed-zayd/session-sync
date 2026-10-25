"""Edge cases across all four adapters."""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sessync import sync
from sessync.adapters import ADAPTERS, antigravity, claude_code, codex, kiro
from sessync.canonical import Message, Session, coalesce

N = datetime.now(timezone.utc)


def sess(cwd, msgs, tool="claude", title="Edge case"):
    return Session(tool=tool, session_id="", title=title, cwd=cwd,
                   created_at=N, updated_at=N, messages=msgs)


def turns(pairs):
    return [Message(role, text, N + timedelta(seconds=i))
            for i, (role, text) in enumerate(pairs)]


@pytest.mark.parametrize("tool", ["codex", "kiro", "antigravity"])
def test_unicode_survives_every_hop(work, tool):
    tricky = "café ☕ 日本語 مرحبا 🎉 emoji + \"quotes\" + \\backslash\\ + \ttab"
    src = claude_code.write(sess(work, turns([("user", tricky),
                                              ("assistant", "ack " + tricky)])))
    r = sync.port("claude", src, tool)
    got = ADAPTERS[tool].read(r.session_id)
    assert got.content_hash() == claude_code.read(src).content_hash()


@pytest.mark.parametrize("tool", ["codex", "kiro", "antigravity"])
def test_working_directory_survives(work, tool):
    """Resume pickers filter by cwd, so a mirror with the wrong one is lost."""
    src = claude_code.write(sess(work, turns([("user", "where am I"),
                                              ("assistant", "here")])))
    got = ADAPTERS[tool].read(sync.port("claude", src, tool).session_id)
    assert os.path.normcase(got.cwd) == os.path.normcase(work)


def test_very_large_message(work):
    big = "x" * 500_000
    src = claude_code.write(sess(work, turns([("user", big),
                                              ("assistant", f"got {len(big)}")])))
    r = sync.port("claude", src, "codex")
    assert codex.read(r.session_id).content_hash() == claude_code.read(src).content_hash()


def test_empty_session_is_refused(work):
    empty = claude_code.write(sess(work, []))
    with pytest.raises(ValueError):
        sync.port("claude", empty, "codex")


def test_user_only_session(work):
    src = claude_code.write(sess(work, turns([("user", "hello?")])))
    r = sync.port("claude", src, "codex")
    assert r.action == "created"
    assert len(codex.read(r.session_id).messages) == 1


def test_tool_only_turns(work):
    src = claude_code.write(sess(work, turns([
        ("user", "run it"), ("assistant", "[used tools: Bash]"),
        ("user", "and again"), ("assistant", "[used tools: Bash, Read]")])))
    r = sync.port("claude", src, "codex")
    assert len(codex.read(r.session_id).messages) == 4


def test_chained_ports_do_not_stack_headers(work):
    src = claude_code.write(sess(work, turns([("user", "one"), ("assistant", "two")])))
    r1 = sync.port("claude", src, "codex")
    r2 = sync.port("codex", r1.session_id, "kiro")
    r3 = sync.port("kiro", r2.session_id, "antigravity")
    final = antigravity.read(r3.session_id)
    assert len(final.messages) == 2
    assert not any("ported from" in m.text for m in final.messages)
    assert final.content_hash() == claude_code.read(src).content_hash()


def test_round_trip_preserves_hash(work):
    src = claude_code.write(sess(work, turns([("user", "alpha"), ("assistant", "beta"),
                                              ("user", "gamma")])))
    back = sync.port("claude", src, "codex")
    r = sync.port("codex", back.session_id, "kiro")
    assert kiro.read(r.session_id).content_hash() == claude_code.read(src).content_hash()


def test_round_trip_keeps_origin_title(work):
    """Claude -> Codex -> back to Claude must not retitle the original."""
    src = claude_code.write(sess(work, turns([("user", "hi"), ("assistant", "yo")])),
                            title="My original")
    mirror = sync.port("claude", src, "codex").session_id
    r = sync.port("codex", mirror, "claude")
    assert (r.action, r.session_id) == ("unchanged", src)
    assert claude_code.read(src).title == "My original"


def test_concurrent_pushes_make_one_session(work):
    src = claude_code.write(sess(work, turns([("user", "race"),
                                              ("assistant", "condition")])))
    results = []

    def go():
        try:
            results.append(sync.port("claude", src, "codex").session_id)
        except Exception as exc:                     # noqa: BLE001
            results.append(f"ERR {exc}")

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert not results[0].startswith("ERR")


def test_coalesce_treats_tool_cycles_as_turn_boundaries():
    mixed = coalesce(turns([("assistant", "a1"), ("assistant", "[used tools: Bash]"),
                            ("assistant", "a2"), ("user", "u1"), ("user", "u2")]))
    # "a1" is toolless, so the note folds into it and becomes a structured call.
    # "a2" then starts a new turn: a turn that made a tool call is a closed
    # request/response cycle, and folding the next one in would misstate it.
    assert (mixed[0].role, mixed[0].text, [t.name for t in mixed[0].tools]) == \
        ("assistant", "a1", ["shell"])
    assert sum(1 for m in mixed if m.role == "assistant") == 2
    assert sum(1 for m in mixed if m.role == "user") == 2
