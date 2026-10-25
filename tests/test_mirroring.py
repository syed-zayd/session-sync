"""Pushing updates one mirror per tool in place, whatever happens in between."""
import json


def _set_codex_source(ss, sid, source):
    path = ss.codex._find(sid)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["source"] = source
    path.write_text("\n".join([json.dumps(meta)] + lines[1:]) + "\n", encoding="utf-8")


def _conversation(ss, k):
    return ss.session(ss.turns("build a browser LLM", *[f"turn {i}" for i in range(1, k)]))


def _lose_registry(ss):
    ss.registry.PATH.unlink(missing_ok=True)


# ------------------------------------------------------- update in place

def test_repush_is_a_no_op(ss):
    src = ss.claude.write(_conversation(ss, 2))
    first = ss.sync.port("claude", src, "kiro")
    again = ss.sync.port("claude", src, "kiro")
    assert (first.action, again.action) == ("created", "unchanged")
    assert again.session_id == first.session_id


def test_growing_source_updates_in_place(ss):
    src = ss.claude.write(_conversation(ss, 2))
    target = ss.sync.port("claude", src, "kiro").session_id
    ss.claude.write(_conversation(ss, 4), session_id=src)
    r = ss.sync.port("claude", src, "kiro")
    assert (r.action, r.session_id) == ("updated", target)
    assert len(ss.kiro.read(target).messages) == 4


def test_continued_mirror_is_not_clobbered(ss):
    src = ss.claude.write(_conversation(ss, 2))
    target = ss.sync.port("claude", src, "kiro").session_id
    ss.kiro.write(_conversation(ss, 6), session_id=target)
    ss.claude.write(ss.session(ss.turns("build a browser LLM", "turn 1", "elsewhere")),
                    session_id=src)
    assert ss.sync.port("claude", src, "kiro").action == "skipped"
    assert len(ss.kiro.read(target).messages) == 6
    assert ss.sync.port("claude", src, "kiro", force=True).action == "updated"
    assert len(ss.kiro.read(target).messages) == 3


# ------------------------------------------------- recovery, no duplicates

def test_lost_registry_finds_the_mirror(ss):
    src = ss.claude.write(_conversation(ss, 2))
    target = ss.sync.port("claude", src, "kiro").session_id
    _lose_registry(ss)
    r = ss.sync.port("claude", src, "kiro")
    assert (r.action, r.session_id) == ("unchanged", target)


def test_lost_registry_and_grown_source_updates_not_duplicates(ss):
    src = ss.claude.write(_conversation(ss, 2))
    target = ss.sync.port("claude", src, "codex").session_id
    _lose_registry(ss)          # e.g. the hook was killed before saving
    ss.claude.write(_conversation(ss, 4), session_id=src)
    r = ss.sync.port("claude", src, "codex")
    assert (r.action, r.session_id) == ("updated", target)
    assert ss.count("codex") == 1


def test_back_and_forth_keeps_one_session_per_tool(ss):
    src = ss.claude.write(_conversation(ss, 2))
    mirror = ss.sync.port("claude", src, "codex").session_id
    for k in (4, 6, 8):
        _lose_registry(ss)
        ss.claude.write(_conversation(ss, k), session_id=src)
        ss.sync.fanout("claude", src)
        _lose_registry(ss)
        ss.codex.write(_conversation(ss, k + 1), session_id=mirror)
        ss.sync.fanout("codex", mirror)
    assert {t: ss.count(t) for t in ss.ADAPTERS} == {t: 1 for t in ss.ADAPTERS}


def test_pushing_from_a_mirror_finds_the_original(ss):
    src = ss.claude.write(_conversation(ss, 2))
    mirror = ss.sync.port("claude", src, "codex").session_id
    _lose_registry(ss)
    ss.codex.write(_conversation(ss, 3), session_id=mirror)
    assert ss.sync.port("codex", mirror, "claude").session_id == src


def test_same_opener_different_conversation_gets_its_own_mirror(ss):
    a = ss.claude.write(_conversation(ss, 2))
    ss.sync.port("claude", a, "codex")
    b = ss.claude.write(ss.session(ss.turns("build a browser LLM", "a different reply")))
    assert ss.sync.port("claude", b, "codex").action == "created"
    assert ss.count("codex") == 2


def test_diverged_copy_is_skipped_not_duplicated(ss):
    src = ss.claude.write(_conversation(ss, 2))
    mirror = ss.sync.port("claude", src, "codex").session_id
    _lose_registry(ss)
    ss.codex.write(ss.session(_conversation(ss, 3).messages
                              + [ss.Message("assistant", "codex-only work", ss.now)]),
                   session_id=mirror)
    ss.claude.write(_conversation(ss, 4), session_id=src)
    r = ss.sync.port("claude", src, "codex")
    assert (r.action, r.session_id) == ("skipped", mirror)


# ---------------------------------------------------------------- hooks

def test_hook_skips_scripted_codex_sessions(ss):
    for source in ("exec", {"subagent": {"thread_spawn": {"depth": 1}}}):
        sid = ss.codex.write(ss.session(ss.turns(f"batch {source!r}", "done")))
        _set_codex_source(ss, sid, source)
        ss.cli.main(["push", "codex", sid, "all", "--from-hook", "--quiet"])
    assert [ss.count(t) for t in ("claude", "kiro", "antigravity")] == [0, 0, 0]
    # Naming one explicitly still works.
    assert ss.sync.port("codex", sid, "claude").action == "created"


def test_hook_pushes_interactive_codex_sessions(ss):
    sid = ss.codex.write(ss.session(ss.turns("a real conversation", "ok")))
    _set_codex_source(ss, sid, "vscode")
    ss.cli.main(["push", "codex", sid, "all", "--from-hook", "--quiet"])
    assert [ss.count(t) for t in ("claude", "kiro", "antigravity")] == [1, 1, 1]
