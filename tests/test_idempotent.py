"""End-to-end check of update-in-place and the overwrite guard."""
from datetime import datetime, timedelta, timezone

from sessync import registry, sync
from sessync.adapters import claude_code, kiro
from sessync.canonical import Message, Session

N = datetime.now(timezone.utc)


def mk(cwd, msgs):
    return Session(tool="claude", session_id="", title="", cwd=cwd,
                   created_at=N, updated_at=N, messages=msgs)


def turns(k):
    return [Message("user" if i % 2 == 0 else "assistant", f"turn {i}",
                    N + timedelta(seconds=i)) for i in range(k)]


def test_update_in_place_and_overwrite_guard(work):
    src = claude_code.write(mk(work, turns(2)))

    r = sync.port("claude", src, "kiro")
    assert r.action == "created"
    target = r.session_id

    r = sync.port("claude", src, "kiro")
    assert (r.action, r.session_id) == ("unchanged", target)

    # Source grows: the mirror is updated in place, keeping its id.
    claude_code.write(mk(work, turns(4)), session_id=src)
    r = sync.port("claude", src, "kiro")
    assert (r.action, r.session_id) == ("updated", target)
    assert len(kiro.read(target).messages) == 4

    # Mirror continued in its own tool: refuse to clobber it.
    kiro.write(mk(work, turns(6)), session_id=target)
    r = sync.port("claude", src, "kiro")
    assert r.action == "skipped"
    assert len(kiro.read(target).messages) == 6

    r = sync.port("claude", src, "kiro", force=True)
    assert r.action == "updated"
    assert len(kiro.read(target).messages) == 4


def test_lost_registry_recovers_by_content(work):
    src = claude_code.write(mk(work, turns(2)))
    target = sync.port("claude", src, "kiro").session_id

    registry.PATH.unlink()
    r = sync.port("claude", src, "kiro")
    assert (r.action, r.session_id) == ("unchanged", target)
