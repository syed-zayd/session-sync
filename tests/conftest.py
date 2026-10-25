"""Run every test inside a throwaway home directory.

sessync reads and writes the agents' real session stores (~/.claude, ~/.codex,
~/.kiro, ~/.gemini). A test that touches those leaves sessions behind in
someone's actual agent history -- worse than no test. So before anything
imports sessync, HOME / USERPROFILE / LOCALAPPDATA / SESSYNC_HOME are pointed at
a temp directory. The adapters resolve their roots at import time, which is why
this has to happen here, at conftest import, and not in a fixture.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SANDBOX = Path(tempfile.mkdtemp(prefix="sessync-sandbox-"))
HOME = SANDBOX / "home"
os.environ["HOME"] = os.environ["USERPROFILE"] = str(HOME)
os.environ["LOCALAPPDATA"] = str(SANDBOX / "localappdata")
os.environ["SESSYNC_HOME"] = str(SANDBOX / "sessync")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sessync import registry                                      # noqa: E402
from sessync.adapters import antigravity, claude_code, codex, kiro  # noqa: E402

# Refuse to run at all if any store escaped the sandbox.
for _root in (claude_code.ROOT, codex.ROOT, codex.STATE_DB, kiro.ROOT,
              kiro.CLASSIC_DB, antigravity.ROOT, registry.ROOT):
    if SANDBOX not in Path(_root).parents:
        raise RuntimeError(f"test sandbox leak: {_root} is outside {SANDBOX}")


def _make_codex_index() -> None:
    """An empty `threads` table, so the picker-index code path runs too."""
    codex.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(codex.STATE_DB)
    con.execute(f"CREATE TABLE threads ({', '.join(codex._THREAD_COLS)},"
                " PRIMARY KEY (id))")
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def clean_sandbox():
    """Fresh, empty agent stores for every test."""
    for d in (HOME, SANDBOX / "localappdata", SANDBOX / "sessync"):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    _make_codex_index()
    antigravity.ROOT.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def work(tmp_path) -> str:
    """A project directory for test sessions to belong to."""
    d = tmp_path / "project"
    d.mkdir()
    return str(d)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(SANDBOX, ignore_errors=True)
