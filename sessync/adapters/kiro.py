"""Kiro CLI adapter.

Kiro keeps sessions in *two* stores, and which one it uses depends on how it was
started:

  file     ~/.kiro/sessions/cli/<uuid>.{jsonl,json,history,lock}
           written by a real interactive terminal; listed as "v2"
             .jsonl   {"version":"v1","kind":"Prompt"|"AssistantMessage",...}
             .json    sidecar: session_id / cwd / title / session_state
             .history plain-text readline history

  classic  the conversations_v2 table in
           %LOCALAPPDATA%\\Kiro-Cli\\data.sqlite3, written whenever stdin is not
           a tty -- `--no-interactive`, or a piped prompt. One JSON blob of
           serde-tagged Rust enums:

             history: [{user: {content: {Prompt: {prompt}}
                                       | {ToolUseResults: {tool_use_results}}},
                        assistant: {ToolUse: {content, tool_uses}}
                                 | {Response: {content}}}]

That table looked like dead legacy when it was empty; it is not. Both stores are
read. New mirrors are written to the file store, which is what an interactive
Kiro produces and therefore what you resume into.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..canonical import (Message, Session, SessionRef, ToolCall, as_json,
                         canonical_tool, coalesce, first_user_key,
                         flatten_for_text)

NAME = "kiro"
ROOT = Path.home() / ".kiro" / "sessions" / "cli"
CLASSIC_DB = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
              / "Kiro-Cli" / "data.sqlite3")


def _parse_ts(raw) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _content_text(content) -> str:
    """Kiro content blocks: {"kind": "text"|"thinking"|..., "data": ...}"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts, tools = [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        kind, data = b.get("kind"), b.get("data")
        if kind == "text" and isinstance(data, str):
            parts.append(data)
        elif kind in ("toolUse", "tool_use"):
            if isinstance(data, dict):
                tools.append(data.get("name", "tool"))
        # "thinking" carries redacted bytes -- dropped by design
    if tools:
        parts.append(f"[used tools: {', '.join(sorted(set(tools)))}]")
    return "\n".join(p for p in parts if p).strip()


def _classic_rows():
    """(conversation_id, cwd, value, created, updated) for classic sessions."""
    if not CLASSIC_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{CLASSIC_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT conversation_id, key, value, created_at, updated_at "
            "FROM conversations_v2").fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _classic_messages(blob: str) -> list[Message]:
    """Decode one classic conversation blob into canonical messages."""
    try:
        doc = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return []
    msgs: list[Message] = []
    pending: dict[str, ToolCall] = {}
    for turn in doc.get("history") or []:
        if not isinstance(turn, dict):
            continue
        user = turn.get("user") or {}
        ts = _parse_ts(user.get("timestamp"))
        content = user.get("content") or {}
        if "Prompt" in content:
            text = (content["Prompt"] or {}).get("prompt", "")
            if text:
                msgs.append(Message(role="user", text=text, ts=ts))
        elif "ToolUseResults" in content:
            # Results come back as a *user* turn referencing the call id.
            for res in (content["ToolUseResults"] or {}).get("tool_use_results") or []:
                call = pending.get(res.get("tool_use_id", ""))
                if call is None:
                    continue
                parts = [b.get("Text", "") for b in res.get("content") or []
                         if isinstance(b, dict)]
                call.result = "\n".join(p for p in parts if p)

        asst = turn.get("assistant") or {}
        inner = asst.get("ToolUse") or asst.get("Response") or {}
        text = inner.get("content", "") if isinstance(inner, dict) else ""
        tools = []
        for tu in (inner.get("tool_uses") or []) if isinstance(inner, dict) else []:
            call = ToolCall(name=canonical_tool(tu.get("name", "tool")),
                            args=as_json(tu.get("args", "")))
            pending[tu.get("id", "")] = call
            tools.append(call)
        if text or tools:
            msgs.append(Message(role="assistant", text=text, ts=ts, tools=tools))
    return msgs


def list_sessions() -> list[SessionRef]:
    refs = []
    # Not `return` on a missing file store -- the classic store may still hold
    # sessions even when no interactive session has ever run here.
    for f in (ROOT.glob("*.jsonl") if ROOT.exists() else ()):
        meta_path = f.with_suffix(".json")
        title, cwd = "", ""
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title, cwd = meta.get("title", ""), meta.get("cwd", "")
            except (json.JSONDecodeError, OSError):
                pass
        n, first_user = 0, ""
        try:
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("kind") in ("Prompt", "AssistantMessage"):
                        n += 1
                        if o.get("kind") == "Prompt" and not first_user:
                            first_user = first_user_key(
                                _content_text((o.get("data") or {}).get("content")))
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

    # ...and the classic store, which a non-tty Kiro writes instead.
    for cid, cwd, blob, created, updated in _classic_rows():
        msgs = coalesce(_classic_messages(blob))
        if not msgs:
            continue
        first = next((m.text for m in msgs if m.role == "user"), "")
        refs.append(SessionRef(
            tool=NAME, session_id=cid, title=" ".join(first.split())[:80],
            cwd=cwd or "", first_user=first_user_key(first),
            updated_at=datetime.fromtimestamp((updated or 0) / 1000, timezone.utc),
            path=str(CLASSIC_DB), n_messages=len(msgs),
        ))
    return refs


def read(session_id: str) -> Session:
    path = ROOT / f"{session_id}.jsonl"
    if not path.exists():
        return _read_classic(session_id)
    meta = {}
    meta_path = path.with_suffix(".json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    msgs: list[Message] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, data = o.get("kind"), o.get("data") or {}
            if kind == "Prompt":
                role = "user"
            elif kind == "AssistantMessage":
                role = "assistant"
            else:
                continue
            text = _content_text(data.get("content"))
            if not text:
                continue
            ts = _parse_ts((data.get("meta") or {}).get("timestamp"))
            msgs.append(Message(role=role, text=text, ts=ts))

    msgs = coalesce(msgs)
    return Session(
        tool=NAME, session_id=session_id,
        title=meta.get("title", ""),
        cwd=meta.get("cwd") or str(Path.home()),
        created_at=_parse_ts(meta.get("created_at")),
        updated_at=_parse_ts(meta.get("updated_at")),
        messages=msgs,
    )


def _template_state() -> dict:
    """Clone session_state from a real session.

    `rts_model_state` and `permissions` are non-nullable structs on the Rust
    side, so a hand-built sidecar with nulls fails to deserialise and Kiro
    silently skips the session. Copying a live one and blanking the
    conversation-specific parts is far more robust than guessing the schema.
    """
    fallback = {
        "version": "v1",
        "conversation_metadata": {
            "user_turn_metadatas": [], "last_context_usage": None,
            "user_turn_start_request": None, "last_request": None,
        },
        "rts_model_state": None, "permissions": None,
        "agent_name": "kiro_default", "goal": None,
    }
    candidates = sorted(ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in candidates:
        try:
            state = json.loads(c.read_text(encoding="utf-8")).get("session_state")
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(state, dict) or "rts_model_state" not in state:
            continue
        meta = state.get("conversation_metadata")
        if isinstance(meta, dict):
            meta["user_turn_metadatas"] = []
            meta["user_turn_start_request"] = None
            meta["last_request"] = None
        rts = state.get("rts_model_state")
        if isinstance(rts, dict):
            rts.pop("conversation_id", None)
        state["goal"] = None
        return state
    return fallback


def _read_classic(session_id: str) -> Session:
    for cid, cwd, blob, created, updated in _classic_rows():
        if cid != session_id:
            continue
        msgs = coalesce(_classic_messages(blob))
        first = next((m.text for m in msgs if m.role == "user"), "")
        return Session(
            tool=NAME, session_id=cid,
            title=" ".join(first.split())[:80], cwd=cwd or str(Path.home()),
            created_at=datetime.fromtimestamp((created or 0) / 1000, timezone.utc),
            updated_at=datetime.fromtimestamp((updated or 0) / 1000, timezone.utc),
            messages=msgs,
        )
    raise FileNotFoundError(f"kiro session {session_id} not found")


def exists(session_id: str) -> bool:
    if (ROOT / f"{session_id}.jsonl").exists():
        return True
    return any(cid == session_id for cid, *_ in _classic_rows())


def write(sess: Session, session_id: str | None = None,
          title: str | None = None) -> str:
    sid = session_id or str(uuid.uuid4())
    ROOT.mkdir(parents=True, exist_ok=True)
    cwd = sess.cwd or str(Path.home())
    title = title or sess.ported_title()

    lines, history = [], []
    # Kiro has no tool_use record; tool cycles ride along as a rendered
    # note, which read() parses back so the round trip still matches.
    for m in flatten_for_text(sess.messages):
        epoch = int(m.ts.timestamp())
        if m.role == "user":
            history.append(m.text.splitlines()[0] if m.text else "")
            lines.append({
                "version": "v1", "kind": "Prompt",
                "data": {
                    "message_id": str(uuid.uuid4()),
                    "content": [{"kind": "text", "data": m.text}],
                    "meta": {"timestamp": epoch},
                },
            })
        else:
            lines.append({
                "version": "v1", "kind": "AssistantMessage",
                "data": {
                    "message_id": str(uuid.uuid4()),
                    "content": [{"kind": "text", "data": m.text}],
                },
            })

    (ROOT / f"{sid}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
        encoding="utf-8", newline="\n")

    def iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    state = _template_state()
    rts = state.get("rts_model_state")
    if isinstance(rts, dict):
        rts["conversation_id"] = sid

    sidecar = {
        "session_id": sid,
        "cwd": cwd,
        "created_at": iso(sess.created_at),
        "updated_at": iso(sess.updated_at),
        "title": title,
        # Deserialised into a Rust enum; "subagent" is what real CLI sessions
        # carry. An unrecognised variant makes Kiro skip the session silently.
        "session_created_reason": "subagent",
        "session_state": state,
    }
    (ROOT / f"{sid}.json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8", newline="\n")
    (ROOT / f"{sid}.history").write_text(
        "\n".join(history) + ("\n" if history else ""), encoding="utf-8", newline="\n")
    # Lock files hold {"pid","started_at"}; a stale pid reads as idle.
    (ROOT / f"{sid}.lock").write_text(
        json.dumps({"pid": 0, "started_at": iso(sess.updated_at)}),
        encoding="utf-8", newline="\n")
    return sid


def delete(session_id: str) -> bool:
    """Remove a session's jsonl, json sidecar, history, and lock files."""
    removed = False
    for suf in (".jsonl", ".json", ".history", ".lock"):
        p = ROOT / f"{session_id}{suf}"
        if p.exists():
            try:
                p.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass
    return removed


def resume_hint(session_id: str, cwd: str) -> str:
    return f'cd "{cwd}" && kiro-cli --resume-id {session_id}'
