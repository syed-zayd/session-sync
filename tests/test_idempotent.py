"""End-to-end check of update-in-place and the overwrite guard."""
import os, sys, tempfile, shutil
from datetime import datetime, timezone, timedelta

REG = tempfile.mkdtemp(prefix="sessync-test-")
os.environ["SESSYNC_HOME"] = REG
sys.path.insert(0, r"c:\Users\zayds\Documents\Projects\Session Sync")

from sessync import sync
from sessync.adapters import claude_code, kiro
from sessync.canonical import Message, Session

WORK = tempfile.mkdtemp(prefix="sessync-work-")
n = datetime.now(timezone.utc)


def mk(msgs):
    return Session(tool="claude", session_id="", title="", cwd=WORK,
                   created_at=n, updated_at=n, messages=msgs)


def msgs(k):
    out = []
    for i in range(k):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(Message(role, f"turn {i}", n + timedelta(seconds=i)))
    return out


fails = []
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f" (want {want})"))
    if not ok:
        fails.append(label)


try:
    src = claude_code.write(mk(msgs(2)))
    print(f"source claude session {src}")

    r = sync.port("claude", src, "kiro")
    check("first port creates", r.action, "created")
    target = r.session_id

    r = sync.port("claude", src, "kiro")
    check("re-port is a no-op", r.action, "unchanged")
    check("  same target id", r.session_id, target)

    # source grows: target must be updated in place, keeping its id
    claude_code.write(mk(msgs(4)), session_id=src)
    r = sync.port("claude", src, "kiro")
    check("source changed -> update", r.action, "updated")
    check("  id preserved", r.session_id, target)
    check("  content followed", len(kiro.read(target).messages), 4)

    # target continued in its own tool: refuse to clobber
    kiro.write(mk(msgs(6)), session_id=target)
    r = sync.port("claude", src, "kiro")
    check("target edited -> skip", r.action, "skipped")
    check("  target untouched", len(kiro.read(target).messages), 6)

    r = sync.port("claude", src, "kiro", force=True)
    check("--force overwrites", r.action, "updated")
    check("  back to source", len(kiro.read(target).messages), 4)

    # registry lost entirely -> content scan must still find the copy
    os.remove(os.path.join(REG, "lineage.json"))
    r = sync.port("claude", src, "kiro")
    check("registry lost -> recovered", r.action, "unchanged")
    check("  same id, no duplicate", r.session_id, target)
finally:
    try:
        os.remove(claude_code._find(src))
    except Exception:
        pass
    for suf in (".jsonl", ".json", ".history", ".lock"):
        p = kiro.ROOT / f"{target}{suf}"
        if p.exists():
            p.unlink()
    # Writing a Claude session also adds its prompts to Claude's up-arrow
    # history; drop them so a test leaves nothing behind in real agent state.
    hp = os.path.expanduser("~/.claude/history.jsonl")
    if os.path.exists(hp):
        kept = [l for l in open(hp, encoding="utf-8") if src not in l]
        open(hp, "w", encoding="utf-8", newline="\n").writelines(kept)
    # Codex also lists the thread in session_index.jsonl.
    si = os.path.expanduser("~/.codex/session_index.jsonl")
    cids = {target}
    if cids and os.path.exists(si):
        kept = [l for l in open(si, encoding="utf-8")
                if not any(c in l for c in cids if c)]
        open(si, "w", encoding="utf-8", newline=chr(10)).writelines(kept)
    shutil.rmtree(REG, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    proj = claude_code.ROOT / claude_code.encode_cwd(WORK)
    if proj.exists():
        shutil.rmtree(proj, ignore_errors=True)

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
