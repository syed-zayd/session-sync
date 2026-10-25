"""`sessync cleanup` removes mirrors of scripted runs and nothing else.

Runs against a throwaway HOME: adapters resolve their stores from it at import.
"""
import io, json, os, shutil, sys, tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone

HOME = tempfile.mkdtemp(prefix="sessync-clean-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME
os.environ["SESSYNC_HOME"] = os.path.join(HOME, ".sessync")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sessync import cli, sync                                     # noqa: E402
from sessync.adapters import ADAPTERS, antigravity, claude_code, codex, kiro  # noqa: E402
from sessync.canonical import Message, Session                     # noqa: E402

N = datetime.now(timezone.utc)
fails: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        fails.append(label)


def codex_session(prompt, source):
    sid = codex.write(Session(
        tool="codex", session_id="", title="", cwd=os.path.join(HOME, "p"),
        created_at=N, updated_at=N,
        messages=[Message("user", prompt, N), Message("assistant", "ok", N)]))
    path = codex._find(sid)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["source"] = source
    path.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")
    return sid


def run(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["cleanup", *argv])
    return code, buf.getvalue()


def count(tool):
    return len(ADAPTERS[tool].list_sessions())


try:
    # What older versions did: every exec run mirrored everywhere.
    batch = [codex_session(f"label problem {i}", "exec") for i in range(3)]
    mirrors = {sid: {r.tool: r.session_id for r in sync.fanout("codex", sid)}
               for sid in batch}
    real = codex_session("a real conversation", "vscode")
    real_mirrors = {r.tool: r.session_id for r in sync.fanout("codex", real)}
    # One exec mirror the user then kept working in.
    kept = mirrors[batch[0]]["kiro"]
    kiro.write(Session(tool="kiro", session_id="", title="", cwd=HOME,
                       created_at=N, updated_at=N, messages=[
                           Message("user", "label problem 0", N),
                           Message("assistant", "ok", N),
                           Message("user", "actually, keep going", N)]),
               session_id=kept)
    before = {t: count(t) for t in ADAPTERS}

    print("--- dry run deletes nothing ---")
    code, out = run()
    check("exit 0", code, 0)
    check("reports 8 to remove", "8 mirrors of scripted sessions" in out, True)
    check("reports the continued one as kept", f"keep          kiro         {kept}" in out, True)
    check("counts unchanged", {t: count(t) for t in ADAPTERS}, before)

    print("\n--- --apply ---")
    code, out = run("--apply")
    check("exit 0", code, 0)
    check("claude mirrors gone", count("claude"), before["claude"] - 3)
    check("antigravity mirrors gone", count("antigravity"), before["antigravity"] - 3)
    check("kiro: continued mirror kept", count("kiro"), before["kiro"] - 2)
    check("  and it is the continued one", kiro.exists(kept), True)
    check("codex sources untouched", all(codex.exists(s) for s in batch + [real]), True)
    check("real conversation's mirrors untouched",
          all(ADAPTERS[t].exists(s) for t, s in real_mirrors.items()), True)
    gone = mirrors[batch[1]]
    check("antigravity brain dir removed",
          (antigravity.BRAIN / gone["antigravity"]).exists(), False)
    hist = claude_code.HISTORY.read_text(encoding="utf-8") if claude_code.HISTORY.exists() else ""
    check("claude history rows removed", gone["claude"] in hist, False)
    check("real mirror's history kept", real_mirrors["claude"] in hist, True)

    print("\n--- running it again is a no-op ---")
    code, out = run("--apply")
    check("removes nothing", "removed 0 mirrors" in out, True)
finally:
    shutil.rmtree(HOME, ignore_errors=True)

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
