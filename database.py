"""SQLite persistence. Data survives restarts."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from config import settings

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    student_id   TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,
    status       TEXT NOT NULL,
    submitted_at TEXT,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
"""


class DuplicateApplicationError(Exception):
    pass


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def _dump(record: Dict[str, Any]) -> str:
    return json.dumps(record, default=str, ensure_ascii=False)


def create_application(record: Dict[str, Any], token_hash: str) -> None:
    with _write_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO applications (student_id, token_hash, status, submitted_at, data) VALUES (?, ?, ?, ?, ?)",
                (record["student_id"], token_hash, record["status"], record.get("submitted_at"), _dump(record)),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateApplicationError(record["student_id"]) from exc


def update_application(record: Dict[str, Any]) -> None:
    with _write_lock, _connect() as conn:
        conn.execute(
            "UPDATE applications SET status = ?, submitted_at = ?, data = ? WHERE student_id = ?",
            (record["status"], record.get("submitted_at"), _dump(record), record["student_id"]),
        )


def set_token_hash(student_id: str, token_hash: str) -> None:
    with _write_lock, _connect() as conn:
        conn.execute("UPDATE applications SET token_hash = ? WHERE student_id = ?", (token_hash, student_id))


def get_application(student_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM applications WHERE student_id = ?", (student_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def get_token_hash(student_id: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT token_hash FROM applications WHERE student_id = ?", (student_id,)).fetchone()
    return row["token_hash"] if row else None


def list_applications(status: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT data FROM applications"
    params: tuple = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY submitted_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [json.loads(r["data"]) for r in rows]
