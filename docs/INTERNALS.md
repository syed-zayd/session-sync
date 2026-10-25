# Session Sync — internals

User-facing instructions live in [README.md](../README.md). This file is the
accumulated knowledge about how each agent stores sessions and why the code is
shaped the way it is. Most of it was found by diffing real sessions; almost none
of it is documented upstream.

## Layout

```
sessync/
  canonical.py            Session / Message model
  proto.py                minimal protobuf wire codec (Antigravity)
  registry.py             lineage.json  (~/.sessync)
  config.py               auto_pull settings
  hooks.py                auto_push: writes each agent's own hook config
  runtime.py              logging, lock, session id out of a Stop payload
  sync.py                 fanout(): the operation a watcher would call
  cli.py                  argparse entry point
  adapters/
    claude_code.py        read + write
    codex.py              read + write
    kiro.py               read + write
    antigravity.py        read + write
```

Each adapter exposes `list_sessions()`, `read(id)`, `write(session)` and
`resume_hint(id, cwd)`. Adding a tool means one module and one entry in
`adapters/__init__.py`.

## Design rules

* **auto_push is not a setting.** Whether an agent pushes *is* whether our Stop
  hook is present in that agent's own config, so `auto_push list` reads the
  agents' files and cannot disagree with reality. Only `auto_pull` lives in
  `~/.sessync/config.json`.
* **Never write to stdout from a hook.** Codex parses a hook's stdout as a JSON
  response; anything printed there becomes instructions to the agent.
* **Never exit non-zero from a hook.** It degrades the agent it is attached to.
  Hence `--quiet`.
* **Read the session id, never infer it.** Resolving "the newest session here"
  was tried and removed: it makes one command mean different things depending on
  timing, and breaks outright with two sessions of one agent open at once. No
  id means no push.
* **Never write an identifier of our own into anyone's session files.** Every
  tool already has ids; the registry just relates them. Each of these formats
  has a strict deserialiser somewhere — Kiro drops a session silently over a
  single bad enum value.
* **Echo suppression needs no special handling.** A mirror we just wrote hashes
  identically to its source, so when the receiving agent's own hook fires the
  push resolves to `unchanged` and stops.

## How a session is recognised across tools

Two layers, in this order:

1. **The lineage registry** (`~/.sessync/lineage.json`), holding each tool's own
   native session id. Fast path only.
2. **A content-hash scan of the target.** The real source of truth.

The second layer exists because the registry cannot be authoritative: another
migration tool, a manual copy, or a deleted registry file all leave a mirror we
have no record of. Matching on conversation content finds it regardless of who
created it. It works only because provenance headers are stripped on read, so a
ported session reads back to the same canonical messages — and therefore the
same hash — in every tool.

`tests/test_idempotent.py` covers create, no-op re-port, update-in-place, the
refuse-to-clobber guard, `--force`, and recovery after the registry is deleted.

## Hook payloads

`--from-hook` takes the session id from the Stop payload the agent pipes in on
stdin.

Codex truncates that payload on Windows when the assistant message contains
non-ASCII (openai/codex#23784), so a plain `json.loads` is not enough. The id
sits near the front and is a well-formed uuid, so a regex over the raw text
recovers it from a payload that never finishes — still reading what the agent
told us, not guessing. Several key spellings are accepted (`session_id`,
`conversation_id`, `thread_id`, camelCase variants), since Kiro's payload is
undocumented.

Hook config shapes, one per agent:

```
claude       ~/.claude/settings.json        hooks.Stop[] -> {matcher, hooks[]}
codex        ~/.codex/hooks.json            hooks.Stop[] -> {hooks[]}
antigravity  ~/.gemini/config/hooks.json    {<name>: {Stop: [...]}}
kiro         .kiro/hooks/sessync.json       own file, and PROJECT-scoped
```

Entries are identified by their command string. Installing then removing leaves
the rest of your hooks semantically identical — verified against a live
`settings.json` carrying unrelated hooks; only JSON formatting changes.

`hooks.executable()` bakes in the resolved absolute path to `sessync`, because
hooks are spawned by the agent and do not necessarily inherit the PATH that
makes a bare `sessync` resolve — on Windows pip puts the shim in Python's
Scripts dir, often absent from the user PATH.

## Message model

### Tool calls are structural, not prose

Claude Code and Codex store real tool records, so mirrors get real ones back:
`tool_use`/`tool_result` for Claude, `function_call`/`function_call_output`
paired by `call_id` for Codex. Names are written in each agent's own vocabulary
(`Bash` on one side, `shell` on the other) via a neutral canonical middle, so an
N-th agent needs one mapping rather than N.

Kiro and Antigravity have no tool record we can write. Their mirrors carry a
`[used tools: ...]` note — but that note is a *serialisation*, not a summary:
readers parse it straight back into tool calls. A session that passes through a
text-only agent still hashes the same as its source, which is what idempotent
pushing depends on.

MCP tools keep their `mcp__<server>__<tool>` name. The server is part of the
identity, since two servers may each expose a `search`.

### Turn boundaries

A turn that has made a tool call is a closed request/response cycle, so nothing
is merged into it. Two bugs lived here, both found on real sessions:

* A run of tool-only turns folded into a single message — **71 collapsed into
  one**. A call that follows an *output* starts a new turn.
* A multi-tool turn split into several, because the writer interleaved
  `call, output, call, output`. Codex issues all calls of a turn first, then
  their outputs; writing it that way keeps the turn intact.

### Turns, not records

One assistant turn is many records (text, tool call, text, tool call). Emitting
each separately produced runs of 432 consecutive assistant messages, most of
them a bare `[used tools: Bash]`. Consecutive **assistant** records are merged
into one turn with their tool notes pooled.

Consecutive **user** records are left alone. They are separate things the user
said, and Codex's compaction window stacks 125 in a row — merging those would
destroy turn structure rather than restore it.

| session | before | after |
|---|---|---|
| 2079-record Claude | 2079 msgs, 53 user / 2026 assistant | 96 msgs, 48 / 48 |
| 4055-record Codex | 4055 msgs, 193 / 3862 | 310 msgs, 155 / 155 |

### Tool payloads are capped

Arguments and output were **84% of the bytes** on a real session; a
full-fidelity mirror came to ~250k tokens, past what the target can hold. Both
are clipped (1000 and 1500 characters) with a marker saying how much was
dropped. Applied on read, so every agent agrees on the content and the hash
stays stable.

### Compaction is honoured, not replayed

Both Claude Code and Codex discard history when they compact but leave every
discarded record in the file. Reading the file whole hands the target a
conversation the source no longer has — one session measured here kept ~1M
tokens of history behind its boundary.

* **Claude Code** writes a `system` record with `subtype: compact_boundary`,
  then a `user` record flagged `isCompactSummary` holding the summary. We start
  at the last boundary, and recover the title from the discarded region so it is
  not lost with the history.
* **Codex** writes a `compacted` record whose `replacement_history` *is* the new
  history. We reset to it. That window keeps the user's turns verbatim and
  replaces the assistant side — one here holds 125 user messages and no
  assistant ones.

### Inline harness noise

Agents splice context blocks *into* real user messages — `<ide_opened_file>`,
`<ide_selection>`, `<system-reminder>`. These cannot be dropped wholesale: the
user's own words sit right after the closing tag. They are cut out and the
message kept. This lives in `canonical.py`, not in one adapter: when only one
side stripped them, sessions were written with the noise and read back without
it, and the content hashes stopped matching.

## Storage, per tool

| Tool | Location | Authoritative store |
|---|---|---|
| Claude Code | `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` | the JSONL |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` | the JSONL |
| Kiro | `~/.kiro/sessions/cli/<uuid>.{jsonl,json,history,lock}` | `.json` sidecar |
| Antigravity | `~/.gemini/antigravity-cli/` | `conversations/<id>.db` |

### Kiro

**Kiro has two stores, not one.** A real interactive terminal writes files under
`~/.kiro/sessions/cli` (listed as "v2"); anything without a tty —
`--no-interactive`, or a piped prompt — writes the `conversations_v2` table in
`%LOCALAPPDATA%\Kiro-Cli\data.sqlite3` instead (listed as "classic"). That table
read as dead legacy while it was empty, and sessions written there were
invisible to sessync. Both are read now; mirrors are still written as files,
which is what an interactive Kiro produces and therefore what you resume into.

That classic blob is exactly what Kiro's own `/chat save` writes — confirmed by
exporting a live session and decoding it with the same code. Kiro also has
`/chat load`, and `/chat save-via-script` / `/chat load-via-script`, which pipe
session JSON to and from a script. An official integration point worth knowing
about, though they are slash commands and so cannot be driven from a hook.

**Kiro's sidecar is strict.** `rts_model_state` and `permissions` are
non-nullable Rust structs and `session_created_reason` is an enum — a hand-built
sidecar with nulls or an unknown variant is skipped silently, with no error. The
writer clones the shape from an existing session.

### Codex

* **`history_mode`** only accepts `legacy` or `paginated`, and the choice decides
  visibility. A `legacy` rollout resumes by id but never appears in the
  `codex resume` picker. Paginated means writing `event_msg`/`item_completed`
  records alongside the `response_item` stream — those are what the picker
  renders. `codex migrate-rollouts` reports the count of "already paginated"
  sessions, a cheap way to confirm you got it right.
* **Codex ids are UUIDv7**, not v4. Match the version; anything ordering threads
  by the timestamp embedded in the id will mishandle a v4.
* **`codex resume` lists from a SQLite index, not from the rollout files.** The
  `threads` table in `~/.codex/state_5.sqlite` is the picker's source of truth.
  Codex indexes sessions it creates itself, so a rollout written from outside is
  resumable by id yet absent from the picker until it has a row there. This was
  the single hardest thing to find, because every file-level check passed.
  Two traps in that table: the stored `cwd` carries the Windows extended-length
  prefix (`\?\C:\...`), so a row written with a plain path is invisible under the
  default cwd filter; and `sandbox_policy` is a structured blob best cloned from
  an existing row rather than invented. It is a derived index Codex can rebuild
  from rollout files, so writing to it is safe and reversible.
* **The VS Code extension** lists from `local_thread_catalog` in
  `~/.codex/sqlite/codex-dev.db`, a separate index from the CLI picker. We do
  not write it: sessions the real CLI creates have no row there either.

### Antigravity

Protobuf-in-SQLite with no published schema. `steps.step_payload` is synthesised
on the wire format observed in real conversations:

```
1  varint  step_type   14=USER_INPUT 15=PLANNER_RESPONSE 23=CHECKPOINT 132=GENERIC
4  varint  status      3=DONE
5  msg     StepMeta    .1 created_at  .3 source (4=user, 2=model)
                       .12 cascade id  .20{.1 trajectory .4 conversation}
19 msg     UserInput       .2 text
20 msg     PlannerResponse .1 text  .8 text
```

The `<USER_REQUEST>` wrapper belongs to the transcript projection, not the
protobuf. The `metadata` column repeats StepMeta standalone.

Details that cost time, all found by diffing a one-turn real conversation:

* `agy`'s picker is the in-session `/resume` command, not a CLI flag, which is
  why it does not show up in `agy --help`. Unlike Codex it needs no index — it
  enumerates `conversations/*.db` directly, and unlike all three others it does
  **not** filter by cwd, so a ported conversation is visible from any directory.
* `trajectory_metadata_blob` keys the conversation on **field 6**, not 4, and
  also carries field 7 (workspace URI) and field 18 (`default-cli-project`).
* Every conversation gets a **CHECKPOINT step** (type 23, source 5), even a
  single-turn one — it is not only emitted on real truncation. Its field 30.4 is
  the conversation title the picker renders (confirmed: ported titles show
  there). Its body is a context summary, which makes it the natural carrier for
  provenance. Antigravity normally writes an LLM-generated title here
  ("Confirming Gemini AI Identity"), the way Claude Code does.
* CLI file URIs look like `file:///C:/Users/.../Session%20Sync` — literal drive
  colon. The percent-encoded `c%3A` form in
  `conversation_summaries.workspace_uris` is the **IDE's**, not the CLI's.
* Real CLI conversations have **no `conversation_summaries` row** at all; that
  table belongs to the IDE, so the writer no longer touches it.
* The flat `transcript*.jsonl` files are mirrored byte-for-byte under
  `logs/chunks/<name>/00000000.jsonl`.

## Verification status

| Target | Verified how |
|---|---|
| Codex | `codex migrate-rollouts` (read-only) parses all rollouts, 0 failed; resumed live with full context; catalog row lands with the right title |
| Kiro | `kiro-cli chat --list-sessions` lists them with correct titles and counts |
| Claude Code | record field-set diffed against a real session; no missing fields |
| Antigravity | ported conversations listed by the real `/resume` picker with correct titles and step counts |
| Kiro classic | decoded from a blob a live Kiro produced, kept as a fixture in `tests/test_kiro_classic.py` |

All four are confirmed listed and resumable by their own CLIs. Claude Code has
no headless listing command, so its checks were run by hand.

Each tool scopes its listing differently, which matters when testing: Claude
Code lists per project directory, Kiro and Codex filter by cwd, and Antigravity
does not filter at all.

## Not built yet

Mirroring on save. Sessions currently propagate on the Stop hook, i.e. when a
turn finishes. `sessync.sync.fanout` is the operation a file watcher would call,
but nothing watches yet.
