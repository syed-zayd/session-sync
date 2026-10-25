"""Pushing back and forth must leave exactly one session per tool.

Runs against a throwaway HOME: adapters resolve their stores from it at import.
"""
import json, os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone

HOME = tempfile.mkdtemp(prefix="sessync-dup-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME
os.environ["SESSYNC_HOME"] = os.path.join(HOME, ".sessync")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessync import cli, registry, sync                           # noqa: E402
from sessync.adapters import ADAPTERS, claude_code, codex    # noqa: E402
from sessync.canonical import Message, Session               # noqa: E402

WORK = os.path.join(HOME, "project")
N = datetime.now(timezone.utc)
fails: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


def turns(k):
    return [Message("user" if i % 2 == 0 else "assistant",
                    "build a browser LLM" if i == 0 else f"turn {i}",
                    N + timedelta(seconds=i))
            for i in range(k)]


def convo(k=0, msgs=None):
    return Session(tool="claude", session_id="", title="", cwd=WORK,
                   created_at=N, updated_at=N,
                   messages=msgs if msgs is not None else turns(k))


def count(tool):
    return len(ADAPTERS[tool].list_sessions())


def lose_registry():
    if registry.PATH.exists():
        registry.PATH.unlink()


try:
    print("--- registry lost, then the source grows ---")
    src = claude_code.write(convo(2))
    first = sync.port("claude", src, "codex").session_id
    lose_registry()                       # e.g. hook killed before saving
    claude_code.write(convo(4), session_id=src)
    r = sync.port("claude", src, "codex")
    check("stale mirror is updated, not duplicated", r.action, "updated")
    check("  same id", r.session_id, first)
    check("  one codex session", count("codex"), 1)

    print("\n--- back and forth, with every save lost ---")
    for k in (6, 8, 10):
        lose_registry()
        claude_code.write(convo(k), session_id=src)
        sync.fanout("claude", src)
        lose_registry()
        # continue in codex, then push it back
        codex.write(convo(k + 1), session_id=first)
        sync.fanout("codex", first)
    for tool in ADAPTERS:
        check(f"one {tool} session", count(tool), 1)

    print("\n--- pushing from a mirror finds the original ---")
    lose_registry()
    sync.fanout("claude", src)
    claude_code.write(convo(12), session_id=src)
    lose_registry()
    r = sync.port("codex", first, "claude")
    check("recovered into the claude original", r.session_id, src)

    print("\n--- a different conversation with the same opener ---")
    other = claude_code.write(convo(msgs=[
        Message("user", "build a browser LLM", N),
        Message("assistant", "a different reply", N)]))
    r = sync.port("claude", other, "codex")
    check("gets its own mirror", r.action, "created")

    print("\n--- a copy continued separately is never overwritten silently ---")
    lose_registry()
    codex.write(convo(msgs=turns(13) + [Message("assistant", "codex-only work", N)]),
                session_id=first)
    claude_code.write(convo(14), session_id=src)
    r = sync.port("claude", src, "codex")
    check("diverged copy is skipped", r.action, "skipped")
    check("  and not duplicated", r.session_id, first)

    print("\n--- scripted codex runs are not mirrored by the hook ---")
    before = {t: count(t) for t in ADAPTERS}
    for source in ("exec", {"subagent": {"thread_spawn": {"depth": 1}}}):
        sid = codex.write(convo(msgs=[Message("user", f"batch job {source!r}", N),
                                      Message("assistant", "done", N)]))
        path = codex._find(sid)
        lines = path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(lines[0])
        meta["payload"]["source"] = source
        path.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n",
                        encoding="utf-8")
        cli.main(["push", "codex", sid, "all", "--from-hook", "--quiet"])
    check("nothing mirrored", {t: count(t) for t in ADAPTERS if t != "codex"},
          {t: before[t] for t in ADAPTERS if t != "codex"})
    r = sync.port("codex", sid, "claude")
    check("an explicit push still works", r.action, "created")
finally:
    shutil.rmtree(HOME, ignore_errors=True)

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
