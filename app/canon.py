"""Canonical serialization + hashing.

Rule: two structurally-equal JSON values must hash identically regardless of
key order or incidental whitespace. We do NOT canonicalize float formatting
beyond what json.dumps gives us, since dossiers are exam-controlled JSON
(strings/ints/bools/lists/dicts) and are not expected to carry floats.
"""
import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_fingerprint(dossier_body: Any) -> str:
    """Fingerprint of a dossier's content (excluding volatile envelope fields
    like evaluationId). This is the cache key for LLM decisions."""
    return sha256_hex(canonical_json(dossier_body))


def call_id_for(dossier_id: str, fingerprint: str) -> str:
    """Deterministic callId: same dossier content -> same callId across
    evaluations and Checks, per the spec's stability requirement."""
    return "call_" + sha256_hex(f"{dossier_id}:{fingerprint}")[:32]


def input_digest(dossier_id: str, action: str, target: Any, payload: Any, evidence: Any) -> str:
    return sha256_hex(
        canonical_json({
            "dossierId": dossier_id,
            "action": action,
            "target": target,
            "payload": payload,
            "evidence": evidence,
        })
    )
