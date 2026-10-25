"""Idempotent porting: update the existing copy instead of making another.

Identity is resolved in two layers, deliberately:

  1. The lineage registry -- a fast path we own.
  2. A content-hash scan of the target tool -- the actual source of truth.

The second layer exists because the registry is not authoritative. Another
migration tool, a manual copy, or a lost registry file all produce a mirror we
have no record of, and matching on content finds it anyway. That works because
provenance headers are stripped on read, so a ported session reads back to the
same canonical messages -- and therefore the same hash -- as its source, in
every tool. A registry hit is just a way to skip the scan.

Overwriting is guarded: if a target's current hash differs from what we last
wrote there, someone has continued that session in its own tool, and replacing
it would destroy their work. That needs `force`.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import runtime
from .adapters import ADAPTERS, resolve
from .canonical import Session, first_user_key
from .registry import Registry

# What happened to one target.
CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class PortResult:
    tool: str
    action: str
    session_id: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.action in (CREATED, UPDATED, UNCHANGED)


def _first_user(sess: Session) -> str:
    for m in sess.messages:
        if m.role == "user" and m.text.strip():
            return first_user_key(m.text)
    return ""


def find_by_content(mod, sess: Session) -> str | None:
    """Locate an existing copy of this conversation in `mod`, whoever made it."""
    want = sess.content_hash()
    first = _first_user(sess)
    for ref in mod.list_sessions():
        # Only pay for a full parse on candidates that open the same way. A
        # message count looks like the cheaper filter but cannot be reproduced
        # from a listing -- read() coalesces turns -- and a wrong count silently
        # rejects a real mirror, which then gets duplicated.
        if first and ref.first_user and ref.first_user != first:
            continue
        try:
            if mod.read(ref.session_id).content_hash() == want:
                return ref.session_id
        except Exception:                              # noqa: BLE001
            continue
    return None


def _title_for(lineage, dst_tool: str, sess: Session, existing: str) -> str | None:
    """Title to give the mirror.

    The tag names where the conversation *started*, not who published last.
    Otherwise a round trip -- Claude -> Codex -> back to Claude -- renames your
    original session "[From Codex]", which is both wrong and alarming.

    The origin's own session is never tagged, and keeps whatever title its tool
    gave it (Claude generates one, Kiro copies the first prompt).
    """
    from .canonical import TOOL_LABELS
    origin = lineage.origin or sess.tool
    if dst_tool == origin:
        return existing or None
    label = TOOL_LABELS.get(origin, origin)
    tag = f"[From {label}]"
    base = (sess.preview or sess.title or "session").strip()
    if base.startswith(tag):
        return base
    return f"{tag} {base}"


def port(src_tool: str, src_id: str, dst_tool: str, *,
         force: bool = False, reg: Registry | None = None,
         save: bool = True) -> PortResult:
    src_key, src_mod = resolve(src_tool)
    dst_key, dst_mod = resolve(dst_tool)
    if src_key == dst_key:
        raise ValueError("source and destination are the same tool")

    # Locking belongs with the operation, not the caller: without it two
    # simultaneous pushes both find no existing mirror and both create one.
    with runtime.Lock():
        return _port(src_key, src_mod, dst_key, dst_mod, src_id, force, reg, save)


def _port(src_key, src_mod, dst_key, dst_mod, src_id, force, reg, save) -> PortResult:
    reg = reg or Registry()
    sess = src_mod.read(src_id)
    if not sess.messages:
        raise ValueError("session has no portable messages")

    digest = sess.content_hash()
    lineage = reg.lineage_for(sess)
    reg.record(lineage, src_key, src_id, digest)

    member = lineage.members.get(dst_key)
    target = member.session_id if member and dst_mod.exists(member.session_id) else None
    recovered = False
    if target is None:
        target = find_by_content(dst_mod, sess)
        recovered = target is not None

    try:
        if target:
            current = dst_mod.read(target).content_hash()
            if current == digest:
                reg.record(lineage, dst_key, target, digest)
                if save:
                    reg.save()
                return PortResult(dst_key, UNCHANGED, target,
                                  "matched by content" if recovered else "")
            if member and member.hash and current != member.hash and not force:
                return PortResult(
                    dst_key, SKIPPED, target,
                    "continued in its own tool since the last port; "
                    "re-run with --force to overwrite")
            existing = dst_mod.read(target)
            dst_mod.write(sess, session_id=target,
                          title=_title_for(lineage, dst_key, sess, existing.title))
            action = UPDATED
        else:
            target = dst_mod.write(
                sess, title=_title_for(lineage, dst_key, sess, ""))
            action = CREATED
    except Exception as exc:                           # noqa: BLE001
        return PortResult(dst_key, FAILED, target, str(exc))

    reg.record(lineage, dst_key, target, digest)
    if save:
        reg.save()
    return PortResult(dst_key, action, target,
                      "recovered existing copy" if recovered else "")


def fanout(src_tool: str, src_id: str, targets: list[str] | None = None, *,
           force: bool = False) -> list[PortResult]:
    """Mirror one session into every other tool (or just `targets`)."""
    src_key, _ = resolve(src_tool)
    wanted = [resolve(t)[0] for t in targets] if targets else list(ADAPTERS)
    reg = Registry()
    results = []
    # Held once for the whole fanout; the nested port calls re-enter it freely.
    with runtime.Lock():
        for dst in wanted:
            if dst == src_key:
                continue
            try:
                results.append(port(src_key, src_id, dst, force=force, reg=reg,
                                    save=False))
            except Exception as exc:                   # noqa: BLE001
                results.append(PortResult(dst, FAILED, None, str(exc)))
    reg.save()
    return results


def cleanup(all_sessions: bool = False) -> list[tuple[str, str, bool]]:
    """Clean up sessions created by sessync.

    By default (all_sessions=False), cleans up all mirror sessions that were created
    by sessync across target agents, preserving original source sessions.
    If all_sessions=True, cleans up all sessions recorded in lineage.json (mirrors
    and original sources).

    Returns a list of (tool, session_id, success).
    """
    reg = Registry()
    results = []
    with runtime.Lock():
        for ln in list(reg.lineages):
            for tool, member in list(ln.members.items()):
                # If not all_sessions and origin is known, preserve the source session.
                if not all_sessions and ln.origin and tool == ln.origin:
                    continue
                sid = member.session_id
                if tool in ADAPTERS:
                    ok = ADAPTERS[tool].delete(sid)
                    results.append((tool, sid, ok))
                    reg.forget(tool, sid)
                else:
                    results.append((tool, sid, False))
        reg.save()
    return results
