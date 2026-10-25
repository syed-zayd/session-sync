# sessync

[![tests](https://github.com/syed-zayd/session-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/syed-zayd/session-sync/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

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

You can use Session Sync manually or set up automatic background sync.

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

If you ever want to preview or remove the mirror sessions created by Session Sync:

```sh
# Preview what would be removed
sessync cleanup

# Delete mirrored sessions created by sessync across all agents
sessync cleanup --apply
```

---

## More Info

For deep-dive documentation on session formats, storage layouts, and agent internals, see [docs/internals.md](docs/internals.md).

## Development

```sh
git clone https://github.com/syed-zayd/session-sync
cd session-sync
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE)
