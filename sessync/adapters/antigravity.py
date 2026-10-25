"""Antigravity CLI adapter.

Storage (~/.gemini/antigravity-cli/):
  conversations/<id>.db                            authoritative; protobuf-in-SQLite
  brain/<id>/.system_generated/logs/transcript*.jsonl   readable projection

Reading uses the transcript JSONL. Writing has to build the .db, because that is
what the CLI enumerates -- the transcript alone is not enough. There is no
published .proto, so steps are synthesised on the wire format observed in real
conversations:

  1  varint  step_type   14=USER_INPUT 15=PLANNER_RESPONSE 23=CHECKPOINT 132=GENERIC
  4  varint  status      3=DONE
  5  msg     StepMeta    .1 created_at  .3 source(4=user,2=model)
                         .12 cascade uuid  .20{.1 trajectory .4 conversation}
                         .26 status history
  19 msg     UserInput       .2 text  .3{.1 text}  .4 ""
  20 msg     PlannerResponse .1 text  .8 text  .12 = 2
  30 msg     Checkpoint      .4 title  .15 brain uri  .19 first prompt

The `metadata` column repeats StepMeta standalone.

The picker is the in-session `/resume` command (not a CLI flag, so it is absent
from `agy --help`). It enumerates `conversations/*.db` directly -- no index to
register with, unlike Codex -- and does not filter by cwd.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import proto
from ..canonical import (TOOL_LABELS, Message, Session, SessionRef, ToolCall,
                         as_json, canonical_tool, coalesce, first_user_key,
                         flatten_for_text, is_provenance_header)

NAME = "antigravity"
ROOT = Path.home() / ".gemini" / "antigravity-cli"
CONV = ROOT / "conversations"
BRAIN = ROOT / "brain"
SUMMARIES = ROOT / "conversation_summaries.db"

STEP_USER, STEP_PLANNER, STEP_CHECKPOINT = 14, 15, 23
SRC_MODEL, SRC_USER, SRC_SYSTEM = 2, 4, 5
STATUS_DONE = 3

_TAGS = re.compile(
    r"</?(?:USER_REQUEST|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|IMPORTANT)>")


def _strip_wrappers(text: str) -> str:
    """USER_INPUT content embeds <USER_REQUEST> / metadata blocks."""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.S)
    if m:
        return m.group(1).strip()
    return _TAGS.sub("", text).strip()


def _logs_dir(cid: str) -> Path:
    return BRAIN / cid / ".system_generated" / "logs"


def _parse_ts(raw) -> datetime:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# ------------------------------------------------------------------ reading

def _read_transcript(cid: str) -> tuple[list[Message], datetime | None]:
    logs = _logs_dir(cid)
    path = logs / "transcript_full.jsonl"
    if not path.exists():
        path = logs / "transcript.jsonl"
    if not path.exists():
        return [], None
    msgs, created = [], None
    awaiting: list[ToolCall] = []   # calls whose GENERIC result has not arrived
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = o.get("type")
            ts = _parse_ts(o.get("created_at"))
            created = created or ts
            if typ == "USER_INPUT":
                text = _strip_wrappers(o.get("content") or "")
                if text and not is_provenance_header(text):
                    msgs.append(Message(role="user", text=text, ts=ts))
            elif typ == "PLANNER_RESPONSE":
                text = str(o.get("content") or "").strip()
                tools = []
                for c in o.get("tool_calls") or []:
                    if not isinstance(c, dict):
                        continue
                    args = dict(c.get("args") or {})
                    # toolAction/toolSummary are UI labels, not arguments.
                    args.pop("toolAction", None)
                    args.pop("toolSummary", None)
                    tools.append(ToolCall(
                        name=canonical_tool(c.get("name", "tool")),
                        args=as_json(args)))
                if text or tools:
                    msgs.append(Message(role="assistant", text=text, ts=ts,
                                        tools=tools))
                # Results arrive as the GENERIC steps that follow, in order.
                awaiting.extend(tools)
            elif typ == "GENERIC":
                body = str(o.get("content") or "").strip()
                if body and awaiting:
                    awaiting.pop(0).result = body
    return msgs, created


def list_sessions() -> list[SessionRef]:
    refs = []
    if not CONV.exists():
        return refs
    for db in CONV.glob("*.db"):
        cid = db.stem
        msgs, _ = _read_transcript(cid)
        if not msgs:
            continue
        first = next((m.text for m in msgs if m.role == "user"), "")
        refs.append(SessionRef(
            first_user=first_user_key(first),
            tool=NAME, session_id=cid,
            title=" ".join(first.split())[:80],
            cwd=_workspace_of(cid) or str(Path.home()),
            updated_at=datetime.fromtimestamp(db.stat().st_mtime, timezone.utc),
            path=str(db), n_messages=len(msgs),
        ))
    return refs


def _workspace_of(cid: str) -> str:
    """Pull the workspace path back out of trajectory_metadata_blob."""
    db = CONV / f"{cid}.db"
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
        con.close()
    except sqlite3.Error:
        return ""
    uri = ""
    if row and row[0]:
        ws = proto.parse(row[0]).get(1)
        if ws:
            uri = proto.first_str(proto.parse(ws[0]), 1) or ""
    if uri.startswith("file:///"):
        p = urllib.parse.unquote(uri[len("file:///"):])
        return p.replace("/", "\\")
    return _workspace_from_history(cid)


def _workspace_from_history(cid: str) -> str:
    """Fallback when the metadata blob is unusable.

    Antigravity blanks `trajectory_metadata_blob` on conversations it opens in
    its own picker, so the workspace has to be recoverable from somewhere it
    does not rewrite. `history.jsonl` records the workspace per prompt.
    """
    path = ROOT / "history.jsonl"
    if not path.exists():
        return ""
    found = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if cid not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("conversationId") == cid and o.get("workspace"):
                    found = o["workspace"]
    except OSError:
        return ""
    return found


def read(session_id: str) -> Session:
    msgs, created = _read_transcript(session_id)
    if not msgs:
        raise FileNotFoundError(f"antigravity session {session_id} has no transcript")
    msgs = coalesce(msgs)
    first = next((m.text for m in msgs if m.role == "user"), "")
    return Session(
        tool=NAME, session_id=session_id,
        title=" ".join(first.split())[:80],
        cwd=_workspace_of(session_id) or str(Path.home()),
        created_at=created or datetime.now(timezone.utc),
        updated_at=msgs[-1].ts, messages=msgs,
    )


# ------------------------------------------------------------------ writing

def _meta_blob(dt: datetime, cascade: str, traj: str, cid: str, seq: int) -> bytes:
    sec, nanos = int(dt.timestamp()), dt.microsecond * 1000
    return (
        proto.ts(1, sec, nanos)
        + proto.s(12, cascade)
        + proto.s(20, proto.s(1, traj) + proto.v(2, seq) + proto.s(4, cid))
        + proto.s(26, proto.s(1, proto.v(1, STATUS_DONE) + proto.ts(2, sec, nanos)))
    )


def _step(step_type: int, source: int, text: str, dt: datetime,
          cascade: str, traj: str, cid: str, seq: int,
          body: bytes | None = None) -> tuple[bytes, bytes]:
    meta = _meta_blob(dt, cascade, traj, cid, seq)
    # StepMeta with .3 source spliced in, matching field order seen on disk
    sec, nanos = int(dt.timestamp()), dt.microsecond * 1000
    meta = (proto.ts(1, sec, nanos) + proto.v(3, source)
            + proto.s(12, cascade)
            + proto.s(20, proto.s(1, traj) + proto.v(2, seq) + proto.s(4, cid))
            + proto.s(26, proto.s(1, proto.v(1, STATUS_DONE) + proto.ts(2, sec, nanos))))

    if body is None:
        if step_type == STEP_USER:
            body = proto.s(19, proto.s(2, text) + proto.s(3, proto.s(1, text))
                           + proto.s(4, ""))
        else:
            body = proto.s(20, proto.s(1, text) + proto.s(8, text) + proto.v(12, 2))

    payload = proto.v(1, step_type) + proto.v(4, STATUS_DONE) + proto.s(5, meta) + body
    return payload, meta


def _init_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`));
CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE INDEX `idx_steps_status` ON `steps`(`status`);
CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`);
CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT "main",`data` blob,PRIMARY KEY (`id`));
CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
""")
    return con


def _uri(path: str) -> str:
    """CLI-style file URI: `file:///C:/Users/.../Session%20Sync`.

    The drive letter keeps its case and the colon stays literal; only spaces and
    friends are escaped. (`conversation_summaries.workspace_uris` uses a
    different, percent-encoded `c%3A` form -- that is the IDE's, not the CLI's.)
    """
    p = path.replace("\\", "/")
    return "file:///" + urllib.parse.quote(p.lstrip("/"), safe="/:")


def _checkpoint_text(title: str, prompts: list[str], log_path: Path) -> str:
    """Antigravity's own context-summary step, reproduced.

    Every conversation gets one, even single-turn ones -- it is not only emitted
    on real truncation. It is also the natural place to explain a ported
    session, since it exists precisely to hand prior context to the model.
    """
    numbered = "\n".join(f"{i}. {p}" for i, p in enumerate(prompts, 1))
    return (
        "{{ CHECKPOINT 0 }}\n **The earlier parts of this conversation have been "
        "truncated due to its long length. The following content summarizes the "
        "truncated context so that you may continue your work. **\n\n\n"
        f"# USER Objective:\n{title}\n\n"
        f"# User Requests\nThe following were user requests from the truncated "
        f"conversation in chronological order:\n{numbered}\n\n"
        "# Conversation Logs\n\nReference the following log files for the full, "
        f"untruncated conversation:\n\n- {log_path}\n\n"
        "**IMPORTANT: this summary is just for your reference. You may respond to "
        "my previous and future messages, but DO NOT ACKNOWLEDGE THIS CHECKPOINT "
        "MESSAGE. JUST READ IT BUT DO NOT MENTION IT, RESPOND TO IT, OR TAKE "
        "ACTION BECAUSE OF IT.**"
    )


def exists(session_id: str) -> bool:
    return (CONV / f"{session_id}.db").exists()


def write(sess: Session, session_id: str | None = None,
          title: str | None = None) -> str:
    cid = session_id or str(uuid.uuid4())
    # Replacing: clear the old rows in place. Unlinking the .db is not enough --
    # Antigravity keeps conversations open in WAL mode, and a leftover -wal file
    # resurrects the old rows (UNIQUE failures on trajectory_metadata_blob).
    replacing = bool(session_id) and (CONV / f"{cid}.db").exists()
    traj = str(uuid.uuid4())
    meta_traj = str(uuid.uuid4())     # blob field 3 is its own id, not traj
    cascade = str(uuid.uuid4())
    cwd = sess.cwd or str(Path.home())
    CONV.mkdir(parents=True, exist_ok=True)
    logs = _logs_dir(cid)
    logs.mkdir(parents=True, exist_ok=True)
    (BRAIN / cid / "scratch").mkdir(parents=True, exist_ok=True)
    (BRAIN / cid / ".user_uploaded").mkdir(parents=True, exist_ok=True)

    tag = f"[From {TOOL_LABELS.get(sess.tool, sess.tool)}]"
    title = title or sess.ported_title()
    if replacing:
        con = sqlite3.connect(CONV / f"{cid}.db", timeout=30)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            con.execute(f'DELETE FROM "{t}"')
        if "trajectory_metadata_blob" not in tables:    # foreign schema: rebuild
            con.close()
            (CONV / f"{cid}.db").unlink()
            con = _init_db(CONV / f"{cid}.db")
    else:
        con = _init_db(CONV / f"{cid}.db")
    transcript: list[dict] = []
    idx = 0

    def add(stype: int, source: int, ttype: str, tsource: str,
            proto_text: str, wire: str, dt: datetime, body: bytes | None = None):
        nonlocal idx
        payload, meta = _step(stype, source, proto_text, dt, cascade, traj,
                              cid, idx, body=body)
        con.execute(
            "INSERT INTO steps (idx, step_type, status, has_subtrajectory,"
            " metadata, step_payload, step_format) VALUES (?,?,?,0,?,?,0)",
            (idx, stype, STATUS_DONE, meta, payload))
        transcript.append({
            "step_index": idx, "source": tsource, "type": ttype, "status": "DONE",
            "created_at": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "content": wire,
        })
        idx += 1

    # Provenance is its own leading turn so real turns keep their authorship.
    turns = [("user", sess.provenance_header(), sess.messages[0].ts)]
    # Same as Kiro: no native tool record, so cycles become a note.
    turns += [(m.role, m.text, m.ts) for m in flatten_for_text(sess.messages)]
    prompts = [t for r, t, _ in turns if r == "user"]
    first_ts = turns[0][2]

    for n, (role, text, mts) in enumerate(turns):
        if role == "user":
            # The protobuf carries raw text; only the transcript wraps user
            # turns in <USER_REQUEST> (verified against a real conversation).
            add(STEP_USER, SRC_USER, "USER_INPUT", "USER_EXPLICIT", text,
                f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n", mts)
            if n == 0:
                ck = _checkpoint_text(title, prompts, logs / "transcript.jsonl")
                body = proto.s(30, (
                    proto.s(4, title) + proto.v(9, 1) + proto.v(12, 1)
                    + proto.s(15, _uri(str(BRAIN / cid)))
                    + proto.s(19, prompts[0] if prompts else title)))
                add(STEP_CHECKPOINT, SRC_SYSTEM, "CHECKPOINT", "SYSTEM",
                    "", ck, mts, body=body)
        else:
            add(STEP_PLANNER, SRC_MODEL, "PLANNER_RESPONSE", "MODEL",
                text, text, mts)

    con.execute("INSERT INTO trajectory_meta VALUES (?,?,?,?)", (traj, cid, 4, 17))
    blob = (proto.s(1, proto.s(1, _uri(cwd)) + proto.s(3, ""))
            + proto.ts(2, int(first_ts.timestamp()), first_ts.microsecond * 1000)
            + proto.s(3, meta_traj)
            + proto.s(6, cid)                 # field 6, not 4
            + proto.s(7, _uri(cwd))
            + proto.s(18, "default-cli-project"))
    con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,))
    con.commit()
    con.close()

    body_txt = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in transcript)
    for name in ("transcript.jsonl", "transcript_full.jsonl"):
        (logs / name).write_text(body_txt, encoding="utf-8", newline="\n")
    # Antigravity keeps identical chunked copies alongside the flat logs.
    for name in ("transcript", "transcript_full"):
        d = logs / "chunks" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "00000000.jsonl").write_text(body_txt, encoding="utf-8", newline="\n")

    _append_history(cid, sess, cwd, tag)
    return cid


def _append_history(cid: str, sess: Session, cwd: str, tag: str) -> None:
    """history.jsonl is the CLI's per-workspace prompt index."""
    path = ROOT / "history.jsonl"
    if not path.parent.exists():
        return
    rows, tagged = [], False
    for m in sess.messages:
        if m.role != "user":
            continue
        display = " ".join(m.text.split())[:200]
        if not tagged:
            display = f"{tag} {display}"
            tagged = True
        rows.append({
            "display": display,
            "timestamp": int(m.ts.timestamp() * 1000),
            "workspace": cwd,
            "conversationId": cid,
        })
    if not rows:
        return
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _drop_rows(path: Path, key: str, value: str) -> None:
    """Remove every JSONL row whose `key` is `value`, leaving the rest as is."""
    if not path.exists():
        return
    kept = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines(True):
        try:
            if json.loads(line).get(key) == value:
                continue
        except (json.JSONDecodeError, AttributeError):
            pass
        kept.append(line)
    path.write_text("".join(kept), encoding="utf-8", newline="\n")


def delete(session_id: str) -> None:
    """Remove a conversation: its database, its brain dir, its history rows."""
    db = CONV / f"{session_id}.db"
    if not db.exists():
        raise FileNotFoundError(f"antigravity session {session_id} not found")
    # WAL side files would resurrect the rows if left behind.
    for suf in ("", "-wal", "-shm"):
        Path(f"{db}{suf}").unlink(missing_ok=True)
    shutil.rmtree(BRAIN / session_id, ignore_errors=True)
    _drop_rows(ROOT / "history.jsonl", "conversationId", session_id)


def resume_hint(session_id: str, cwd: str) -> str:
    return f'cd "{cwd}" && agy --conversation={session_id}'
