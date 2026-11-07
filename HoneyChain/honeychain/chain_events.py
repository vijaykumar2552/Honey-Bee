"""
honeychain.chain_events
=======================
THE chain-event service (Phase 6). From this phase on, every supply-chain
module records traceability events ONLY through ``record_event``; nothing
else writes to ``chain_events`` or talks to the blockchain.

    domain write (same transaction)
        -> record_event()          append to the hash-linked event log
    later / periodically
        -> create_checkpoint()     Merkle root over the un-checkpointed range
        -> submit_checkpoints()    anchor the root on-chain (chain_anchor)
    any time
        -> verify_log()            recompute every hash + cross-check domain
        -> verify_checkpoint()     local root recompute + on-chain comparison
        -> event_proof()           Merkle inclusion proof for one event

Honesty rules enforced here:

* A checkpoint is CONFIRMED only after chain_anchor returns a mined,
  status-1 receipt -- or after reading back an identical root already on
  chain. Every failure is stored as FAILED with its reason and retried up
  to CHECKPOINT_MAX_ATTEMPTS; nothing is ever reported as anchored when it
  was not.
* An on-chain root that DIFFERS from the local one is never "fixed": it is
  flagged as a mismatch, because either the local log or the chain record
  has been tampered with.
* Payloads carry identifiers and hashes only; keys that look like personal
  data or secrets are rejected at write time.

Attribution: ``actor_id`` is the authenticated user's (or device's) external
id, taken from the verified session by the caller -- never from a request
body -- and is covered by the event hash. Per-organization asymmetric
signatures are NOT implemented (they need per-organization key custody);
this is documented as a limitation rather than simulated.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets
import sqlite3
from typing import Any

from . import chain_anchor, config, db, ledger as ledger_module, merkle

logger = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "BATCH_CREATED", "PROCESSING_RECORDED", "LAB_RESULT_RECORDED", "PACKAGED",
    "CUSTODY_TRANSFERRED", "SHIPPED", "RECEIVED", "RETAIL_REGISTERED", "RECALLED",
)
GENESIS_PREVIOUS_HASH = "0" * 64
EVENT_HASHED_FIELDS: tuple[str, ...] = (
    "seq", "event_id", "event_type", "resource_type", "resource_id",
    "organization_id", "actor_id", "occurred_at", "payload_hash", "previous_hash",
)
#: Payload keys that must never enter the event log (personal data / secrets).
FORBIDDEN_PAYLOAD_KEYS = re.compile(
    r"(password|secret|token|private|email|phone|name|address|latitude|longitude|gps|notes)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class ChainEventError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def payload_hash(payload: dict[str, Any]) -> str:
    return ledger_module.sha256_hex(ledger_module.canonical_json(payload))


def compute_event_hash(event: dict[str, Any]) -> str:
    return ledger_module.sha256_hex(
        ledger_module.canonical_json({f: event.get(f) for f in EVENT_HASHED_FIELDS})
    )


def _validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not payload:
        raise ChainEventError("payload must be a non-empty object")
    for key, value in payload.items():
        if FORBIDDEN_PAYLOAD_KEYS.search(str(key)):
            raise ChainEventError(f"payload key '{key}' is not allowed in the event log")
        if not (value is None or isinstance(value, (str, int, float, bool))):
            raise ChainEventError(f"payload value for '{key}' must be a scalar")


# ---------------------------------------------------------------------------
# Recording (inside the caller's transaction)
# ---------------------------------------------------------------------------

def record_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    resource_type: str,
    resource_id: str,
    organization_id: str | None,
    actor_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Append one event and return it. MUST be called inside an open write
    transaction (the caller's), so the event commits or rolls back together
    with the domain change it describes. Idempotent: an identical
    (event_type, resource_type, resource_id) returns the existing event."""
    if event_type not in EVENT_TYPES:
        raise ChainEventError(f"unknown event type '{event_type}'")
    for label, value in (("resource_type", resource_type), ("resource_id", resource_id)):
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise ChainEventError(f"invalid {label}")
    _validate_payload(payload)

    existing = conn.execute(
        "SELECT * FROM chain_events WHERE event_type = ? AND resource_type = ? AND resource_id = ?;",
        (event_type, resource_type, resource_id),
    ).fetchone()
    if existing is not None:
        return dict(existing)

    tip = conn.execute("SELECT seq, event_hash FROM chain_events ORDER BY seq DESC LIMIT 1;").fetchone()
    event: dict[str, Any] = {
        "seq": (tip["seq"] + 1) if tip else 1,
        "event_id": f"EVT-{_secrets.token_hex(8).upper()}",
        "event_type": event_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "organization_id": organization_id,
        "actor_id": actor_id,
        "occurred_at": db.utc_now(),
        "payload": ledger_module.canonical_json(payload),
        "payload_hash": payload_hash(payload),
        "previous_hash": tip["event_hash"] if tip else GENESIS_PREVIOUS_HASH,
    }
    event["event_hash"] = compute_event_hash(event)
    conn.execute(
        "INSERT INTO chain_events (seq, event_id, event_type, resource_type, resource_id, "
        " organization_id, actor_id, occurred_at, payload, payload_hash, previous_hash, event_hash) "
        "VALUES (:seq, :event_id, :event_type, :resource_type, :resource_id, :organization_id, "
        " :actor_id, :occurred_at, :payload, :payload_hash, :previous_hash, :event_hash);",
        event,
    )
    return event


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_event(event_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM chain_events WHERE event_id = ?;", (event_id,))
    return dict(row) if row else None


def events_for_resource(resource_type: str, resource_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query_all(
        "SELECT * FROM chain_events WHERE resource_type = ? AND resource_id = ? ORDER BY seq;",
        (resource_type, resource_id))]


def list_events(*, organization_id: str | None = None, restrict: bool = False,
                limit: int = 200) -> list[dict[str, Any]]:
    if restrict:
        rows = db.query_all("SELECT * FROM chain_events WHERE organization_id IS ? "
                            "ORDER BY seq DESC LIMIT ?;", (organization_id, int(limit)))
    else:
        rows = db.query_all("SELECT * FROM chain_events ORDER BY seq DESC LIMIT ?;", (int(limit),))
    return [dict(r) for r in rows]


def serialize_event(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row[k] for k in ("seq", "event_id", "event_type", "resource_type", "resource_id",
                               "organization_id", "actor_id", "occurred_at", "payload_hash",
                               "previous_hash", "event_hash")}
    out["payload"] = json.loads(row["payload"])
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _domain_check(event: dict[str, Any], payload: dict[str, Any]) -> str | None:
    """Cross-check an event against the live domain records it describes."""
    if event["event_type"] == "BATCH_CREATED":
        batch = ledger_module.ledger.get_batch(event["resource_id"])
        if batch is None:
            return "referenced batch no longer exists"
        if batch["block_hash"] != payload.get("block_hash"):
            return "batch block_hash differs from the value recorded in the event"
        if ledger_module.compute_block_hash(batch) != batch["block_hash"]:
            return "batch block contents no longer match its block_hash"
        harvest_id = payload.get("harvest_id")
        if harvest_id:
            harvest = db.get_harvest(harvest_id)
            if harvest is None or harvest.get("event_hash") != payload.get("harvest_event_hash"):
                return "linked harvest event hash differs from the recorded value"
    elif event["event_type"] == "LAB_RESULT_RECORDED":
        from . import supply_chain  # local import: supply_chain depends on this module
        row = db.query_one("SELECT * FROM lab_results WHERE lab_result_id = ?;", (event["resource_id"],))
        if row is None:
            return "referenced lab result no longer exists"
        row = dict(row)
        if row.get("result_hash") != payload.get("result_hash") or row["status"] != payload.get("verdict"):
            return "lab result differs from the value recorded in the event"
        if supply_chain.lab_result_hash(row) != row["result_hash"]:
            return "lab result contents no longer match their result_hash"
    elif event["event_type"] == "PACKAGED":
        from . import supply_chain
        units = [r["unit_id"] for r in db.query_all(
            "SELECT unit_id FROM product_units WHERE packaging_id = ?;", (event["resource_id"],))]
        if len(units) != payload.get("unit_count") or \
                supply_chain.units_digest(units) != payload.get("units_digest"):
            return "product units differ from the set recorded at packaging"
    return None


def verify_log() -> dict[str, Any]:
    """Recompute every payload hash, event hash and link; check seq is
    contiguous; cross-check the domain records events refer to."""
    rows = [dict(r) for r in db.query_all("SELECT * FROM chain_events ORDER BY seq;")]
    problems: list[dict[str, Any]] = []
    expected_prev, expected_seq = GENESIS_PREVIOUS_HASH, 1
    for row in rows:
        issue = None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload, issue = {}, "payload is not valid JSON"
        if issue is None and row["seq"] != expected_seq:
            issue = f"sequence gap: expected {expected_seq}"
        elif issue is None and payload_hash(payload) != row["payload_hash"]:
            issue = "payload does not match payload_hash"
        elif issue is None and row["previous_hash"] != expected_prev:
            issue = "previous_hash does not link to the preceding event"
        elif issue is None and compute_event_hash(row) != row["event_hash"]:
            issue = "event contents do not match event_hash"
        elif issue is None:
            issue = _domain_check(row, payload)
        if issue:
            problems.append({"seq": row["seq"], "event_id": row["event_id"], "issue": issue})
        expected_prev, expected_seq = row["event_hash"], row["seq"] + 1
    return {"valid": not problems, "event_count": len(rows), "problems": problems}


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def _range_hashes(from_seq: int, to_seq: int) -> list[str]:
    return [r["event_hash"] for r in db.query_all(
        "SELECT event_hash FROM chain_events WHERE seq BETWEEN ? AND ? ORDER BY seq;",
        (from_seq, to_seq))]


def create_checkpoint() -> dict[str, Any] | None:
    """Freeze every event not yet in a checkpoint into a new PENDING
    checkpoint. Returns None when there is nothing new."""
    with db.transaction() as conn:
        last = conn.execute("SELECT MAX(to_seq) AS m FROM blockchain_checkpoints "
                            "WHERE from_seq IS NOT NULL;").fetchone()["m"] or 0
        tip = conn.execute("SELECT MAX(seq) AS m FROM chain_events;").fetchone()["m"] or 0
        if tip <= last:
            return None
        from_seq, to_seq = last + 1, tip
        hashes = [r["event_hash"] for r in conn.execute(
            "SELECT event_hash FROM chain_events WHERE seq BETWEEN ? AND ? ORDER BY seq;",
            (from_seq, to_seq))]
        checkpoint_id = f"CHK-{from_seq:08d}-{to_seq:08d}"
        conn.execute(
            "INSERT INTO blockchain_checkpoints (checkpoint_id, checkpoint_type, entity_type, "
            " entity_id, status, created_at, from_seq, to_seq, merkle_root, attempts, contract_address) "
            "VALUES (?, 'MERKLE_CHECKPOINT', 'chain_events', ?, 'PENDING', ?, ?, ?, ?, 0, ?);",
            (checkpoint_id, f"{from_seq}-{to_seq}", db.utc_now(), from_seq, to_seq,
             merkle.merkle_root(hashes), config.CONTRACT_ADDRESS),
        )
    return get_checkpoint(checkpoint_id)


def get_checkpoint(checkpoint_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM blockchain_checkpoints WHERE checkpoint_id = ?;",
                       (checkpoint_id,))
    return dict(row) if row else None


def list_checkpoints(limit: int = 100) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query_all(
        "SELECT * FROM blockchain_checkpoints WHERE from_seq IS NOT NULL "
        "ORDER BY from_seq DESC LIMIT ?;", (int(limit),))]


def _update(checkpoint_id: str, **fields: Any) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.transaction() as conn:
        conn.execute(f"UPDATE blockchain_checkpoints SET {cols} WHERE checkpoint_id = ?;",
                     (*fields.values(), checkpoint_id))


def _mark_batches_anchored(from_seq: int, to_seq: int, tx_hash: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE honey_batches SET anchor_tx_hash = ? WHERE anchor_tx_hash IS NULL AND batch_id IN "
            "(SELECT resource_id FROM chain_events WHERE event_type = 'BATCH_CREATED' "
            " AND seq BETWEEN ? AND ?);", (tx_hash, from_seq, to_seq))


def submit_checkpoints(anchor: Any = chain_anchor) -> list[dict[str, Any]]:
    """Try to anchor every PENDING / retryable FAILED checkpoint, oldest first.

    ``anchor`` is the blockchain client (``honeychain.chain_anchor`` in
    production; tests may pass a stand-in with the same two functions).
    Returns a per-checkpoint outcome list. Stops at the first checkpoint that
    cannot be anchored, so roots always land on chain in order.
    """
    outcomes: list[dict[str, Any]] = []
    candidates = db.query_all(
        "SELECT * FROM blockchain_checkpoints WHERE from_seq IS NOT NULL "
        " AND (status = 'PENDING' OR (status = 'FAILED' AND attempts < ?)) ORDER BY from_seq;",
        (config.CHECKPOINT_MAX_ATTEMPTS,))
    for cp in (dict(r) for r in candidates):
        cid = cp["checkpoint_id"]
        # Re-derive the root from the log right now: never anchor a root the
        # current log no longer supports.
        root = merkle.merkle_root(_range_hashes(cp["from_seq"], cp["to_seq"]))
        if root != cp["merkle_root"]:
            _update(cid, status="FAILED", attempts=cp["attempts"] + 1, last_attempt_at=db.utc_now(),
                    last_error="local event log no longer matches this checkpoint's root")
            outcomes.append({"checkpoint_id": cid, "status": "FAILED", "error": "local log changed"})
            break
        try:
            onchain = anchor.read_checkpoint(cid)
            if onchain is not None:
                # Already anchored (e.g. a previous attempt timed out after mining).
                if onchain["merkle_root"] != root:
                    _update(cid, status="FAILED", attempts=cp["attempts"] + 1,
                            last_attempt_at=db.utc_now(),
                            last_error="ON-CHAIN ROOT DIFFERS from local root -- investigate")
                    outcomes.append({"checkpoint_id": cid, "status": "FAILED", "error": "root mismatch"})
                    break
                _update(cid, status="CONFIRMED", block_number=onchain["block_number"],
                        chain_id=config.CHAIN_ID, confirmed_at=db.utc_now(), last_error=None,
                        last_attempt_at=db.utc_now())
                outcomes.append({"checkpoint_id": cid, "status": "CONFIRMED", "already_on_chain": True})
                continue
            _update(cid, status="SUBMITTED", attempts=cp["attempts"] + 1, last_attempt_at=db.utc_now())
            result = anchor.anchor_checkpoint(cid, cp["from_seq"], cp["to_seq"], root)
        except chain_anchor.AnchorError as exc:
            tx = getattr(exc, "tx_hash", None)
            _update(cid, status="FAILED", last_error=str(exc)[:300],
                    tx_hash=("0x" + tx) if tx and not tx.startswith("0x") else tx,
                    attempts=cp["attempts"] + 1, last_attempt_at=db.utc_now())
            db.insert_audit_event(actor_user_pk=None, organization_pk=None,
                                  action="CHECKPOINT_ANCHOR_FAILED", resource_type="blockchain_checkpoints",
                                  resource_id=cid, metadata=str(exc)[:300], result="FAILURE")
            outcomes.append({"checkpoint_id": cid, "status": "FAILED", "error": str(exc)})
            break
        _update(cid, status="CONFIRMED", tx_hash=result["tx_hash"], block_number=result["block_number"],
                chain_id=result["chain_id"], confirmed_at=db.utc_now(), last_error=None)
        _mark_batches_anchored(cp["from_seq"], cp["to_seq"], result["tx_hash"])
        db.insert_audit_event(actor_user_pk=None, organization_pk=None, action="CHECKPOINT_ANCHORED",
                              resource_type="blockchain_checkpoints", resource_id=cid,
                              metadata=f"tx={result['tx_hash']} block={result['block_number']}",
                              result="SUCCESS")
        outcomes.append({"checkpoint_id": cid, "status": "CONFIRMED", **result})
    return outcomes


def verify_checkpoint(checkpoint_id: str, anchor: Any = chain_anchor) -> dict[str, Any]:
    cp = get_checkpoint(checkpoint_id)
    if cp is None or cp.get("from_seq") is None:
        return {"checkpoint_id": checkpoint_id, "found": False}
    recomputed = merkle.merkle_root(_range_hashes(cp["from_seq"], cp["to_seq"]))
    local_ok = recomputed == cp["merkle_root"]
    onchain_status, onchain_root = "NOT_ANCHORED", None
    if cp["status"] == "CONFIRMED":
        try:
            onchain = anchor.read_checkpoint(checkpoint_id)
            if onchain is None:
                onchain_status = "MISSING_ON_CHAIN"
            else:
                onchain_root = onchain["merkle_root"]
                onchain_status = "VERIFIED" if onchain_root == cp["merkle_root"] == recomputed else "MISMATCH"
        except chain_anchor.AnchorError as exc:
            onchain_status = f"UNAVAILABLE: {exc}"
    return {
        "checkpoint_id": checkpoint_id, "found": True, "status": cp["status"],
        "from_seq": cp["from_seq"], "to_seq": cp["to_seq"],
        "stored_root": cp["merkle_root"], "recomputed_root": recomputed,
        "local_root_valid": local_ok, "onchain": onchain_status, "onchain_root": onchain_root,
        "tx_hash": cp.get("tx_hash"), "block_number": cp.get("block_number"),
        "chain_id": cp.get("chain_id"),
    }


def event_proof(event_id: str) -> dict[str, Any] | None:
    event = get_event(event_id)
    if event is None:
        return None
    cp = db.query_one("SELECT * FROM blockchain_checkpoints WHERE from_seq IS NOT NULL "
                      "AND ? BETWEEN from_seq AND to_seq;", (event["seq"],))
    if cp is None:
        return {"event_id": event_id, "event_hash": event["event_hash"], "checkpoint": None,
                "note": "event is not yet included in a checkpoint"}
    cp = dict(cp)
    hashes = _range_hashes(cp["from_seq"], cp["to_seq"])
    proof = merkle.merkle_proof(hashes, event["seq"] - cp["from_seq"])
    return {"event_id": event_id, "event_hash": event["event_hash"],
            "checkpoint_id": cp["checkpoint_id"], "checkpoint_status": cp["status"],
            "merkle_root": cp["merkle_root"], "proof": proof,
            "proof_valid": merkle.verify_proof(event["event_hash"], proof, cp["merkle_root"]),
            "tx_hash": cp.get("tx_hash"), "chain_id": cp.get("chain_id")}
