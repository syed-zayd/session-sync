"""Config file: which agents accept incoming sessions.

Only `auto_pull` lives here. `auto_push` deliberately does not: whether an agent
pushes is decided by whether our hook is present in *that agent's own* config,
so there is exactly one source of truth and nothing to drift. See `hooks.py`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .adapters import ADAPTERS, resolve
from .registry import ROOT

PATH = ROOT / "config.json"

DEFAULT_AUTO_PULL = True


@dataclass
class Config:
    auto_pull: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        cfg = cls(auto_pull={t: DEFAULT_AUTO_PULL for t in ADAPTERS})
        if not PATH.exists():
            return cfg
        try:
            raw = json.loads(PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cfg
        for tool, value in (raw.get("auto_pull") or {}).items():
            if tool in ADAPTERS:
                cfg.auto_pull[tool] = bool(value)
        return cfg

    def save(self) -> None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"auto_pull": self.auto_pull}, indent=2),
                       encoding="utf-8", newline="\n")
        tmp.replace(PATH)

    # ------------------------------------------------------------------ query

    def pulls(self, tool: str) -> bool:
        return self.auto_pull.get(tool, DEFAULT_AUTO_PULL)

    def targets_for(self, source: str) -> list[str]:
        """Agents a session should be pushed to when `source` publishes."""
        return [t for t in ADAPTERS if t != source and self.pulls(t)]

    def set(self, tool: str, value: bool) -> None:
        if tool == "all":
            for t in ADAPTERS:
                self.auto_pull[t] = value
        else:
            key, _ = resolve(tool)
            self.auto_pull[key] = value
