"""귀가ON 모바일 웹/API. 실행: gunicorn 'webapp:app'. 기존 분석 지도는 app.py에 유지."""

import io
import hashlib
import json
import math
import os
import re
import secrets
import push_service
import sqlite3
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import Flask, g, jsonify, request, send_from_directory, session
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

import storage
from tmap_client import get_app_key

ROOT = Path(__file__).resolve().parent
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def _secret(directory):
    configured = os.environ.get("COMEBACK_SECRET_KEY", "").strip()
    if configured:
        return configured
    target = directory / ".session-secret"
    if target.exists():
        return target.read_text().strip()
    # 완성된 파일을 원자적으로 연결해 여러 worker의 첫 실행 시 빈 키를 읽지 않게 한다.
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=".secret-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(secrets.token_hex(32))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return target.read_text().strip()


def create_app(test_config=None):
    app = Flask(__name__, static_folder=None)
    directory = Path(os.environ.get("COMEBACK_DATA_DIR", str(ROOT / "instance")))
    if test_config and test_config.get("DATA_DIR"):
        directory = Path(test_config["DATA_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    app.config.update(
        SECRET_KEY=_secret(directory),
        DATABASE=os.environ.get("DATABASE_URL", "").strip() or str(directory / "comeback.sqlite3"),
        DATA_DIR=str(directory),
        PHOTO_DIR=str(directory / "report_photos"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COMEBACK_COOKIE_SECURE", os.environ.get("COOKIE_SECURE", "0")) == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("RENDER"):
        # Render의 마지막 신뢰할 수 있는 프록시가 알려 준 HTTPS 스킴만 반영한다.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)
    Path(app.config["PHOTO_DIR"]).mkdir(parents=True, exist_ok=True)
    storage.initialize(app.config["DATABASE"])
    account_storage_ready = storage.account_storage_ready(app.config["DATABASE"], app.config["DATA_DIR"])
    if not account_storage_ready:
        app.logger.warning(
            "Email authentication is unavailable: Render account storage is not on a persistent disk. "
            "Attach a disk at /var/data and set COMEBACK_DATA_DIR=/var/data/comeback-on."
        )
    attempts = defaultdict(deque)

    def db():
        if "db" not in g:
            g.db = storage.connect(app.config["DATABASE"])
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def current_user():
        if "user" not in g:
            uid, sid = session.get("user_id"), session.get("sid")
            token_hash = hashlib.sha256(sid.encode()).hexdigest() if isinstance(sid, str) else None
            g.user = db().execute(
                "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE u.id=? AND s.token_hash=? AND s.expires_at>?",
                (uid, token_hash, int(time.time())),
            ).fetchone() if uid and token_hash else None
        return g.user

    def signed_in(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_user() is None:
                raise APIError("로그인 후 이용할 수 있어요.", 401)
            return fn(*args, **kwargs)
        return wrapper

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise APIError("요청 내용을 확인해 주세요.")
        return value

    def text(value, label, max_length, required=True):
        if not isinstance(value, str):
            raise APIError(f"{label}을(를) 확인해 주세요.")
        value = value.strip()
        if (required and not value) or len(value) > max_length:
            raise APIError(f"{label}은(는) {'1~' if required else '최대 '}{max_length}자로 입력해 주세요.")
        return value

    def coordinates(data):
        try:
            lat, lon = float(data["lat"]), float(data["lon"])
        except (KeyError, TypeError, ValueError):
            raise APIError("위치의 위도·경도를 확인해 주세요.")
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise APIError("올바른 위치 좌표가 아니에요.")
        return lat, lon

    def throttle(key, limit=12, seconds=300):
        if app.config.get("TESTING"):
            return
        now = time.monotonic()
        queue = attempts[(request.remote_addr, key)]
        while queue and queue[0] < now - seconds:
            queue.popleft()
        if len(queue) >= limit:
            raise APIError("요청이 많아요. 잠시 후 다시 시도해 주세요.", 429)
        queue.append(now)

    @app.before_request
    def validate_request():
        if not request.path.startswith("/api/"):
            return
        if request.method == "POST" and request.path in {"/api/auth/login", "/api/auth/register"} and not account_storage_ready:
            raise APIError("계정 저장소를 준비하고 있어요. 잠시 후 다시 이용해 주세요.", 503)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            origin = request.headers.get("Origin")
            allowed = {request.host_url.rstrip("/")}
            public_origin = os.environ.get("COMEBACK_PUBLIC_ORIGIN", "").rstrip("/")
            if public_origin:
                allowed.add(public_origin)
            if origin and origin not in allowed:
                raise APIError("허용되지 않은 요청 출처예요.", 403)
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError("허용되지 않은 요청 출처예요.", 403)
            if not request.is_json and not request.path.endswith("/photo"):
                raise APIError("JSON 형식으로 요청해 주세요.", 415)
            # 로그인/가입은 JSON과 Origin으로 보호하며 로그인 성공 시 토큰을 회전한다.
            if request.path not in {"/api/auth/login", "/api/auth/register", "/api/routes"} and current_user():
                supplied = request.headers.get("X-CSRF-Token", "")
                if not supplied or not secrets.compare_digest(supplied, session.get("csrf", "")):
                    raise APIError("화면을 새로고침한 후 다시 시도해 주세요.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if response.status_code < 400:
            for recipient, notification_id in set(g.pop('push_recipients', [])):
                try:
                    push_service.send(db(), recipient, push_service.keys(db())['private_key'], notification_id)
                except Exception:
                    app.logger.warning("Web push attempt failed; in-app notification retained")
        return response

    @app.errorhandler(APIError)
    def expected_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            message = "파일이 너무 커요. 3MB 이하 사진을 올려 주세요." if error.code == 413 else "요청한 정보를 찾을 수 없거나 요청 형식이 올바르지 않아요."
            return jsonify(error=message), error.code
        return error

    @app.errorhandler(sqlite3.IntegrityError)
    def concurrent_update(_error):
        return jsonify(error="이미 처리된 요청이에요. 화면을 새로고침해 주세요."), 409

    @app.errorhandler(Exception)
    def unexpected_error(error):
        # 응답/로그에 사용자 입력, 비밀번호, 서비스 키를 노출하지 않는다.
        app.logger.error("Unexpected request error: %s", type(error).__name__)
        return jsonify(error="요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요."), 500

    def capabilities():
        graph = os.environ.get("ROUTING_GRAPH_FILE", "")
        return {
            "web_push": account_storage_ready, "email_login": account_storage_ready, "phone_login": False,
            "account_storage_ready": account_storage_ready,
            "social_login": {"kakao": False, "naver": False, "apple": False},
            "sms": False, "road_graph": bool(graph and Path(graph).is_file()),
            "tmap": bool(get_app_key()),
            "report_photos": True,
            "background_location": False,
            "automatic_alerts": False,
        }

    def session_view():
        row = current_user()
        return {"user": storage.user_view(row), "csrf_token": csrf_token(),
                "capabilities": capabilities(), "settings": storage.settings_for(row)}

    @app.get("/api/me")
    @app.get("/api/session")
    def me():
        return jsonify(session_view())

    @app.get("/api/config")
    def config():
        return jsonify(capabilities=capabilities())

    @app.get("/health")
    @app.get("/_stcore/health")
    def health():
        return jsonify(status="ok")

    def authenticate(row):
        sid = secrets.token_urlsafe(32)
        with db():
            db().execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
            db().execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(sid.encode()).hexdigest(), row["id"], int(time.time()) + 7 * 86400))
        session.clear()
        session["user_id"] = row["id"]
        session["sid"] = sid
        session.permanent = True
        g.user = row
        return jsonify(session_view())

    @app.post("/api/auth/register")
    def register():
        throttle("register", 5, 600)
        data = body()
        email = text(data.get("email"), "이메일", 254).lower()
        if not EMAIL.match(email):
            raise APIError("이메일 형식을 확인해 주세요.")
        nickname = text(data.get("nickname"), "닉네임", 30)
        password = data.get("password")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise APIError("비밀번호는 8~128자로 입력해 주세요.")
        if db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise APIError("이미 가입된 이메일이에요.", 409)
        uid = uuid4().hex
        with db():
            db().execute("INSERT INTO users(id,email,nickname,password_hash,created_at) VALUES(?,?,?,?,?)",
                         (uid, email, nickname, generate_password_hash(password), storage.utcnow()))
        return authenticate(db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()), 201

    @app.post("/api/auth/login")
    def login():
        throttle("login")
        data = body()
        email = text(data.get("email"), "이메일", 254).lower()
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 128:
            raise APIError("이메일 또는 비밀번호를 확인해 주세요.", 401)
        row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            raise APIError("이메일 또는 비밀번호를 확인해 주세요.", 401)
        return authenticate(row)

    @app.post("/api/auth/logout")
    @signed_in
    def logout():
        # 공유 중인 상태에서 계정을 나가면 위치 제공을 함께 중단한다.
        with db():
            db().execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE user_id=? AND status='active'", (current_user()["id"],))
            db().execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(session["sid"].encode()).hexdigest(),))
        with db():
            db().execute('DELETE FROM push_subscriptions WHERE user_id=?', (current_user()['id'],))
        session.clear()
        g.user = None
        return jsonify(session_view())

    @app.get("/api/settings")
    @signed_in
    def get_settings():
        return jsonify(settings=storage.settings_for(current_user()))

    @app.patch("/api/settings")
    @signed_in
    def update_settings():
        data = body()
        settings = storage.settings_for(current_user())
        for key in ("share_location", "notifications", "shake_sos", "avoid_stairs"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise APIError("설정값을 확인해 주세요.")
                settings[key] = data[key]
        for key, low, high in (("detour_percent", 0, 100), ("max_extra_minutes", 0, 60)):
            if key in data:
                if isinstance(data[key], bool) or not isinstance(data[key], (int, float)) or not low <= data[key] <= high:
                    raise APIError(f"{key} 값은 {low}~{high} 범위로 입력해 주세요.")
                settings[key] = data[key]
        if "facility_filters" in data:
            choices = data["facility_filters"]
            allowed = {"CCTV", "가로등", "비상벨", "경찰시설", "편의점", "안전시설물", "안심귀갓길"}
            if not isinstance(choices, list) or not all(isinstance(x, str) and x in allowed for x in choices):
                raise APIError("시설 종류를 확인해 주세요.")
            settings["facility_filters"] = list(dict.fromkeys(choices))
        with db():
            db().execute("UPDATE users SET settings=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), current_user()["id"]))
            if data.get("share_location") is False:
                db().execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE user_id=? AND status='active'", (current_user()["id"],))
        return jsonify(settings=settings)

    @app.delete("/api/account")
    @signed_in
    def delete_account():
        password = body().get("password", "")
        if not isinstance(password, str) or not check_password_hash(current_user()["password_hash"], password):
            raise APIError("계정 삭제를 위해 비밀번호를 확인해 주세요.", 401)
        names = db().execute("SELECT photo_name FROM reports WHERE user_id=? AND photo_name IS NOT NULL", (current_user()["id"],)).fetchall()
        with db():
            db().execute("DELETE FROM users WHERE id=?", (current_user()["id"],))
            db().execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE status='active' AND share_location=1 AND NOT EXISTS (SELECT 1 FROM trip_guardians WHERE trip_id=trips.id)")
        for name in names:
            (Path(app.config["PHOTO_DIR"]) / name[0]).unlink(missing_ok=True)
        with db():
            db().execute('DELETE FROM push_subscriptions WHERE user_id=?', (current_user()['id'],))
        session.clear()
        g.user = None
        return jsonify(deleted=True, csrf_token=csrf_token())

    @app.get("/api/favorites")
    @signed_in
    def favorites():
        rows = db().execute("SELECT id,name,address,lat,lon FROM favorites WHERE user_id=? ORDER BY created_at DESC", (current_user()["id"],)).fetchall()
        return jsonify(favorites=[dict(row) for row in rows])

    @app.post("/api/favorites")
    @signed_in
    def add_favorite():
        data = body()
        lat, lon = coordinates(data)
        name = text(data.get("name"), "장소 이름", 60)
        address = text(data.get("address", ""), "주소", 300, False)
        fid = uuid4().hex
        if db().execute("SELECT count(*) FROM favorites WHERE user_id=?", (current_user()["id"],)).fetchone()[0] >= 100:
            raise APIError("즐겨찾기는 최대 100개까지 저장할 수 있어요.")
        with db():
            db().execute("INSERT INTO favorites VALUES(?,?,?,?,?,?,?)", (fid, current_user()["id"], name, address, lat, lon, storage.utcnow()))
        return jsonify(favorite={"id": fid, "name": name, "address": address, "lat": lat, "lon": lon}), 201

    @app.delete("/api/favorites/<fid>")
    @signed_in
    def delete_favorite(fid):
        with db():
            result = db().execute("DELETE FROM favorites WHERE id=? AND user_id=?", (fid, current_user()["id"]))
        if result.rowcount == 0:
            raise APIError("즐겨찾기를 찾을 수 없어요.", 404)
        return jsonify(deleted=True)

    def guardian_view(row):
        outgoing = row["sender_id"] == current_user()["id"]
        person_id = row["recipient_id"] if outgoing else row["sender_id"]
        person = db().execute("SELECT * FROM users WHERE id=?", (person_id,)).fetchone()
        status = row["status"]
        if status == "pending" and datetime.fromisoformat(row["created_at"]) < datetime.now(timezone.utc) - timedelta(days=7):
            status = "expired"
        return {"id": row["id"], "status": status, "direction": "outgoing" if outgoing else "incoming",
                "person": storage.user_view(person), "created_at": row["created_at"]}

    @app.get("/api/guardians")
    @signed_in
    def guardians():
        uid = current_user()["id"]
        rows = db().execute("SELECT * FROM guardians WHERE sender_id=? OR recipient_id=? ORDER BY created_at DESC", (uid, uid)).fetchall()
        return jsonify(guardians=[guardian_view(row) for row in rows])

    @app.post("/api/guardians")
    @signed_in
    def invite_guardian():
        throttle("guardian", 20, 3600)
        email = text(body().get("email"), "보호자 이메일", 254).lower()
        target = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not target:
            raise APIError("귀가ON에 가입된 보호자 이메일인지 확인해 주세요.", 404)
        uid = current_user()["id"]
        if target["id"] == uid:
            raise APIError("다른 사람을 보호자로 등록해 주세요.")
        prior = db().execute("SELECT * FROM guardians WHERE sender_id=? AND recipient_id=? AND status IN ('pending','accepted') ORDER BY created_at DESC", (uid, target["id"])).fetchone()
        if prior and guardian_view(prior)["status"] != "expired":
            raise APIError("이미 요청했거나 연결된 보호자예요.", 409)
        gid, now = uuid4().hex, storage.utcnow()
        with db():
            db().execute("INSERT INTO guardians VALUES(?,?,?,?,?,?)", (gid, uid, target["id"], "pending", now, now))
        row = db().execute("SELECT * FROM guardians WHERE id=?", (gid,)).fetchone()
        return jsonify(guardian=guardian_view(row)), 201

    @app.patch("/api/guardians/<gid>")
    @signed_in
    def change_guardian(gid):
        row = db().execute("SELECT * FROM guardians WHERE id=?", (gid,)).fetchone()
        uid, action = current_user()["id"], body().get("action")
        if not row or uid not in {row["sender_id"], row["recipient_id"]}:
            raise APIError("연결 요청을 찾을 수 없어요.", 404)
        status = guardian_view(row)["status"]
        transitions = {"accept": ("pending", "accepted", row["recipient_id"]),
                       "reject": ("pending", "rejected", row["recipient_id"]),
                       "cancel": ("pending", "cancelled", row["sender_id"]),
                       "disconnect": ("accepted", "disconnected", uid)}
        if action not in transitions:
            raise APIError("연결 요청 처리 방법을 확인해 주세요.")
        required, next_status, actor = transitions[action]
        if status != required or actor != uid:
            raise APIError("이 요청을 처리할 권한이 없거나 이미 처리됐어요.", 403)
        with db():
            db().execute("UPDATE guardians SET status=?,updated_at=? WHERE id=?", (next_status, storage.utcnow(), gid))
            if action == "disconnect":
                db().execute("DELETE FROM trip_guardians WHERE guardian_id=?", (gid,))
                # 공유 상대가 모두 사라지면 최신 위치도 즉시 지운다.
                db().execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE status='active' AND share_location=1 AND NOT EXISTS (SELECT 1 FROM trip_guardians WHERE trip_id=trips.id)")
        return jsonify(guardian=guardian_view(db().execute("SELECT * FROM guardians WHERE id=?", (gid,)).fetchone()))

    def trip_view(row, guardian=False):
        if not row:
            return None
        result = {key: row[key] for key in ("id", "status", "lat", "lon", "location_updated_at", "started_at", "eta", "finished_at")}
        result.update(destination=json.loads(row["destination"]), route=json.loads(row["route"]), share_location=bool(row["share_location"]))
        if guardian:
            person = db().execute("SELECT id,nickname FROM users WHERE id=?", (row["user_id"],)).fetchone()
            result["traveler"] = dict(person)
        return result

    def own_trip(tid, active=True):
        row = db().execute("SELECT * FROM trips WHERE id=? AND user_id=?", (tid, current_user()["id"])).fetchone()
        if row is None or (active and row["status"] != "active"):
            raise APIError("진행 중인 귀가를 찾을 수 없어요.", 404)
        return row

    def record_notification(row, kind, message):
        sender = db().execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if kind not in {"sos", "hazard"} and not storage.settings_for(sender)["notifications"]:
            return {"in_app": "disabled", "recipients": 0,
                    "sms": {"status": "unavailable", "message": "문자 전송 서비스가 연결되지 않았어요."}}
        recipients = db().execute("SELECT g.recipient_id FROM trip_guardians tg JOIN guardians g ON tg.guardian_id=g.id WHERE tg.trip_id=? AND g.status='accepted' AND g.sender_id=?", (row["id"], row["user_id"])).fetchall()
        for recipient in recipients:
            notification_id = uuid4().hex
            g.push_recipients = getattr(g, 'push_recipients', []) + [(recipient[0], notification_id)]
            db().execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?)", (notification_id, row["id"], row["user_id"], recipient[0], kind, message, storage.utcnow()))
        return {"in_app": "recorded" if recipients else "no_recipients", "recipients": len(recipients),
                "sms": {"status": "unavailable", "message": "문자 전송 서비스가 연결되지 않았어요. 필요하면 문자 앱에서 직접 보내 주세요."}}

    @app.get("/api/trips")
    @signed_in
    def get_trip():
        row = db().execute("SELECT * FROM trips WHERE user_id=? AND status='active' ORDER BY started_at DESC LIMIT 1", (current_user()["id"],)).fetchone()
        return jsonify(trip=trip_view(row))

    @app.post("/api/trips")
    @signed_in
    def start_trip():
        data = body()
        destination = data.get("destination")
        if not isinstance(destination, dict):
            raise APIError("목적지를 선택해 주세요.")
        lat, lon = coordinates(destination)
        destination = {"name": text(destination.get("name"), "목적지", 200), "lat": lat, "lon": lon}
        route = data.get("route")
        if not isinstance(route, dict):
            raise APIError("안내할 경로를 선택해 주세요.")
        coords = route.get("coordinates", [])
        if not isinstance(coords, list) or not 2 <= len(coords) <= 15000:
            raise APIError("경로 좌표를 확인해 주세요.")
        checked_coords = []
        for pair in coords:
            if not isinstance(pair, list) or len(pair) != 2:
                raise APIError("경로 좌표를 확인해 주세요.")
            checked_coords.append(list(coordinates({"lat": pair[0], "lon": pair[1]})))
        try:
            duration, distance = float(route["duration_s"]), float(route["distance_m"])
        except (KeyError, TypeError, ValueError):
            raise APIError("경로 시간과 거리를 확인해 주세요.")
        if not 0 < duration <= 86400 or not 0 < distance <= 200000:
            raise APIError("안내 가능한 경로 범위를 벗어났어요.")
        route = {"id": text(route.get("id", "basic"), "경로 종류", 30), "name": text(route.get("name", "기본 경로"), "경로 이름", 60),
                 "coordinates": checked_coords, "duration_s": duration, "distance_m": distance}
        share = data.get("share_location", False)
        if not isinstance(share, bool):
            raise APIError("위치 공유 여부를 확인해 주세요.")
        uid = current_user()["id"]
        guardian_rows = db().execute("SELECT id FROM guardians WHERE sender_id=? AND status='accepted'", (uid,)).fetchall()
        if share and not guardian_rows:
            raise APIError("위치 공유를 하려면 보호자가 연결 요청을 수락해야 해요.")
        if db().execute("SELECT 1 FROM trips WHERE user_id=? AND status='active'", (uid,)).fetchone():
            raise APIError("이미 귀가 안내 중이에요. 먼저 진행 중인 귀가를 종료해 주세요.", 409)
        tid, now = uuid4().hex, storage.utcnow()
        eta = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat(timespec="seconds")
        with db():
            db().execute("INSERT INTO trips(id,user_id,destination,route,share_location,started_at,eta) VALUES(?,?,?,?,?,?,?)", (tid, uid, json.dumps(destination, ensure_ascii=False), json.dumps(route, ensure_ascii=False), int(share), now, eta))
            # 출발/도착 알림 상대도 귀가 시작 시 연결된 보호자로 한정한다.
            for guardian in guardian_rows:
                db().execute("INSERT INTO trip_guardians VALUES(?,?)", (tid, guardian[0]))
            row = db().execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
            notice = record_notification(row, "departure", f"{route['name']}로 귀가를 시작했어요. 예상 도착: {eta}")
        return jsonify(trip=trip_view(row), notification=notice), 201

    @app.patch("/api/trips/<tid>")
    @signed_in
    def update_trip(tid):
        row, data = own_trip(tid), body()
        action = data.get("action")
        notice = None
        with db():
            if action == "location":
                lat, lon = coordinates(data)
                if not row["share_location"]:
                    raise APIError("위치 공유가 중단된 귀가예요.", 409)
                # 브라우저 위치 요청이 늦게 도착해도 중단/로그아웃/연결 해제를 덮어쓰지 않는다.
                # 읽을 때의 상태 대신 UPDATE 시점의 동의·보호자·세션을 함께 확인한다.
                result = db().execute(
                    """UPDATE trips SET lat=?,lon=?,location_updated_at=?
                       WHERE id=? AND status='active' AND share_location=1
                       AND EXISTS (
                           SELECT 1 FROM trip_guardians tg JOIN guardians gr ON gr.id=tg.guardian_id
                           WHERE tg.trip_id=trips.id AND gr.sender_id=trips.user_id AND gr.status='accepted'
                       )
                       AND EXISTS (
                           SELECT 1 FROM sessions s WHERE s.user_id=trips.user_id
                           AND s.token_hash=? AND s.expires_at>?
                       )""",
                    (lat, lon, storage.utcnow(), tid, hashlib.sha256(session["sid"].encode()).hexdigest(), int(time.time())),
                )
                if result.rowcount != 1:
                    raise APIError("위치 공유 또는 로그인이 종료됐어요. 화면을 새로고침해 주세요.", 409)
            elif action == "stop_sharing":
                db().execute("UPDATE trips SET share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL WHERE id=?", (tid,))
            elif action in {"finish", "cancel"}:
                db().execute("UPDATE trips SET status=?,share_location=0,lat=NULL,lon=NULL,location_updated_at=NULL,finished_at=? WHERE id=?", ("finished" if action == "finish" else "cancelled", storage.utcnow(), tid))
                notice = record_notification(row, "arrival" if action == "finish" else "ended", "목적지에 도착했어요." if action == "finish" else "귀가 안내를 종료했어요.")
            else:
                raise APIError("귀가 처리 방법을 확인해 주세요.")
        result = {"trip": trip_view(db().execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone())}
        if notice:
            result["notification"] = notice
        return jsonify(result)

    @app.post("/api/trips/<tid>/events")
    @signed_in
    def trip_event(tid):
        throttle("trip-event", 20, 60)
        row, data = own_trip(tid), body()
        kind = data.get("type")
        messages = {"sos": "보호자의 도움이 필요해요.", "hazard": "귀가 경로에서 주의할 구간을 확인했어요.",
                    "deviation": "경로 이탈 확인 요청에 응답이 없어요.", "delay": "귀가 지연 확인 요청에 응답이 없어요."}
        if kind not in messages:
            raise APIError("알림 종류를 확인해 주세요.")
        message = text(data.get("message", messages[kind]), "알림 내용", 500)
        with db():
            notice = record_notification(row, kind, message)
        return jsonify(notification=notice)

    @app.post("/api/sos")
    @signed_in
    def general_sos():
        """귀가 안내를 시작하지 않은 상태에서도 연결된 보호자에게 앱 내 도움 요청."""
        throttle("trip-event", 20, 60)
        uid = current_user()["id"]
        message = text(body().get("message", "보호자의 도움이 필요해요."), "알림 내용", 500)
        active = db().execute("SELECT * FROM trips WHERE user_id=? AND status='active'", (uid,)).fetchone()
        with db():
            # 명시적인 SOS 요청은 현재 연결된 보호자에게 전달한다. 귀가 시작 후 연결된
            # 보호자에게도 도움을 요청하지만 trip_guardians를 추가하거나 위치를 공개하지 않는다.
            targets = db().execute("SELECT DISTINCT recipient_id FROM guardians WHERE sender_id=? AND status='accepted'", (uid,)).fetchall()
            for target in targets:
                notification_id = uuid4().hex
                g.push_recipients = getattr(g, 'push_recipients', []) + [(target[0], notification_id)]
                db().execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?)", (notification_id, active["id"] if active else None, uid, target[0], "sos", message, storage.utcnow()))
            notice = {"in_app": "recorded" if targets else "no_recipients", "recipients": len(targets),
                      "sms": {"status": "unavailable", "message": "문자 전송 서비스가 연결되지 않았어요. 필요하면 문자 앱에서 직접 보내 주세요."}}
        return jsonify(notification=notice, location_shared=bool(active and active["share_location"]))

    @app.get("/api/guardian/trips")
    @signed_in
    def guardian_trips():
        rows = db().execute("SELECT DISTINCT t.* FROM trips t JOIN trip_guardians tg ON tg.trip_id=t.id JOIN guardians g ON g.id=tg.guardian_id WHERE g.recipient_id=? AND g.status='accepted' AND g.sender_id=t.user_id AND t.status='active' AND t.share_location=1 ORDER BY t.started_at DESC", (current_user()["id"],)).fetchall()
        return jsonify(trips=[trip_view(row, guardian=True) for row in rows])

    @app.get('/api/push/key')
    @signed_in
    def push_key():
        return jsonify(public_key=push_service.keys(db())['public_key'])

    @app.post('/api/push/subscription')
    @signed_in
    def push_subscribe():
        throttle('push-subscribe', 20, 60)
        try:
            subscription = push_service.validate(body())
        except ValueError as error:
            raise APIError(str(error))
        uid = current_user()['id']
        endpoint = subscription['endpoint']
        with db():
            existing = db().execute('SELECT user_id FROM push_subscriptions WHERE endpoint=?', (endpoint,)).fetchone()
            if not existing and db().execute('SELECT count(*) FROM push_subscriptions WHERE user_id=?', (uid,)).fetchone()[0] >= 5:
                raise APIError('알림 기기는 최대 5대까지 등록할 수 있어요.')
            db().execute('INSERT INTO push_subscriptions VALUES(?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,subscription=excluded.subscription', (endpoint, uid, json.dumps(subscription)))
        session['push_endpoint'] = endpoint
        return jsonify(subscribed=True)

    @app.delete('/api/push/subscription')
    @signed_in
    def push_unsubscribe():
        with db():
            db().execute('DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?', (body().get('endpoint', ''), current_user()['id']))
        session.pop('push_endpoint', None)
        return jsonify(subscribed=False)

    @app.post('/api/push/test')
    @signed_in
    def push_test():
        throttle('push-test', 3, 60)
        accepted = push_service.send(db(), current_user()['id'], push_service.keys(db())['private_key'])
        return jsonify(accepted=accepted)

    @app.get('/api/notifications/<notification_id>')
    @signed_in
    def notification_detail(notification_id):
        row = db().execute('SELECT n.*,u.nickname AS sender_name FROM notifications n JOIN users u ON u.id=n.sender_id WHERE n.id=? AND n.recipient_id=?', (notification_id, current_user()['id'])).fetchone()
        if row is None:
            raise APIError('이 알림을 찾을 수 없어요. 알림을 받은 계정인지 확인해 주세요.', 404)
        return jsonify(notification=dict(row))

    @app.get("/api/notifications")
    @signed_in
    def notifications():
        rows = db().execute("SELECT n.id,n.kind,n.trip_id,n.message,n.created_at,u.nickname AS sender_name FROM notifications n JOIN users u ON n.sender_id=u.id WHERE n.recipient_id=? ORDER BY n.created_at DESC LIMIT 50", (current_user()["id"],)).fetchall()
        items = [dict(row, type=row["kind"], traveler_nickname=row["sender_name"]) for row in rows]
        return jsonify(notifications=items, sms_available=False)

    def report_view(row):
        uid = current_user()["id"] if current_user() else None
        count = db().execute("SELECT count(*) FROM confirmations WHERE report_id=?", (row["id"],)).fetchone()[0]
        confirmed = bool(uid and db().execute("SELECT 1 FROM confirmations WHERE report_id=? AND user_id=?", (row["id"], uid)).fetchone())
        result = {key: row[key] for key in ("id", "type", "title", "content", "address", "lat", "lon", "status", "created_at")}
        result.update(confirmation_count=count, confirmed_by_me=confirmed, is_mine=row["user_id"] == uid,
                      photo_url=f"/api/reports/{row['id']}/photo" if row["photo_name"] else None)
        return result

    @app.get("/api/reports")
    def reports():
        query, params = "SELECT * FROM reports", []
        if request.args.get("mine") == "1":
            if not current_user():
                raise APIError("로그인 후 내 제보를 볼 수 있어요.", 401)
            query += " WHERE user_id=?"
            params.append(current_user()["id"])
        elif "lat" in request.args or "lon" in request.args:
            lat, lon = coordinates(request.args)
            query += " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
            params.extend((lat - .02, lat + .02, lon - .025, lon + .025))
        rows = db().execute(query + " ORDER BY created_at DESC LIMIT 100", params).fetchall()
        return jsonify(reports=[report_view(row) for row in rows])

    @app.post("/api/reports")
    @signed_in
    def add_report():
        throttle("report", 10, 3600)
        data = body()
        lat, lon = coordinates(data)
        kind = text(data.get("type", "기타"), "제보 유형", 40)
        content = text(data.get("content"), "제보 내용", 1500)
        title = text(data.get("title", kind), "제목", 80)
        address = text(data.get("address", ""), "위치 설명", 300, False)
        rid = uuid4().hex
        with db():
            db().execute("INSERT INTO reports(id,user_id,type,title,content,address,lat,lon,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (rid, current_user()["id"], kind, title, content, address, lat, lon, storage.utcnow()))
        return jsonify(report=report_view(db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone())), 201

    @app.post("/api/reports/<rid>/confirm")
    @signed_in
    def confirm_report(rid):
        row = db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        if not row:
            raise APIError("제보를 찾을 수 없어요.", 404)
        uid = current_user()["id"]
        with db():
            exists = db().execute("SELECT 1 FROM confirmations WHERE report_id=? AND user_id=?", (rid, uid)).fetchone()
            if exists:
                db().execute("DELETE FROM confirmations WHERE report_id=? AND user_id=?", (rid, uid))
            else:
                db().execute("INSERT INTO confirmations VALUES(?,?)", (rid, uid))
        return jsonify(report=report_view(row))

    @app.post("/api/reports/<rid>/photo")
    @signed_in
    def upload_photo(rid):
        row = db().execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, current_user()["id"])).fetchone()
        if not row:
            raise APIError("내 제보만 수정할 수 있어요.", 404)
        upload = request.files.get("photo")
        if not upload:
            raise APIError("첨부할 사진을 선택해 주세요.")
        raw = upload.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            raise APIError("사진은 3MB 이하로 첨부해 주세요.", 413)
        try:
            picture = Image.open(io.BytesIO(raw))
            if picture.format not in {"JPEG", "PNG"} or picture.width * picture.height > 12_000_000:
                raise APIError("1200만 화소 이하 JPG·PNG 사진만 첨부할 수 있어요.")
            picture.load()
            picture = picture.convert("RGB")
            picture.thumbnail((1600, 1600))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise APIError("사진 파일을 읽을 수 없어요. JPG·PNG 파일을 확인해 주세요.")
        name = uuid4().hex + ".jpg"
        target = Path(app.config["PHOTO_DIR"]) / name
        # 새로 인코딩해 GPS/EXIF와 원본 파일명을 제거한다.
        picture.save(target, "JPEG", quality=85)
        with db():
            db().execute("UPDATE reports SET photo_name=? WHERE id=?", (name, rid))
        if row["photo_name"]:
            (Path(app.config["PHOTO_DIR"]) / row["photo_name"]).unlink(missing_ok=True)
        return jsonify(report=report_view(db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()))

    @app.get("/api/reports/<rid>/photo")
    def report_photo(rid):
        row = db().execute("SELECT photo_name FROM reports WHERE id=?", (rid,)).fetchone()
        if not row or not row[0]:
            raise APIError("사진을 찾을 수 없어요.", 404)
        return send_from_directory(app.config["PHOTO_DIR"], row[0], mimetype="image/jpeg")

    @app.get("/api/search")
    def search():
        import routing
        throttle("search", 60, 60)
        try:
            query = request.args.get("q", "")
            if "lat" in request.args or "lon" in request.args:
                lat, lon = coordinates(request.args)
                return jsonify(places=routing.search_places(query, lat=lat, lon=lon), sort="distance")
            return jsonify(places=routing.search_places(query), sort="relevance")
        except ValueError as error:
            raise APIError(str(error), 400)

    @app.get("/api/facilities")
    def facilities():
        import routing
        lat, lon = coordinates(request.args)
        try:
            radius = float(request.args.get("radius", 500))
            if not 50 <= radius <= 2000:
                raise ValueError("시설 검색 반경은 50~2000m로 설정해 주세요.")
            return jsonify(facilities=routing.nearby_facilities(lat, lon, radius))
        except ValueError as error:
            raise APIError(str(error), 400)

    @app.post("/api/routes")
    def routes():
        import routing
        throttle("routes", 20, 60)
        try:
            return jsonify(routing.compare_routes(body()))
        except ValueError as error:
            raise APIError(str(error), 400)

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "mobile", "index.html")

    @app.get("/<path:filename>")
    @app.get("/static/<path:filename>")
    def asset(filename):
        # DB, 환경 변수 파일, 기존 분석용 소스 등은 정적 파일 경로 밖에 둔다.
        return send_from_directory(ROOT / "mobile", filename)

    return app


app = create_app()
