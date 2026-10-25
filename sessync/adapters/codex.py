"""Codex CLI adapter.

Storage: ~/.codex/sessions/YYYY/MM/DD/rollout-<local-ts>-<uuid>.jsonl

Codex writes two parallel streams: `response_item` (model-facing, authoritative)
and `event_msg` (UI event log). We read only `response_item` so turns are not
counted twice, and write only `response_item` plus the `session_meta` header.

There is no title field in session_meta -- the picker previews the first user
message -- so provenance is carried in a synthetic leading user turn.

Two things gate whether the picker lists a session, as opposed to merely
resuming it by id:

  * `history_mode` must be "paginated", and the rollout must carry the
    `event_msg/item_completed` records that paginated mode implies. A "legacy"
    rollout resumes fine by id but is invisible in `codex resume`.
  * ids must be UUIDv7. Codex mints v7 (`01a02af7-0b80-7341-...`); a v4 carries
    no usable embedded timestamp.

The picker also filters by cwd, so a ported session only shows up when
`codex resume` runs from the same directory the source session used.

Critically, the picker lists from the `threads` table in
~/.codex/state_5.sqlite, not from the rollout files. Codex indexes sessions it
creates itself, so an externally written rollout is resumable by id but absent
from the picker until it has a row there. Note the indexed `cwd` carries the
Windows extended-length prefix, so the cwd filter misses a row
stored with a plain path.

We deliberately do NOT write `local_thread_catalog` (the VS Code extension's
index): sessions the real CLI creates have no row there either.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..canonical import (Message, Session, SessionRef, ToolCall, agent_tool,
                         as_json, canonical_tool, coalesce, first_user_key,
                         flatten_output, is_provenance_header)

NAME = "codex"
ROOT = Path.home() / ".codex" / "sessions"
CLI_VERSION = "0.149.0"

# Harness-injected pseudo-user turns that are not real conversation.
_INJECTED_PREFIXES = (
    "<environment_context>", "<skills_instructions>", "<user_instructions>",
    "<multi_agent_mode>", "<plan_mode>", "<AGENTS.md>", "<recommended_plugins>",
)


def _parse_ts(raw) -> datetime:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"):
            out.append(b.get("text", ""))
    return "\n".join(p for p in out if p).strip()


def list_sessions() -> list[SessionRef]:
    refs = []
    if not ROOT.exists():
        return refs
    for f in ROOT.rglob("rollout-*.jsonl"):
        sid, cwd, n, first = f.stem, "", 0, ""
        closed, last_assistant = False, False
        try:
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    p = o.get("payload") or {}
                    if o.get("type") == "session_meta":
                        sid = p.get("session_id", sid)
                        cwd = p.get("cwd", cwd)
                        continue
                    if o.get("type") == "compacted":
                        # read() restarts from the replacement window, so the
                        # count has to as well.
                        n, closed, last_assistant = len(
                            _messages_from(p.get("replacement_history") or [],
                                           datetime.now(timezone.utc))), False, False
                        continue
                    if o.get("type") != "response_item":
                        continue
                    ptype = p.get("type")
                    if ptype == "message":
                        role = p.get("role")
                        if role not in ("user", "assistant"):
                            continue
                        txt = _content_text(p.get("content"))
                        if not txt or txt.startswith(_INJECTED_PREFIXES):
                            continue
                        if is_provenance_header(txt):
                            continue
                        n += 1
                        closed, last_assistant = False, role == "assistant"
                        if role == "user" and not first:
                            first = first_user_key(txt)
                    elif ptype in ("function_call", "custom_tool_call"):
                        # A tool-only turn is a message too. Counting only
                        # `message` records made this disagree with read(), and
                        # find_by_content prefilters on it -- so a real mirror
                        # was rejected and would have been duplicated.
                        if not (last_assistant and not closed):
                            n += 1
                            last_assistant = True
                        closed = False
                    elif ptype in ("function_call_output", "custom_tool_call_output"):
                        closed = True
        except OSError:
            continue
        if n == 0:
            continue
        refs.append(SessionRef(
            tool=NAME, session_id=sid, title=first, cwd=cwd, first_user=first,
            updated_at=datetime.fromtimestamp(f.stat().st_mtime, timezone.utc),
            path=str(f), n_messages=n,
        ))
    return refs


def _find(session_id: str) -> Path:
    for f in ROOT.rglob("rollout-*.jsonl"):
        if session_id in f.name:
            return f
    for f in ROOT.rglob("rollout-*.jsonl"):
        try:
            first = json.loads(f.open(encoding="utf-8").readline())
        except (json.JSONDecodeError, OSError):
            continue
        if (first.get("payload") or {}).get("session_id") == session_id:
            return f
    raise FileNotFoundError(f"codex session {session_id} not found")


# Sessions a person sits in. `exec` is `codex exec` -- scripted, often fired in
# bulk -- and subagent threads carry a {"subagent": ...} object. Neither is a
# conversation anyone resumes, and mirroring a batch of 250 exec runs filled
# every other agent's picker with near-identical sessions.
_INTERACTIVE_SOURCES = ("cli", "vscode")


def interactive(session_id: str) -> bool:
    """True unless session_meta marks this as a scripted or subagent run."""
    try:
        with _find(session_id).open(encoding="utf-8") as fh:
            meta = json.loads(fh.readline())
    except (OSError, json.JSONDecodeError):
        return True
    if meta.get("type") != "session_meta":
        return True
    source = (meta.get("payload") or {}).get("source", "cli")
    return source in _INTERACTIVE_SOURCES


def _messages_from(items: list, ts) -> list[Message]:
    """Turn a list of response-item dicts into canonical messages."""
    out: list[Message] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        txt = _content_text(item.get("content"))
        if not txt or txt.startswith(_INJECTED_PREFIXES):
            continue
        if is_provenance_header(txt):
            continue
        out.append(Message(role=role, text=txt, ts=ts))
    return out


def read(session_id: str) -> Session:
    path = _find(session_id)
    msgs: list[Message] = []
    pending: dict[str, ToolCall] = {}      # call_id -> call awaiting output
    closed = False                         # did an output just close a turn?
    cwd, created, sid = str(Path.home()), None, session_id
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = o.get("payload") or {}
            ts = _parse_ts(o.get("timestamp"))
            if o.get("type") == "session_meta":
                sid = p.get("session_id", sid)
                cwd = p.get("cwd", cwd)
                created = _parse_ts(p.get("timestamp"))
                continue
            if o.get("type") == "compacted":
                # Codex replaces its whole history at a compaction point. The
                # records before it stay in the rollout but are no longer part
                # of the conversation, so carrying them would hand the target
                # context the source has already dropped -- one session here
                # holds 10 compactions and 272 messages for a 129-item window.
                msgs = _messages_from(p.get("replacement_history") or [], ts)
                continue
            if o.get("type") != "response_item":
                continue
            ptype = p.get("type")
            if ptype == "message":
                role = p.get("role")
                if role not in ("user", "assistant"):
                    continue          # developer = system prompt, skipped
                txt = _content_text(p.get("content"))
                if not txt or txt.startswith(_INJECTED_PREFIXES):
                    continue
                if is_provenance_header(txt):
                    continue      # header from a previous port, not real input
                msgs.append(Message(role=role, text=txt, ts=ts))
                closed = False
            elif ptype in ("function_call", "custom_tool_call"):
                # `arguments` on function_call, `input` on custom_tool_call.
                call = ToolCall(
                    name=canonical_tool(p.get("name") or "tool"),
                    args=as_json(p.get("arguments", p.get("input", ""))))
                pending[p.get("call_id", "")] = call
                # Calls issued together in one turn arrive back-to-back; a call
                # that follows an *output* is a new turn. Without this, a run of
                # sequential tool turns folds into a single message -- 71 of
                # them collapsed into one on a real session.
                if msgs and msgs[-1].role == "assistant" and not closed:
                    msgs[-1].tools.append(call)
                else:
                    msgs.append(Message(role="assistant", text="", ts=ts,
                                        tools=[call]))
                closed = False
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call = pending.get(p.get("call_id", ""))
                if call is not None:
                    call.result = flatten_output(p.get("output"))
                closed = True
    msgs = coalesce(msgs)
    first_user = next((m.text for m in msgs if m.role == "user"), "")
    return Session(
        tool=NAME, session_id=sid,
        title=" ".join(first_user.split())[:80],
        cwd=cwd, created_at=created or datetime.now(timezone.utc),
        updated_at=msgs[-1].ts if msgs else datetime.now(timezone.utc),
        messages=msgs,
    )


def _uuid7(when: datetime | None = None) -> str:
    """RFC 9562 UUIDv7 -- 48-bit ms timestamp, version 7, random tail.

    Codex mints v7 ids (`01a02af7-0b80-7341-...`). We match that: anything that
    sorts threads by the timestamp embedded in the id would mis-sort or reject a
    v4.
    """
    ms = int((when or datetime.now(timezone.utc)).timestamp() * 1000)
    rand = uuid.uuid4().int
    val = (ms & 0xFFFFFFFFFFFF) << 80
    val |= 0x7 << 76                                  # version
    val |= ((rand >> 64) & 0xFFF) << 64               # rand_a
    val |= 0b10 << 62                                 # variant
    val |= rand & 0x3FFFFFFFFFFFFFFF                  # rand_b
    return str(uuid.UUID(int=val))


def _turns(sess: Session) -> list[tuple[str, list[Message]]]:
    """Group the flat message list into Codex turns (one user + its replies)."""
    header = Message(role="user", text=sess.provenance_header(),
                     ts=sess.messages[0].ts)
    grouped: list[tuple[str, list[Message]]] = []
    for m in [header] + list(sess.messages):
        if m.role == "user" or not grouped:
            grouped.append((_uuid7(m.ts), [m]))
        else:
            grouped[-1][1].append(m)
    return grouped


def exists(session_id: str) -> bool:
    try:
        _find(session_id)
        return True
    except (FileNotFoundError, OSError):
        return False


def write(sess: Session, session_id: str | None = None,
          title: str | None = None) -> str:
    now_utc = datetime.now(timezone.utc)
    cwd = sess.cwd or str(Path.home())
    local = now_utc.astimezone()

    path = None
    if session_id:
        sid = session_id
        try:
            path = _find(sid)          # keep the original filename
        except FileNotFoundError:
            path = None
    else:
        sid = _uuid7(now_utc)
    if path is None:
        out_dir = ROOT / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"rollout-{local:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"

    def stamp(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    recs: list[dict] = [{
        "timestamp": stamp(now_utc), "ordinal": 0, "type": "session_meta",
        "payload": {
            "session_id": sid, "id": sid, "timestamp": stamp(now_utc),
            "cwd": cwd, "originator": "codex-tui", "cli_version": CLI_VERSION,
            "source": "cli", "thread_source": "user", "model_provider": "openai",
            # "paginated" is what current Codex writes. Legacy rollouts stay
            # resumable by id but never appear in the `codex resume` picker.
            "history_mode": "paginated",
            "context_window": {"window_id": _uuid7(now_utc)},
        },
    }]
    ordinal = 1

    def emit(rtype: str, payload: dict, dt: datetime) -> None:
        nonlocal ordinal
        recs.append({"timestamp": stamp(dt), "ordinal": ordinal,
                     "type": rtype, "payload": payload})
        ordinal += 1

    for turn_id, msgs in _turns(sess):
        start, end = msgs[0].ts, msgs[-1].ts
        emit("event_msg", {
            "type": "task_started", "turn_id": turn_id,
            "started_at": int(start.timestamp()),
            "model_context_window": 258400,
            "collaboration_mode_kind": "default",
        }, start)
        emit("turn_context", {
            "turn_id": turn_id, "cwd": cwd, "workspace_roots": [cwd],
            "current_date": start.astimezone().strftime("%Y-%m-%d"),
            "approval_policy": "on-request", "approvals_reviewer": "user",
            "sandbox_policy": {"type": "workspace-write", "network_access": False,
                               "exclude_tmpdir_env_var": False,
                               "exclude_slash_tmp": False},
        }, start)

        for m in msgs:
            item_id = _uuid7(m.ts) if m.role == "user" else f"msg_{uuid.uuid4().hex}"
            if m.text:
                emit("response_item", {
                    "type": "message", "id": f"msg_{uuid.uuid4()}", "role": m.role,
                    "content": [{
                        "type": "input_text" if m.role == "user" else "output_text",
                        "text": m.text,
                    }],
                    **({} if m.role == "user" else {"phase": "final_answer"}),
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id, "create_time": m.ts.timestamp()},
                }, m.ts)

            # Real function_call / function_call_output pairs, matched by
            # call_id. A call without its output leaves the conversation
            # invalid, so both are always written together.
            # All calls of a turn, then all their outputs -- the order Codex
            # itself uses for parallel calls. Interleaving them makes the
            # reader see each output as closing the turn, splitting one
            # multi-tool turn into several.
            issued = []
            for call in m.tools:
                call_id = f"call_{uuid.uuid4().hex[:24]}"
                issued.append((call_id, call))
                emit("response_item", {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex}",
                    "name": agent_tool(call.name, NAME),
                    "arguments": call.args or "{}",
                    "call_id": call_id,
                }, m.ts)
            for call_id, call in issued:
                emit("response_item", {
                    "type": "function_call_output",
                    "id": f"fco_{uuid.uuid4()}",
                    "call_id": call_id,
                    "output": [{"type": "input_text", "text": call.result}],
                }, m.ts)

            # The picker renders threads from these; a rollout without them is
            # invisible to `codex resume` even though it loads fine by id.
            if not m.text:
                continue          # tool-only turn: nothing for the UI to show
            if m.role == "user":
                item = {"type": "UserMessage", "id": item_id,
                        "content": [{"type": "text", "text": m.text,
                                     "text_elements": []}]}
            else:
                item = {"type": "AgentMessage", "id": item_id,
                        "content": [{"type": "Text", "text": m.text}],
                        "phase": "final_answer"}
            emit("event_msg", {
                "type": "item_completed", "thread_id": sid, "turn_id": turn_id,
                "item": item,
                "started_at_ms": ms(m.ts), "completed_at_ms": ms(m.ts),
            }, m.ts)

        last_agent = next((m.text for m in reversed(msgs) if m.role == "assistant"), "")
        emit("event_msg", {
            "type": "task_complete", "turn_id": turn_id,
            "last_agent_message": last_agent,
            "started_at": int(start.timestamp()),
            "completed_at": int(end.timestamp()),
            "duration_ms": max(ms(end) - ms(start), 0),
        }, end)

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    _index_thread(sid, sess, cwd, path, now_utc, title)
    _index_jsonl(sid, title or sess.ported_title())
    return sid


SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"


def _index_jsonl(sid: str, title: str) -> None:
    """Also list the thread in ~/.codex/session_index.jsonl.

    `state_5.sqlite` is what the picker reads, but this file matters separately:
    ai-session-bridge found that `codex resume` will *read* an unindexed rollout
    while *appending* a new turn to it fails with "thread not found". Cheap
    insurance, and idempotent on the id.
    """
    try:
        if SESSION_INDEX.exists():
            if f'"id":"{sid}"' in SESSION_INDEX.read_text(encoding="utf-8"):
                return
        else:
            SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        row = {"id": sid, "thread_name": title[:120], "updated_at": stamp}
        with SESSION_INDEX.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


STATE_DB = Path.home() / ".codex" / "state_5.sqlite"

_THREAD_COLS = (
    "id", "rollout_path", "created_at", "updated_at", "source", "model_provider",
    "cwd", "title", "sandbox_policy", "approval_mode", "tokens_used",
    "has_user_event", "archived", "cli_version", "first_user_message",
    "memory_mode", "model", "reasoning_effort", "created_at_ms", "updated_at_ms",
    "thread_source", "preview", "recency_at", "recency_at_ms", "history_mode",
    "is_pinned",
)


def _index_cwd(cwd: str) -> str:
    """Codex stores cwd with the Windows extended-length prefix."""
    if os.name == "nt" and not cwd.startswith("\\\\?\\"):
        return "\\\\?\\" + cwd
    return cwd


def _index_thread(sid: str, sess: Session, cwd: str, path: Path,
                  created: datetime, title: str | None = None) -> None:
    """Register the thread in ~/.codex/state_5.sqlite.

    This is what `codex resume` actually lists -- the rollout file alone is only
    enough to resume by id. Codex indexes sessions it creates itself, so an
    externally written rollout stays invisible in the picker until it has a row
    here. The table is a derived index (Codex can rebuild it from rollout
    files), so this is safe to write and best effort.

    `sandbox_policy` and the model fields are cloned from an existing row rather
    than invented, for the same reason as Kiro's sidecar: they are structured
    values we have no schema for.
    """
    if not STATE_DB.exists():
        return
    title = (title or sess.ported_title())[:200]
    started = sess.messages[0].ts if sess.messages else created
    end = sess.messages[-1].ts if sess.messages else created
    try:
        con = sqlite3.connect(STATE_DB, timeout=10)
        con.execute("PRAGMA busy_timeout = 10000")
        tpl = con.execute(
            "SELECT sandbox_policy, approval_mode, model, reasoning_effort, "
            "memory_mode, cli_version FROM threads "
            "WHERE cwd = ? ORDER BY updated_at DESC LIMIT 1", (_index_cwd(cwd),)
        ).fetchone() or con.execute(
            "SELECT sandbox_policy, approval_mode, model, reasoning_effort, "
            "memory_mode, cli_version FROM threads "
            "ORDER BY updated_at DESC LIMIT 1").fetchone()
        sandbox, approval, model, effort, memory, ver = tpl or (
            "{}", "on-request", None, None, "enabled", CLI_VERSION)

        values = (
            sid, str(path), int(started.timestamp()), int(end.timestamp()),
            "cli", "openai", _index_cwd(cwd), title, sandbox, approval, 0,
            0, 0, ver or CLI_VERSION, title, memory or "enabled", model, effort,
            int(started.timestamp() * 1000), int(end.timestamp() * 1000),
            "user", title, int(end.timestamp()), int(end.timestamp() * 1000),
            "paginated", 0,
        )
        cols = ", ".join(_THREAD_COLS)
        marks = ", ".join("?" * len(_THREAD_COLS))
        con.execute(f"INSERT OR REPLACE INTO threads ({cols}) VALUES ({marks})",
                    values)
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def resume_hint(session_id: str, cwd: str) -> str:
    return f'cd "{cwd}" && codex resume {session_id}'
