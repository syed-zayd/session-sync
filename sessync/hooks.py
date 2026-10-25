"""auto_push: install our Stop hook into each agent's own hook config.

There is no `auto_push` setting in our config file on purpose. Whether an agent
pushes *is* whether the hook is there, so `list` reads the agents' own files and
cannot disagree with reality.

Every agent has a Stop event, but no two agree on where it lives or what it
looks like:

  claude       ~/.claude/settings.json        hooks.Stop[] -> {matcher, hooks[]}
  codex        ~/.codex/hooks.json            hooks.Stop[] -> {hooks[]}
  antigravity  ~/.gemini/config/hooks.json    {<name>: {Stop: [...]}}
  kiro         .kiro/hooks/sessync.json       own file, and PROJECT-scoped

Kiro is the odd one: its hooks are per project directory, so `auto_push add
kiro` only affects the directory you run it in.

Edits are surgical -- we add or remove exactly one entry, identified by its
command string, and never touch anything else in the file. A backup is written
the first time we modify a file that already existed.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .adapters import ADAPTERS

MARKER = "push {agent} all --from-hook"


def _marker(agent: str) -> str:
    return MARKER.format(agent=agent)


def executable() -> str:
    """Absolute path to our own entry point.

    Hooks are spawned by the agent, not by your shell, so they do not
    necessarily inherit the PATH that makes a bare `sessync` resolve -- on
    Windows pip puts the shim in Python's Scripts dir, which is often absent
    from the user PATH. Baking in the resolved path removes that variable.
    """
    found = shutil.which("sessync") or shutil.which("sessync.exe")
    if found:
        return found
    # Fall back to running the module through the interpreter we are using.
    return f'"{sys.executable}" -m sessync'


def command_for(agent: str) -> str:
    # --quiet because Codex parses a hook's stdout as instructions to the agent.
    exe = executable()
    if " " in exe and not exe.startswith('"'):
        exe = f'"{exe}"'
    return f"{exe} push {agent} all --from-hook --quiet"


@dataclass
class HookSite:
    path: Path
    scope: str = "global"          # "global" | "project"

    @property
    def where(self) -> str:
        return f"{self.path}" + ("  (this project only)" if self.scope == "project" else "")


def site(agent: str) -> HookSite:
    if agent == "claude":
        return HookSite(Path.home() / ".claude" / "settings.json")
    if agent == "codex":
        return HookSite(Path.home() / ".codex" / "hooks.json")
    if agent == "antigravity":
        return HookSite(Path.home() / ".gemini" / "config" / "hooks.json")
    if agent == "kiro":
        return HookSite(Path.cwd() / ".kiro" / "hooks" / "sessync.json", "project")
    raise KeyError(agent)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".sessync-bak")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    body = json.dumps(data, indent=2)
    json.loads(body)                       # never write anything unparseable
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(path)


# ------------------------------------------------------------------- per agent

def _stop_list(data: dict, agent: str) -> list:
    """The Stop array for claude/codex, created if absent."""
    hooks = data.setdefault("hooks", {})
    if agent == "claude":
        return hooks.setdefault("Stop", [])
    return hooks.setdefault("Stop", [])


def installed(agent: str) -> bool:
    s = site(agent)
    if agent == "kiro":
        return s.path.exists() and _marker(agent) in s.path.read_text(encoding="utf-8")
    data = _load(s.path)
    if agent == "antigravity":
        return _marker(agent) in json.dumps(data.get("sessync", {}))
    return _marker(agent) in json.dumps((data.get("hooks") or {}).get("Stop", []))


def install(agent: str) -> str:
    if agent not in ADAPTERS:
        raise KeyError(agent)
    if installed(agent):
        return f"{agent}: already installed"
    s, cmd = site(agent), command_for(agent)

    if agent == "kiro":
        _save(s.path, {"version": "v1", "hooks": [{
            "name": "sessync", "trigger": "Stop",
            "action": {"type": "command", "command": cmd},
        }]})
    elif agent == "antigravity":
        data = _load(s.path)
        data["sessync"] = {"Stop": [
            {"type": "command", "command": cmd, "timeout": 30}]}
        _save(s.path, data)
    elif agent == "claude":
        data = _load(s.path)
        _stop_list(data, agent).append(
            {"matcher": "", "hooks": [{"type": "command", "command": cmd}]})
        _save(s.path, data)
    else:                                   # codex
        data = _load(s.path)
        _stop_list(data, agent).append(
            {"hooks": [{"type": "command", "command": cmd}]})
        _save(s.path, data)
    return f"{agent}: hook added -> {s.where}"


def remove(agent: str) -> str:
    if agent not in ADAPTERS:
        raise KeyError(agent)
    if not installed(agent):
        return f"{agent}: not installed"
    s = site(agent)

    if agent == "kiro":
        s.path.unlink(missing_ok=True)
    elif agent == "antigravity":
        data = _load(s.path)
        data.pop("sessync", None)
        _save(s.path, data)
    else:
        data = _load(s.path)
        stop = (data.get("hooks") or {}).get("Stop", [])
        data["hooks"]["Stop"] = [e for e in stop if _marker(agent) not in json.dumps(e)]
        _save(s.path, data)
    return f"{agent}: hook removed from {s.path}"


def status() -> list[tuple[str, bool, str]]:
    out = []
    for agent in ADAPTERS:
        try:
            out.append((agent, installed(agent), site(agent).where))
        except Exception:                              # noqa: BLE001
            out.append((agent, False, "(unavailable)"))
    return out
