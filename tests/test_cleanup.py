"""`sessync cleanup` removes mirrors of scripted runs and nothing else."""
import json


def _codex(ss, prompt, source):
    sid = ss.codex.write(ss.session(ss.turns(prompt, "ok"), tool="codex"))
    path = ss.codex._find(sid)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["source"] = source
    path.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")
    return sid


def _setup(ss):
    # What older versions did: every exec run mirrored everywhere.
    batch = [_codex(ss, f"label problem {i}", "exec") for i in range(3)]
    mirrors = {s: {r.tool: r.session_id for r in ss.sync.fanout("codex", s)} for s in batch}
    real = _codex(ss, "a real conversation", "vscode")
    real_mirrors = {r.tool: r.session_id for r in ss.sync.fanout("codex", real)}
    # One exec mirror the user then kept working in.
    kept = mirrors[batch[0]]["kiro"]
    ss.kiro.write(ss.session(ss.turns("label problem 0", "ok", "keep going")),
                  session_id=kept)
    return batch, mirrors, real, real_mirrors, kept


def test_dry_run_deletes_nothing(ss, capsys):
    *_, kept = _setup(ss)
    before = {t: ss.count(t) for t in ss.ADAPTERS}
    assert ss.cli.main(["cleanup"]) == 0
    out = capsys.readouterr().out
    assert "8 mirrors of scripted sessions" in out
    assert kept in next(l for l in out.splitlines() if l.strip().startswith("keep"))
    assert {t: ss.count(t) for t in ss.ADAPTERS} == before


def test_apply_removes_exactly_the_scripted_mirrors(ss):
    batch, mirrors, real, real_mirrors, kept = _setup(ss)
    before = {t: ss.count(t) for t in ss.ADAPTERS}
    assert ss.cli.main(["cleanup", "--apply"]) == 0

    assert ss.count("claude") == before["claude"] - 3
    assert ss.count("antigravity") == before["antigravity"] - 3
    assert ss.count("kiro") == before["kiro"] - 2       # the continued one stays
    assert ss.kiro.exists(kept)
    assert all(ss.codex.exists(s) for s in batch + [real])
    assert all(ss.ADAPTERS[t].exists(s) for t, s in real_mirrors.items())

    gone = mirrors[batch[1]]
    assert not (ss.antigravity.BRAIN / gone["antigravity"]).exists()
    history = ss.claude.HISTORY.read_text(encoding="utf-8")
    assert gone["claude"] not in history
    assert real_mirrors["claude"] in history


def test_second_run_is_a_no_op(ss, capsys):
    _setup(ss)
    ss.cli.main(["cleanup", "--apply"])
    capsys.readouterr()
    ss.cli.main(["cleanup", "--apply"])
    assert "removed 0 mirrors" in capsys.readouterr().out
