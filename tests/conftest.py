"""Every test runs against a throwaway home directory.

sessync resolves each agent's store from the home directory when its modules
are imported, so the fixture points HOME somewhere empty and re-imports the
package. No test ever reads or writes your real agent sessions.
"""
import importlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

NOW = datetime.now(timezone.utc)


@pytest.fixture
def ss(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("SESSYNC_HOME", str(home / ".sessync"))
    for name in [m for m in sys.modules if m == "sessync" or m.startswith("sessync.")]:
        del sys.modules[name]

    adapters = importlib.import_module("sessync.adapters")
    canonical = importlib.import_module("sessync.canonical")
    work = home / "project"
    work.mkdir()
    Message, Session = canonical.Message, canonical.Session

    def session(messages, tool="claude", title=""):
        return Session(tool=tool, session_id="", title=title, cwd=str(work),
                       created_at=NOW, updated_at=NOW, messages=messages)

    def turns(*texts):
        """Alternating user/assistant messages, one second apart."""
        return [Message("user" if i % 2 == 0 else "assistant", t,
                        NOW + timedelta(seconds=i))
                for i, t in enumerate(texts)]

    def count(tool):
        return len(adapters.ADAPTERS[tool].list_sessions())

    yield SimpleNamespace(
        session=session, turns=turns, count=count, now=NOW,
        home=home, work=str(work),
        sync=importlib.import_module("sessync.sync"),
        cli=importlib.import_module("sessync.cli"),
        registry=importlib.import_module("sessync.registry"),
        ADAPTERS=adapters.ADAPTERS, **adapters.ADAPTERS,
        Message=canonical.Message, Session=canonical.Session,
        ToolCall=canonical.ToolCall, coalesce=canonical.coalesce,
    )
    for name in [m for m in sys.modules if m == "sessync" or m.startswith("sessync.")]:
        del sys.modules[name]
