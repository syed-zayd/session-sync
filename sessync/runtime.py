"""Shared runtime bits for hook-driven pushes: logging, the lock, and reading
the session id out of a Stop hook payload.

Design constraints, all learned the hard way:

  * **Never write to stdout.** Codex parses a hook's stdout as a JSON response;
    anything printed there becomes instructions to the agent.
  * **Never exit non-zero.** A failing hook degrades the agent it is attached to.
  * **Read the id, never infer it.** Resolving "the newest session here" was
    tried and removed: it makes one command mean different things depending on
    timing, and breaks outright with two sessions open at once.

Echo suppression needs no special handling: a mirror we just wrote hashes
identically to its source, so when the receiving agent's own hook fires the push
resolves to `unchanged` and nothing propagates further.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .registry import ROOT

LOG = ROOT / "notify.log"
LOCK = ROOT / "sync.lock"
LOCK_STALE_SECONDS = 120


def log(msg: str) -> None:
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {msg}\n")
    except OSError:
        pass


_ID_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId",
            "thread_id", "threadId")
_ID_RE = re.compile(
    r'"(?:' + "|".join(_ID_KEYS) + r')"\s*:\s*"([0-9a-fA-F-]{32,36})"')


def session_id_from_stdin() -> str | None:
    """The id the agent tells us on stdin. Never a guess.

    Read strictly: whatever the agent names is what we act on. Codex truncates
    this payload on Windows when the assistant message contains non-ASCII
    (openai/codex#23784), so a plain json.loads is not enough -- but the id sits
    near the front and is a well-formed uuid, so a regex over the raw text
    recovers it from a payload that never finishes. That is still reading what
    we were told, not inferring it.
    """
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        raw = sys.stdin.read(1 << 20)
    except Exception:                                  # noqa: BLE001
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            for key in _ID_KEYS:
                if payload.get(key):
                    return str(payload[key])
    except json.JSONDecodeError:
        pass
    m = _ID_RE.search(raw)
    return m.group(1) if m else None


# Reentrancy is per *thread*: a process-wide counter would let sibling threads
# walk straight through a lock the first one is holding.
_local = threading.local()
# ...and threads inside one process never contend for the lock *file* (the
# holder's own pid owns it), so they need a real mutex of their own.
_process_mutex = threading.Lock()


class Lock:
    """Serialises pushes across processes *and* threads.

    Reentrant per thread, so a fanout holds it once and the port calls nested
    underneath do not fight for it. Without this two simultaneous pushes both
    find no existing mirror and both create one.
    """

    def __init__(self, path: Path = LOCK):
        self.path = path
        self.fd = None
        self.held = False

    def __enter__(self):
        depth = getattr(_local, "depth", 0)
        _local.depth = depth + 1
        if depth:
            return self
        self.held = True
        _process_mutex.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Wait as long as it takes for a holder to go stale, and never proceed
        # without the lock: two unlocked pushes both miss each other's mirror
        # and both create one. Being killed while waiting costs nothing -- the
        # next push catches up.
        deadline = time.time() + LOCK_STALE_SECONDS + 5
        while time.time() < deadline:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.25)
        _local.depth -= 1
        self.held = False
        _process_mutex.release()
        raise TimeoutError(f"could not acquire {self.path}")

    def __exit__(self, *exc):
        _local.depth = max(getattr(_local, "depth", 1) - 1, 0)
        if not self.held:
            return False
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            # Only the holder removes the file; anyone else would be deleting
            # a lock another process still holds.
            self.path.unlink(missing_ok=True)
            self.fd = None
        self.held = False
        _process_mutex.release()
        return False
