"""Tool calls survive a port with their arguments and output.

Claude Code and Codex store real tool records; Kiro and Antigravity have none,
so their mirrors carry a rendered note that readers parse back. Either way the
session must still hash the same as its source, because idempotent pushing
depends on it.
"""
import os, shutil, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone

REG = tempfile.mkdtemp(prefix="sessync-tool-")
os.environ["SESSYNC_HOME"] = REG
sys.path.insert(0, r"c:\Users\zayds\Documents\Projects\Session Sync")

from sessync import sync                                             # noqa: E402
from sessync.adapters import ADAPTERS, antigravity, claude_code, codex, kiro  # noqa: E402
from sessync.canonical import Message, Session, ToolCall             # noqa: E402

WORK = tempfile.mkdtemp(prefix="sessync-tool-work-")
STATE = os.path.expanduser("~/.codex/state_5.sqlite")
N = datetime.now(timezone.utc)
made: list[tuple[str, str]] = []
fails: list[str] = []

NATIVE = ("claude", "codex")      # agents with a real tool record


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


def sample():
    return [
        Message("user", "run the tests", N),
        Message("assistant", "Running them.", N + timedelta(seconds=1), tools=[
            ToolCall(name="shell", args='{"command": "pytest -q"}',
                     result="12 passed in 3.2s"),
            ToolCall(name="read_file", args='{"path": "conftest.py"}',
                     result="import pytest"),
        ]),
        Message("assistant", "All 12 pass.", N + timedelta(seconds=2)),
        Message("user", "ship it", N + timedelta(seconds=3)),
    ]


try:
    src = claude_code.write(Session("claude", "", "Tool fidelity", WORK, N, N, sample()))
    made.append(("claude", src))
    base = claude_code.read(src)

    check("source keeps both calls on one turn",
          [len(m.tools) for m in base.messages], [0, 2, 0, 0])
    call = base.messages[1].tools[0]
    check("  args survive", call.args, '{"command": "pytest -q"}')
    check("  output survives", call.result, "12 passed in 3.2s")

    for tgt in ("codex", "kiro", "antigravity"):
        r = sync.port("claude", src, tgt)
        made.append((tgt, r.session_id))
        got = ADAPTERS[tgt].read(r.session_id)
        check(f"{tgt}: hash matches source", got.content_hash(), base.content_hash())
        check(f"  {tgt}: message count", len(got.messages), len(base.messages))
        names = sorted(t.name for m in got.messages for t in m.tools)
        check(f"  {tgt}: tool names", names, ["read_file", "shell"])
        if tgt in NATIVE:
            calls = [t for m in got.messages for t in m.tools]
            check(f"  {tgt}: args round-trip", calls[0].args or calls[1].args,
                  '{"command": "pytest -q"}')
            check(f"  {tgt}: output round-trip",
                  sorted(t.result for t in calls),
                  ["12 passed in 3.2s", "import pytest"])

    print("\n--- tool names are written in each agent's own vocabulary ---")
    import json
    path = codex._find([s for t, s in made if t == "codex"][0])
    codex_names = sorted({json.loads(l)["payload"]["name"]
                          for l in open(path, encoding="utf-8")
                          if '"function_call"' in l})
    check("codex spells them natively", codex_names, ["read_file", "shell"])
    cpath = claude_code._find(src)
    claude_names = sorted({b["name"] for l in open(cpath, encoding="utf-8")
                           for b in (json.loads(l).get("message") or {}).get("content", [])
                           if isinstance(b, dict) and b.get("type") == "tool_use"})
    check("claude spells them natively", claude_names, ["Bash", "Read"])

    print("\n--- turn boundaries around tool cycles ---")
    # Regression: a run of tool-only turns used to fold into one message (71
    # collapsed into 1 on a real session), and a multi-tool turn used to split
    # into several. Both come down to where a turn ends.
    seq = [Message("user", "go", N)]
    for i in range(5):                     # five sequential one-tool turns
        seq.append(Message("assistant", "", N + timedelta(seconds=i + 1),
                           tools=[ToolCall(name="shell", args="{}",
                                           result=f"out{i}")]))
    seq.append(Message("assistant", "done", N + timedelta(seconds=9)))
    s2 = claude_code.write(Session("claude", "", "Turns", WORK, N, N, seq))
    made.append(("claude", s2))
    a2 = claude_code.read(s2)
    check("five sequential tool turns stay five",
          sum(1 for m in a2.messages if m.tools), 5)
    r2 = sync.port("claude", s2, "codex")
    made.append(("codex", r2.session_id))
    b2 = codex.read(r2.session_id)
    check("  and survive the round trip",
          sum(1 for m in b2.messages if m.tools), 5)
    check("  outputs stay with their own call",
          [t.result for m in b2.messages for t in m.tools],
          ["out0", "out1", "out2", "out3", "out4"])
    check("  hash matches", b2.content_hash(), a2.content_hash())

    print("\n--- chained: claude -> kiro -> codex keeps tools alive ---")
    kiro_id = [s for t, s in made if t == "kiro"][0]
    r = sync.port("kiro", kiro_id, "codex")
    check("via a text-only agent, tools survive as names",
          sorted(t.name for m in codex.read(r.session_id).messages for t in m.tools),
          ["read_file", "shell"])

finally:
    for tool, sid in made:
        try:
            if tool == "claude":
                os.remove(claude_code._find(sid))
            elif tool == "codex":
                os.remove(codex._find(sid))
                con = sqlite3.connect(STATE)
                con.execute("DELETE FROM threads WHERE id=?", (sid,))
                con.commit(); con.close()
            elif tool == "kiro":
                for suf in (".jsonl", ".json", ".history", ".lock"):
                    p = kiro.ROOT / f"{sid}{suf}"
                    if p.exists():
                        p.unlink()
            else:
                (antigravity.CONV / f"{sid}.db").unlink(missing_ok=True)
                shutil.rmtree(antigravity.BRAIN / sid, ignore_errors=True)
        except Exception:                                # noqa: BLE001
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
