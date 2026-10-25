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


# How an existing copy relates to the source, best first.
SAME = "same"            # identical conversation
BEHIND = "behind"        # a prefix of the source: the source has moved on
DIVERGED = "diverged"    # same conversation, continued separately


def _keys(sess: Session) -> list[tuple]:
    return [(m.role, m.text, tuple(sorted(t.name for t in m.tools)))
            for m in sess.messages]


def relation(src: list[tuple], dst: list[tuple]) -> str | None:
    """How `dst` relates to `src`, or None if it is a different conversation.

    Exact hash equality is not enough to recognise a mirror. The source keeps
    growing, so the moment a mirror is one turn stale its hash no longer
    matches -- and a mirror the registry has lost track of then gets duplicated,
    again on every later turn. A copy that is a prefix of the source is still
    the same conversation, and overwriting it loses nothing.
    """
    common = 0
    for a, b in zip(src, dst):
        if a != b:
            break
        common += 1
    if common == len(src) == len(dst):
        return SAME
    # The opening exchange, verbatim, is what makes it the same conversation;
    # a matching first prompt alone ("hi") is not.
    if common < min(2, len(src), len(dst)):
        return None
    if common == len(dst):
        return BEHIND
    # The source may have extended the mirror's final assistant turn rather
    # than starting a new one -- coalescing folds those together.
    if common == len(dst) - 1 <= len(src) - 1:
        s, d = src[common], dst[common]
        if s[0] == d[0] == "assistant" and s[1].startswith(d[1]) and not d[2]:
            return BEHIND
    return DIVERGED


_RANK = {SAME: 0, BEHIND: 1, DIVERGED: 2}


def find_by_content(mod, sess: Session) -> tuple[str, str] | tuple[None, None]:
    """Locate an existing copy of this conversation in `mod`, whoever made it.

    Returns its id and how it relates to `sess` (see `relation`). When several
    copies exist -- duplicates left by an older version -- the closest wins.
    """
    want = _keys(sess)
    first = _first_user(sess)
    best = None
    for ref in mod.list_sessions():
        # Only pay for a full parse on candidates that open the same way. A
        # message count looks like the cheaper filter but cannot be reproduced
        # from a listing -- read() coalesces turns -- and a wrong count silently
        # rejects a real mirror, which then gets duplicated.
        if first and ref.first_user and ref.first_user != first:
            continue
        try:
            got = _keys(mod.read(ref.session_id))
        except Exception:                              # noqa: BLE001
            continue
        rel = relation(want, got)
        if rel is None:
            continue
        score = (_RANK[rel], -len(got), -ref.updated_at.timestamp())
        if best is None or score < best[0]:
            best = (score, ref.session_id, rel)
    return (best[1], best[2]) if best else (None, None)


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
    recovered, rel = False, None
    if target is None:
        target, rel = find_by_content(dst_mod, sess)
        recovered = target is not None
        if recovered:
            lineage = reg.adopt(lineage, dst_key, target)

    try:
        if target:
            existing = dst_mod.read(target)
            current = existing.content_hash()
            if current == digest:
                reg.record(lineage, dst_key, target, digest)
                if save:
                    reg.save()
                return PortResult(dst_key, UNCHANGED, target,
                                  "matched by content" if recovered else "")
            if rel is None:
                rel = relation(_keys(sess), _keys(existing))
            # Overwriting is only destructive when the target holds something
            # the source does not. A target that is merely behind -- including
            # one whose own hook never got to report its last turn -- is safe.
            continued = (rel != BEHIND if recovered else
                         bool(member and member.hash and current != member.hash
                              and rel != BEHIND))
            if continued and not force:
                return PortResult(
                    dst_key, SKIPPED, target,
                    "continued in its own tool since the last port; "
                    "re-run with --force to overwrite")
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
            # Saved after every target, not once at the end. A hook can be
            # killed mid-fanout (timeout, agent closed); a mirror written but
            # never recorded is one the next push cannot find, and duplicates.
            try:
                results.append(port(src_key, src_id, dst, force=force, reg=reg))
            except Exception as exc:                   # noqa: BLE001
                results.append(PortResult(dst, FAILED, None, str(exc)))
    reg.save()
    return results


# ----------------------------------------------------------------- cleanup

@dataclass
class Mirror:
    tool: str
    session_id: str
    origin: str
    origin_id: str
    kept: str = ""          # why it stays, if it does


def scripted_mirrors(reg: Registry) -> list[Mirror]:
    """Mirrors sessync made of sessions nobody sat in -- `codex exec` runs,
    subagent threads. Older versions pushed those from the hook; nothing does
    now, but the copies are still in every other agent's picker.

    Only registry members are candidates, so nothing sessync did not write is
    ever touched, and the source session itself never is. A mirror that was
    continued in its own tool is reported but kept.
    """
    found = []
    for ln in reg.lineages:
        src = ln.members.get(ln.origin)
        check = getattr(ADAPTERS.get(ln.origin), "interactive", None)
        if not src or not check or not ADAPTERS[ln.origin].exists(src.session_id):
            continue
        if check(src.session_id):
            continue
        for tool, m in ln.members.items():
            if tool == ln.origin or not ADAPTERS[tool].exists(m.session_id):
                continue
            mirror = Mirror(tool, m.session_id, ln.origin, src.session_id)
            try:
                current = ADAPTERS[tool].read(m.session_id).content_hash()
            except Exception:                          # noqa: BLE001
                current = ""
            if m.hash and current != m.hash:
                mirror.kept = "continued in its own tool"
            found.append(mirror)
    return found


def remove(reg: Registry, mirror: Mirror) -> None:
    ADAPTERS[mirror.tool].delete(mirror.session_id)
    reg.forget(mirror.tool, mirror.session_id)
