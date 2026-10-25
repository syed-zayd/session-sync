"""Edge cases across all four adapters.

Every session this creates is deleted in the finally block, including the rows
Codex keeps in state_5.sqlite -- a test that leaves sessions behind in someone's
real agent history is worse than no test.
"""
import os, shutil, sqlite3, sys, tempfile, threading
from datetime import datetime, timedelta, timezone

REG = tempfile.mkdtemp(prefix="sessync-edge-")
os.environ["SESSYNC_HOME"] = REG
sys.path.insert(0, r"c:\Users\zayds\Documents\Projects\Session Sync")

from sessync import sync                                    # noqa: E402
from sessync.adapters import ADAPTERS, antigravity, claude_code, codex, kiro  # noqa: E402
from sessync.canonical import Message, Session, coalesce     # noqa: E402

WORK = tempfile.mkdtemp(prefix="sessync-edge-work-")
STATE = os.path.expanduser("~/.codex/state_5.sqlite")
N = datetime.now(timezone.utc)
made: list[tuple[str, str]] = []          # (tool, session_id) to clean up

fails: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


def sess(msgs, tool="claude", title="Edge case"):
    return Session(tool=tool, session_id="", title=title, cwd=WORK,
                   created_at=N, updated_at=N, messages=msgs)


def turns(pairs):
    out = []
    for i, (role, text) in enumerate(pairs):
        out.append(Message(role, text, N + timedelta(seconds=i)))
    return out


def write(tool, s, **kw):
    sid = ADAPTERS[tool].write(s, **kw)
    made.append((tool, sid))
    return sid


try:
    print("--- C1: non-ASCII, emoji, CJK, RTL survive every hop ---")
    tricky = "café ☕ 日本語 مرحبا 🎉 emoji + \"quotes\" + \\backslash\\ + \ttab"
    src = write("claude", sess(turns([("user", tricky), ("assistant", "ack " + tricky)])))
    base = claude_code.read(src)
    for tool in ("codex", "kiro", "antigravity"):
        r = sync.port("claude", src, tool)
        made.append((tool, r.session_id))
        got = ADAPTERS[tool].read(r.session_id)
        check(f"{tool} preserves unicode", got.content_hash(), base.content_hash())

    print("\n--- C2: a very large single message ---")
    big = "x" * 500_000
    src2 = write("claude", sess(turns([("user", big), ("assistant", "got " + str(len(big)))])))
    r = sync.port("claude", src2, "codex")
    made.append(("codex", r.session_id))
    check("500k char message round-trips",
          codex.read(r.session_id).content_hash(), claude_code.read(src2).content_hash())

    print("\n--- B3: degenerate sessions ---")
    empty = write("claude", sess([]))
    try:
        sync.port("claude", empty, "codex")
        check("empty session refused", "no error", "ValueError")
    except ValueError:
        check("empty session refused", "ValueError", "ValueError")

    only_user = write("claude", sess(turns([("user", "hello?")])))
    r = sync.port("claude", only_user, "codex")
    made.append(("codex", r.session_id))
    check("user-only session ports", r.action, "created")
    check("  message survives", len(codex.read(r.session_id).messages), 1)

    print("\n--- B4: a turn that is only tool activity ---")
    tool_only = write("claude", sess(turns([
        ("user", "run it"), ("assistant", "[used tools: Bash]"),
        ("user", "and again"), ("assistant", "[used tools: Bash, Read]")])))
    r = sync.port("claude", tool_only, "codex")
    made.append(("codex", r.session_id))
    check("tool-only turns survive", len(codex.read(r.session_id).messages), 4)

    print("\n--- D1: chained port A -> B -> C does not stack headers ---")
    chain = write("claude", sess(turns([("user", "one"), ("assistant", "two")])))
    r1 = sync.port("claude", chain, "codex"); made.append(("codex", r1.session_id))
    r2 = sync.port("codex", r1.session_id, "kiro"); made.append(("kiro", r2.session_id))
    r3 = sync.port("kiro", r2.session_id, "antigravity"); made.append(("antigravity", r3.session_id))
    final = antigravity.read(r3.session_id)
    check("3 hops keep message count", len(final.messages), 2)
    check("  no header leaked into content",
          sum(1 for m in final.messages if "ported from" in m.text), 0)
    check("  content identical after 3 hops",
          final.content_hash(), claude_code.read(chain).content_hash())

    print("\n--- D2: round trip A -> B -> A ---")
    rt = write("claude", sess(turns([("user", "alpha"), ("assistant", "beta"),
                                     ("user", "gamma")])))
    back = sync.port("claude", rt, "codex"); made.append(("codex", back.session_id))
    r = sync.port("codex", back.session_id, "kiro"); made.append(("kiro", r.session_id))
    check("A->B->C preserves hash",
          kiro.read(r.session_id).content_hash(), claude_code.read(rt).content_hash())

    print("\n--- D3: concurrent pushes serialise (no duplicates) ---")
    conc = write("claude", sess(turns([("user", "race"), ("assistant", "condition")])))
    results = []
    def go():
        try:
            results.append(sync.port("claude", conc, "codex").session_id)
        except Exception as exc:                     # noqa: BLE001
            results.append(f"ERR {exc}")
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    for sid in results:
        if isinstance(sid, str) and not sid.startswith("ERR"):
            made.append(("codex", sid))
    check("4 concurrent pushes -> 1 session", len(set(results)), 1)

    print("\n--- coalesce: tool cycles are turn boundaries ---")
    mixed = coalesce(turns([("assistant", "a1"), ("assistant", "[used tools: Bash]"),
                            ("assistant", "a2"), ("user", "u1"), ("user", "u2")]))
    # "a1" is toolless, so the note folds into it and becomes a structured call.
    # "a2" then starts a new turn: a turn that made a tool call is a closed
    # request/response cycle, and folding the next one in would misstate it.
    check("note folds into the toolless turn before it",
          (mixed[0].role, mixed[0].text, [t.name for t in mixed[0].tools]),
          ("assistant", "a1", ["shell"]))
    check("a completed tool turn is not merged into",
          sum(1 for m in mixed if m.role == "assistant"), 2)
    check("user records stay separate",
          sum(1 for m in mixed if m.role == "user"), 2)
    check("  note left the text once structured",
          "[used tools" in mixed[0].text, False)

finally:
    for tool, sid in made:
        try:
            if tool == "claude":
                os.remove(claude_code._find(sid))
            elif tool == "codex":
                os.remove(codex._find(sid))
                con = sqlite3.connect(STATE); con.execute(
                    "DELETE FROM threads WHERE id=?", (sid,)); con.commit(); con.close()
            elif tool == "kiro":
                for suf in (".jsonl", ".json", ".history", ".lock"):
                    p = kiro.ROOT / f"{sid}{suf}"
                    if p.exists():
                        p.unlink()
            elif tool == "antigravity":
                (antigravity.CONV / f"{sid}.db").unlink(missing_ok=True)
                shutil.rmtree(antigravity.BRAIN / sid, ignore_errors=True)
        except Exception:                            # noqa: BLE001
            pass
    # Claude's history.jsonl gets a row per ported prompt; drop the ones this
    # run added so a test never leaves anything in real agent state.
    hp = os.path.expanduser("~/.claude/history.jsonl")
    ids = {sid for tool, sid in made if tool == "claude"}
    if ids and os.path.exists(hp):
        kept = [l for l in open(hp, encoding="utf-8")
                if not any(i in l for i in ids)]
        open(hp, "w", encoding="utf-8", newline="\n").writelines(kept)
    # Codex also lists the thread in session_index.jsonl.
    si = os.path.expanduser("~/.codex/session_index.jsonl")
    cids = {sid for tool, sid in made if tool == "codex"}
    if cids and os.path.exists(si):
        kept = [l for l in open(si, encoding="utf-8")
                if not any(c in l for c in cids if c)]
        open(si, "w", encoding="utf-8", newline=chr(10)).writelines(kept)
    shutil.rmtree(REG, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    d = claude_code.ROOT / claude_code.encode_cwd(WORK)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    print(f"\ncleaned up {len(made)} test sessions")

print("ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
