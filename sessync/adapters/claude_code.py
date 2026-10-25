"""Claude Code adapter.

Storage: ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
The JSONL is authoritative, so writing a well-formed file is enough for
`claude --resume` to list and load it.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..canonical import (Message, Session, SessionRef, ToolCall, agent_tool,
                         as_json, canonical_tool, coalesce, first_user_key,
                         flatten_output, render_tool_note)

NAME = "claude"
ROOT = Path.home() / ".claude" / "projects"
CLI_VERSION = "2.1.222"

# Records that carry no conversational content.
_SKIP = {
    "mode", "permission-mode", "bridge-session", "ai-title", "last-prompt",
    "progress", "queue-operation", "system", "attachment", "summary",
    "file-history-snapshot", "file-history-delta",
}


def _as_input(args: str):
    """Claude's tool_use.input is an object; ours is JSON text."""
    try:
        parsed = json.loads(args) if args else {}
    except (json.JSONDecodeError, TypeError):
        return {"input": args}
    return parsed if isinstance(parsed, dict) else {"input": parsed}


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _blocks(content) -> tuple[str, list[tuple[str, ToolCall]], list[tuple[str, str]]]:
    """Split Claude content blocks into text, tool calls, and tool results.

    Calls and results live in *different records* -- the call in an assistant
    turn, the result in the user record that follows -- and are paired by
    `tool_use_id`, so both are returned for the caller to stitch together.
    """
    if isinstance(content, str):
        return content, [], []
    if not isinstance(content, list):
        return "", [], []
    parts: list[str] = []
    calls: list[tuple[str, ToolCall]] = []
    results: list[tuple[str, str]] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_use":
            calls.append((b.get("id", ""), ToolCall(
                name=canonical_tool(b.get("name", "tool")),
                args=as_json(b.get("input", "")))))
        elif t == "tool_result":
            results.append((b.get("tool_use_id", ""),
                            flatten_output(b.get("content"))))
        # thinking / image / document are dropped by design
    return "\n".join(p for p in parts if p).strip(), calls, results


def _parse_ts(raw) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def list_sessions() -> list[SessionRef]:
    refs = []
    if not ROOT.exists():
        return refs
    for proj in ROOT.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            title, cwd, n, first_user = "", "", 0, ""
            try:
                with f.open(encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            o = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if o.get("type") == "ai-title":
                            title = o.get("aiTitle", "") or title
                        elif o.get("type") in ("user", "assistant"):
                            cwd = o.get("cwd", cwd)
                            # Count exactly what read() yields; find_by_content
                            # prefilters on this, so a looser count here can
                            # reject a real match and mint a duplicate.
                            if o.get("isSidechain"):
                                continue
                            txt, calls, _ = _blocks((o.get("message") or {}).get("content"))
                            if txt or calls:
                                n += 1
                            if o["type"] == "user" and txt and not first_user:
                                first_user = first_user_key(txt)
            except OSError:
                continue
            if n == 0:
                continue
            refs.append(SessionRef(
                tool=NAME, session_id=f.stem, title=title, cwd=cwd,
                first_user=first_user,
                updated_at=datetime.fromtimestamp(f.stat().st_mtime, timezone.utc),
                path=str(f), n_messages=n,
            ))
    return refs


def _find(session_id: str) -> Path:
    for proj in ROOT.iterdir():
        p = proj / f"{session_id}.jsonl"
        if p.exists():
            return p
    raise FileNotFoundError(f"claude session {session_id} not found")


def _compaction_start(lines: list[str]) -> int:
    """Index just after the last compaction boundary, or 0.

    When Claude compacts, it drops everything before the boundary from its own
    context and replaces it with a summary -- but every discarded record stays
    in the file. Reading the whole file would hand the target a conversation the
    source no longer has, and one measured here had ~1M tokens of history behind
    the boundary. The summary sits immediately after it as a user record flagged
    `isCompactSummary`, so starting there reproduces what the session knows.
    """
    start = 0
    for i, line in enumerate(lines):
        if "compact_boundary" not in line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("subtype") == "compact_boundary":
            start = i + 1
    return start


def read(session_id: str) -> Session:
    path = _find(session_id)
    msgs: list[Message] = []
    pending: dict[str, ToolCall] = {}      # tool_use_id -> call awaiting output
    title, cwd = "", str(Path.home())
    created = None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = _compaction_start(lines)

    # The title is written once, near the top, so recover it from the discarded
    # region rather than losing it along with the compacted history.
    for line in lines[:start]:
        if '"ai-title"' in line:
            try:
                title = json.loads(line).get("aiTitle", "") or title
            except json.JSONDecodeError:
                pass

    for line in lines[start:]:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type")
        if t == "ai-title":
            title = o.get("aiTitle", "") or title
            continue
        if t in _SKIP or t not in ("user", "assistant"):
            continue
        if o.get("isSidechain"):
            continue
        m = o.get("message") or {}
        text, calls, results = _blocks(m.get("content"))
        for use_id, output in results:
            call = pending.get(use_id)
            if call is not None:
                call.result = output
        if not text and not calls:
            continue          # a record carrying only tool_results
        cwd = o.get("cwd", cwd)
        ts = _parse_ts(o.get("timestamp"))
        created = created or ts
        tools = []
        for use_id, call in calls:
            pending[use_id] = call
            tools.append(call)
        msgs.append(Message(role=t, text=text, ts=ts, tools=tools))
    msgs = coalesce(msgs)
    return Session(
        tool=NAME, session_id=session_id, title=title, cwd=cwd,
        created_at=created or datetime.now(timezone.utc),
        updated_at=msgs[-1].ts if msgs else datetime.now(timezone.utc),
        messages=msgs,
    )


def exists(session_id: str) -> bool:
    try:
        _find(session_id)
        return True
    except (FileNotFoundError, OSError):
        return False


def write(sess: Session, session_id: str | None = None,
          title: str | None = None) -> str:
    """Materialise a canonical session, replacing `session_id` if given."""
    sid = session_id or str(uuid.uuid4())
    cwd = sess.cwd or str(Path.home())
    out_dir = ROOT / encode_cwd(cwd)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sid}.jsonl"
    title = title or sess.ported_title()

    lines: list[dict] = [
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "ai-title", "aiTitle": title, "sessionId": sid},
    ]

    parent = None
    last_user, leaf = "", None
    for m in sess.messages:
        u = str(uuid.uuid4())
        stamp = m.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        base = {
            "parentUuid": parent, "isSidechain": False, "uuid": u,
            "timestamp": stamp, "cwd": cwd, "sessionId": sid,
            "version": CLI_VERSION, "gitBranch": "", "userType": "external",
            "entrypoint": "cli",
        }
        if m.role == "user":
            base.update({
                "type": "user",
                "promptId": str(uuid.uuid4()),
                "message": {"role": "user", "content": m.text},
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "permissionMode": "default",
            })
            last_user = m.text
        else:
            content: list[dict] = []
            if m.text:
                content.append({"type": "text", "text": m.text})
            use_ids = []
            for call in m.tools:
                use_id = f"toolu_{uuid.uuid4().hex[:24]}"
                use_ids.append((use_id, call))
                content.append({
                    "type": "tool_use", "id": use_id,
                    "name": agent_tool(call.name, NAME),
                    "input": _as_input(call.args),
                })
            # Assistant records carry both sessionId and session_id on disk.
            base.update({
                "type": "assistant",
                "requestId": f"req_{uuid.uuid4().hex[:24]}",
                "session_id": sid,
                "effort": "high",
                "message": {
                    "model": "claude-opus-5",
                    "id": f"msg_{uuid.uuid4().hex[:24]}",
                    "type": "message", "role": "assistant",
                    "content": content or [{"type": "text", "text": ""}],
                    "stop_reason": "tool_use" if use_ids else "end_turn",
                    "stop_sequence": None,
                    "stop_details": None, "diagnostics": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0},
                },
            })
        lines.append(base)
        parent = leaf = u

        # Results come back in their own user record, as Claude Code writes
        # them -- an assistant tool_use with no matching tool_result makes the
        # API reject the conversation on the first resumed turn.
        if m.role == "assistant" and m.tools:
            r_uuid = str(uuid.uuid4())
            lines.append({
                "parentUuid": parent, "isSidechain": False, "uuid": r_uuid,
                "timestamp": stamp, "cwd": cwd, "sessionId": sid,
                "version": CLI_VERSION, "gitBranch": "", "userType": "external",
                "entrypoint": "cli", "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": use_id,
                     "content": [{"type": "text", "text": call.result}]}
                    for use_id, call in use_ids]},
            })
            parent = leaf = r_uuid

    lines.append({"type": "last-prompt", "lastPrompt": last_user,
                  "leafUuid": leaf, "sessionId": sid})

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in lines:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    _append_history(sid, sess, cwd)
    return sid


HISTORY = Path.home() / ".claude" / "history.jsonl"


def _append_history(sid: str, sess: Session, cwd: str) -> None:
    """Add the ported prompts to Claude's up-arrow history.

    This file is prompt recall, not a session index -- ported sessions list in
    `claude --resume` without it. Written anyway so the conversation's prompts
    are recallable in the mirror, matching what we do on the Antigravity side.
    Idempotent on the session id, so re-pushing does not duplicate.
    """
    try:
        if HISTORY.exists() and f'"{sid}"' in HISTORY.read_text(
                encoding="utf-8", errors="replace"):
            return
        rows = [{"display": m.text[:2000], "pastedContents": {},
                 "timestamp": int(m.ts.timestamp() * 1000),
                 "project": cwd, "sessionId": sid}
                for m in sess.messages if m.role == "user" and m.text.strip()]
        if not rows:
            return
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass


def delete(session_id: str) -> bool:
    """Remove a session file and any appended entries in history.jsonl."""
    removed = False
    try:
        path = _find(session_id)
        path.unlink(missing_ok=True)
        # Clean up empty project directory if no files remain
        if path.parent.exists() and not any(path.parent.iterdir()):
            path.parent.rmdir()
        removed = True
    except (FileNotFoundError, OSError):
        pass

    try:
        if HISTORY.exists():
            lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            kept = [l for l in lines if f'"{session_id}"' not in l]
            if len(kept) != len(lines):
                HISTORY.write_text("".join(kept), encoding="utf-8", newline="\n")
    except OSError:
        pass

    return removed


def resume_hint(session_id: str, cwd: str) -> str:
    return f'cd "{cwd}" && claude --resume {session_id}'
