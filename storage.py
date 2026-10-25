"""귀가ON 데이터 저장: 계정/연결/최신 공유 위치만 보관하며 이동 궤적은 저장하지 않는다."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
 password_hash TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_keys (
 id INTEGER PRIMARY KEY, private_key TEXT NOT NULL, public_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
 endpoint TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 subscription TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardians (
 id TEXT PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(sender_id != recipient_id)
);
CREATE INDEX IF NOT EXISTS guardians_people ON guardians(sender_id, recipient_id, status);
CREATE TABLE IF NOT EXISTS trips (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 destination TEXT NOT NULL, route TEXT NOT NULL, share_location INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'active', lat REAL, lon REAL, location_updated_at TEXT,
 started_at TEXT NOT NULL, eta TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS trips_owner_status ON trips(user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_trip_per_user ON trips(user_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS trip_guardians (
 trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
 guardian_id TEXT NOT NULL REFERENCES guardians(id) ON DELETE CASCADE,
 PRIMARY KEY(trip_id, guardian_id)
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, trip_id TEXT REFERENCES trips(id) ON DELETE CASCADE,
 sender_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, address TEXT NOT NULL,
 lat REAL NOT NULL, lon REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 photo_name TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
 report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 PRIMARY KEY(report_id, user_id)
);
"""

DEFAULT_SETTINGS = {
    "share_location": False,
    "notifications": True,
    "shake_sos": False,
    "facility_filters": ["CCTV", "가로등", "비상벨", "경찰시설", "편의점"],
    "detour_percent": 30,
    "max_extra_minutes": 20,
    "avoid_stairs": False,
}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def account_storage_ready(database_path, data_dir):
    """Render에서는 외부 PostgreSQL 또는 실제 연결된 디스크에 계정을 보관한다.

    디렉터리 생성이나 환경 변수 지정만으로는 영구 저장소가 생기지 않는다.
    기본 디스크 경로는 /var/data이고, 다른 경로를 쓰면 명시적으로 지정한다.
    로컬 개발의 SQLite 파일은 기존 경로를 그대로 사용한다.
    """
    if is_postgres(database_path):
        return True
    if not os.environ.get("RENDER"):
        return True
    mount = Path(os.environ.get("COMEBACK_PERSISTENT_MOUNT", "/var/data")).resolve()
    return (
        mount != Path(mount.anchor)
        and mount.is_dir()
        and os.path.ismount(mount)
        and Path(database_path).resolve().is_relative_to(mount)
        and Path(data_dir).resolve().is_relative_to(mount)
    )


def is_postgres(value):
    return str(value).startswith(("postgresql://", "postgres://"))


class DatabaseRow(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class PostgresConnection:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.connection = psycopg.connect(url, autocommit=True, row_factory=dict_row,
                                         connect_timeout=10, sslmode="require", prepare_threshold=None)
        self.connection.execute("SET search_path TO comeback_private")
        self.transactions = []

    def execute(self, query, parameters=()):
        import psycopg
        try:
            cursor = self.connection.execute(query.replace("?", "%s"), parameters)
        except psycopg.IntegrityError as error:
            raise sqlite3.IntegrityError("Database constraint violation") from error
        return PostgresCursor(cursor)

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, *args):
        return self.transactions.pop().__exit__(*args)

    def close(self):
        self.connection.close()


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return DatabaseRow(row) if row is not None else None

    def fetchall(self):
        return [DatabaseRow(row) for row in self.cursor.fetchall()]


def connect(path):
    if is_postgres(path):
        return PostgresConnection(path)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(path):
    db = connect(path)
    try:
        with db:
            if is_postgres(path):
                db.execute("CREATE SCHEMA IF NOT EXISTS comeback_private")
                db.execute("REVOKE ALL ON SCHEMA comeback_private FROM PUBLIC")
                for statement in SCHEMA.replace("PRAGMA foreign_keys = ON;", "").replace(" REAL", " DOUBLE PRECISION").split(";"):
                    if statement.strip():
                        db.execute(statement)
            else:
                db.executescript(SCHEMA)
                db.execute("PRAGMA journal_mode = WAL")
    finally:
        db.close()


def settings_for(row):
    settings = dict(DEFAULT_SETTINGS)
    if row:
        settings.update(json.loads(row["settings"]))
    return settings


def user_view(row):
    if not row:
        return None
    return {key: row[key] for key in ("id", "email", "nickname")}
