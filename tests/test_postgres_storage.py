"""Adapter contracts; no production database credentials are used."""
from unittest.mock import MagicMock

import psycopg
import pytest
import sqlite3

import storage


@pytest.fixture
def adapter(monkeypatch):
    connection = MagicMock()
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    db = storage.connect("postgresql://example.invalid/app")
    return db, connection, connect


def test_private_schema_tls_and_bound_parameters(adapter):
    db, connection, connect = adapter
    assert connect.call_args.kwargs["sslmode"] == "require"
    assert connect.call_args.kwargs["prepare_threshold"] is None
    connection.execute.assert_called_with("SET search_path TO comeback_private")
    value = "literal?quote'"
    db.execute("SELECT * FROM users WHERE email=?", (value,))
    connection.execute.assert_called_with("SELECT * FROM users WHERE email=%s", (value,))


def test_constraint_failure_rolls_back_transaction(adapter):
    db, connection, _ = adapter
    transaction = connection.transaction.return_value
    transaction.__exit__.return_value = False
    connection.execute.side_effect = psycopg.errors.UniqueViolation()
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute("INSERT INTO users VALUES(?)", ("duplicate",))
    assert transaction.__exit__.call_args.args[0] is sqlite3.IntegrityError
    assert db.transactions == []


def test_row_supports_existing_named_and_numeric_access(adapter):
    db, connection, _ = adapter
    connection.execute.return_value.fetchone.return_value = {"count": 3}
    row = db.execute("SELECT count(*) FROM users").fetchone()
    assert row[0] == row["count"] == 3
    assert dict(row) == {"count": 3}


def test_external_database_does_not_require_render_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER", "true")
    assert storage.account_storage_ready("postgresql://example.invalid/app", tmp_path)


def test_connection_failure_does_not_fall_back_to_sqlite(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError()))
    with pytest.raises(psycopg.OperationalError):
        storage.connect("postgresql://example.invalid/app")
