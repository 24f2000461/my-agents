"""Durable persistence.

Uses SQLite (WAL mode) as the single source of truth so nothing lives only
in process memory. A single file works fine for one instance; if you deploy
with multiple replicas, point DB_PATH at a shared volume or swap this module
for Postgres (same table shapes).
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

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
            CREATE TABLE IF NOT EXISTS dossier_decisions (
                dossier_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                call_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (dossier_id, fingerprint)
            );

            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id TEXT PRIMARY KEY,
                dossier_set_hash TEXT NOT NULL,
                proposals_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_receipts',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS receipts (
                evaluation_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL,
                verification_key TEXT,
                result TEXT NOT NULL,
                detail TEXT,
                effect_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (evaluation_id, call_id)
            );

            CREATE TABLE IF NOT EXISTS commit_replies (
                evaluation_id TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )


# ---- dossier decision cache -------------------------------------------------

def get_cached_decision(dossier_id: str, fingerprint: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT call_id, action, target_json, payload_json, evidence_json, input_digest "
            "FROM dossier_decisions WHERE dossier_id=? AND fingerprint=?",
            (dossier_id, fingerprint),
        ).fetchone()
    if not row:
        return None
    call_id, action, target_json, payload_json, evidence_json, digest = row
    return {
        "callId": call_id,
        "action": action,
        "target": json.loads(target_json),
        "payload": json.loads(payload_json),
        "evidence": json.loads(evidence_json),
        "inputDigest": digest,
    }


def put_cached_decision(dossier_id: str, fingerprint: str, call_id: str, action: str,
                         target: Dict[str, Any], payload: Dict[str, Any],
                         evidence: List[Dict[str, Any]], digest: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dossier_decisions "
            "(dossier_id, fingerprint, call_id, action, target_json, payload_json, evidence_json, input_digest) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dossier_id, fingerprint, call_id, action, json.dumps(target), json.dumps(payload),
             json.dumps(evidence), digest),
        )


# ---- evaluations -------------------------------------------------------------

def get_evaluation(evaluation_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT dossier_set_hash, proposals_json, status FROM evaluations WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
    if not row:
        return None
    dossier_set_hash, proposals_json, status = row
    return {
        "dossierSetHash": dossier_set_hash,
        "proposals": json.loads(proposals_json),
        "status": status,
    }


def put_evaluation(evaluation_id: str, dossier_set_hash: str, proposals: List[Dict[str, Any]]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO evaluations (evaluation_id, dossier_set_hash, proposals_json) "
            "VALUES (?,?,?)",
            (evaluation_id, dossier_set_hash, json.dumps(proposals)),
        )


# ---- receipts ----------------------------------------------------------------

def get_receipt(evaluation_id: str, call_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT receipt_id, verification_key, result, detail, effect_json "
            "FROM receipts WHERE evaluation_id=? AND call_id=?",
            (evaluation_id, call_id),
        ).fetchone()
    if not row:
        return None
    receipt_id, vkey, result, detail, effect_json = row
    return {
        "receiptId": receipt_id,
        "verificationKey": vkey,
        "result": result,
        "detail": detail,
        "effect": json.loads(effect_json) if effect_json else None,
    }


def put_receipt(evaluation_id: str, call_id: str, receipt_id: str, verification_key: Optional[str],
                 result: str, detail: Optional[str], effect: Optional[Dict[str, Any]]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO receipts "
            "(evaluation_id, call_id, receipt_id, verification_key, result, detail, effect_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (evaluation_id, call_id, receipt_id, verification_key, result, detail,
             json.dumps(effect) if effect is not None else None),
        )


def get_commit_reply(evaluation_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        row = conn.execute(
            "SELECT response_json FROM commit_replies WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def put_commit_reply(evaluation_id: str, response: Dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO commit_replies (evaluation_id, response_json) VALUES (?,?)",
            (evaluation_id, json.dumps(response)),
        )
