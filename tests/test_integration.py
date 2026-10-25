"""Privacy and ownership contracts across account, guardian and trip workflows."""
import io
import sqlite3

import pytest
from PIL import Image

from webapp import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "DATA_DIR": str(tmp_path),
        "DATABASE": str(tmp_path / "integration.sqlite3"),
        "SECRET_KEY": "test-only-secret",
    })


def user(app, name):
    client = app.test_client()
    response = client.post("/api/auth/register", json={
        "email": f"{name}@example.test", "password": "testing-12345", "nickname": name,
    })
    assert response.status_code == 201, response.get_json()
    return client


def change(client, method, path, data=None, **kwargs):
    token = client.get("/api/me").get_json()["csrf_token"]
    headers = {"X-CSRF-Token": token}
    return client.open(path, method=method, json=data or {}, headers=headers, **kwargs)


def connect(owner, guardian_name, guardian):
    response = change(owner, "POST", "/api/guardians", {"email": f"{guardian_name}@example.test"})
    assert response.status_code == 201, response.get_json()
    gid = response.get_json()["guardian"]["id"]
    assert change(guardian, "PATCH", f"/api/guardians/{gid}", {"action": "accept"}).status_code == 200
    return gid


def trip(owner, shared):
    response = change(owner, "POST", "/api/trips", {
        "destination": {"name": "테스트 목적지", "lat": 37.48, "lon": 126.93},
        "route": {"id": "basic", "name": "테스트 경로", "coordinates": [[37.479, 126.929], [37.48, 126.93]], "distance_m": 200, "duration_s": 180},
        "share_location": shared,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()["trip"]["id"]


def shared_trips(client):
    response = client.get("/api/guardian/trips")
    assert response.status_code == 200
    return response.get_json()["trips"]


def test_connection_requires_recipient_and_private_trip_has_no_shared_location(app):
    owner, guardian, stranger = (user(app, name) for name in ("owner", "guardian", "stranger"))
    gid = change(owner, "POST", "/api/guardians", {"email": "guardian@example.test"}).get_json()["guardian"]["id"]
    assert change(owner, "PATCH", f"/api/guardians/{gid}", {"action": "accept"}).status_code == 403
    assert change(stranger, "PATCH", f"/api/guardians/{gid}", {"action": "accept"}).status_code == 404
    assert shared_trips(guardian) == []
    assert change(guardian, "PATCH", f"/api/guardians/{gid}", {"action": "accept"}).status_code == 200
    tid = trip(owner, False)
    assert shared_trips(guardian) == []
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.479, "lon": 126.929}).status_code == 409
    assert owner.get("/api/trips").get_json()["trip"]["lat"] is None


def test_only_latest_location_then_stop_and_logout_remove_access(app):
    owner, guardian = user(app, "walker"), user(app, "watcher")
    connect(owner, "watcher", guardian)
    tid = trip(owner, True)
    for lat in (37.471, 37.472):
        assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": lat, "lon": 126.93}).status_code == 200
    visible = shared_trips(guardian)
    assert len(visible) == 1 and visible[0]["lat"] == 37.472
    assert "email" not in visible[0]["traveler"]
    stopped = change(owner, "PATCH", f"/api/trips/{tid}", {"action": "stop_sharing"}).get_json()["trip"]
    assert stopped["lat"] is None and stopped["lon"] is None and stopped["location_updated_at"] is None
    assert shared_trips(guardian) == []
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.473, "lon": 126.93}).status_code == 409
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "finish"}).status_code == 200
    tid = trip(owner, True)
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.47, "lon": 126.93}).status_code == 200
    assert change(owner, "POST", "/api/auth/logout").status_code == 200
    assert shared_trips(guardian) == []
    assert owner.get("/api/trips").status_code == 401
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute("SELECT lat,lon,share_location FROM trips WHERE id=?", (tid,)).fetchone() == (None, None, 0)


def test_new_connection_does_not_retroactively_join_shared_trip(app):
    owner, first, second = (user(app, name) for name in ("walker", "first", "second"))
    gid = connect(owner, "first", first)
    tid = trip(owner, True)
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.47, "lon": 126.93}).status_code == 200
    connect(owner, "second", second)
    assert len(shared_trips(first)) == 1
    assert shared_trips(second) == []
    assert change(first, "PATCH", f"/api/guardians/{gid}", {"action": "disconnect"}).status_code == 200
    assert shared_trips(first) == [] and shared_trips(second) == []
    own = owner.get("/api/trips").get_json()["trip"]
    assert own["lat"] is None and own["share_location"] is False


def test_unrelated_account_cannot_modify_trip_or_favorite(app):
    owner, stranger = user(app, "owner"), user(app, "stranger")
    favorite = change(owner, "POST", "/api/favorites", {"name": "집", "lat": 37.47, "lon": 126.93}).get_json()["favorite"]
    tid = trip(owner, False)
    assert stranger.get("/api/favorites").get_json()["favorites"] == []
    assert change(stranger, "DELETE", f"/api/favorites/{favorite['id']}").status_code == 404
    assert change(stranger, "PATCH", f"/api/trips/{tid}", {"action": "finish"}).status_code == 404
    assert change(stranger, "POST", f"/api/trips/{tid}/events", {"type": "sos"}).status_code == 404
    assert stranger.get("/api/trips").get_json()["trip"] is None
    assert app.test_client().get("/instance/comeback.sqlite3").status_code == 404


def test_csrf_origin_and_notification_delivery_are_truthful(app):
    owner, guardian = user(app, "walker"), user(app, "watcher")
    connect(owner, "watcher", guardian)
    tid = trip(owner, True)
    assert owner.patch("/api/settings", json={"notifications": False}).status_code == 403
    token = owner.get("/api/me").get_json()["csrf_token"]
    assert owner.patch("/api/settings", json={}, headers={"X-CSRF-Token": token, "Origin": "https://unrelated.example"}).status_code == 403
    response = change(owner, "POST", f"/api/trips/{tid}/events", {"type": "sos"})
    assert response.status_code == 200
    assert response.get_json()["notification"]["sms"]["status"] == "unavailable"
    assert owner.get("/api/me").headers["Cache-Control"] == "no-store"


def test_delete_account_removes_photos_connections_and_shared_trip(app):
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    tid = trip(owner, True)
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.47, "lon": 126.93}).status_code == 200
    report = change(owner, "POST", "/api/reports", {"type": "가로등 고장", "content": "테스트 제보", "lat": 37.47, "lon": 126.93}).get_json()["report"]
    raw = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(raw, "PNG")
    raw.seek(0)
    token = owner.get("/api/me").get_json()["csrf_token"]
    upload = owner.post(f"/api/reports/{report['id']}/photo", data={"photo": (raw, "image.png")}, headers={"X-CSRF-Token": token})
    assert upload.status_code == 200, upload.get_json()
    photo = upload.get_json()["report"]["photo_url"]
    assert guardian.get(photo).status_code == 200
    assert change(guardian, "POST", f"/api/reports/{report['id']}/confirm").get_json()["report"]["confirmation_count"] == 1
    assert change(owner, "DELETE", "/api/account", {"password": "testing-12345"}).status_code == 200
    assert shared_trips(guardian) == []
    assert guardian.get("/api/guardians").get_json()["guardians"] == []
    assert guardian.get(photo).status_code == 404
    assert guardian.get("/api/reports").get_json()["reports"] == []
    assert guardian.get("/api/notifications").get_json()["notifications"] == []
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute("SELECT count(*) FROM trips").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM confirmations").fetchone()[0] == 0


def test_logged_out_cookie_cannot_be_replayed(app):
    owner = user(app, "session-owner")
    old_cookie = owner.get_cookie("session").value
    old_token = owner.get("/api/me").get_json()["csrf_token"]
    assert change(owner, "POST", "/api/auth/logout").status_code == 200
    owner.set_cookie("session", old_cookie)
    assert owner.get("/api/me").get_json()["user"] is None
    assert owner.post("/api/favorites", json={"name": "집", "lat": 37.47, "lon": 126.93}, headers={"X-CSRF-Token": old_token}).status_code == 401


def test_deleting_last_guardian_clears_formerly_shared_position(app):
    owner, guardian = user(app, "traveler"), user(app, "last-guardian")
    connect(owner, "last-guardian", guardian)
    tid = trip(owner, True)
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "location", "lat": 37.47, "lon": 126.93}).status_code == 200
    assert change(guardian, "DELETE", "/api/account", {"password": "testing-12345"}).status_code == 200
    row = owner.get("/api/trips").get_json()["trip"]
    assert row["share_location"] is False
    assert row["lat"] is None and row["lon"] is None and row["location_updated_at"] is None


def test_index_same_origin_assets_are_served(app):
    import re
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert '<html lang="ko">' in response.get_data(as_text=True)
    assets = set(re.findall(r'(?:src|href)=["\'](/[^"\']+)["\']', response.get_data(as_text=True)))
    assert "/static/app.js" in assets and "/static/style.css" in assets
    for path in assets:
        asset = client.get(path)
        assert asset.status_code == 200, path
        assert asset.mimetype != "text/html", path
        assert len(asset.data) > 0, path
