"""Solapi 문자 알림: 번호 등록 동의, 발송 범위, 개인정보 최소화. 실제 솔라피는 호출하지 않는다."""
import hashlib
import hmac
import io
import json
import sqlite3
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest

import push_service
import sms_service
from webapp import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "DATABASE": str(tmp_path / "sms.sqlite3"),
                       "SECRET_KEY": "test-only-secret"})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SOLAPI_API_KEY", "TEST-KEY")
    monkeypatch.setenv("SOLAPI_API_SECRET", "TEST-SECRET")
    monkeypatch.setenv("SOLAPI_SENDER", "01000000000")


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(sms_service, "send", lambda to, text, timeout=5: calls.append((to, text)) or {"status": "sent"})
    return calls


def user(app, name):
    client = app.test_client()
    response = client.post("/api/auth/register", json={"email": f"{name}@example.test", "password": "testing-12345", "nickname": name})
    assert response.status_code == 201, response.get_json()
    return client


def change(client, method, path, data=None):
    token = client.get("/api/me").get_json()["csrf_token"]
    return client.open(path, method=method, json=data or {}, headers={"X-CSRF-Token": token})


def connect(owner, guardian_name, guardian):
    gid = change(owner, "POST", "/api/guardians", {"email": f"{guardian_name}@example.test"}).get_json()["guardian"]["id"]
    assert change(guardian, "PATCH", f"/api/guardians/{gid}", {"action": "accept"}).status_code == 200


def register_phone(client, phone="010-1234-5678"):
    response = change(client, "PUT", "/api/sms-contact", {"phone": phone, "consent": True})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_phone_normalizing_and_masking():
    assert sms_service.normalize_phone("010-1234-5678") == "01012345678"
    assert sms_service.normalize_phone("+82 10 1234 5678") == "01012345678"
    assert sms_service.normalize_phone("02-123-4567") == ""
    assert sms_service.normalize_phone("hello") == ""
    assert sms_service.mask_phone("01012345678") == "010-****-5678"


def test_authorization_header_is_a_verifiable_hmac():
    now = datetime(2026, 11, 18, 1, 2, 3, 456000, tzinfo=timezone.utc)
    header = sms_service.authorization("KEY", "SECRET", now=now, salt="abc123")
    date = "2026-11-18T01:02:03.456Z"
    expected = hmac.new(b"SECRET", (date + "abc123").encode(), hashlib.sha256).hexdigest()
    assert header == f"HMAC-SHA256 apiKey=KEY, date={date}, salt=abc123, signature={expected}"


def test_send_is_unavailable_without_credentials(monkeypatch):
    for name in ("SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER"):
        monkeypatch.delenv(name, raising=False)
    assert not sms_service.configured()
    assert sms_service.send("01012345678", "x") == {"status": "unavailable"}


def test_send_posts_expected_request(configured, monkeypatch):
    captured = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
        return Response()

    monkeypatch.setattr(sms_service, "urlopen", fake_urlopen)
    assert sms_service.send("010-1234-5678", "안녕") == {"status": "sent"}
    assert captured["url"] == "https://api.solapi.com/messages/v4/send"
    assert captured["body"] == {"message": {"to": "01012345678", "from": "01000000000", "text": "안녕"}}
    assert captured["headers"]["Authorization"].startswith("HMAC-SHA256 apiKey=TEST-KEY, date=")


def test_send_failure_never_raises_or_leaks_secrets(configured, monkeypatch):
    def refuse(request, timeout):
        raise HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"TEST-KEY TEST-SECRET"))

    monkeypatch.setattr(sms_service, "urlopen", refuse)
    result = sms_service.send("01012345678", "x")
    assert result == {"status": "failed", "reason": "http_401"}
    assert "TEST" not in json.dumps(result)


def test_phone_registration_needs_consent_and_valid_number(app):
    guardian = user(app, "guardian")
    assert change(guardian, "PUT", "/api/sms-contact", {"phone": "01012345678"}).status_code == 400
    assert change(guardian, "PUT", "/api/sms-contact", {"phone": "01012345678", "consent": "yes"}).status_code == 400
    assert change(guardian, "PUT", "/api/sms-contact", {"phone": "12345", "consent": True}).status_code == 400
    assert guardian.get("/api/sms-contact").get_json()["phone"] is None
    assert register_phone(guardian)["phone"] == "010-****-5678"
    assert register_phone(guardian, "01099998888")["phone"] == "010-****-8888"
    assert change(guardian, "DELETE", "/api/sms-contact").get_json()["phone"] is None


def test_guests_cannot_use_sms_contact_endpoints(app):
    client = app.test_client()
    assert client.get("/api/sms-contact").status_code == 401


def test_sos_texts_only_guardians_who_registered_a_number(app, configured, sent):
    owner, ready, silent = (user(app, name) for name in ("owner", "ready", "silent"))
    connect(owner, "ready", ready)
    connect(owner, "silent", silent)
    register_phone(ready)
    response = change(owner, "POST", "/api/sos", {})
    assert response.status_code == 200, response.get_json()
    sms = response.get_json()["notification"]["sms"]
    assert sms == {"status": "partial", "sent": 1, "failed": 0, "no_phone": 1, "push": 0}
    assert [number for number, _ in sent] == ["01012345678"]
    text = sent[0][1]
    stored = sqlite3.connect(app.config["DATABASE"]).execute(
        "SELECT n.id FROM notifications n JOIN users u ON u.id=n.recipient_id WHERE u.email='ready@example.test'").fetchone()[0]
    assert text == f"[귀가ON] owner님이 도움을 요청했어요! 바로 연락해 주세요.\nhttps://comeback-on.kr/?screen=guardian-view&notification={stored}"
    assert not any(ch in text for ch in ("위도", "경도", "37.", "126."))
    log = sqlite3.connect(app.config["DATABASE"]).execute("SELECT kind,status FROM sms_log").fetchall()
    assert log == [("sos", "sent")]


def test_sos_without_configuration_reports_unavailable(app, monkeypatch):
    for name in ("SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER"):
        monkeypatch.delenv(name, raising=False)
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    sms = change(owner, "POST", "/api/sos", {}).get_json()["notification"]["sms"]
    assert sms["status"] == "unavailable"


def test_failed_delivery_is_reported_honestly(app, configured, monkeypatch):
    monkeypatch.setattr(sms_service, "send", lambda to, text, timeout=5: {"status": "failed", "reason": "network"})
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    response = change(owner, "POST", "/api/sos", {})
    assert response.status_code == 200
    notification = response.get_json()["notification"]
    assert notification["in_app"] == "recorded"
    assert notification["sms"] == {"status": "failed", "sent": 0, "failed": 1, "no_phone": 0, "push": 0}


def test_daily_limit_protects_the_balance(app, configured, sent, monkeypatch):
    monkeypatch.setenv("SMS_DAILY_LIMIT", "1")
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    assert change(owner, "POST", "/api/sos", {}).get_json()["notification"]["sms"]["status"] == "sent"
    assert change(owner, "POST", "/api/sos", {}).get_json()["notification"]["sms"]["status"] == "limited"
    assert len(sent) == 1


def test_disconnected_guardian_gets_no_text(app, configured, sent):
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    gid = guardian.get("/api/guardians").get_json()["guardians"][0]["id"]
    assert change(owner, "PATCH", f"/api/guardians/{gid}", {"action": "disconnect"}).status_code == 200
    change(owner, "POST", "/api/sos", {})
    assert sent == []


def trip_with_guardian(owner):
    response = change(owner, "POST", "/api/trips", {
        "destination": {"name": "테스트 목적지", "lat": 37.48, "lon": 126.93},
        "route": {"id": "basic", "name": "테스트 경로", "coordinates": [[37.479, 126.929], [37.48, 126.93]], "distance_m": 200, "duration_s": 180},
        "share_location": False,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()["trip"]["id"]


def test_each_situation_gets_its_own_wording(app, configured, sent):
    owner, guardian = user(app, "하늘"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    tid = trip_with_guardian(owner)
    for kind in ("hazard", "deviation", "delay"):
        assert change(owner, "POST", f"/api/trips/{tid}/events", {"type": kind}).status_code == 200
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "finish"}).status_code == 200
    first_lines = [text.split("\n")[0] for _, text in sent]
    assert first_lines == [
        "[귀가ON] 하늘님이 귀가를 시작했어요.",
        "[귀가ON] 하늘님이 귀가 경로에서 주의 구간을 확인했어요.",
        "[귀가ON] 하늘님이 경로를 벗어났는데 확인 요청에 응답이 없어요. 연락해 보세요.",
        "[귀가ON] 하늘님의 귀가가 지연되는데 확인 요청에 응답이 없어요. 연락해 보세요.",
        "[귀가ON] 하늘님이 안전하게 도착했어요.",
    ]
    assert len(set(first_lines)) == len(first_lines)
    assert not any("테스트 목적지" in text or "테스트 경로" in text for _, text in sent)
    # 확인할 내용이 있는 알림에는 링크가 붙고, 도착 알림에는 붙지 않는다.
    assert all("\nhttps://comeback-on.kr/?screen=guardian-view&notification=" in text for _, text in sent[:4])
    assert "\n" not in sent[4][1]


def test_link_opens_only_for_the_guardian_it_was_sent_to(app, configured, sent):
    owner, guardian, stranger = (user(app, name) for name in ("owner", "guardian", "stranger"))
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    change(owner, "POST", "/api/sos", {})
    link = sent[0][1].split("\n")[1]
    nid = link.rsplit("notification=", 1)[1]
    assert len(nid) == 32
    opened = guardian.get(f"/api/notifications/{nid}")
    assert opened.status_code == 200 and opened.get_json()["notification"]["kind"] == "sos"
    assert stranger.get(f"/api/notifications/{nid}").status_code == 404
    assert owner.get(f"/api/notifications/{nid}").status_code == 404
    assert app.test_client().get(f"/api/notifications/{nid}").status_code == 401


def test_link_base_can_be_configured_and_falls_back_safely(app, configured, sent, monkeypatch):
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    monkeypatch.setenv("COMEBACK_PUBLIC_URL", "https://example.test/")
    change(owner, "POST", "/api/sos", {})
    assert sent[-1][1].split("\n")[1].startswith("https://example.test/?screen=guardian-view&notification=")
    monkeypatch.setenv("COMEBACK_PUBLIC_URL", "javascript:alert(1)")
    change(owner, "POST", "/api/sos", {})
    assert sent[-1][1].split("\n")[1].startswith("https://comeback-on.kr/?screen=guardian-view&notification=")


@pytest.fixture
def push_calls(monkeypatch):
    calls = []

    def fake_send(db, user_id, private_key, notification_id=None):
        calls.append(user_id)
        return fake_send.accepted

    fake_send.accepted = 1
    monkeypatch.setattr(push_service, "send", fake_send)
    return fake_send, calls


def test_accepted_push_replaces_the_text_for_everyday_alerts(app, configured, sent, push_calls):
    fake_send, calls = push_calls
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    tid = trip_with_guardian(owner)
    response = change(owner, "POST", f"/api/trips/{tid}/events", {"type": "hazard"})
    assert response.get_json()["notification"]["sms"] == {"status": "push", "sent": 0, "failed": 0, "no_phone": 0, "push": 1}
    assert change(owner, "PATCH", f"/api/trips/{tid}", {"action": "finish"}).status_code == 200
    assert sent == []
    # 출발, 주의 구간, 도착 알림마다 푸시는 한 번씩만 시도한다.
    assert len(calls) == 3
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM sms_log").fetchone()[0] == 0


def test_failed_push_falls_back_to_the_text(app, configured, sent, push_calls):
    fake_send, calls = push_calls
    fake_send.accepted = 0
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    tid = trip_with_guardian(owner)
    sms = change(owner, "POST", f"/api/trips/{tid}/events", {"type": "hazard"}).get_json()["notification"]["sms"]
    assert sms["status"] == "sent" and sms["push"] == 0
    assert len(sent) == 2  # 출발 + 주의 구간
    assert len(calls) == 2  # 실패한 푸시를 요청 끝에 다시 보내지 않는다


def test_sos_always_texts_even_when_the_push_was_accepted(app, configured, sent, push_calls):
    fake_send, calls = push_calls
    owner, guardian = user(app, "owner"), user(app, "guardian")
    connect(owner, "guardian", guardian)
    register_phone(guardian)
    sms = change(owner, "POST", "/api/sos", {}).get_json()["notification"]["sms"]
    assert sms["status"] == "sent" and len(sent) == 1
    assert len(calls) == 1  # SOS 푸시는 응답 직후 한 번 보낸다


def test_push_to_one_guardian_still_texts_the_other(app, configured, sent, monkeypatch):
    owner, pushed, texted = (user(app, name) for name in ("owner", "pushed", "texted"))
    connect(owner, "pushed", pushed)
    connect(owner, "texted", texted)
    register_phone(pushed, "01011112222")
    register_phone(texted, "01033334444")
    pushed_id = sqlite3.connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE email='pushed@example.test'").fetchone()[0]
    monkeypatch.setattr(push_service, "send", lambda db, user_id, private_key, notification_id=None: 1 if user_id == pushed_id else 0)
    tid = trip_with_guardian(owner)
    sent.clear()  # 출발 알림은 제외하고 주의 구간 알림만 확인한다.
    sms = change(owner, "POST", f"/api/trips/{tid}/events", {"type": "hazard"}).get_json()["notification"]["sms"]
    assert sms == {"status": "sent", "sent": 1, "failed": 0, "no_phone": 0, "push": 1}
    assert [number for number, _ in sent] == ["01033334444"]
