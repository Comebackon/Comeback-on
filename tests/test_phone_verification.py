"""가입·번호 변경 때의 휴대폰 문자 인증. 실제 솔라피는 호출하지 않는다."""
import re
import sqlite3
import time

import pytest

import sms_service
from webapp import create_app

PHONE = "01012345678"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "DATABASE": str(tmp_path / "otp.sqlite3"),
                       "SECRET_KEY": "test-only-secret"})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SOLAPI_API_KEY", "TEST-KEY")
    monkeypatch.setenv("SOLAPI_API_SECRET", "TEST-SECRET")
    monkeypatch.setenv("SOLAPI_SENDER", "01000000000")


@pytest.fixture
def texts(monkeypatch):
    box = []
    monkeypatch.setattr(sms_service, "send", lambda to, text, timeout=5: box.append((to, text)) or {"status": "sent"})
    return box


def send(client, phone=PHONE):
    return client.post("/api/phone/send", json={"phone": phone})


def code_of(texts):
    return re.search(r"\d{6}", texts[-1][1]).group()


def verify(client, code, phone=PHONE):
    return client.post("/api/phone/verify", json={"phone": phone, "code": code})


def proof_for(client, texts, phone=PHONE):
    assert send(client, phone).status_code == 200
    response = verify(client, code_of(texts), phone)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["proof"]


def signup(client, **extra):
    payload = {"email": "a@example.test", "password": "testing-12345", "nickname": "a"}
    payload.update(extra)
    return client.post("/api/auth/register", json=payload)


def rows(app, sql, args=()):
    connection = sqlite3.connect(app.config["DATABASE"])
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def test_verification_is_unavailable_without_sms_service(app, monkeypatch):
    for name in ("SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_SENDER"):
        monkeypatch.delenv(name, raising=False)
    client = app.test_client()
    assert send(client).status_code == 503
    assert client.get("/api/config").get_json()["capabilities"]["phone_verification"] is False
    assert signup(client).status_code == 201  # 문자 서비스가 없으면 기존처럼 가입된다.


def test_capability_is_on_when_configured(app, configured):
    assert app.test_client().get("/api/config").get_json()["capabilities"]["phone_verification"] is True


def test_invalid_number_is_rejected_before_any_text(app, configured, texts):
    client = app.test_client()
    assert send(client, "12345").status_code == 400
    assert send(client, "02-123-4567").status_code == 400
    assert texts == []


def test_code_is_six_digits_and_stored_only_as_a_hash(app, configured, texts):
    client = app.test_client()
    assert send(client).get_json() == {"sent": True, "expires_in": 300, "resend_after": 60}
    code = code_of(texts)
    assert texts[0][0] == PHONE and len(code) == 6 and "인증번호" in texts[0][1]
    stored = rows(app, "SELECT code_hash FROM phone_codes")[0][0]
    assert code not in stored and len(stored) == 64


def test_signup_requires_a_verified_number_and_consent(app, configured, texts):
    client = app.test_client()
    assert signup(client).status_code == 400
    assert signup(client, phone=PHONE, consent=True).status_code == 400  # 증표 없음
    proof = proof_for(client, texts)
    assert signup(client, phone=PHONE, phone_proof=proof).status_code == 400  # 동의 없음
    assert signup(client, phone=PHONE, phone_proof="wrong", consent=True).status_code == 400
    assert signup(client, phone="01099998888", phone_proof=proof, consent=True).status_code == 400  # 다른 번호의 증표
    response = signup(client, phone=PHONE, phone_proof=proof, consent=True)
    assert response.status_code == 201, response.get_json()
    saved = rows(app, "SELECT phone FROM sms_contacts")
    assert saved == [(PHONE,)]
    assert rows(app, "SELECT count(*) FROM users")[0][0] == 1


def test_proof_works_once(app, configured, texts):
    client = app.test_client()
    proof = proof_for(client, texts)
    assert signup(client, phone=PHONE, phone_proof=proof, consent=True).status_code == 201
    other = app.test_client()
    assert signup(other, email="b@example.test", nickname="b", phone=PHONE, phone_proof=proof, consent=True).status_code == 400


def test_wrong_code_is_rejected_and_locks_after_five_tries(app, configured, texts):
    client = app.test_client()
    send(client)
    real = code_of(texts)
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(5):
        assert verify(client, wrong).status_code == 400
    locked = verify(client, real)
    assert locked.status_code == 429
    assert "proof" not in locked.get_json()


def test_malformed_code_is_rejected(app, configured, texts):
    client = app.test_client()
    send(client)
    for bad in ("", "12345", "1234567", "abcdef", None):
        assert client.post("/api/phone/verify", json={"phone": PHONE, "code": bad}).status_code == 400


def test_verify_without_requesting_a_code_fails(app, configured, texts):
    assert verify(app.test_client(), "123456").status_code == 400


def test_expired_code_is_rejected(app, configured, texts):
    client = app.test_client()
    send(client)
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("UPDATE phone_codes SET expires_at=?", (int(time.time()) - 1,))
    connection.commit()
    connection.close()
    assert verify(client, code_of(texts)).status_code == 400


def test_resend_has_a_cooldown_and_a_new_code_replaces_the_old(app, configured, texts):
    client = app.test_client()
    assert send(client).status_code == 200
    assert send(client).status_code == 429
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("UPDATE phone_codes SET sent_at=sent_at-61")
    connection.commit()
    connection.close()
    first = code_of(texts)
    assert send(client).status_code == 200
    second = code_of(texts)
    if first != second:
        assert verify(client, first).status_code == 400
    assert verify(client, second).status_code == 200


def test_one_number_can_only_be_texted_five_times_an_hour(app, configured, texts):
    client = app.test_client()
    for _ in range(5):
        assert send(client).status_code == 200
        connection = sqlite3.connect(app.config["DATABASE"])
        connection.execute("UPDATE phone_codes SET sent_at=sent_at-61")
        connection.commit()
        connection.close()
    assert send(client).status_code == 429
    assert len(texts) == 5


def test_daily_total_is_capped(app, configured, texts, monkeypatch):
    monkeypatch.setenv("SMS_OTP_DAILY_LIMIT", "2")
    client = app.test_client()
    assert send(client, "01011110001").status_code == 200
    assert send(client, "01011110002").status_code == 200
    assert send(client, "01011110003").status_code == 429
    assert len(texts) == 2


def test_failed_delivery_returns_an_error_and_leaves_no_code(app, configured, monkeypatch):
    monkeypatch.setattr(sms_service, "send", lambda to, text, timeout=5: {"status": "failed", "reason": "http_400"})
    client = app.test_client()
    response = send(client)
    assert response.status_code == 502
    assert "http_400" not in response.get_data(as_text=True)
    assert rows(app, "SELECT count(*) FROM phone_codes")[0][0] == 0


def test_changing_the_saved_number_needs_a_fresh_verification(app, configured, texts):
    client = app.test_client()
    proof = proof_for(client, texts)
    assert signup(client, phone=PHONE, phone_proof=proof, consent=True).status_code == 201
    token = client.get("/api/me").get_json()["csrf_token"]

    def put(data):
        return client.put("/api/sms-contact", json=data, headers={"X-CSRF-Token": token})

    assert put({"phone": "01099998888", "consent": True}).status_code == 400
    assert put({"phone": "01099998888", "consent": True, "phone_proof": proof}).status_code == 400
    new_proof = proof_for(app.test_client(), texts, "01099998888")
    assert put({"phone": "01099998888", "consent": True, "phone_proof": new_proof}).get_json()["phone"] == "010-****-8888"
    assert rows(app, "SELECT phone FROM sms_contacts") == [("01099998888",)]
