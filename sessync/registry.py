"""Lineage registry -- which sessions across tools are the same conversation.

A *lineage* is one logical conversation and the id it has in each tool:

    {"id": "...", "members": {"claude": {"session_id": ..., "hash": ...}, ...}}

Two reasons this lives in a file we own rather than inside the tools' sessions:

  * Every one of these formats has a strict deserialiser somewhere. Kiro drops a
    session silently over one bad enum value; smuggling our own fields into
    theirs is how we would reintroduce that bug.
  * The mapping has to survive their format changes, and it has to be readable
    without parsing four different session stores.

The stored `hash` is of the canonical messages we last wrote. If a target's
current hash differs, the user has continued that session in its own tool, and
re-porting would destroy their work -- so we refuse unless forced.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .canonical import Session, now

ROOT = Path(os.environ.get("SESSYNC_HOME", Path.home() / ".sessync"))
PATH = ROOT / "lineage.json"
VERSION = 3


@dataclass
class Member:
    session_id: str
    hash: str = ""
    updated: str = ""

    def to_json(self) -> dict:
        return {"session_id": self.session_id, "hash": self.hash,
                "updated": self.updated}


@dataclass
class Lineage:
    id: str
    members: dict[str, Member] = field(default_factory=dict)
    # Tool the conversation started in. Mirrors are labelled after this, so the
    # tag stays stable no matter which tool most recently published.
    origin: str = ""

    def to_json(self) -> dict:
        return {"id": self.id, "origin": self.origin,
                "members": {k: v.to_json() for k, v in self.members.items()}}


class Registry:
    def __init__(self, path: Path = PATH):
        self.path = path
        self.lineages: list[Lineage] = []
        self.load()

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        # Hashes from an older schema version are stale (v1 predates turn
        # coalescing, v2 predates structured tool calls).
        # Clearing them makes the next push update rather than refuse: a stale
        # hash would look exactly like "someone continued this in its own tool".
        stale = raw.get("version", 1) < VERSION
        for ln in raw.get("lineages", []):
            members = {t: Member(**m) for t, m in (ln.get("members") or {}).items()}
            if stale:
                for m in members.values():
                    m.hash = ""
            self.lineages.append(Lineage(id=ln["id"], members=members,
                                         origin=ln.get("origin", "")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION,
                   "lineages": [l.to_json() for l in self.lineages]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- lookup

    def find(self, tool: str, session_id: str) -> Lineage | None:
        for ln in self.lineages:
            m = ln.members.get(tool)
            if m and m.session_id == session_id:
                return ln
        return None

    def lineage_for(self, sess: Session) -> Lineage:
        """The lineage a source session belongs to, creating one if new."""
        found = self.find(sess.tool, sess.session_id)
        if found:
            return found
        import uuid
        ln = Lineage(id=str(uuid.uuid4()), origin=sess.tool)
        ln.members[sess.tool] = Member(session_id=sess.session_id,
                                       hash=sess.content_hash(),
                                       updated=now().isoformat())
        self.lineages.append(ln)
        return ln

    def adopt(self, ln: Lineage, tool: str, session_id: str) -> Lineage:
        """Join `ln` to the lineage that already owns tool/session_id, if any.

        A source the registry did not know gets a fresh lineage, which would
        make it the origin and retag every recovered mirror "[From <source>]".
        If the copy we just found belongs to a known lineage, that one is the
        truth: fold the source into it and keep its origin.
        """
        other = self.find(tool, session_id)
        if other is None or other is ln:
            return ln
        # The session being pushed is authoritative for its own tool.
        other.members.update(ln.members)
        if ln in self.lineages:
            self.lineages.remove(ln)
        return other

    def record(self, ln: Lineage, tool: str, session_id: str, digest: str) -> None:
        ln.members[tool] = Member(session_id=session_id, hash=digest,
                                  updated=now().isoformat())

    def forget(self, tool: str, session_id: str) -> bool:
        ln = self.find(tool, session_id)
        if not ln:
            return False
        ln.members.pop(tool, None)
        if not ln.members:
            self.lineages.remove(ln)
        return True
