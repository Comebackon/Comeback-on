"""Accounts must survive process replacement, and Render must not accept transient accounts."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import storage
from webapp import create_app


REPOSITORY = Path(__file__).resolve().parents[1]
ACCOUNT = {
    "email": "persistence-test@example.test",
    "password": "synthetic-password-123",
    "nickname": "저장 확인",
}


def app_config(directory, secret="test-session-secret"):
    return {
        "TESTING": True,
        "DATA_DIR": str(directory),
        "DATABASE": str(directory / "comeback.sqlite3"),
        "PHOTO_DIR": str(directory / "report_photos"),
        "SECRET_KEY": secret,
        "SESSION_COOKIE_SECURE": False,
    }


@pytest.fixture
def render_disk(tmp_path, monkeypatch):
    mount = tmp_path / "persistent-disk"
    mount.mkdir()
    data = mount / "comeback"
    data.mkdir()
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("COMEBACK_PERSISTENT_MOUNT", str(mount))
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path).resolve() == mount.resolve())
    return mount, data


def test_local_accounts_do_not_require_a_cloud_mount(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(os.path, "ismount", lambda _path: False)
    assert storage.account_storage_ready(tmp_path / "comeback.sqlite3", tmp_path) is True


def test_render_accepts_database_and_app_data_on_a_mounted_disk(render_disk):
    _mount, data = render_disk
    assert storage.account_storage_ready(data / "comeback.sqlite3", data) is True


def test_render_rejects_a_directory_without_a_disk(render_disk, monkeypatch):
    _mount, data = render_disk
    monkeypatch.setattr(os.path, "ismount", lambda _path: False)
    assert storage.account_storage_ready(data / "comeback.sqlite3", data) is False


def test_render_rejects_a_missing_mount_directory(tmp_path, monkeypatch):
    mount = tmp_path / "missing-disk"
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("COMEBACK_PERSISTENT_MOUNT", str(mount))
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    assert storage.account_storage_ready(mount / "comeback.sqlite3", mount) is False


def test_render_root_filesystem_is_not_persistent_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("COMEBACK_PERSISTENT_MOUNT", "/")
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    assert storage.account_storage_ready(tmp_path / "comeback.sqlite3", tmp_path) is False


@pytest.mark.parametrize("outside", ["database", "data_directory"])
def test_render_requires_both_database_and_data_directory_on_disk(render_disk, tmp_path, outside):
    _mount, data = render_disk
    transient = tmp_path / "ephemeral"
    transient.mkdir()
    database = transient / "comeback.sqlite3" if outside == "database" else data / "comeback.sqlite3"
    directory = transient if outside == "data_directory" else data
    assert storage.account_storage_ready(database, directory) is False


@pytest.mark.parametrize("escape", ["database", "data_directory"])
def test_render_rejects_symlinks_to_transient_storage(render_disk, tmp_path, escape):
    _mount, data = render_disk
    transient = tmp_path / "ephemeral"
    transient.mkdir()
    database = data / "comeback.sqlite3"
    directory = data
    if escape == "database":
        (transient / "outside.sqlite3").touch()
        database.symlink_to(transient / "outside.sqlite3")
    else:
        directory = data / "outside"
        directory.symlink_to(transient, target_is_directory=True)
    assert storage.account_storage_ready(database, directory) is False


def test_render_transient_storage_blocks_auth_without_mutating_existing_accounts(tmp_path, monkeypatch):
    directory = tmp_path / "ephemeral-app"
    monkeypatch.delenv("RENDER", raising=False)
    original = create_app(app_config(directory))
    signup = original.test_client().post("/api/auth/register", json=ACCOUNT)
    assert signup.status_code == 201
    with sqlite3.connect(original.config["DATABASE"]) as connection:
        before_users = connection.execute("SELECT id,email,password_hash FROM users").fetchall()
        before_sessions = connection.execute("SELECT token_hash,user_id,expires_at FROM sessions").fetchall()

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("COMEBACK_PERSISTENT_MOUNT", str(tmp_path / "unmounted-disk"))
    monkeypatch.setattr(os.path, "ismount", lambda _path: False)
    restarted = create_app(app_config(directory))
    client = restarted.test_client()
    capabilities = client.get("/api/config").get_json()["capabilities"]
    assert capabilities["account_storage_ready"] is False
    assert capabilities["email_login"] is False

    for endpoint, account in (
        ("register", {**ACCOUNT, "email": "another-account@example.test"}),
        ("login", ACCOUNT),
    ):
        response = client.post(f"/api/auth/{endpoint}", json=account)
        assert response.status_code == 503
        message = response.get_json()["error"]
        assert message and "비밀번호를 확인" not in message
        assert str(directory) not in message
        assert ACCOUNT["password"] not in message

    with sqlite3.connect(restarted.config["DATABASE"]) as connection:
        assert connection.execute("SELECT id,email,password_hash FROM users").fetchall() == before_users
        assert connection.execute("SELECT token_hash,user_id,expires_at FROM sessions").fetchall() == before_sessions
    for path in ("/", "/health", "/_stcore/health", "/api/config", "/api/reports"):
        assert client.get(path).status_code == 200


def test_render_mounted_storage_allows_account_creation_and_login(render_disk):
    _mount, directory = render_disk
    app = create_app(app_config(directory))
    client = app.test_client()
    capabilities = client.get("/api/config").get_json()["capabilities"]
    assert capabilities["account_storage_ready"] is True
    assert capabilities["email_login"] is True
    response = client.post("/api/auth/register", json=ACCOUNT)
    assert response.status_code == 201
    user_id = response.get_json()["user"]["id"]

    restarted = create_app(app_config(directory))
    response = restarted.test_client().post("/api/auth/login", json=ACCOUNT)
    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user_id


PROCESS_WORKER = r'''
import json
import sqlite3
import sys

from webapp import create_app

mode, secret = sys.argv[1:]
app = create_app({"TESTING": True, "SECRET_KEY": secret, "SESSION_COOKIE_SECURE": False})
client = app.test_client()
account = {"email": "restart-check@example.test", "password": "synthetic-password-123", "nickname": "재시작 확인"}
response = client.post("/api/auth/" + ("register" if mode == "create" else "login"), json=account)
assert response.status_code == (201 if mode == "create" else 200), response.get_json()
signed_in = response.get_json()
headers = {"X-CSRF-Token": signed_in["csrf_token"]}

if mode == "create":
    favorite = client.post("/api/favorites", json={
        "name": "저장 장소", "address": "검증용 주소", "lat": 37.5, "lon": 127.0,
    }, headers=headers)
    assert favorite.status_code == 201, favorite.get_json()

favorites = client.get("/api/favorites").get_json()["favorites"]
duplicate = client.post("/api/auth/register", json={**account, "email": account["email"].upper()})
assert duplicate.status_code == 409, duplicate.get_json()

with sqlite3.connect(app.config["DATABASE"]) as connection:
    rows = connection.execute("SELECT password_hash FROM users").fetchall()
assert len(rows) == 1
password_hash = rows[0][0]
assert password_hash.startswith(("scrypt:", "pbkdf2:"))
assert account["password"] not in password_hash
assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
assert client.get("/api/me").get_json()["user"] is None

print(json.dumps({"user_id": signed_in["user"]["id"], "favorites": favorites, "duplicate_status": duplicate.status_code}))
'''


def test_existing_credentials_and_favorites_survive_process_restart_and_secret_rotation(tmp_path):
    """Each request group uses a new interpreter; no Python app/session state is shared."""
    environment = dict(os.environ, COMEBACK_DATA_DIR=str(tmp_path / "persistent-app-data"))
    for key in ("RENDER", "COMEBACK_SECRET_KEY", "COMEBACK_PERSISTENT_MOUNT"):
        environment.pop(key, None)

    def run(mode, secret):
        result = subprocess.run(
            [sys.executable, "-c", PROCESS_WORKER, mode, secret],
            cwd=REPOSITORY,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    created = run("create", "test-session-secret-first")
    restarted = run("login", "test-session-secret-first")
    rotated = run("login", "test-session-secret-rotated")
    assert created["user_id"] == restarted["user_id"] == rotated["user_id"]
    assert len(created["favorites"]) == 1
    assert created["favorites"] == restarted["favorites"] == rotated["favorites"]
    assert created["favorites"][0]["name"] == "저장 장소"
    assert all(result["duplicate_status"] == 409 for result in (created, restarted, rotated))
