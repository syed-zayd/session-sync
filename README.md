# Session Sync

[![tests](https://github.com/syed-zayd/session-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/syed-zayd/session-sync/actions/workflows/tests.yml)

Move coding-agent sessions between CLIs so you can pick up where you left off in another tool without rebuilding context.

Supported agents: **Claude Code**, **Codex CLI**, **Kiro CLI**, and **Antigravity CLI** (in all 12 directions).

## Installation

Requires Python 3.10+.

```sh
pip install git+https://github.com/syed-zayd/session-sync
```

*(or `pip install -e .` from a local clone)*

---

## Quick Setup

You can use Session Sync manually or have sessions sync automatically in the background.

### Option 1: Manual Sync (End-to-End Example)

1. **Finish or exit your session.**  
   When working in Claude Code, run `/exit` or check your prompt history. Claude displays or stores the session ID:
   ```
   # Example session ID: ad4ccd05-d797-495f-b5b9-ca6b81c51c0d
   ```
   *(You can also see recent IDs in `~/.claude/projects/<project>/`)*

2. **Push the session to Codex:**
   ```sh
   sessync push claude ad4ccd05-d797-495f-b5b9-ca6b81c51c0d codex
   ```
   This outputs the exact resume command:
   ```
   claude/ad4ccd05-d797-495f-b5b9-ca6b81c51c0d (24 messages)
     + codex        created   019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11
                    cd "/path/to/project" && codex resume 019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11
   ```

3. **Resume in Codex:**
   ```sh
   codex resume 019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11
   ```

*(Tip: You can push to all agents at once with `sessync push claude <id> all`)*

---

### Option 2: Automatic Background Sync

Keep sessions automatically shared across your tools whenever a session turn finishes.

1. **Enable auto-push for Claude and Codex:**
   ```sh
   sessync auto_push add claude
   sessync auto_push add codex
   ```

2. **Check your configured hooks:**
   ```sh
   sessync auto_push list
   ```
   Output:
   ```
   agent         auto_push  hook location
   claude        true       ~/.claude/settings.json
   codex         true       ~/.codex/hooks.json
   kiro          false      .kiro/hooks/sessync.json
   antigravity   false      ~/.gemini/config/hooks.json
   ```

Now, every session completed in Claude Code or Codex is automatically mirrored to the other.

---

## Cleanup

If you ever want to delete the mirror sessions created by Session Sync and keep your session history clean:

```sh
# Deletes all mirrored sessions created by sessync across all agents
sessync cleanup
```

To remove all tracked sessions including original sources:
```sh
sessync cleanup --all
```

---

## More Info

For deep-dive documentation on session formats, storage layouts, and agent internals, see [docs/INTERNALS.md](docs/INTERNALS.md).

## License

[MIT](LICENSE)
