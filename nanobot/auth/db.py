"""SQLite database connection and schema management."""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups_ (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups_(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY (group_id, user_id)
);
"""


def _get_db_path() -> Path:
    from nanobot.config.paths import get_data_dir

    return get_data_dir() / "auth.db"


def get_db() -> sqlite3.Connection:
    global _DB, _DB_PATH
    if _DB is not None:
        return _DB
    with _LOCK:
        if _DB is not None:
            return _DB
        db_path = _get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(db_path), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute("PRAGMA foreign_keys=ON")
        _DB_PATH = db_path
        _init_schema(_DB)
        _maybe_create_default_admin(_DB)
        return _DB


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def _maybe_create_default_admin(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT COUNT(*) as c FROM users").fetchone()
    if row and row["c"] > 0:
        return
    import secrets
    import string
    from datetime import datetime, timezone

    from nanobot.auth.password import hash_password

    password = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
    )
    hash_result = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO users (id, username, password_hash, display_name, role, is_active, settings, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _new_id(),
            "admin",
            hash_result,
            "Administrator",
            "admin",
            1,
            "{}",
            now,
            now,
        ),
    )
    db.commit()
    logger.warning(
        "==========================================================\n"
        "  Default admin account created:\n"
        "    Username: admin\n"
        "    Password: %s"
        "\n  Change this password after first login!\n"
        "==========================================================",
        password,
    )


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


def close_db() -> None:
    global _DB, _DB_PATH
    with _LOCK:
        if _DB is not None:
            _DB.close()
            _DB = None
            _DB_PATH = None
