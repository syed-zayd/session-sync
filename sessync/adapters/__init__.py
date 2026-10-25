from . import antigravity, claude_code, codex, kiro

ADAPTERS = {
    "claude": claude_code,
    "codex": codex,
    "kiro": kiro,
    "antigravity": antigravity,
}

ALIASES = {
    "cc": "claude", "claude-code": "claude",
    "cx": "codex", "openai": "codex",
    "k": "kiro", "kiro-cli": "kiro",
    "ag": "antigravity", "antigravity-cli": "antigravity", "gemini": "antigravity",
}


def resolve(name: str):
    key = ALIASES.get(name.lower(), name.lower())
    if key not in ADAPTERS:
        raise KeyError(f"unknown tool {name!r}; expected one of {', '.join(ADAPTERS)}")
    return key, ADAPTERS[key]


__all__ = ["ADAPTERS", "ALIASES", "resolve"]
