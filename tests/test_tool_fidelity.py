"""Tool calls survive a port with their arguments and output.

Claude Code and Codex store real tool records; Kiro and Antigravity have none,
so their mirrors carry a rendered note that readers parse back. Either way the
session must still hash the same as its source, because idempotent pushing
depends on it.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sessync import sync
from sessync.adapters import ADAPTERS, claude_code, codex
from sessync.canonical import Message, Session, ToolCall

N = datetime.now(timezone.utc)
NATIVE = ("claude", "codex")      # agents with a real tool record


def sample():
    return [
        Message("user", "run the tests", N),
        Message("assistant", "Running them.", N + timedelta(seconds=1), tools=[
            ToolCall(name="shell", args='{"command": "pytest -q"}',
                     result="12 passed in 3.2s"),
            ToolCall(name="read_file", args='{"path": "conftest.py"}',
                     result="import pytest"),
        ]),
        Message("assistant", "All 12 pass.", N + timedelta(seconds=2)),
        Message("user", "ship it", N + timedelta(seconds=3)),
    ]


@pytest.fixture
def src(work):
    return claude_code.write(Session("claude", "", "Tool fidelity", work, N, N, sample()))


def test_source_keeps_calls(src):
    base = claude_code.read(src)
    assert [len(m.tools) for m in base.messages] == [0, 2, 0, 0]
    call = base.messages[1].tools[0]
    assert call.args == '{"command": "pytest -q"}'
    assert call.result == "12 passed in 3.2s"


@pytest.mark.parametrize("tgt", ["codex", "kiro", "antigravity"])
def test_tools_survive_port(src, tgt):
    base = claude_code.read(src)
    got = ADAPTERS[tgt].read(sync.port("claude", src, tgt).session_id)
    assert got.content_hash() == base.content_hash()
    assert len(got.messages) == len(base.messages)
    assert sorted(t.name for m in got.messages for t in m.tools) == ["read_file", "shell"]
    if tgt in NATIVE:
        calls = [t for m in got.messages for t in m.tools]
        assert '{"command": "pytest -q"}' in (calls[0].args, calls[1].args)
        assert sorted(t.result for t in calls) == ["12 passed in 3.2s", "import pytest"]


def test_names_written_in_each_agents_vocabulary(src):
    path = codex._find(sync.port("claude", src, "codex").session_id)
    codex_names = {json.loads(l)["payload"]["name"]
                   for l in open(path, encoding="utf-8") if '"function_call"' in l}
    assert sorted(codex_names) == ["read_file", "shell"]

    claude_names = {b["name"] for l in open(claude_code._find(src), encoding="utf-8")
                    for b in (json.loads(l).get("message") or {}).get("content", [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"}
    assert sorted(claude_names) == ["Bash", "Read"]


def test_codex_mirror_is_indexed_for_the_picker(src, work):
    sid = sync.port("claude", src, "codex").session_id
    con = sqlite3.connect(codex.STATE_DB)
    row = con.execute("SELECT cwd, history_mode, title FROM threads WHERE id=?",
                      (sid,)).fetchone()
    con.close()
    assert row is not None
    assert row[0].endswith(work)
    assert row[1] == "paginated"
    assert row[2].startswith("[From Claude]")


def test_sequential_tool_turns_keep_their_boundaries(work):
    # Regression: a run of tool-only turns used to fold into one message (71
    # collapsed into 1 on a real session), and a multi-tool turn used to split
    # into several. Both come down to where a turn ends.
    seq = [Message("user", "go", N)]
    for i in range(5):
        seq.append(Message("assistant", "", N + timedelta(seconds=i + 1),
                           tools=[ToolCall(name="shell", args="{}", result=f"out{i}")]))
    seq.append(Message("assistant", "done", N + timedelta(seconds=9)))
    s2 = claude_code.write(Session("claude", "", "Turns", work, N, N, seq))

    a2 = claude_code.read(s2)
    assert sum(1 for m in a2.messages if m.tools) == 5
    b2 = codex.read(sync.port("claude", s2, "codex").session_id)
    assert sum(1 for m in b2.messages if m.tools) == 5
    assert [t.result for m in b2.messages for t in m.tools] == \
        ["out0", "out1", "out2", "out3", "out4"]
    assert b2.content_hash() == a2.content_hash()


def test_tools_survive_a_text_only_hop(src):
    kiro_id = sync.port("claude", src, "kiro").session_id
    r = sync.port("kiro", kiro_id, "codex")
    assert sorted(t.name for m in codex.read(r.session_id).messages
                  for t in m.tools) == ["read_file", "shell"]
