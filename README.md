# Session Sync

Move a coding-agent session from one CLI into another, so you can pick up where
you left off without rebuilding context.

Supports **Claude Code**, **Codex CLI**, **Kiro CLI** and **Antigravity CLI**, in
all 12 directions. Your existing sessions are never modified.

## Install

```sh
pip install -e .
```

This puts a `sessync` command on your PATH.

## Quick start

Copy a Claude Code session into Codex:

```sh
sessync push claude ad4ccd05-d797-495f-b5b9-ca6b81c51c0d codex
```

It prints what happened and the command to resume the new session:

```
claude/ad4ccd05-d797-495f-b5b9-ca6b81c51c0d  (96 messages)
  + codex        created   019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11
                 cd "C:\Users\you\project" && codex resume 019823f1-...
```

Copy it into every agent at once:

```sh
sessync push claude ad4ccd05-d797-495f-b5b9-ca6b81c51c0d all
```

## Agent names

| Name | Aliases |
|---|---|
| `claude` | `cc`, `claude-code` |
| `codex` | `cx`, `openai` |
| `kiro` | `k`, `kiro-cli` |
| `antigravity` | `ag`, `antigravity-cli`, `gemini` |

`all` is accepted wherever an agent name is.

## Finding a session id

Use the id the source agent itself shows you:

| Agent | Where to get the id |
|---|---|
| Claude Code | the filename in `~/.claude/projects/<project>/<id>.jsonl` |
| Codex | `codex resume` picker, or the uuid in the rollout filename |
| Kiro | `kiro-cli chat --list-sessions` |
| Antigravity | the `/resume` picker inside `agy` |

## Commands

### `sessync push <source_agent> [source_id] <target_agent|all>`

Copies one session into another agent.

* `all` as the target means every agent that has `auto_pull` on. Naming a target
  explicitly always works, regardless of its `auto_pull` setting.
* Pushing the same session twice **updates the existing mirror in place**,
  keeping its id and its spot in the agent's resume list. It does not create a
  duplicate.
* If the mirror has been continued in its own tool since the last push, the push
  is refused rather than clobbering your work.

| Flag | What it does |
|---|---|
| `--force` | overwrite a mirror that was continued in its own tool |
| `--from-hook` | read the session id from a Stop hook payload on stdin instead of taking it as an argument |
| `--quiet` | print nothing and always exit 0, so a failure can't disrupt the agent that ran the hook |

`--from-hook` and `--quiet` exist for the hooks below; you don't need them by hand.

### `sessync auto_push add|remove|list [agent]`

Controls which agents push their finished sessions out automatically, by
installing a Stop hook in that agent's own config.

```sh
sessync auto_push add claude     # claude pushes every session it finishes
sessync auto_push list
sessync auto_push remove all
```

`list` reads the agents' config files directly, so it always reflects reality:

```
agent         auto_push  hook location
claude        true       ~\.claude\settings.json
codex         false      ~\.codex\hooks.json
kiro          false      <project>\.kiro\hooks\sessync.json  (this project only)
antigravity   false      ~\.gemini\config\hooks.json
```

Two things to know:

* **Kiro's hooks are per project directory.** `auto_push add kiro` only affects
  the directory you run it in.
* Edits are surgical — exactly one entry is added or removed, and a
  `.sessync-bak` copy is written before the first change to an existing file.

The installed hook runs `sessync push <agent> all --from-hook --quiet`.

### `sessync auto_pull add|remove|list [agent]`

Controls which agents *accept* pushed sessions — that is, which ones
`push <src> all` writes into. All agents accept by default.

```sh
sessync auto_pull remove kiro    # stop mirroring into kiro
sessync auto_pull list
```

This is a setting, stored in `~/.sessync/config.json`.

### Turning on full automatic mirroring

```sh
sessync auto_push add all
sessync auto_pull add all
```

Every agent then mirrors each finished session into all the others. There's no
feedback loop: a mirror that's already up to date resolves to `unchanged`, so
nothing propagates further.

## Resuming a mirror

`push` prints the exact command, but for reference:

```sh
claude --resume <id>
codex resume <id>
kiro-cli --resume-id <id>
agy --conversation=<id>
```

Run it from the same directory the original session was in — Claude Code, Codex
and Kiro all scope their session lists by directory. (Antigravity doesn't, so
its mirrors show up in `/resume` from anywhere.)

## What carries over

* Your messages and the assistant's replies, verbatim.
* Tool calls with their arguments and output. Kiro and Antigravity have no tool
  record to write into, so their mirrors carry a `[used tools: ...]` note
  instead.
* Long tool arguments and output are truncated, with a marker saying how much
  was dropped. A full-fidelity mirror of a real session came to ~250k tokens,
  more than the target can hold.

What doesn't:

* Reasoning and thinking blocks — they're signed or encrypted and genuinely
  can't be transferred.
* Token accounting, file-history checkpoints and UI state.
* History the source agent already discarded when it compacted. You get the
  conversation the source actually has, not the one its file still contains.

This is context transfer, not a faithful replay. The target agent will know what
happened and what was run; it won't reproduce a byte-identical transcript.

## Files sessync writes

| Path | What |
|---|---|
| `~/.sessync/config.json` | `auto_pull` settings |
| `~/.sessync/lineage.json` | which session in tool A corresponds to which in tool B |
| `~/.sessync/notify.log` | what every push did — start here when a hook seems to have done nothing |

Deleting `lineage.json` is safe: mirrors are re-found by matching conversation
content.

Beyond those, sessync only writes into the target agent's own session store, and
never adds an identifier of its own to anyone's session files.

## Troubleshooting

**A hook fired but nothing was mirrored.** Check `~/.sessync/notify.log`. Hooks
run with `--quiet`, so failures are logged rather than printed.

**"refused: mirror was continued in its own tool".** The target session has
moved on since the last push. Use `--force` if you want to overwrite it.

**`push` reports `unchanged`.** The mirror is already current. That's a success.

**A ported session doesn't appear in the picker.** Make sure you're in the
directory the original session ran in.

## Internals

See [CLAUDE.md](CLAUDE.md) for session formats, storage layout, and the
per-agent details behind all of this.
