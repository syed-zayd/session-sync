# How sessync works

This is the design behind `sessync`. For using it, see the [README](../README.md).

## The pipeline

Every push goes through the same three steps:

```
source agent ──read()──▶ canonical Session ──write()──▶ target agent
```

Each agent has an adapter in `sessync/adapters/` that turns its own session format into a
shared canonical model (`sessync/canonical.py`) and back. Adding a fifth agent means writing
one adapter. It never means writing a converter for every other agent.

A canonical `Session` is a list of `Message`s. Each message has a role (`user` or
`assistant`), text, a timestamp, and any `ToolCall`s it made: a canonical tool name, the
arguments and the output.

What gets dropped on purpose:

- **Reasoning and thinking blocks.** They are signed or encrypted, so they can't be
  transferred.
- **Token accounting, checkpoints and UI state.**
- **Harness-injected context**, such as `<environment_context>`, `<system-reminder>` and IDE
  selections. It's stripped on read in one shared place. When only some adapters stripped
  it, the same conversation hashed differently depending on where it was read.
- **History the source already compacted away.** Claude Code and Codex both keep discarded
  turns in the file after a compaction. Reading starts at the last compaction boundary, so
  the target gets the conversation the source actually has.

### Tool calls

Tool names are mapped through a neutral vocabulary (`shell`, `read_file`, `edit_file`, and so
on), so `Bash` in Claude Code becomes `shell` in Codex. MCP tools keep their full
`mcp__server__tool` name, because the server is part of the tool's identity.

Claude Code and Codex store real tool records, so calls are written natively there. Kiro and
Antigravity have nowhere to store a tool call, so their mirrors carry a `[used tools: ...]`
note instead. Readers parse that note back into tool calls, so a session that passed
through Kiro still hashes the same as one that didn't.

Long tool arguments and output are truncated on read. Before truncation, a real session came
to about 250k tokens, which is more than a target agent can hold.

## Identity: never make a second copy

The core promise is one mirror per conversation per agent. sessync looks for an existing
mirror in two ways.

1. **The lineage registry** (`~/.sessync/lineage.json`). It records which session in each
   agent is the same conversation, which agent the conversation started in, and the content
   hash last written to each mirror. It's a fast path, not the source of truth. The registry
   is saved after each target, so a hook that gets killed partway through still records the
   mirrors it wrote.
2. **A content scan of the target agent.** Provenance headers are stripped on read, so a
   mirror reads back as the same canonical messages as its source. A scan therefore finds
   mirrors the registry has no record of. A copy counts as the same conversation if its
   opening exchange matches word for word. The scan then classifies it:
   - `same`: identical. Nothing to do.
   - `behind`: a prefix of the source. The source moved on, so the copy is updated in
     place. Nothing is lost, because everything in the copy is also in the source.
   - `diverged`: the copy was continued somewhere else. It's skipped unless you pass
     `--force`.

**Overwrite guard.** sessync refuses to overwrite a mirror that holds turns the source
doesn't, which means you continued it in its own tool. The registry's stored hash is what
reveals this.

**Locking.** Pushes are serialised by a lock file in `~/.sessync`. A push never proceeds
without the lock. Two unlocked pushes would each miss the other's mirror and both create
one.

**Echo suppression** needs no special code. With every agent pushing to every other, the
receiving agent's own hook fires after a mirror is written. The mirror hashes identically
to its source, so that push resolves to `unchanged`.

**Scripted sessions are not pushed by hooks.** Codex fires its Stop hook for `codex exec`
runs and subagent threads too. Mirroring a batch of those floods every other agent's picker.

## Titles and provenance

Mirrors are tagged with the agent the conversation **started** in, for example
`[From Claude]`, not whichever agent pushed last. The original session is never retagged.

Agents with a title field (Claude Code, Kiro) get the tag in the title. Codex and
Antigravity show a preview of the first message instead, so their mirrors open with a short
synthetic user turn: "This session was ported from …". Readers drop that turn, so it never
stacks across hops.

## Per-agent storage

| Agent | Where sessions live | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/<encoded-cwd>/<id>.jsonl` | The JSONL is authoritative; a well-formed file is enough for `claude --resume`. Prompts are also added to `~/.claude/history.jsonl` for up-arrow recall. |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` | The picker lists from the `threads` table in `~/.codex/state_5.sqlite`, not the files, so mirrors are registered there too. Ids must be UUIDv7 and `history_mode` must be `paginated`, or the session resumes by id but never shows in `codex resume`. |
| Kiro | `~/.kiro/sessions/cli/<id>.{jsonl,json,history,lock}` | Non-interactive runs write to a second store, a SQLite table under `%LOCALAPPDATA%\Kiro-Cli`, which is also read. The sidecar's `session_created_reason` must be a variant Kiro's deserialiser knows, or the session is silently skipped. |
| Antigravity | `~/.gemini/antigravity-cli/conversations/<id>.db` | Protobuf-in-SQLite with no published schema; steps are written in the wire format seen in real conversations. A readable transcript is kept under `brain/<id>/`. |

Claude Code, Codex and Kiro filter their resume pickers by working directory, so a mirror
only shows up when you resume from the directory the original ran in. Antigravity's picker
doesn't filter.

## Hooks

`sessync auto_push add <agent>` installs a Stop hook in that agent's own config. Hooks run
`sessync push <agent> all --from-hook --quiet` under three constraints:

- **Nothing goes to stdout.** Codex treats a hook's stdout as instructions to the agent.
- **The exit code is always 0.** A failing hook degrades the agent it's attached to.
- **The session id is read from the hook payload, never inferred.** Guessing "the newest
  session" breaks as soon as two sessions are open at once.

Failures go to `~/.sessync/notify.log` instead.
