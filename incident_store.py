"""Durable persistence for the incident agent (separate tables from the
mailroom agent so both can share one SQLite file / one deployed service)."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Optional

DB_PATH = os.environ.get("MAILROOM_DB_PATH", "/data/mailroom.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_lock = threading.RLock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    return conn


@contextmanager
def tx():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with tx() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS incident_runs (
                run_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                initial_response_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS incident_receipts (
                run_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, receipt_id)
            );
            """
        )


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT request_hash, initial_response_json, state_json FROM incident_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    request_hash, initial_response_json, state_json = row
    return {
        "requestHash": request_hash,
        "initialResponse": json.loads(initial_response_json),
        "state": json.loads(state_json),
    }


def create_run(run_id: str, request_hash: str, initial_response: Dict[str, Any], state: Dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO incident_runs (run_id, request_hash, initial_response_json, state_json) "
            "VALUES (?,?,?,?)",
            (run_id, request_hash, json.dumps(initial_response), json.dumps(state)),
        )


def update_state(run_id: str, state: Dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE incident_runs SET state_json=? WHERE run_id=?",
            (json.dumps(state), run_id),
        )


def get_receipt(run_id: str, receipt_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT content_hash, response_json FROM incident_receipts WHERE run_id=? AND receipt_id=?",
            (run_id, receipt_id),
        ).fetchone()
    if not row:
        return None
    content_hash, response_json = row
    return {"contentHash": content_hash, "response": json.loads(response_json)}


def put_receipt(run_id: str, receipt_id: str, content_hash: str, response: Dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO incident_receipts (run_id, receipt_id, content_hash, response_json) "
            "VALUES (?,?,?,?)",
            (run_id, receipt_id, content_hash, json.dumps(response)),
        )
