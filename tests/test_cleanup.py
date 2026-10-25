"""Tests for sessync cleanup command."""
from datetime import datetime, timezone

from sessync import registry, sync
from sessync.adapters import antigravity, claude_code, codex, kiro
from sessync.canonical import Message, Session

N = datetime.now(timezone.utc)


def test_cleanup_removes_created_mirrors(work):
    # Create a source session in Claude
    src_id = claude_code.write(Session(
        tool="claude", session_id="", title="Source to keep", cwd=work,
        created_at=N, updated_at=N,
        messages=[Message("user", "keep me", N), Message("assistant", "sure", N)]
    ))

    # Port to codex, kiro, and antigravity
    r_codex = sync.port("claude", src_id, "codex")
    r_kiro = sync.port("claude", src_id, "kiro")
    r_ag = sync.port("claude", src_id, "antigravity")

    assert codex.exists(r_codex.session_id)
    assert kiro.exists(r_kiro.session_id)
    assert antigravity.exists(r_ag.session_id)
    assert claude_code.exists(src_id)

    # Standard cleanup (mirrors only)
    results = sync.cleanup(all_sessions=False)
    assert len(results) == 3
    assert all(ok for _, _, ok in results)

    # Mirrors are deleted
    assert not codex.exists(r_codex.session_id)
    assert not kiro.exists(r_kiro.session_id)
    assert not antigravity.exists(r_ag.session_id)

    # Original source is preserved
    assert claude_code.exists(src_id)

    # Lineage is updated (only source remains)
    reg = registry.Registry()
    ln = reg.find("claude", src_id)
    assert ln is not None
    assert set(ln.members.keys()) == {"claude"}


def test_cleanup_all_removes_sources_too(work):
    src_id = claude_code.write(Session(
        tool="claude", session_id="", title="Source to delete", cwd=work,
        created_at=N, updated_at=N,
        messages=[Message("user", "delete me", N), Message("assistant", "okay", N)]
    ))
    r_codex = sync.port("claude", src_id, "codex")

    # Cleanup with all_sessions=True
    results = sync.cleanup(all_sessions=True)
    assert len(results) == 2
    assert all(ok for _, _, ok in results)

    assert not codex.exists(r_codex.session_id)
    assert not claude_code.exists(src_id)
