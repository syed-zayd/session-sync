"""Content survives a port, in every direction and across chained hops."""
import json
import threading

import pytest

TARGETS = ("codex", "kiro", "antigravity")


@pytest.mark.parametrize("target", TARGETS)
def test_unicode_survives(ss, target):
    tricky = 'café ☕ 日本語 مرحبا 🎉 "quotes" \\backslash\\ \ttab'
    src = ss.claude.write(ss.session(ss.turns(tricky, "ack " + tricky)))
    r = ss.sync.port("claude", src, target)
    assert ss.ADAPTERS[target].read(r.session_id).content_hash() == \
        ss.claude.read(src).content_hash()


def test_very_large_message(ss):
    big = "x" * 500_000
    src = ss.claude.write(ss.session(ss.turns(big, "got it")))
    r = ss.sync.port("claude", src, "codex")
    assert ss.codex.read(r.session_id).content_hash() == ss.claude.read(src).content_hash()


def test_empty_session_is_refused(ss):
    src = ss.claude.write(ss.session([]))
    with pytest.raises(ValueError):
        ss.sync.port("claude", src, "codex")


def test_user_only_session(ss):
    src = ss.claude.write(ss.session(ss.turns("hello?")))
    r = ss.sync.port("claude", src, "codex")
    assert r.action == "created"
    assert len(ss.codex.read(r.session_id).messages) == 1


def test_tool_only_turns(ss):
    src = ss.claude.write(ss.session(ss.turns(
        "run it", "[used tools: Bash]", "and again", "[used tools: Bash, Read]")))
    r = ss.sync.port("claude", src, "codex")
    assert len(ss.codex.read(r.session_id).messages) == 4


def test_chained_ports_do_not_stack_headers(ss):
    src = ss.claude.write(ss.session(ss.turns("one", "two")))
    r1 = ss.sync.port("claude", src, "codex")
    r2 = ss.sync.port("codex", r1.session_id, "kiro")
    r3 = ss.sync.port("kiro", r2.session_id, "antigravity")
    final = ss.antigravity.read(r3.session_id)
    assert len(final.messages) == 2
    assert not any("ported from" in m.text for m in final.messages)
    assert final.content_hash() == ss.claude.read(src).content_hash()


def test_concurrent_pushes_make_one_mirror(ss):
    src = ss.claude.write(ss.session(ss.turns("race", "condition")))
    results = []

    def go():
        results.append(ss.sync.port("claude", src, "codex").session_id)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    assert ss.count("codex") == 1


def test_coalesce_treats_tool_cycles_as_turn_boundaries(ss):
    mixed = ss.coalesce([ss.Message(role, text, ss.now) for role, text in (
        ("assistant", "a1"), ("assistant", "[used tools: Bash]"),
        ("assistant", "a2"), ("user", "u1"), ("user", "u2"))])
    # "a1" is toolless, so the note folds into it as a structured call; "a2"
    # then starts a new turn, since a turn that made a call is a closed cycle.
    assert (mixed[0].text, [t.name for t in mixed[0].tools]) == ("a1", ["shell"])
    assert sum(m.role == "assistant" for m in mixed) == 2
    assert sum(m.role == "user" for m in mixed) == 2


# ------------------------------------------------------------- tool calls

def _tool_session(ss):
    M, T, now = ss.Message, ss.ToolCall, ss.now
    return ss.session([
        M("user", "run the tests", now),
        M("assistant", "Running them.", now, tools=[
            T(name="shell", args='{"command": "pytest -q"}', result="12 passed"),
            T(name="read_file", args='{"path": "conftest.py"}', result="import pytest"),
        ]),
        M("assistant", "All 12 pass.", now),
        M("user", "ship it", now),
    ])


def test_source_keeps_tool_calls(ss):
    base = ss.claude.read(ss.claude.write(_tool_session(ss)))
    assert [len(m.tools) for m in base.messages] == [0, 2, 0, 0]
    assert base.messages[1].tools[0].args == '{"command": "pytest -q"}'
    assert base.messages[1].tools[0].result == "12 passed"


@pytest.mark.parametrize("target", TARGETS)
def test_tool_calls_survive(ss, target):
    src = ss.claude.write(_tool_session(ss))
    base = ss.claude.read(src)
    got = ss.ADAPTERS[target].read(ss.sync.port("claude", src, target).session_id)
    assert got.content_hash() == base.content_hash()
    assert sorted(t.name for m in got.messages for t in m.tools) == ["read_file", "shell"]
    if target == "codex":           # has a real tool record, so details survive too
        calls = [t for m in got.messages for t in m.tools]
        assert sorted(t.result for t in calls) == ["12 passed", "import pytest"]


def test_tool_names_use_each_agents_vocabulary(ss):
    src = ss.claude.write(_tool_session(ss))
    r = ss.sync.port("claude", src, "codex")
    with open(ss.codex._find(r.session_id), encoding="utf-8") as fh:
        codex_names = {json.loads(l)["payload"]["name"] for l in fh if '"function_call"' in l}
    with open(ss.claude._find(src), encoding="utf-8") as fh:
        claude_names = {b["name"] for l in fh
                        for b in (json.loads(l).get("message") or {}).get("content", [])
                        if isinstance(b, dict) and b.get("type") == "tool_use"}
    assert codex_names == {"read_file", "shell"}
    assert claude_names == {"Bash", "Read"}


def test_sequential_tool_turns_stay_separate(ss):
    M, T, now = ss.Message, ss.ToolCall, ss.now
    seq = [M("user", "go", now)] + [
        M("assistant", "", now, tools=[T(name="shell", args="{}", result=f"out{i}")])
        for i in range(5)] + [M("assistant", "done", now)]
    src = ss.claude.write(ss.session(seq))
    a = ss.claude.read(src)
    b = ss.codex.read(ss.sync.port("claude", src, "codex").session_id)
    assert sum(1 for m in a.messages if m.tools) == 5
    assert [t.result for m in b.messages for t in m.tools] == [f"out{i}" for i in range(5)]
    assert b.content_hash() == a.content_hash()


def test_tools_survive_a_text_only_hop(ss):
    src = ss.claude.write(_tool_session(ss))
    k = ss.sync.port("claude", src, "kiro").session_id
    c = ss.sync.port("kiro", k, "codex").session_id
    assert sorted(t.name for m in ss.codex.read(c).messages for t in m.tools) == \
        ["read_file", "shell"]
