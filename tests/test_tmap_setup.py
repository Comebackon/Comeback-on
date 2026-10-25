"""로컬 키 저장/연결을 합성 키로만 확인한다. 실제 API는 호출하지 않는다."""
import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from scripts import setup_tmap
import tmap_client
import webapp


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COMEBACK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TMAP_APP_KEY", raising=False)
    return tmp_path


def test_saved_key_persists_with_private_permissions_and_wins_over_old_export(key_dir, monkeypatch, capsys):
    target = setup_tmap.save_key("  sample-local-key-123  ")
    assert target == key_dir / "tmap_app_key"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    monkeypatch.setenv("TMAP_APP_KEY", "stale-terminal-value")
    assert tmap_client.get_app_key() == "sample-local-key-123"
    assert "sample-local-key-123" not in capsys.readouterr().out
    assert list(key_dir.glob(".tmap-key-*")) == []


def test_missing_local_key_uses_existing_deployment_environment(key_dir, monkeypatch):
    monkeypatch.setenv("TMAP_APP_KEY", "deployment-value")
    assert tmap_client.get_app_key() == "deployment-value"
    monkeypatch.delenv("TMAP_APP_KEY")
    assert tmap_client.get_app_key() == ""


@pytest.mark.parametrize("invalid", ["", "two words", "line\nbreak", "bad\x00key", "x" * 513, "키"])
def test_invalid_input_does_not_replace_saved_key(key_dir, invalid):
    target = setup_tmap.save_key("previous-value")
    with pytest.raises(ValueError):
        setup_tmap.save_key(invalid)
    assert target.read_text().strip() == "previous-value"


def test_cancel_keeps_existing_value(key_dir, monkeypatch, capsys):
    target = setup_tmap.save_key("keep-existing-value")
    monkeypatch.setattr(setup_tmap, "prompt_key", lambda: None)
    assert setup_tmap.main() == 0
    assert target.read_text().strip() == "keep-existing-value"
    assert "keep-existing-value" not in capsys.readouterr().out


def test_mac_dialog_keeps_entered_value_out_of_command_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_tmap.sys, "platform", "darwin")
    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="sample-dialog-key\n", stderr="")
    monkeypatch.setattr(setup_tmap.subprocess, "run", run)
    assert setup_tmap.prompt_key() == "sample-dialog-key"
    assert "sample-dialog-key" not in str(calls)
    assert "with hidden answer" in calls[0][0][-1]
    assert calls[0][1]["capture_output"] is True


def test_saved_key_reaches_provider_headers_but_not_public_api(key_dir, monkeypatch):
    secret = "sample-test-only-local-key"
    setup_tmap.save_key(secret)
    captured = []
    def open_request(request, timeout):
        captured.append(request)
        return io.BytesIO(json.dumps({"features": []}).encode())
    monkeypatch.setattr(tmap_client, "urlopen", open_request)
    assert tmap_client.request_pedestrian_route(127, 37, 127.01, 37.01) == {"features": []}
    assert captured[0].get_header("Appkey") == secret
    app = webapp.create_app({"TESTING": True, "DATA_DIR": str(key_dir)})
    client = app.test_client()
    response = client.get("/api/config")
    assert response.json["capabilities"]["tmap"] is True
    assert secret not in response.get_data(as_text=True)
    assert client.get("/instance/tmap_app_key").status_code == 404

