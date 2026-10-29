"""SQLite storage for LostLink.

Public item details and confidential details live in SEPARATE tables
(lost_secrets, found_hidden_marks). Agent-facing MCP tools only ever read
the public tables; confidential tables are read only inside the server
(for blind verification) or by officer-only tools.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "lostlink.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    reg_no TEXT DEFAULT '',                       -- login id (register number, or 'security')
    password_hash TEXT DEFAULT '',                -- PBKDF2-SHA256, never stored in plain text
    salt TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lost_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    category TEXT NOT NULL,
    color TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    location TEXT DEFAULT '',
    date_lost TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',          -- open | matched | recovered
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lost_secrets (           -- CONFIDENTIAL
    lost_id INTEGER PRIMARY KEY REFERENCES lost_items(id),
    details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS found_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finder_id TEXT NOT NULL,
    category TEXT NOT NULL,
    color TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    location TEXT DEFAULT '',
    date_found TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'in_office',     -- in_office | claim_pending | returned
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS found_hidden_marks (     -- CONFIDENTIAL
    found_id INTEGER PRIMARY KEY REFERENCES found_items(id),
    marks TEXT NOT NULL                              -- one mark per line
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_id INTEGER NOT NULL REFERENCES found_items(id),
    claimant_id TEXT NOT NULL,
    lost_id INTEGER,
    answers TEXT NOT NULL,
    confidence REAL,
    verdict TEXT NOT NULL,          -- strong_match | partial_match | manual_check | no_match
    status TEXT NOT NULL,           -- awaiting_officer | approved | rejected | failed_verification
    thread_id TEXT DEFAULT '',
    officer_note TEXT DEFAULT '',
    pickup_code TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    actor TEXT DEFAULT '',
    summary TEXT DEFAULT ''
);
"""


def db_path() -> Path:
    return Path(os.getenv("LOSTLINK_DB", str(DEFAULT_DB)))


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the database, commit on success, and ALWAYS close the connection.

    (sqlite3's own `with conn:` only commits; it leaves the file open, which on
    Windows blocks other processes from replacing it.)
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Upgrade databases created before login existed.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for col in ("reg_no", "password_hash", "salt"):
            if col not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")


PBKDF2_ROUNDS = 100_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hash, salt). Passwords are salted and hashed; the plain text is never stored."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return digest.hex(), salt


def check_password(password: str, password_hash: str, salt: str) -> bool:
    if not password_hash or not salt:
        return False
    return secrets.compare_digest(hash_password(password, salt)[0], password_hash)


def lost_code(item_id: int) -> str:
    return f"L{item_id:03d}"


def found_code(item_id: int) -> str:
    return f"F{item_id:03d}"


def claim_code(claim_id: int) -> str:
    return f"C{claim_id:03d}"


def parse_code(code: str | int, prefix: str) -> int:
    """Accept 'F003', 'f3', '3' or 3 and return the integer id."""
    text = str(code).strip().upper()
    if text.startswith(prefix):
        text = text[len(prefix):]
    text = text.lstrip("-# ")
    if not text.isdigit():
        raise ValueError(f"'{code}' is not a valid {prefix}-code (example: {prefix}001)")
    return int(text)


def log(conn: sqlite3.Connection, tool: str, actor: str, summary: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, tool, actor, summary) VALUES (?, ?, ?, ?)",
        (now(), tool, actor or "", summary[:500]),
    )


def notify(conn: sqlite3.Connection, user_id: str, message: str) -> None:
    conn.execute(
        "INSERT INTO notifications (user_id, message, created_at) VALUES (?, ?, ?)",
        (user_id, message, now()),
    )


def user_name(conn: sqlite3.Connection, user_id: str) -> str:
    row = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["name"] if row else user_id
