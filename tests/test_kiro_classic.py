"""Kiro's classic (SQLite) store decodes to the same canonical shape.

Kiro writes sessions to one of two places depending on how it started: files
under ~/.kiro/sessions/cli for a real terminal, and the conversations_v2 table
for anything without a tty. The table looked like dead legacy when it was empty
-- it is not, and sessions written there were invisible to sessync.

The blob here is a trimmed copy of one Kiro actually produced, so this keeps
working without needing a live session to test against.

The same shape is what Kiro's own `/chat save` writes -- verified by exporting a
live session and decoding it with this decoder unchanged. So this is not a
guess at an internal format; it is Kiro's documented export.
"""
import json

from sessync.adapters.kiro import _classic_messages

BLOB = json.dumps({
    "conversation_id": "21822122-cc0a-4a37-94dd-a4e05e1616c5",
    "history": [
        {
            "user": {"content": {"Prompt": {"prompt": "Read notes.txt"}},
                     "timestamp": "2026-08-22T21:57:35.312010900-04:00"},
            "assistant": {"ToolUse": {
                "message_id": "cc0c5a4d", "content": "",
                "tool_uses": [{"id": "tooluse_7Sl0", "name": "fs_read",
                               "orig_name": "fs_read",
                               "args": {"operations": [{"mode": "Line",
                                                        "path": "notes.txt"}]}}]}},
        },
        {
            "user": {"content": {"ToolUseResults": {"tool_use_results": [
                {"tool_use_id": "tooluse_7Sl0",
                 "content": [{"Text": "alpha\nbeta\ngamma"}],
                 "status": "Success"}]}},
                "timestamp": "2026-08-22T21:57:36.000000000-04:00"},
            "assistant": {"Response": {"message_id": "d1", "content": "Three lines."}},
        },
    ],
})


def test_classic_blob_decodes():
    msgs = _classic_messages(BLOB)
    assert [(m.role, bool(m.tools)) for m in msgs] == \
        [("user", False), ("assistant", True), ("assistant", False)]
    assert msgs[0].text == "Read notes.txt"
    call = msgs[1].tools[0]
    assert call.name == "read_file"
    assert json.loads(call.args)["operations"][0]["path"] == "notes.txt"
    assert call.result == "alpha\nbeta\ngamma"
    assert msgs[2].text == "Three lines."


def test_malformed_blob_yields_nothing():
    assert _classic_messages("not json") == []
