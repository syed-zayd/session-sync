"""Canonical session model shared by every adapter.

Fidelity: verbatim user and assistant text, plus tool calls with their arguments
and output. Reasoning and thinking blocks are dropped -- they are signed or
encrypted and genuinely non-transferable -- along with token accounting,
file-history checkpoints and UI state.

Tool calls are carried structurally, not as prose. Agents that can represent
them (Claude Code, Codex) get real `tool_use`/`function_call` records; agents
that cannot get a `[used tools: ...]` note rendered into the text. That note is
a *serialisation*, not a lossy summary: readers parse it back into `tools`, so a
session that round-trips through a text-only agent still hashes the same as its
source.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


TOOL_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "kiro": "Kiro",
    "antigravity": "Antigravity",
}

# Canonical tool vocabulary. Each agent names the same operations differently;
# mapping through a neutral middle keeps an N-th agent from needing N mappings.
# Unknown names pass through untouched rather than being forced into a bucket.
CANONICAL_TOOLS = {
    "bash": "shell", "shell": "shell", "exec": "shell",
    "exec_command": "shell", "shell_command": "shell", "run_command": "shell",
    "read": "read_file", "read_file": "read_file", "view_file": "read_file",
    "write": "write_file", "write_file": "write_file", "create_file": "write_file",
    "edit": "edit_file", "apply_patch": "edit_file", "patch_file": "edit_file",
    "str_replace": "edit_file",
    "glob": "list_dir", "list_directory": "list_dir", "list_dir": "list_dir",
    "grep": "search", "search_files": "search", "search": "search",
    "todowrite": "plan", "update_plan": "plan", "manage_task": "plan",
    "askuserquestion": "ask_user", "request_user_input": "ask_user",
    "ask_question": "ask_user",
    "websearch": "web_search", "web_search": "web_search",
    "webfetch": "web_fetch", "web_fetch": "web_fetch",
    # antigravity
    "find_by_name": "list_dir", "grep_search": "search",
    "search_in_file": "search", "replace_file_content": "edit_file",
    "read_url_content": "web_fetch", "search_web": "web_search",
    "invoke_subagent": "subagent", "send_message": "message",
    # kiro
    "fs_read": "read_file", "fs_write": "write_file",
    "execute_bash": "shell", "use_aws": "aws",
    # other claude code tools
    "powershell": "shell", "notebookedit": "edit_file",
    "task": "subagent", "agent": "subagent", "skill": "skill",
    "toolsearch": "tool_search", "schedulewakeup": "schedule",
    "sendmessage": "message", "senduserfile": "send_file",
}

# MCP tools are `mcp__<server>__<tool>`. The server is part of the identity --
# two servers may both expose `search` -- so they keep their own namespace
# rather than being flattened into the vocabulary above.
MCP_PREFIX = "mcp__"

# How each agent spells the canonical names when writing.
TOOL_NAMES_BY_AGENT = {
    "claude": {"shell": "Bash", "read_file": "Read", "write_file": "Write",
               "edit_file": "Edit", "list_dir": "Glob", "search": "Grep",
               "plan": "TodoWrite", "ask_user": "AskUserQuestion",
               "web_search": "WebSearch", "web_fetch": "WebFetch"},
    "codex": {"shell": "shell", "read_file": "read_file",
              "write_file": "write_file", "edit_file": "apply_patch",
              "list_dir": "list_directory", "search": "search_files",
              "plan": "update_plan", "ask_user": "request_user_input",
              "web_search": "web_search", "web_fetch": "web_fetch"},
}


def canonical_tool(name: str) -> str:
    raw = (name or "").strip()
    if raw.startswith(MCP_PREFIX):
        return raw          # server-qualified already; do not collapse
    return CANONICAL_TOOLS.get(raw.lower(), raw or "tool")


def agent_tool(name: str, agent: str) -> str:
    return TOOL_NAMES_BY_AGENT.get(agent, {}).get(name, name)


# Tool payloads dwarf the conversation: on a real session here, arguments and
# output were 84% of the bytes -- a full-fidelity mirror came to ~250k tokens,
# past what the target can hold. Capping keeps the shape (what ran, what roughly
# came back) while leaving room for the conversation itself. Applied on read, so
# every agent agrees on the content and the hash stays stable.
MAX_TOOL_ARGS = 1000
MAX_TOOL_RESULT = 1500


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"... [{dropped} more characters]"


@dataclass
class ToolCall:
    """One tool invocation and what it returned."""
    name: str                  # canonical name
    args: str = ""             # JSON text, or raw input for free-form tools
    result: str = ""           # flattened output

    def __post_init__(self):
        self.args = clip(self.args, MAX_TOOL_ARGS)
        self.result = clip(self.result, MAX_TOOL_RESULT)


@dataclass
class Message:
    role: str                  # "user" | "assistant"
    text: str
    ts: datetime
    tools: list[ToolCall] = field(default_factory=list)

    def __post_init__(self):
        if self.ts.tzinfo is None:
            self.ts = self.ts.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- normalising

# IDE/harness context spliced *into* a real user message. The user's own words
# sit right after the closing tag. This lives here, not in one adapter: when
# only one side stripped it, sessions were written with the noise and read back
# without it, and their content hashes stopped matching.
_INLINE_NOISE = re.compile(
    "<ide_opened_file>.*?</ide_opened_file>"
    "|<ide_selection>.*?</ide_selection>"
    "|<environment_context>.*?</environment_context>"
    "|<system-reminder>.*?</system-reminder>",
    re.S)

_TOOL_NOTE = re.compile(r"\[used tools: ([^\]]*)\]")


def normalize_text(text: str) -> str:
    return _INLINE_NOISE.sub("", text or "").strip()


def extract_tool_note(text: str) -> tuple[str, list[str]]:
    """Pull a rendered `[used tools: ...]` note back out into names.

    Text-only agents carry tool activity this way, so reading one back has to
    reverse it -- otherwise a session that passed through Kiro would hash
    differently from the same session that did not.
    """
    names: list[str] = []
    for group in _TOOL_NOTE.findall(text or ""):
        names += [n.strip() for n in group.split(",") if n.strip()]
    return _TOOL_NOTE.sub("", text or "").strip(), names


def first_user_key(text: str) -> str:
    """Normalised opening user message, used to prefilter content matching.

    Must be derived the same way on both sides -- normalise, drop any tool
    note, collapse whitespace -- or a listing and a read disagree and a real
    mirror gets skipped.
    """
    body, _ = extract_tool_note(normalize_text(text))
    return " ".join(body.split())[:200]


def render_tool_note(tools: list[ToolCall]) -> str:
    if not tools:
        return ""
    names = sorted({t.name for t in tools if t.name})
    return f"[used tools: {', '.join(names)}]" if names else ""


def coalesce(messages: list[Message]) -> list[Message]:
    """Merge consecutive assistant records into single turns.

    One assistant turn is many records -- text, tool call, text, tool call --
    and emitting each separately produced runs of 432 consecutive assistant
    messages, most of them a bare tool note with no content.

    Consecutive *user* records are left alone. They are separate things the user
    said, and Codex's compaction window stacks 125 in a row; merging those would
    destroy the turn structure rather than restore it.
    """
    out: list[Message] = []
    for m in messages:
        text, noted = extract_tool_note(normalize_text(m.text))
        tools = list(m.tools)
        known = {t.name for t in tools}
        # Names recovered from a rendered note, with no structured call to
        # match, still count as tool activity.
        tools += [ToolCall(name=canonical_tool(n)) for n in noted
                  if canonical_tool(n) not in known]
        if not text and not tools:
            continue
        # Merge only into a *toolless* assistant turn. Once a turn has made a
        # tool call it is a closed request/response cycle -- the target
        # represents that natively as assistant(tool_use) -> user(tool_result)
        # -- and folding the next turn into it would misstate what happened.
        if out and out[-1].role == m.role == "assistant" and not out[-1].tools:
            prev = out[-1]
            merged = "\n\n".join(p for p in (prev.text, text) if p)
            out[-1] = Message(role=prev.role, text=merged, ts=prev.ts,
                              tools=tools)
        else:
            out.append(Message(role=m.role, text=text, ts=m.ts, tools=tools))
    return [m for m in out if m.text.strip() or m.tools]


def flatten_for_text(messages: list[Message]) -> list[Message]:
    """Render tool cycles into prose, for agents with no tool record.

    Kiro and Antigravity cannot store a tool call, so their mirrors carry a
    `[used tools: ...]` note instead. Messages are kept one-to-one on purpose:
    merging them here would change the message count, and a session that passed
    through one of these agents would stop hashing the same as its source --
    which is what idempotent pushing depends on. Readers parse the note back
    into `tools`, closing the round trip.
    """
    out: list[Message] = []
    for m in messages:
        note = render_tool_note(m.tools)
        text = "\n".join(p for p in (m.text, note) if p)
        if text.strip():
            out.append(Message(role=m.role, text=text, ts=m.ts))
    return out


@dataclass
class Session:
    tool: str
    session_id: str
    title: str
    cwd: str
    created_at: datetime
    updated_at: datetime
    messages: list[Message] = field(default_factory=list)

    @property
    def preview(self) -> str:
        for m in self.messages:
            if m.role == "user" and m.text.strip():
                return " ".join(m.text.split())[:80]
        return self.title or "(empty)"

    def content_hash(self) -> str:
        """Stable digest of the conversation.

        Over role, text and tool *names* only -- not tool arguments or output.
        Those survive the trip in different shapes depending on what the target
        can represent, and hashing them would make a faithful mirror look like a
        different conversation. Identity is what was said and what was used.
        """
        h = hashlib.sha256()
        for m in self.messages:
            h.update(f"{m.role}:{len(m.text)}:".encode())
            h.update(m.text.encode("utf-8"))
            names = ",".join(sorted(t.name for t in m.tools))
            h.update(f"|tools:{names}|".encode())
        return h.hexdigest()[:16]

    def provenance_header(self) -> str:
        """Leading synthetic user turn for agents with no title field.

        The tag used to be prepended to the first real user message, which made
        the target read that message as something the *other agent* said. Its
        own turn keeps every real turn's authorship intact.
        """
        label = TOOL_LABELS.get(self.tool, self.tool)
        return "\n".join([
            f"[From {label}] {self.preview or self.title}",
            "",
            f"This session was ported from {label}. Everything below already "
            f"happened: the user turns are mine, the assistant turns are the "
            f"previous agent's. Continue from where it left off.",
        ])

    def ported_title(self) -> str:
        """Title for the mirror, tagged with the origin.

        Prefers the first user message over any stored title: Codex and Kiro
        preview the first message natively and Claude Code stores a generated
        `ai-title`, so the raw prompt keeps mirrors recognisable everywhere.
        """
        label = TOOL_LABELS.get(self.tool, self.tool)
        base = (self.preview or self.title or "session").strip()
        tag = f"[From {label}]"
        return base if base.startswith(tag) else f"{tag} {base}"


@dataclass
class SessionRef:
    """Lightweight listing entry, cheap to produce without a full parse."""
    tool: str
    session_id: str
    title: str
    cwd: str
    updated_at: datetime
    path: str
    n_messages: int | None = None
    # First user message, normalised. Cheap to collect while listing and highly
    # selective, which makes it a sound prefilter for content matching --
    # unlike a message count, which cannot be reproduced without replaying the
    # same coalescing rules read() applies.
    first_user: str = ""


PROVENANCE_MARK = "This session was ported from "


def is_provenance_header(text: str) -> bool:
    """True for a header this tool injected on a previous port.

    Reading such a session back must not treat that turn as real input, or
    chained ports (A->B->C) would stack a header per hop.
    """
    t = (text or "").lstrip()
    return t.startswith("[From ") and PROVENANCE_MARK in t[:400]


def as_json(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def flatten_output(value) -> str:
    """Codex tool output is a list of typed blocks; Claude's may be either."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for b in value:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return value.get("text") or as_json(value)
    return "" if value is None else str(value)


def now() -> datetime:
    return datetime.now(timezone.utc)
