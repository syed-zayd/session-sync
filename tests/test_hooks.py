"""auto_push edits other tools' config files, so it must be surgical."""
import io
import json

import pytest

from sessync import hooks, runtime

UNRELATED = {"hooks": {"Stop": [{"matcher": "", "hooks": [
    {"type": "command", "command": "echo mine"}]}]}, "theme": "dark"}


@pytest.mark.parametrize("agent", ["claude", "codex", "antigravity"])
def test_install_then_remove_leaves_other_settings_alone(agent):
    path = hooks.site(agent).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(UNRELATED), encoding="utf-8")

    assert not hooks.installed(agent)
    hooks.install(agent)
    assert hooks.installed(agent)
    assert hooks.install(agent).endswith("already installed")
    assert path.with_suffix(path.suffix + ".sessync-bak").exists()

    hooks.remove(agent)
    assert not hooks.installed(agent)
    assert json.loads(path.read_text(encoding="utf-8")) == UNRELATED


def test_kiro_hook_is_project_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hooks.install("kiro")
    assert (tmp_path / ".kiro" / "hooks" / "sessync.json").exists()
    hooks.remove("kiro")
    assert not (tmp_path / ".kiro" / "hooks" / "sessync.json").exists()


def test_session_id_from_clean_payload(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"session_id": "ad4ccd05-d797-495f-b5b9-ca6b81c51c0d", "x": 1}'))
    assert runtime.session_id_from_stdin() == "ad4ccd05-d797-495f-b5b9-ca6b81c51c0d"


def test_session_id_from_truncated_payload(monkeypatch):
    # Codex cuts this payload short on Windows (openai/codex#23784).
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '{"thread_id":"019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11","last_message":"caf'))
    assert runtime.session_id_from_stdin() == "019823f1-7c4e-7c3a-9f10-2b6d5e4a8c11"


def test_no_payload_means_no_id(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert runtime.session_id_from_stdin() is None
