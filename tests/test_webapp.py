"""서버 기본 기능/입력 검증. 외부 위치·문자 서비스는 호출하지 않는다."""

import io
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from webapp import create_app
import storage


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test-session-key",
                       "DATA_DIR": str(tmp_path), "DATABASE": str(tmp_path / "test.sqlite3"),
                       "PHOTO_DIR": str(tmp_path / "photos"), "SESSION_COOKIE_SECURE": False})


def register(client, email="walker@example.com", nickname="귀가자"):
    response = client.post("/api/auth/register", json={"email": email, "password": "example-pass-123", "nickname": nickname})
    assert response.status_code == 201, response.json
    return {"X-CSRF-Token": response.json["csrf_token"]}


def test_register_hashes_password_and_login_rotates_session(app):
    client = app.test_client()
    headers = register(client)
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "example-pass-123" not in stored
    assert stored.startswith(("scrypt:", "pbkdf2:"))
    assert client.get("/api/me").json["user"]["nickname"] == "귀가자"
    assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
    wrong = client.post("/api/auth/login", json={"email": "walker@example.com", "password": "wrong"})
    assert wrong.status_code == 401
    valid = client.post("/api/auth/login", json={"email": "walker@example.com", "password": "example-pass-123"})
    assert valid.status_code == 200
    assert valid.json["csrf_token"] != headers["X-CSRF-Token"]
    assert "password" not in valid.json["user"]


def test_auth_requests_reject_bad_email_short_password_duplicate(app):
    client = app.test_client()
    data = {"email": "not-email", "password": "example-pass-123", "nickname": "별명"}
    assert client.post("/api/auth/register", json=data).status_code == 400
    data.update(email="valid@example.com", password="short")
    assert client.post("/api/auth/register", json=data).status_code == 400
    register(client)
    assert client.post("/api/auth/register", json={"email": "WALKER@example.com", "password": "another-long-pass", "nickname": "다른이름"}).status_code == 409


def test_csrf_origin_and_json_boundaries(app):
    client = app.test_client()
    headers = register(client)
    payload = {"name": "학교", "lat": 37.5, "lon": 127.0}
    assert client.post("/api/favorites", json=payload).status_code == 403
    assert client.post("/api/favorites", json=payload, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/favorites", json=payload, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/login", data="email=walker@example.com").status_code == 415
    response = client.post("/api/favorites", json=payload, headers={**headers, "Origin": "http://localhost"})
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_guest_can_read_reports_but_cannot_write(app):
    client = app.test_client()
    assert client.get("/api/reports").status_code == 200
    assert client.get("/api/reports?mine=1").status_code == 401
    assert client.post("/api/reports", json={"type": "기타", "content": "점검 필요", "lat": 37.5, "lon": 127}).status_code == 401
    assert client.get("/api/favorites").status_code == 401
    assert client.get("/api/guardians").status_code == 401


@pytest.mark.parametrize("lat,lon", [(float("nan"), 127), (37.5, float("inf")), (91, 127), (37.5, 181), (None, 127)])
def test_coordinates_reject_invalid_values(app, lat, lon):
    client = app.test_client()
    headers = register(client)
    response = client.post("/api/favorites", json={"name": "잘못된 위치", "lat": lat, "lon": lon}, headers=headers)
    assert response.status_code == 400


def test_settings_validate_and_persist(app):
    client = app.test_client()
    headers = register(client)
    valid = {"notifications": False, "detour_percent": 35, "max_extra_minutes": 12, "facility_filters": ["CCTV", "비상벨"]}
    assert client.patch("/api/settings", json=valid, headers=headers).status_code == 200
    settings = client.get("/api/settings").json["settings"]
    assert all(settings[k] == v for k, v in valid.items())
    assert client.patch("/api/settings", json={"detour_percent": -1}, headers=headers).status_code == 400
    assert client.patch("/api/settings", json={"notifications": "false"}, headers=headers).status_code == 400
    assert client.patch("/api/settings", json={"facility_filters": ["unsupported"]}, headers=headers).status_code == 400


def test_report_public_response_hides_account_identity_and_stays_unverified(app):
    client = app.test_client()
    headers = register(client)
    response = client.post("/api/reports", json={"type": "비상벨 고장", "content": "작동 상태 확인이 필요해요.", "lat": 37.5, "lon": 127, "address": "학교 옆"}, headers=headers)
    assert response.status_code == 201
    report = response.json["report"]
    assert report["status"] == "pending"
    assert "user_id" not in report
    assert "email" not in report
    assert "walker@example.com" not in str(report)
    public = app.test_client().get("/api/reports").json["reports"][0]
    assert public["is_mine"] is False


def test_report_photo_is_decoded_reencoded_and_metadata_removed(app):
    client = app.test_client()
    headers = register(client)
    report = client.post("/api/reports", json={"type": "기타", "content": "사진 확인 요청", "lat": 37.5, "lon": 127}, headers=headers).json["report"]
    path = f"/api/reports/{report['id']}/photo"
    bad = client.post(path, data={"photo": (io.BytesIO(b"<svg>not-an-image</svg>"), "image.png")}, headers=headers)
    assert bad.status_code == 400
    photo = Image.new("RGB", (32, 24), "yellow")
    exif = Image.Exif()
    exif[270] = "private original camera description"
    payload = io.BytesIO()
    photo.save(payload, "JPEG", exif=exif)
    payload.seek(0)
    result = client.post(path, data={"photo": (payload, "my-private-filename.jpg")}, headers=headers)
    assert result.status_code == 200
    fetched = client.get(result.json["report"]["photo_url"])
    assert fetched.status_code == 200
    assert fetched.mimetype == "image/jpeg"
    decoded = Image.open(io.BytesIO(fetched.data))
    assert not decoded.getexif()
    assert "my-private-filename" not in str(result.json)


def test_services_capabilities_are_honest(app):
    response = app.test_client().get("/api/config")
    caps = response.json["capabilities"]
    assert caps["sms"] is False
    assert caps["phone_login"] is False
    assert not any(caps["social_login"].values())
    assert caps["automatic_alerts"] is False
    assert caps["background_location"] is False


def test_global_sos_only_notifies_accepted_guardian_without_location(app):
    walker, guardian, stranger = (app.test_client() for _ in range(3))
    wh = register(walker)
    gh = register(guardian, "guardian@example.com", "보호자")
    register(stranger, "stranger@example.com", "다른사람")
    invite = walker.post("/api/guardians", json={"email": "guardian@example.com"}, headers=wh).json["guardian"]
    before = walker.post("/api/sos", json={}, headers=wh)
    assert before.json["notification"]["in_app"] == "no_recipients"
    assert not guardian.get("/api/notifications").json["notifications"]
    assert guardian.patch(f"/api/guardians/{invite['id']}", json={"action": "accept"}, headers=gh).status_code == 200
    walker.patch("/api/settings", json={"notifications": False}, headers=wh)
    response = walker.post("/api/sos", json={}, headers=wh)
    assert response.status_code == 200
    assert response.json["notification"]["recipients"] == 1
    assert response.json["notification"]["in_app"] == "recorded"
    assert response.json["notification"]["sms"]["status"] == "unavailable"
    assert response.json["location_shared"] is False
    notices = guardian.get("/api/notifications").json["notifications"]
    assert len(notices) == 1 and notices[0]["type"] == "sos"
    assert notices[0]["trip_id"] is None
    assert not stranger.get("/api/notifications").json["notifications"]
    assert guardian.get("/api/guardian/trips").json["trips"] == []
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        assert connection.execute("SELECT count(*) FROM trips").fetchone()[0] == 0


@pytest.mark.parametrize("revocation", ["stop", "session", "guardian"])
def test_late_location_cannot_restore_data_after_revocation(app, monkeypatch, revocation):
    """SELECT 후 UPDATE 직전에 철회가 커밋되는 순서를 결정적으로 재현한다."""
    walker, guardian = app.test_client(), app.test_client()
    wh = register(walker)
    gh = register(guardian, "guardian@example.com", "보호자")
    gid = walker.post("/api/guardians", json={"email": "guardian@example.com"}, headers=wh).json["guardian"]["id"]
    guardian.patch(f"/api/guardians/{gid}", json={"action": "accept"}, headers=gh)
    tid = walker.post("/api/trips", json={
        "destination": {"name": "집", "lat": 37.5, "lon": 127},
        "route": {"id": "basic", "name": "기본 경로", "coordinates": [[37.49, 127], [37.5, 127]], "duration_s": 900, "distance_m": 1200},
        "share_location": True,
    }, headers=wh).json["trip"]["id"]
    original_connect = storage.connect

    class InterleavingConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if statement.startswith("UPDATE trips SET lat="):
                with original_connect(app.config["DATABASE"]) as other:
                    if revocation == "stop":
                        other.execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE id=?", (tid,))
                    elif revocation == "session":
                        other.execute("DELETE FROM sessions WHERE user_id=(SELECT user_id FROM trips WHERE id=?)", (tid,))
                    else:
                        other.execute("UPDATE guardians SET status='disconnected' WHERE id=?", (gid,))
            return super().execute(statement, parameters)

    def intercepted_connect(path):
        connection = sqlite3.connect(path, factory=InterleavingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    monkeypatch.setattr(storage, "connect", intercepted_connect)
    response = walker.patch(f"/api/trips/{tid}", json={"action": "location", "lat": 37.495, "lon": 127}, headers=wh)
    assert response.status_code == 409
    with original_connect(app.config["DATABASE"]) as connection:
        row = connection.execute("SELECT lat,lon,location_updated_at FROM trips WHERE id=?", (tid,)).fetchone()
        assert tuple(row) == (None, None, None)


def test_sos_to_new_guardian_does_not_grant_existing_trip_access(app):
    walker, guardian = app.test_client(), app.test_client()
    wh = register(walker)
    gh = register(guardian, "guardian@example.com", "보호자")
    response = walker.post("/api/trips", json={
        "destination": {"name": "집", "lat": 37.5, "lon": 127},
        "route": {"id": "basic", "name": "기본 경로", "coordinates": [[37.49, 127], [37.5, 127]], "duration_s": 900, "distance_m": 1200},
        "share_location": False,
    }, headers=wh)
    assert response.status_code == 201
    gid = walker.post("/api/guardians", json={"email": "guardian@example.com"}, headers=wh).json["guardian"]["id"]
    guardian.patch(f"/api/guardians/{gid}", json={"action": "accept"}, headers=gh)
    result = walker.post("/api/sos", json={}, headers=wh)
    assert result.json["notification"]["recipients"] == 1
    assert len(guardian.get("/api/notifications").json["notifications"]) == 1
    assert guardian.get("/api/guardian/trips").json["trips"] == []


def test_routing_adapters_keep_guest_access_and_public_errors(app, monkeypatch):
    def routes(data):
        if data.get("invalid"):
            raise ValueError("실제 경로를 찾을 수 없어요.")
        return {"routes": [], "safe_available": False, "warnings": ["경로 데이터 없음"]}
    monkeypatch.setitem(sys.modules, "routing", SimpleNamespace(
        search_places=lambda q: [{"name": q, "lat": 37.5, "lon": 127}],
        nearby_facilities=lambda lat, lon, radius: [{"name": "CCTV", "lat": lat, "lon": lon}],
        compare_routes=routes,
    ))
    client = app.test_client()
    assert client.get("/api/search?q=학교").json["places"][0]["name"] == "학교"
    assert client.get("/api/facilities?lat=37.5&lon=127").status_code == 200
    assert client.post("/api/routes", json={}).json["safe_available"] is False
    response = client.post("/api/routes", json={"invalid": True})
    assert response.status_code == 400
    assert response.json["error"] == "실제 경로를 찾을 수 없어요."


def test_search_endpoint_uses_current_position_and_reports_sort_mode(app, monkeypatch):
    import routing

    routing._search_cache.clear()
    monkeypatch.setattr(routing, "get_app_key", lambda: "synthetic-search-key")
    payload = {"searchPoiInfo": {"totalCount": "2", "pois": {"poi": [
        {"name": "멀리 있는 카페", "noorLat": "37.6", "noorLon": "127"},
        {"name": "가까운 카페", "noorLat": "37.501", "noorLon": "127"},
    ]}}}
    monkeypatch.setattr(routing, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    try:
        client = app.test_client()
        nearby = client.get("/api/search", query_string={"q": "카페", "lat": 37.5, "lon": 127})
        assert nearby.status_code == 200
        assert nearby.json["sort"] == "distance"
        assert [place["name"] for place in nearby.json["places"]] == ["가까운 카페", "멀리 있는 카페"]
        assert nearby.json["places"][0]["distance_m"] == pytest.approx(111.2, abs=1)

        regular = client.get("/api/search", query_string={"q": "카페"})
        assert regular.status_code == 200
        assert regular.json["sort"] == "relevance"
        assert regular.json["places"][0]["name"] == "멀리 있는 카페"
        assert all("distance_m" not in place for place in regular.json["places"])
    finally:
        routing._search_cache.clear()


@pytest.mark.parametrize("origin", [
    {"lat": "37.5"}, {"lon": "127"}, {"lat": "", "lon": "127"},
    {"lat": "nan", "lon": "127"}, {"lat": "37.5", "lon": "inf"},
    {"lat": "91", "lon": "127"}, {"lat": "37.5", "lon": "181"},
])
def test_search_endpoint_rejects_incomplete_or_invalid_position(app, monkeypatch, origin):
    import routing

    def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid search position must not reach TMap")

    monkeypatch.setattr(routing, "get_app_key", lambda: "synthetic-search-key")
    monkeypatch.setattr(routing, "urlopen", unexpected_call)
    result = app.test_client().get("/api/search", query_string={"q": "카페", **origin})
    assert result.status_code == 400
    assert result.json["error"]
