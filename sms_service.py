"""Solapi 문자 발송.

키는 환경 변수(SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER)로만 받는다.
키·전화번호·문자 내용은 로그와 오류 응답에 남기지 않는다.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SEND_URL = "https://api.solapi.com/messages/v4/send"
MOBILE_PATTERN = re.compile(r"^01[016789]\d{7,8}$")


def normalize_phone(value):
    """국내 휴대폰 번호를 숫자만 남긴 010… 형태로 바꾼다. 형식이 틀리면 빈 문자열."""
    digits = re.sub(r"[\s\-().]", "", str(value or ""))
    if digits.startswith("+82"):
        digits = "0" + digits[3:]
    return digits if MOBILE_PATTERN.fullmatch(digits) else ""


def mask_phone(phone):
    """화면에 보여줄 때는 가운데 자리를 가린다: 010-****-1234."""
    digits = normalize_phone(phone)
    return f"{digits[:3]}-****-{digits[-4:]}" if digits else ""


def credentials():
    key = os.environ.get("SOLAPI_API_KEY", "").strip()
    secret = os.environ.get("SOLAPI_API_SECRET", "").strip()
    sender = normalize_phone(os.environ.get("SOLAPI_SENDER", "")) or re.sub(r"\D", "", os.environ.get("SOLAPI_SENDER", ""))
    if key and secret and sender:
        return key, secret, sender
    return None


def configured():
    return credentials() is not None


def authorization(key, secret, now=None, salt=None):
    date = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    salt = salt or secrets.token_hex(16)
    signature = hmac.new(secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={signature}"


def send(to, text, timeout=5):
    """문자 1건 발송을 요청한다. 예외를 던지지 않고 결과만 돌려준다.

    status가 "sent"여도 통신사 도착까지 보장하는 값은 아니고 솔라피가 요청을 접수했다는 뜻이다.
    """
    config = credentials()
    if not config:
        return {"status": "unavailable"}
    number = normalize_phone(to)
    if not number:
        return {"status": "failed", "reason": "invalid_number"}
    key, secret, sender = config
    payload = json.dumps({"message": {"to": number, "from": sender, "text": text}}, ensure_ascii=False).encode("utf-8")
    request = Request(SEND_URL, data=payload, method="POST", headers={
        "Authorization": authorization(key, secret),
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            return {"status": "sent" if 200 <= response.status < 300 else "failed"}
    except HTTPError as error:
        return {"status": "failed", "reason": f"http_{error.code}"}
    except (URLError, TimeoutError, OSError, ValueError):
        return {"status": "failed", "reason": "network"}
