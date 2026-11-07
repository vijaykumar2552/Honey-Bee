"""
honeychain.supply_chain
=======================
The complete honey supply chain (Phase 8):

    batch -> processing / blending -> lab request -> lab result -> packaging
          -> product units -> custody transfer -> shipment -> retail
          -> on shelf -> sold          (recall can interrupt at any point)

Rules applied everywhere:

* The acting organization always comes from the authenticated user, never
  from the request body. Only the CURRENT HOLDER of a batch may process,
  blend, package, request tests on, or transfer it; ADMIN/AUTHORITY oversee
  but do not act as a supply-chain party.
* Every state change is a conditional UPDATE (``WHERE status = <expected>``)
  inside one transaction, so a replayed or racing request finds nothing to
  change and gets 409 instead of acting twice.
* Every lifecycle event is recorded through honeychain.chain_events in the
  SAME transaction (no second hashing scheme, no blockchain calls here).
  Anchoring happens later via Merkle checkpoints and is reported honestly by
  that service.
* Provenance is never overwritten: processing appends events; blending
  creates a NEW ledger batch and records every input batch and weight;
  consumed inputs remain in the ledger, marked CONSUMED.
* Nothing is inferred: lab results exist only when a laboratory submitted
  them in answer to a request; "not tested" is reported as NOT_TESTED.
"""

from __future__ import annotations

import base64
import json
import re
import secrets as _secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import chain_events, unit_qr, db, ledger as ledger_module, validation

HOLDER_ROLES = ("BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LOGISTICS", "RETAILER")
RECEIVER_ORG_TYPES = ("BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LOGISTICS", "RETAILER")
PROCESS_TYPES = ("FILTERING", "HEATING", "SETTLING", "STORAGE")
LAB_VERDICTS = ("PASSED", "FAILED", "REVIEW")
SHIPMENT_TRANSITIONS = {
    "CREATED": ("IN_TRANSIT", "EXCEPTION"),
    "IN_TRANSIT": ("DELIVERED", "EXCEPTION"),
    "EXCEPTION": ("IN_TRANSIT",),
    "DELIVERED": (),
}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
EPS = 1e-9


class SupplyChainError(Exception):
    def __init__(self, status: int, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.message, self.details = status, message, details


def _err(status: int, message: str, field: str | None = None, issue: str | None = None):
    return SupplyChainError(status, message, [{"field": field, "issue": issue}] if field else None)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{_secrets.token_hex(6).upper()}"


def _hash(obj: dict[str, Any]) -> str:
    return ledger_module.sha256_hex(ledger_module.canonical_json(obj))


def _check_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not ID_RE.match(text):
        raise _err(400 if value is not None else 400, f"invalid {label}", label, "format")
    return text


def _org(conn, pk: int | None) -> dict[str, Any] | None:
    if pk is None:
        return None
    row = conn.execute("SELECT * FROM organizations WHERE id = ?;", (pk,)).fetchone()
    return dict(row) if row else None


def _org_ext(conn, pk: int | None) -> str | None:
    org = _org(conn, pk)
    return org["org_id"] if org else None


def _ext(pk: int | None) -> str | None:
    """Read-only org external id (no transaction / write lock)."""
    if pk is None:
        return None
    row = db.query_one("SELECT org_id FROM organizations WHERE id = ?;", (pk,))
    return row["org_id"] if row else None


def _audit(user: dict, org_pk: int | None, action: str, rtype: str, rid: str, meta: str = "",
           ip: str | None = None) -> None:
    db.insert_audit_event(actor_user_pk=user["id"], organization_pk=org_pk, action=action,
                          resource_type=rtype, resource_id=rid, metadata=meta[:500],
                          ip_address=ip, result="SUCCESS")


def _num(payload: dict, key: str, low: float, high: float, *, required=False) -> float | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise _err(400, f"{key} is required", key, "missing")
        return None
    if isinstance(value, bool):
        raise _err(400, f"{key} must be a number", key, "type")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _err(400, f"{key} must be a number", key, "type") from None
    if number != number or not low <= number <= high:
        raise _err(400, f"{key} must be between {low} and {high}", key, "out_of_range")
    return round(number, 4)


def _text(payload: dict, key: str, limit: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise _err(400, f"{key} must be at most {limit} characters", key, "too_long")
    return text or None


def _time(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    parsed = validation.parse_device_timestamp(value)
    if parsed is None:
        raise _err(400, f"{key} must be an ISO-8601 datetime", key, "format")
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise _err(400, f"{key} cannot be in the future", key, "future")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Batch state / custody holder
# ---------------------------------------------------------------------------

def _batch_row(conn, batch_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM honey_batches WHERE batch_id = ?;", (batch_id,)).fetchone()
    if row is None:
        raise _err(404, f"No batch '{batch_id}'")
    return dict(row)


def _state(conn, batch_id: str) -> dict[str, Any]:
    """batch_state row, created on first use from the hive owner and ledger
    weight (batches from before Phase 8 have none yet)."""
    row = conn.execute("SELECT * FROM batch_state WHERE batch_id = ?;", (batch_id,)).fetchone()
    if row is not None:
        return dict(row)
    batch = _batch_row(conn, batch_id)
    owner = conn.execute("SELECT organization_id FROM hives WHERE hive_id = ?;",
                         (batch["hive_id"],)).fetchone()
    if owner is None or owner["organization_id"] is None:
        raise _err(409, "This batch has no owning organization and cannot enter the supply chain")
    conn.execute("INSERT INTO batch_state (batch_id, origin_org_id, holder_org_id, remaining_kg, "
                 "status, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?);",
                 (batch_id, owner["organization_id"], owner["organization_id"],
                  float(batch["total_weight"]), db.utc_now()))
    return dict(conn.execute("SELECT * FROM batch_state WHERE batch_id = ?;", (batch_id,)).fetchone())


def _require_holder(conn, user: dict, batch_id: str, *,
                    allow_pending_transfer: bool = False) -> dict[str, Any]:
    """The caller's organization must hold the batch, and it must be ACTIVE.

    Unless ``allow_pending_transfer``, the batch must also not be in the
    middle of a custody hand-off: once a transfer is offered, the sender may
    not process, blend, package or re-offer it, otherwise the receiver would
    accept a batch whose contents no longer match what was offered.
    """
    state = _state(conn, batch_id)
    if user.get("organization_id") is None or state["holder_org_id"] != user["organization_id"]:
        raise _err(403, "Only the organization currently holding this batch may do this")
    if state["status"] != "ACTIVE":
        raise _err(409, f"Batch is {state['status']}")
    if not allow_pending_transfer:
        pending = conn.execute("SELECT transfer_id FROM custody_transfers WHERE batch_id = ? "
                               "AND status = 'PENDING';", (batch_id,)).fetchone()
        if pending is not None:
            raise _err(409, f"Batch is in pending custody transfer {pending['transfer_id']}; "
                            "cancel or complete it first")
    return state


def _set_remaining(conn, batch_id: str, remaining: float) -> None:
    remaining = 0.0 if remaining < EPS else round(remaining, 6)
    conn.execute("UPDATE batch_state SET remaining_kg = ?, status = CASE WHEN ? = 0 AND status = "
                 "'ACTIVE' THEN 'CONSUMED' ELSE status END, updated_at = ? WHERE batch_id = ?;",
                 (remaining, remaining, db.utc_now(), batch_id))


def viewer_org_ids(batch_id: str) -> set[int]:
    """Organizations that legitimately participate in this batch's chain."""
    ids: set[int] = set()
    queries = (
        ("SELECT h.organization_id AS o FROM honey_batches b JOIN hives h ON h.hive_id = b.hive_id "
         "WHERE b.batch_id = ?;"),
        "SELECT origin_org_id AS o FROM batch_state WHERE batch_id = ?;",
        "SELECT holder_org_id AS o FROM batch_state WHERE batch_id = ?;",
        "SELECT from_org_id AS o FROM custody_transfers WHERE batch_id = ?;",
        "SELECT to_org_id AS o FROM custody_transfers WHERE batch_id = ?;",
        "SELECT lab_org_id AS o FROM lab_results WHERE batch_id = ?;",
        "SELECT retailer_org_id AS o FROM retail_records WHERE batch_id = ?;",
    )
    for sql in queries:
        ids.update(r["o"] for r in db.query_all(sql, (batch_id,)) if r["o"] is not None)
    return ids


def can_view_batch(user: dict, batch_id: str) -> bool:
    """Only organizations that legitimately participate in this batch's chain
    (origin, current/previous holders, requested lab, receiving retailer) may
    read it; ADMIN/AUTHORITY see everything. A user without an organization
    sees nothing. (A previous version returned True unconditionally -- a
    cross-organization read bypass caught by the Phase 5 and 8 IDOR tests.)"""
    from .auth import ORG_UNRESTRICTED_ROLES
    if user["role"] in ORG_UNRESTRICTED_ROLES:
        return True
    org = user.get("organization_id")
    if org is None:
        return False
    return int(org) in viewer_org_ids(batch_id)


# ---------------------------------------------------------------------------
# Processing and blending
# ---------------------------------------------------------------------------

def record_processing(user: dict, batch_id: str, payload: dict, ip: str | None = None) -> dict:
    batch_id = _check_id(batch_id, "batch_id")
    process_type = str(payload.get("process_type") or "").upper()
    if process_type not in PROCESS_TYPES:
        raise _err(400, f"process_type must be one of {', '.join(PROCESS_TYPES)} "
                        "(blending uses POST /api/blends)", "process_type", "invalid")
    started, completed = _time(payload, "started_at"), _time(payload, "completed_at")
    if started and completed and completed < started:
        raise _err(400, "completed_at is before started_at", "completed_at", "order")
    temperature = _num(payload, "temperature_c", -10, 100)
    equipment, notes = _text(payload, "equipment", 120), _text(payload, "notes", 1000)
    processing_id = _new_id("PRC")
    with db.transaction() as conn:
        _batch_row(conn, batch_id)
        state = _require_holder(conn, user, batch_id)
        remaining = float(state["remaining_kg"])
        input_kg = _num(payload, "input_kg", 0.0001, 100_000) or remaining
        output_kg = _num(payload, "output_kg", 0.0001, 100_000) or input_kg
        if input_kg > remaining + EPS:
            raise _err(400, f"input_kg exceeds the {remaining} kg remaining", "input_kg", "exceeds_remaining")
        if output_kg > input_kg + EPS:
            raise _err(400, "output_kg cannot exceed input_kg (processing cannot create honey)",
                       "output_kg", "exceeds_input")
        conn.execute(
            "INSERT INTO processing_events (processing_id, input_batch_id, output_batch_id, "
            " processor_org_id, performed_by_user_id, process_type, started_at, completed_at, "
            " temperature_c, equipment, notes, created_at, input_kg, output_kg) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (processing_id, batch_id, user["organization_id"], user["id"], process_type, started,
             completed, temperature, equipment, notes, db.utc_now(), input_kg, output_kg))
        _set_remaining(conn, batch_id, remaining - input_kg + output_kg)
        event = chain_events.record_event(
            conn, event_type="PROCESSING_RECORDED", resource_type="processing_events",
            resource_id=processing_id, organization_id=_org_ext(conn, user["organization_id"]),
            actor_id=user["user_id"],
            payload={"processing_id": processing_id, "batch_id": batch_id,
                     "process_type": process_type, "input_kg": input_kg, "output_kg": output_kg})
    _audit(user, user["organization_id"], "PROCESSING_RECORDED", "processing_events", processing_id,
           f"batch={batch_id} type={process_type} in={input_kg} out={output_kg}", ip)
    return {"processing_id": processing_id, "batch_id": batch_id, "process_type": process_type,
            "input_kg": input_kg, "output_kg": output_kg, "chain_event_id": event["event_id"]}


def blend(user: dict, payload: dict, ip: str | None = None) -> dict:
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, list) or not 2 <= len(raw_inputs) <= 20:
        raise _err(400, "inputs must be a list of 2 to 20 {batch_id, weight_kg}", "inputs", "count")
    inputs: list[tuple[str, float]] = []
    for item in raw_inputs:
        if not isinstance(item, dict):
            raise _err(400, "each input must be an object", "inputs", "type")
        inputs.append((_check_id(item.get("batch_id"), "batch_id"),
                       _num(item, "weight_kg", 0.0001, 100_000, required=True)))
    if len({b for b, _ in inputs}) != len(inputs):
        raise _err(400, "the same batch appears more than once", "inputs", "duplicate")
    output_id = _check_id(payload["batch_id"], "batch_id") if payload.get("batch_id") else \
        f"HNY-BLD-{_secrets.token_hex(4).upper()}"
    notes = _text(payload, "notes", 1000)

    with db.transaction() as conn:  # validate up front for clear errors (rechecked below)
        sources = []
        for batch_id, weight in inputs:
            row = _batch_row(conn, batch_id)
            state = _require_holder(conn, user, batch_id)
            if weight > float(state["remaining_kg"]) + EPS:
                raise _err(400, f"{batch_id}: {weight} kg requested, {state['remaining_kg']} kg remaining",
                           "inputs", "exceeds_remaining")
            sources.append((row, weight))
        if conn.execute("SELECT 1 FROM honey_batches WHERE batch_id = ?;", (output_id,)).fetchone():
            raise _err(409, f"Batch id '{output_id}' already exists", "batch_id", "duplicate")

    total = round(sum(w for _, w in inputs), 6)
    hives = sorted({h for row, _ in sources for h in str(row["hive_id"]).split("+")})
    hive_field = "+".join(hives) if len("+".join(hives)) <= 250 else f"{len(hives)}-SOURCE-HIVES"
    florals = " + ".join(sorted({str(row["floral_type"]) for row, _ in sources}))[:120]
    quality = round(sum(float(row["quality_score"]) * w for row, w in sources) / total, 4)
    inputs_digest = _hash({b: w for b, w in inputs})
    processing_id = _new_id("PRC")
    organization = user["organization_id"]
    recorded: dict[str, Any] = {}

    def write(conn: Any, block: dict[str, Any]) -> None:
        for batch_id, weight in inputs:  # recheck inside the ledger transaction (races)
            state = _require_holder(conn, user, batch_id)
            if weight > float(state["remaining_kg"]) + EPS:
                raise _err(409, f"{batch_id} changed concurrently; retry")
        conn.execute(
            "INSERT INTO processing_events (processing_id, input_batch_id, output_batch_id, "
            " processor_org_id, performed_by_user_id, process_type, notes, created_at, input_kg, "
            " output_kg) VALUES (?, NULL, ?, ?, ?, 'BLENDING', ?, ?, ?, ?);",
            (processing_id, block["batch_id"], organization, user["id"], notes, db.utc_now(), total, total))
        for batch_id, weight in inputs:
            conn.execute("INSERT INTO batch_blend_inputs (processing_id, input_batch_id, weight_kg) "
                         "VALUES (?, ?, ?);", (processing_id, batch_id, weight))
            state = _state(conn, batch_id)
            _set_remaining(conn, batch_id, float(state["remaining_kg"]) - weight)
        conn.execute("INSERT INTO batch_state (batch_id, origin_org_id, holder_org_id, remaining_kg, "
                     "status, updated_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?);",
                     (block["batch_id"], organization, organization, total, db.utc_now()))
        org_ext = _org_ext(conn, organization)
        recorded["processing"] = chain_events.record_event(
            conn, event_type="PROCESSING_RECORDED", resource_type="processing_events",
            resource_id=processing_id, organization_id=org_ext, actor_id=user["user_id"],
            payload={"processing_id": processing_id, "process_type": "BLENDING",
                     "output_batch_id": block["batch_id"], "input_count": len(inputs),
                     "inputs_digest": inputs_digest, "input_kg": total, "output_kg": total})
        recorded["batch"] = chain_events.record_event(
            conn, event_type="BATCH_CREATED", resource_type="honey_batches",
            resource_id=block["batch_id"], organization_id=org_ext, actor_id=user["user_id"],
            payload={"batch_id": block["batch_id"], "block_index": block["block_index"],
                     "block_hash": block["block_hash"], "blended_by_processing_id": processing_id,
                     "inputs_digest": inputs_digest})

    try:
        block = ledger_module.ledger.add_batch(
            batch_id=output_id, hive_id=hive_field,
            harvest_date=max(str(row["harvest_date"]) for row, _ in sources),
            total_weight=total, floral_type=florals, quality_score=quality,
            quality_detail={"blend": {"processing_id": processing_id,
                                      "inputs": [{"batch_id": b, "weight_kg": w} for b, w in inputs],
                                      "quality_score_basis": "weight-averaged input husbandry scores"}},
            in_transaction=write)
    except ledger_module.DuplicateBatchError:
        raise _err(409, f"Batch id '{output_id}' already exists", "batch_id", "duplicate") from None
    _audit(user, organization, "BATCH_BLENDED", "honey_batches", output_id,
           f"processing={processing_id} inputs={','.join(b for b, _ in inputs)} total_kg={total}", ip)
    return {"batch_id": output_id, "processing_id": processing_id, "total_kg": total,
            "inputs": [{"batch_id": b, "weight_kg": w} for b, w in inputs],
            "block_index": block["block_index"], "block_hash": block["block_hash"],
            "chain_event_ids": [recorded["processing"]["event_id"], recorded["batch"]["event_id"]]}


# ---------------------------------------------------------------------------
# Laboratory
# ---------------------------------------------------------------------------

LAB_HASHED_FIELDS = ("lab_result_id", "batch_id", "lab_org", "technician", "test_date",
                     "moisture_pct", "hmf_mg_kg", "c4_sugar_pct", "acidity", "colour",
                     "pollen_analysis", "certificate_id", "certificate_url", "status", "completed_at")


def lab_result_hash(row: dict[str, Any], conn=None) -> str:
    """Hash of a COMPLETED result from its stored columns. Used at write time
    (inside the writing transaction) and by chain_events.verify_log to detect
    later tampering."""
    run = (conn.execute if conn is not None else db.get_connection().execute)
    lab = run("SELECT org_id FROM organizations WHERE id = ?;", (row["lab_org_id"],)).fetchone()
    tech = run("SELECT user_id FROM users WHERE id = ?;", (row.get("technician_user_id"),)).fetchone()
    view = {**{k: row.get(k) for k in LAB_HASHED_FIELDS},
            "lab_org": lab["org_id"] if lab else None, "technician": tech["user_id"] if tech else None}
    return _hash(view)


def request_lab_test(user: dict, batch_id: str, payload: dict, ip: str | None = None) -> dict:
    batch_id = _check_id(batch_id, "batch_id")
    lab_ext = _check_id(payload.get("lab_organization_id"), "lab_organization_id")
    lab_result_id = _new_id("LAB")
    with db.transaction() as conn:
        _batch_row(conn, batch_id)
        _require_holder(conn, user, batch_id, allow_pending_transfer=True)  # sampling is fine
        lab = conn.execute("SELECT * FROM organizations WHERE org_id = ?;", (lab_ext,)).fetchone()
        if lab is None or lab["org_type"] != "LAB" or lab["status"] != "ACTIVE":
            raise _err(400, "lab_organization_id must be an ACTIVE laboratory organization",
                       "lab_organization_id", "not_a_lab")
        try:
            conn.execute("INSERT INTO lab_results (lab_result_id, batch_id, lab_org_id, status, "
                         " created_at, requested_by_user_id, requested_by_org_id) "
                         "VALUES (?, ?, ?, 'PENDING', ?, ?, ?);",
                         (lab_result_id, batch_id, lab["id"], db.utc_now(), user["id"],
                          user["organization_id"]))
        except sqlite3.IntegrityError:
            raise _err(409, "A test request for this batch is already pending at that laboratory") from None
    _audit(user, user["organization_id"], "LAB_TEST_REQUESTED", "lab_results", lab_result_id,
           f"batch={batch_id} lab={lab_ext}", ip)
    return {"lab_result_id": lab_result_id, "batch_id": batch_id, "lab_organization_id": lab_ext,
            "status": "PENDING"}


def complete_lab_result(user: dict, lab_result_id: str, payload: dict, ip: str | None = None) -> dict:
    lab_result_id = _check_id(lab_result_id, "lab_result_id")
    verdict = str(payload.get("status") or "").upper()
    if verdict not in LAB_VERDICTS:
        raise _err(400, f"status must be one of {', '.join(LAB_VERDICTS)}", "status", "invalid")
    test_date = str(payload.get("test_date") or "").strip()
    try:
        tested = date.fromisoformat(test_date)
    except ValueError:
        raise _err(400, "test_date must be YYYY-MM-DD", "test_date", "format") from None
    if tested > datetime.now(timezone.utc).date() + timedelta(days=1):
        raise _err(400, "test_date cannot be in the future", "test_date", "future")
    measures = {"moisture_pct": _num(payload, "moisture_pct", 0, 100),
                "hmf_mg_kg": _num(payload, "hmf_mg_kg", 0, 1000),
                "c4_sugar_pct": _num(payload, "c4_sugar_pct", 0, 100),
                "acidity": _num(payload, "acidity", 0, 500)}
    if verdict in ("PASSED", "FAILED") and all(v is None for v in measures.values()):
        raise _err(400, "a PASSED/FAILED result must include at least one measured value",
                   "moisture_pct", "missing_measurement")
    colour, pollen = _text(payload, "colour", 40), _text(payload, "pollen_analysis", 500)
    certificate_id = _text(payload, "certificate_id", 64)
    if certificate_id and not re.match(r"^[A-Za-z0-9/_\-.]+$", certificate_id):
        raise _err(400, "certificate_id has invalid characters", "certificate_id", "format")
    certificate_url = _text(payload, "certificate_url", 300)
    if certificate_url and not certificate_url.startswith("https://"):
        raise _err(400, "certificate_url must be an https:// URL", "certificate_url", "format")

    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM lab_results WHERE lab_result_id = ?;", (lab_result_id,)).fetchone()
        if row is None:
            raise _err(404, "No such lab request")
        row = dict(row)
        if user.get("organization_id") != row["lab_org_id"]:
            raise _err(403, "Only the laboratory this sample was sent to may record its result")
        if row["status"] != "PENDING":
            raise _err(409, f"Result already recorded ({row['status']}); results are immutable")
        if tested < date.fromisoformat(str(row["created_at"])[:10]):
            # A result cannot be dated before the sample was even requested.
            raise _err(400, "test_date is before the sample was requested", "test_date", "before_request")
        completed_at = db.utc_now()
        updated = conn.execute(
            "UPDATE lab_results SET status = ?, test_date = ?, moisture_pct = ?, hmf_mg_kg = ?, "
            " c4_sugar_pct = ?, acidity = ?, colour = ?, pollen_analysis = ?, certificate_id = ?, "
            " certificate_url = ?, technician_user_id = ?, completed_at = ? "
            "WHERE lab_result_id = ? AND status = 'PENDING';",
            (verdict, test_date, measures["moisture_pct"], measures["hmf_mg_kg"],
             measures["c4_sugar_pct"], measures["acidity"], colour, pollen, certificate_id,
             certificate_url, user["id"], completed_at, lab_result_id)).rowcount
        if updated != 1:
            raise _err(409, "Result was recorded concurrently")
        stored = dict(conn.execute("SELECT * FROM lab_results WHERE lab_result_id = ?;",
                                   (lab_result_id,)).fetchone())
        result_hash = lab_result_hash(stored, conn)
        conn.execute("UPDATE lab_results SET result_hash = ? WHERE lab_result_id = ?;",
                     (result_hash, lab_result_id))
        event = chain_events.record_event(
            conn, event_type="LAB_RESULT_RECORDED", resource_type="lab_results",
            resource_id=lab_result_id, organization_id=_org_ext(conn, row["lab_org_id"]),
            actor_id=user["user_id"],
            payload={"lab_result_id": lab_result_id, "batch_id": row["batch_id"],
                     "verdict": verdict, "result_hash": result_hash})
    _audit(user, row["lab_org_id"], "LAB_RESULT_RECORDED", "lab_results", lab_result_id,
           f"batch={row['batch_id']} verdict={verdict} hash={result_hash}", ip)
    return {**serialize_lab(dict(db.query_one(
        "SELECT * FROM lab_results WHERE lab_result_id = ?;", (lab_result_id,)))),
        "chain_event_id": event["event_id"]}


def serialize_lab(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("lab_result_id", "batch_id", "status", "test_date", "moisture_pct", "hmf_mg_kg",
            "c4_sugar_pct", "acidity", "colour", "pollen_analysis", "certificate_id",
            "certificate_url", "created_at", "completed_at", "result_hash")
    lab = db.query_one("SELECT org_id FROM organizations WHERE id = ?;", (row["lab_org_id"],))
    return {**{k: row.get(k) for k in keys}, "lab_organization_id": lab["org_id"] if lab else None,
            "units": {"moisture_pct": "% w/w", "hmf_mg_kg": "mg/kg", "c4_sugar_pct": "%",
                      "acidity": "meq/kg"}}


def lab_summary(batch_id: str) -> dict[str, Any]:
    rows = [dict(r) for r in db.query_all(
        "SELECT * FROM lab_results WHERE batch_id = ? ORDER BY id;", (batch_id,))]
    done = [r["status"] for r in rows if r["status"] != "PENDING"]
    if not rows:
        status = "NOT_TESTED"
    elif "FAILED" in done:
        status = "FAILED"
    elif "REVIEW" in done:
        status = "REVIEW"
    elif "PASSED" in done:
        status = "PASSED"
    else:
        status = "PENDING"
    return {"status": status, "results": [serialize_lab(r) for r in rows]}


# ---------------------------------------------------------------------------
# Packaging and product units
# ---------------------------------------------------------------------------

def _unit_id() -> str:
    # 80 random bits, base32 -- non-sequential, not guessable, copy-resistant.
    return "UNIT-" + base64.b32encode(_secrets.token_bytes(10)).decode("ascii").rstrip("=")


def units_digest(unit_ids: list[str]) -> str:
    return _hash({"units": sorted(unit_ids)})


def package(user: dict, batch_id: str, payload: dict, ip: str | None = None) -> dict:
    batch_id = _check_id(batch_id, "batch_id")
    size = payload.get("unit_size_g")
    count = payload.get("unit_count")
    for key, value, low, high in (("unit_size_g", size, 50, 5000), ("unit_count", count, 1, 2000)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _err(400, f"{key} must be an integer between {low} and {high}", key, "out_of_range")
    try:
        best_before = date.fromisoformat(str(payload.get("best_before") or ""))
    except ValueError:
        raise _err(400, "best_before must be YYYY-MM-DD", "best_before", "format") from None
    today = datetime.now(timezone.utc).date()
    if not today < best_before <= today + timedelta(days=5 * 366):
        raise _err(400, "best_before must be in the future and within 5 years", "best_before", "out_of_range")
    summary = lab_summary(batch_id)
    if summary["status"] != "PASSED":
        raise _err(409, f"Packaging requires a PASSED laboratory result; current status is "
                        f"{summary['status']}")
    kg = round(size * count / 1000.0, 6)
    packaging_id = _new_id("PKG")
    units = [(_unit_id(), _secrets.token_urlsafe(24)) for _ in range(count)]
    digest = units_digest([u for u, _ in units])
    with db.transaction() as conn:
        _batch_row(conn, batch_id)
        state = _require_holder(conn, user, batch_id)
        if kg > float(state["remaining_kg"]) + EPS:
            raise _err(400, f"{count} x {size} g = {kg} kg exceeds the {state['remaining_kg']} kg remaining",
                       "unit_count", "exceeds_remaining")
        now = db.utc_now()
        conn.execute("INSERT INTO packaging_events (packaging_id, batch_id, packaged_by_user_id, "
                     " organization_id, unit_size_g, unit_count, packed_at, best_before, created_at, "
                     " units_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                     (packaging_id, batch_id, user["id"], user["organization_id"], size, count, now,
                      best_before.isoformat(), now, digest))
        conn.executemany("INSERT INTO product_units (unit_id, packaging_id, batch_id, secret_hash, "
                         "status, created_at, qr_verifier, qr_version) "
                         "VALUES (?, ?, ?, ?, 'ISSUED', ?, ?, ?);",
                         [(u, packaging_id, batch_id, ledger_module.sha256_hex(s), now,
                           unit_qr.verifier(unit_qr.tag(u, s)), unit_qr.QR_VERSION)
                          for u, s in units])
        _set_remaining(conn, batch_id, float(state["remaining_kg"]) - kg)
        event = chain_events.record_event(
            conn, event_type="PACKAGED", resource_type="packaging_events", resource_id=packaging_id,
            organization_id=_org_ext(conn, user["organization_id"]), actor_id=user["user_id"],
            payload={"packaging_id": packaging_id, "batch_id": batch_id, "unit_count": count,
                     "unit_size_g": size, "units_digest": digest})
    _audit(user, user["organization_id"], "BATCH_PACKAGED", "packaging_events", packaging_id,
           f"batch={batch_id} units={count} size_g={size} digest={digest}", ip)
    _audit(user, user["organization_id"], "PRODUCT_UNITS_CREATED", "product_units", packaging_id,
           f"count={count}", ip)
    return {"packaging_id": packaging_id, "batch_id": batch_id, "unit_count": count,
            "unit_size_g": size, "best_before": best_before.isoformat(), "units_digest": digest,
            "chain_event_id": event["event_id"],
            "units": [{"unit_id": u, "unit_secret": s,
                       "qr_payload": unit_qr.payload(u, s),
                       "qr_url": unit_qr.url(unit_qr.payload(u, s))} for u, s in units],
            "notice": "unit_secret values are shown ONCE (for printing QR labels); only a "
                      "hash is stored."}


# ---------------------------------------------------------------------------
# Custody
# ---------------------------------------------------------------------------

def _transfer(conn, transfer_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM custody_transfers WHERE transfer_id = ?;", (transfer_id,)).fetchone()
    if row is None:
        raise _err(404, "No such custody transfer")
    return dict(row)


def initiate_transfer(user: dict, payload: dict, ip: str | None = None) -> dict:
    if user["role"] not in HOLDER_ROLES:
        raise _err(403, "This role cannot hold or transfer honey")
    batch_id = _check_id(payload.get("batch_id"), "batch_id")
    to_ext = _check_id(payload.get("to_organization_id"), "to_organization_id")
    transfer_id = _new_id("CUS")
    with db.transaction() as conn:
        _batch_row(conn, batch_id)
        state = _require_holder(conn, user, batch_id)
        to = conn.execute("SELECT * FROM organizations WHERE org_id = ?;", (to_ext,)).fetchone()
        if to is None or to["status"] != "ACTIVE" or to["org_type"] not in RECEIVER_ORG_TYPES:
            raise _err(400, "to_organization_id must be an ACTIVE supply-chain organization",
                       "to_organization_id", "invalid")
        if to["id"] == state["holder_org_id"]:
            raise _err(400, "Cannot transfer a batch to its current holder", "to_organization_id", "same_org")
        try:
            conn.execute("INSERT INTO custody_transfers (transfer_id, batch_id, from_org_id, to_org_id, "
                         " quantity_kg, status, initiated_at, initiated_by_user_id) "
                         "VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?);",
                         (transfer_id, batch_id, state["holder_org_id"], to["id"],
                          state["remaining_kg"], db.utc_now(), user["id"]))
        except sqlite3.IntegrityError:
            raise _err(409, "A custody transfer for this batch is already pending") from None
    _audit(user, user["organization_id"], "CUSTODY_TRANSFER_INITIATED", "custody_transfers", transfer_id,
           f"batch={batch_id} to={to_ext}", ip)
    return serialize_transfer(dict(db.query_one(
        "SELECT * FROM custody_transfers WHERE transfer_id = ?;", (transfer_id,))))


def _accept_in(conn, user: dict, transfer: dict) -> dict:
    state = _state(conn, transfer["batch_id"])
    if state["holder_org_id"] != transfer["from_org_id"] or state["status"] != "ACTIVE":
        raise _err(409, "The sender no longer holds this batch in an ACTIVE state")
    updated = conn.execute("UPDATE custody_transfers SET status = 'ACCEPTED', resolved_at = ?, "
                           "resolved_by_user_id = ? WHERE transfer_id = ? AND status = 'PENDING';",
                           (db.utc_now(), user["id"], transfer["transfer_id"])).rowcount
    if updated != 1:
        raise _err(409, "Transfer is no longer PENDING")
    conn.execute("UPDATE batch_state SET holder_org_id = ?, updated_at = ? WHERE batch_id = ?;",
                 (transfer["to_org_id"], db.utc_now(), transfer["batch_id"]))
    return chain_events.record_event(
        conn, event_type="CUSTODY_TRANSFERRED", resource_type="custody_transfers",
        resource_id=transfer["transfer_id"], organization_id=_org_ext(conn, transfer["to_org_id"]),
        actor_id=user["user_id"],
        payload={"transfer_id": transfer["transfer_id"], "batch_id": transfer["batch_id"],
                 "from_org": _org_ext(conn, transfer["from_org_id"]),
                 "to_org": _org_ext(conn, transfer["to_org_id"]),
                 "quantity_kg": transfer["quantity_kg"]})


def resolve_transfer(user: dict, transfer_id: str, action: str, ip: str | None = None) -> dict:
    transfer_id = _check_id(transfer_id, "transfer_id")
    if user["role"] not in HOLDER_ROLES:
        raise _err(403, "This role cannot take part in custody transfers")
    with db.transaction() as conn:
        transfer = _transfer(conn, transfer_id)
        party = transfer["from_org_id"] if action == "cancel" else transfer["to_org_id"]
        if user.get("organization_id") != party:
            who = "sender" if action == "cancel" else "receiving organization"
            raise _err(403, f"Only the {who} may {action} this transfer")
        if transfer["status"] != "PENDING":
            raise _err(409, f"Transfer is already {transfer['status']}")
        shipment = conn.execute("SELECT status FROM shipments WHERE custody_transfer_id = ?;",
                                (transfer_id,)).fetchone()
        if action == "accept":
            if shipment is not None and shipment["status"] != "DELIVERED":
                raise _err(409, f"The shipment for this transfer is {shipment['status']}, not DELIVERED")
            _accept_in(conn, user, transfer)
            new_status = "ACCEPTED"
        else:
            if action == "cancel" and shipment is not None:
                raise _err(409, "A shipment exists for this transfer; record a shipment EXCEPTION instead")
            new_status = "REJECTED" if action == "reject" else "CANCELLED"
            if conn.execute("UPDATE custody_transfers SET status = ?, resolved_at = ?, resolved_by_user_id = ? "
                            "WHERE transfer_id = ? AND status = 'PENDING';",
                            (new_status, db.utc_now(), user["id"], transfer_id)).rowcount != 1:
                raise _err(409, "Transfer is no longer PENDING")
    _audit(user, user["organization_id"], f"CUSTODY_TRANSFER_{new_status}", "custody_transfers",
           transfer_id, f"batch={transfer['batch_id']}", ip)
    return serialize_transfer(dict(db.query_one(
        "SELECT * FROM custody_transfers WHERE transfer_id = ?;", (transfer_id,))))


def serialize_transfer(row: dict[str, Any]) -> dict[str, Any]:
    return {"transfer_id": row["transfer_id"], "batch_id": row["batch_id"],
            "from_organization_id": _ext(row["from_org_id"]),
            "to_organization_id": _ext(row["to_org_id"]),
            "quantity_kg": row["quantity_kg"], "status": row["status"],
            "initiated_at": row["initiated_at"], "resolved_at": row["resolved_at"]}


def list_transfers(user: dict) -> list[dict]:
    from .auth import ORG_UNRESTRICTED_ROLES
    if user["role"] in ORG_UNRESTRICTED_ROLES:
        rows = db.query_all("SELECT * FROM custody_transfers ORDER BY id DESC LIMIT 500;")
    else:
        org = user.get("organization_id")
        rows = db.query_all("SELECT * FROM custody_transfers WHERE from_org_id = ? OR to_org_id = ? "
                            "ORDER BY id DESC LIMIT 500;", (org, org))
    return [serialize_transfer(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

def create_shipment(user: dict, payload: dict, ip: str | None = None) -> dict:
    if user["role"] not in HOLDER_ROLES:
        raise _err(403, "This role cannot ship honey")
    transfer_id = _check_id(payload.get("custody_transfer_id"), "custody_transfer_id")
    fields = {k: _text(payload, k, 120) for k in ("origin", "destination", "vehicle", "driver")}
    shipment_id = _new_id("SHP")
    retail_id = None
    with db.transaction() as conn:
        transfer = _transfer(conn, transfer_id)
        if user.get("organization_id") != transfer["from_org_id"]:
            raise _err(403, "Only the sending organization may ship this transfer")
        if transfer["status"] != "PENDING":
            raise _err(409, f"Transfer is {transfer['status']}; only a PENDING transfer can be shipped")
        now = db.utc_now()
        try:
            conn.execute("INSERT INTO shipments (shipment_id, batch_id, custody_transfer_id, origin, "
                         " destination, vehicle, driver, status, created_at, from_org_id, to_org_id, "
                         " created_by_user_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?);",
                         (shipment_id, transfer["batch_id"], transfer_id, fields["origin"],
                          fields["destination"], fields["vehicle"], fields["driver"], now,
                          transfer["from_org_id"], transfer["to_org_id"], user["id"], now))
        except sqlite3.IntegrityError:
            raise _err(409, "A shipment already exists for this transfer") from None
        receiver = _org(conn, transfer["to_org_id"])
        if receiver["org_type"] == "RETAILER":
            retail_id = _new_id("RTL")
            conn.execute("INSERT INTO retail_records (retail_record_id, batch_id, retailer_org_id, status, "
                         " created_at, shipment_id, custody_transfer_id) VALUES (?, ?, ?, 'EXPECTED', ?, ?, ?);",
                         (retail_id, transfer["batch_id"], transfer["to_org_id"], now, shipment_id, transfer_id))
    _audit(user, user["organization_id"], "SHIPMENT_CREATED", "shipments", shipment_id,
           f"transfer={transfer_id} batch={transfer['batch_id']}", ip)
    return {**get_shipment(shipment_id), "retail_record_id": retail_id}


def update_shipment(user: dict, shipment_id: str, payload: dict, ip: str | None = None) -> dict:
    shipment_id = _check_id(shipment_id, "shipment_id")
    target = str(payload.get("status") or "").upper()
    tmin, tmax = _num(payload, "min_temperature_c", -30, 70), _num(payload, "max_temperature_c", -30, 70)
    if tmin is not None and tmax is not None and tmin > tmax:
        raise _err(400, "min_temperature_c exceeds max_temperature_c", "min_temperature_c", "order")
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM shipments WHERE shipment_id = ?;", (shipment_id,)).fetchone()
        if row is None:
            raise _err(404, "No such shipment")
        row = dict(row)
        if user.get("organization_id") != row["from_org_id"] or user["role"] not in HOLDER_ROLES:
            raise _err(403, "Only the shipping organization may update this shipment")
        if target not in SHIPMENT_TRANSITIONS.get(row["status"], ()):
            raise _err(409, f"Cannot move a {row['status']} shipment to {target or '(none)'}")
        reason = None
        if target == "EXCEPTION":
            reason = str(payload.get("exception_reason") or "").strip()
            if not 5 <= len(reason) <= 300:
                raise _err(400, "exception_reason (5-300 characters) is required for EXCEPTION",
                           "exception_reason", "missing")
        now = db.utc_now()
        if conn.execute(
                "UPDATE shipments SET status = ?, updated_at = ?, "
                " departure_at = CASE WHEN ? = 'IN_TRANSIT' AND departure_at IS NULL THEN ? ELSE departure_at END, "
                " arrival_at = CASE WHEN ? = 'DELIVERED' THEN ? ELSE arrival_at END, "
                " min_temperature_c = COALESCE(?, min_temperature_c), "
                " max_temperature_c = COALESCE(?, max_temperature_c), "
                " exception_reason = COALESCE(?, exception_reason) "
                "WHERE shipment_id = ? AND status = ?;",
                (target, now, target, now, target, now, tmin, tmax, reason, shipment_id,
                 row["status"])).rowcount != 1:
            raise _err(409, "Shipment changed concurrently")
        if target == "IN_TRANSIT":
            chain_events.record_event(  # idempotent: a resumed shipment keeps its first SHIPPED event
                conn, event_type="SHIPPED", resource_type="shipments", resource_id=shipment_id,
                organization_id=_org_ext(conn, row["from_org_id"]), actor_id=user["user_id"],
                payload={"shipment_id": shipment_id, "batch_id": row["batch_id"],
                         "custody_transfer_id": row["custody_transfer_id"],
                         "from_org": _org_ext(conn, row["from_org_id"]),
                         "to_org": _org_ext(conn, row["to_org_id"])})
    _audit(user, user["organization_id"], "SHIPMENT_STATUS_CHANGED", "shipments", shipment_id,
           f"{row['status']}->{target}" + (f" reason={reason[:200]}" if reason else ""), ip)
    return get_shipment(shipment_id)


def get_shipment(shipment_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM shipments WHERE shipment_id = ?;", (shipment_id,))
    if row is None:
        raise _err(404, "No such shipment")
    row = dict(row)
    return {k: row.get(k) for k in ("shipment_id", "batch_id", "custody_transfer_id", "origin",
                                    "destination", "vehicle", "driver", "status", "departure_at",
                                    "arrival_at", "min_temperature_c", "max_temperature_c",
                                    "created_at", "updated_at")} | {
        "from_organization_id": _ext(row["from_org_id"]),
        "to_organization_id": _ext(row["to_org_id"]),
        "_from_org_pk": row["from_org_id"], "_to_org_pk": row["to_org_id"]}


def public_shipment(view: dict) -> dict:
    return {k: v for k, v in view.items() if not k.startswith("_")}


def list_shipments(user: dict) -> list[dict]:
    from .auth import ORG_UNRESTRICTED_ROLES
    if user["role"] in ORG_UNRESTRICTED_ROLES:
        rows = db.query_all("SELECT shipment_id FROM shipments ORDER BY id DESC LIMIT 500;")
    else:
        org = user.get("organization_id")
        rows = db.query_all("SELECT shipment_id FROM shipments WHERE from_org_id = ? OR to_org_id = ? "
                            "ORDER BY id DESC LIMIT 500;", (org, org))
    return [public_shipment(get_shipment(r["shipment_id"])) for r in rows]


# ---------------------------------------------------------------------------
# Retail
# ---------------------------------------------------------------------------

def _retail(conn, user: dict, retail_record_id: str, expected: str) -> dict:
    row = conn.execute("SELECT * FROM retail_records WHERE retail_record_id = ?;",
                       (retail_record_id,)).fetchone()
    if row is None:
        raise _err(404, "No such retail record")
    row = dict(row)
    if user["role"] != "RETAILER" or user.get("organization_id") != row["retailer_org_id"]:
        raise _err(403, "Only the receiving retailer may do this")
    if row["status"] != expected:
        raise _err(409, f"Retail record is {row['status']}, expected {expected}")
    return row


def receive_retail(user: dict, retail_record_id: str, ip: str | None = None) -> dict:
    retail_record_id = _check_id(retail_record_id, "retail_record_id")
    with db.transaction() as conn:
        row = _retail(conn, user, retail_record_id, "EXPECTED")
        shipment = conn.execute("SELECT * FROM shipments WHERE shipment_id = ?;", (row["shipment_id"],)).fetchone()
        if shipment is None or shipment["status"] != "DELIVERED":
            raise _err(409, "The shipment has not been DELIVERED yet")
        transfer = _transfer(conn, row["custody_transfer_id"])
        if transfer["status"] != "PENDING":
            raise _err(409, f"The custody transfer is {transfer['status']}")
        _accept_in(conn, user, transfer)
        now = db.utc_now()
        if conn.execute("UPDATE retail_records SET status = 'RECEIVED', received_at = ? "
                        "WHERE retail_record_id = ? AND status = 'EXPECTED';", (now, retail_record_id)).rowcount != 1:
            raise _err(409, "Retail record changed concurrently")
        chain_events.record_event(
            conn, event_type="RECEIVED", resource_type="retail_records", resource_id=retail_record_id,
            organization_id=_org_ext(conn, row["retailer_org_id"]), actor_id=user["user_id"],
            payload={"retail_record_id": retail_record_id, "shipment_id": row["shipment_id"],
                     "batch_id": row["batch_id"], "retailer_org": _org_ext(conn, row["retailer_org_id"])})
    _audit(user, user["organization_id"], "RETAIL_RECEIVED", "retail_records", retail_record_id,
           f"batch={row['batch_id']} shipment={row['shipment_id']}", ip)
    return get_retail(retail_record_id)


def shelve_retail(user: dict, retail_record_id: str, ip: str | None = None) -> dict:
    retail_record_id = _check_id(retail_record_id, "retail_record_id")
    with db.transaction() as conn:
        row = _retail(conn, user, retail_record_id, "RECEIVED")
        state = _state(conn, row["batch_id"])
        if state["holder_org_id"] != user["organization_id"] or state["status"] == "RECALLED":
            raise _err(409, f"Batch is not held by this retailer in a sellable state ({state['status']})")
        activated = conn.execute("UPDATE product_units SET status = 'ACTIVATED' WHERE batch_id = ? "
                                 "AND status = 'ISSUED';", (row["batch_id"],)).rowcount
        if activated == 0:
            raise _err(409, "This batch has no packaged units to put on the shelf")
        conn.execute("UPDATE retail_records SET status = 'ON_SHELF', shelved_at = ? "
                     "WHERE retail_record_id = ? AND status = 'RECEIVED';", (db.utc_now(), retail_record_id))
        chain_events.record_event(
            conn, event_type="RETAIL_REGISTERED", resource_type="retail_records",
            resource_id=retail_record_id, organization_id=_org_ext(conn, row["retailer_org_id"]),
            actor_id=user["user_id"],
            payload={"retail_record_id": retail_record_id, "batch_id": row["batch_id"],
                     "activated_units": activated})
    _audit(user, user["organization_id"], "RETAIL_ON_SHELF", "retail_records", retail_record_id,
           f"batch={row['batch_id']} units={activated}", ip)
    return get_retail(retail_record_id)


def sell_unit(user: dict, unit_id: str, ip: str | None = None) -> dict:
    if not re.match(r"^UNIT-[A-Z2-7]{16}$", str(unit_id or "")):
        raise _err(404, "No such product unit")
    with db.transaction() as conn:
        unit = conn.execute("SELECT * FROM product_units WHERE unit_id = ?;", (unit_id,)).fetchone()
        if unit is None:
            raise _err(404, "No such product unit")
        record = conn.execute("SELECT * FROM retail_records WHERE batch_id = ? AND retailer_org_id = ? "
                              "ORDER BY id DESC LIMIT 1;",
                              (unit["batch_id"], user.get("organization_id"))).fetchone()
        if user["role"] != "RETAILER" or record is None:
            # Authorization: this retailer never received this batch.
            raise _err(403, "This unit was not delivered to this retailer")
        if record["status"] not in ("ON_SHELF", "SOLD") or unit["status"] != "ACTIVATED":
            # State: right retailer, but not (or no longer) sellable.
            raise _err(409, f"Unit is {unit['status']} (retail record {record['status']}); "
                            "only ACTIVATED units on the shelf can be sold")
        conn.execute("UPDATE product_units SET status = 'SOLD' WHERE unit_id = ? AND status = 'ACTIVATED';",
                     (unit_id,))
        left = conn.execute("SELECT COUNT(*) AS n FROM product_units WHERE batch_id = ? "
                            "AND status IN ('ISSUED', 'ACTIVATED');", (unit["batch_id"],)).fetchone()["n"]
        if left == 0:
            conn.execute("UPDATE retail_records SET status = 'SOLD' WHERE retail_record_id = ? "
                         "AND status = 'ON_SHELF';", (record["retail_record_id"],))
    _audit(user, user["organization_id"], "UNIT_SOLD", "product_units", unit_id,
           f"batch={unit['batch_id']}", ip)
    return {"unit_id": unit_id, "status": "SOLD", "batch_id": unit["batch_id"]}


def get_retail(retail_record_id: str) -> dict[str, Any]:
    row = dict(db.query_one("SELECT * FROM retail_records WHERE retail_record_id = ?;", (retail_record_id,)))
    return {k: row.get(k) for k in ("retail_record_id", "batch_id", "status", "shipment_id",
                                    "custody_transfer_id", "received_at", "shelved_at", "created_at")} | {
        "retailer_organization_id": _ext(row["retailer_org_id"])}


def list_retail(user: dict) -> list[dict]:
    from .auth import ORG_UNRESTRICTED_ROLES
    if user["role"] in ORG_UNRESTRICTED_ROLES:
        rows = db.query_all("SELECT retail_record_id FROM retail_records ORDER BY id DESC LIMIT 500;")
    else:
        rows = db.query_all("SELECT retail_record_id FROM retail_records WHERE retailer_org_id = ? "
                            "ORDER BY id DESC LIMIT 500;", (user.get("organization_id"),))
    return [get_retail(r["retail_record_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def descendants(conn, batch_id: str) -> list[str]:
    """Every batch made (directly or transitively) from ``batch_id``."""
    seen, frontier = [], [batch_id]
    while frontier:
        current = frontier.pop()
        for r in conn.execute(
                "SELECT p.output_batch_id AS b FROM batch_blend_inputs i JOIN processing_events p "
                "ON p.processing_id = i.processing_id WHERE i.input_batch_id = ?;", (current,)):
            if r["b"] and r["b"] not in seen and r["b"] != batch_id:
                seen.append(r["b"])
                frontier.append(r["b"])
    return seen


def issue_recall(user: dict, payload: dict, ip: str | None = None) -> dict:
    batch_id = _check_id(payload.get("batch_id"), "batch_id")
    reason = _text(payload, "reason", 500)
    if not reason or len(reason) < 5:
        raise _err(400, "reason (5-500 characters) is required", "reason", "missing")
    recall_id = _new_id("RCL")
    with db.transaction() as conn:
        _batch_row(conn, batch_id)
        if conn.execute("SELECT 1 FROM recalls WHERE batch_id = ? AND status = 'ACTIVE';", (batch_id,)).fetchone():
            raise _err(409, "An active recall already exists for this batch")
        affected = [batch_id] + descendants(conn, batch_id)
        now = db.utc_now()
        conn.execute("INSERT INTO recalls (recall_id, batch_id, reason, issued_by_user_id, status, issued_at) "
                     "VALUES (?, ?, ?, ?, 'ACTIVE', ?);", (recall_id, batch_id, reason, user["id"], now))
        units = 0
        for b in affected:
            try:
                _state(conn, b)
                conn.execute("UPDATE batch_state SET status = 'RECALLED', updated_at = ? WHERE batch_id = ?;", (now, b))
            except SupplyChainError:
                pass  # a batch outside the supply chain still has its units/records recalled below
            units += conn.execute("UPDATE product_units SET status = 'RECALLED' WHERE batch_id = ? "
                                  "AND status != 'RECALLED';", (b,)).rowcount
            conn.execute("UPDATE retail_records SET status = 'RECALLED' WHERE batch_id = ?;", (b,))
        event = chain_events.record_event(
            conn, event_type="RECALLED", resource_type="recalls", resource_id=recall_id,
            organization_id=None, actor_id=user["user_id"],
            payload={"recall_id": recall_id, "batch_id": batch_id, "affected_batches": len(affected),
                     "affected_digest": _hash({"batches": sorted(affected)})})
    _audit(user, user.get("organization_id"), "RECALL_ISSUED", "recalls", recall_id,
           f"root={batch_id} affected={','.join(affected)} units={units}", ip)
    return {"recall_id": recall_id, "batch_id": batch_id, "reason": reason, "status": "ACTIVE",
            "affected_batches": affected, "recalled_units": units, "chain_event_id": event["event_id"]}


def recall_status(batch_id: str) -> dict[str, Any] | None:
    """Active recall covering this batch directly or via any source batch."""
    conn = db.get_connection()
    for row in conn.execute("SELECT * FROM recalls WHERE status = 'ACTIVE';").fetchall():
        if row["batch_id"] == batch_id or batch_id in descendants(conn, row["batch_id"]):
            return {"recall_id": row["recall_id"], "root_batch_id": row["batch_id"],
                    "reason": row["reason"], "issued_at": row["issued_at"]}
    return None


# ---------------------------------------------------------------------------
# Trace (backward provenance)
# ---------------------------------------------------------------------------

def trace_batch(batch_id: str, _depth: int = 0) -> dict[str, Any]:
    batch = db.get_batch(batch_id)
    if batch is None:
        raise _err(404, f"No batch '{batch_id}'")
    verify = ledger_module.ledger.verify_batch_authenticity(batch_id)
    state = db.query_one("SELECT * FROM batch_state WHERE batch_id = ?;", (batch_id,))
    harvests = []
    for hid in db.harvests_for_batch(batch_id):
        h = db.get_harvest(hid)
        if h:
            harvests.append({"harvest_id": hid, "hive_id": h["hive_external_id"],
                             "organization_id": h["org_external_id"], "harvest_date": h["harvest_date"],
                             "event_hash": h["event_hash"]})
    processing = [dict(r) for r in db.query_all(
        "SELECT processing_id, process_type, input_batch_id, output_batch_id, input_kg, output_kg, "
        "created_at FROM processing_events WHERE input_batch_id = ? OR output_batch_id = ? ORDER BY id;",
        (batch_id, batch_id))]
    blended_from = []
    if _depth < 20:
        for r in db.query_all("SELECT i.input_batch_id, i.weight_kg FROM batch_blend_inputs i JOIN "
                              "processing_events p ON p.processing_id = i.processing_id "
                              "WHERE p.output_batch_id = ? ORDER BY i.id;", (batch_id,)):
            blended_from.append({"batch_id": r["input_batch_id"], "weight_kg": r["weight_kg"],
                                 "trace": trace_batch(r["input_batch_id"], _depth + 1)})
    return {
        "batch_id": batch_id, "hive_id": batch["hive_id"], "harvest_date": batch["harvest_date"],
        "floral_type": batch["floral_type"], "total_weight": batch["total_weight"],
        "block_index": batch["block_index"], "block_hash": batch["block_hash"],
        "ledger_verified": bool(verify.get("verified")),
        "state": ({**{k: state[k] for k in ("remaining_kg", "status")},
                   "holder_organization_id": _ext(state["holder_org_id"]),
                   "origin_organization_id": _ext(state["origin_org_id"])} if state else None),
        "harvests": harvests, "processing": processing, "blended_from": blended_from,
        "lab": lab_summary(batch_id), "recall": recall_status(batch_id),
        "custody": [serialize_transfer(dict(r)) for r in db.query_all(
            "SELECT * FROM custody_transfers WHERE batch_id = ? ORDER BY id;", (batch_id,))],
        "shipments": [public_shipment(get_shipment(r["shipment_id"])) for r in db.query_all(
            "SELECT shipment_id FROM shipments WHERE batch_id = ? ORDER BY id;", (batch_id,))],
        "retail": [get_retail(r["retail_record_id"]) for r in db.query_all(
            "SELECT retail_record_id FROM retail_records WHERE batch_id = ? ORDER BY id;", (batch_id,))],
        "chain_events": [
            {"event_id": e["event_id"], "event_type": e["event_type"], "seq": e["seq"]}
            for e in db.query_all("SELECT event_id, event_type, seq, payload FROM chain_events ORDER BY seq;")
            if batch_id in {json.loads(e["payload"]).get(k) for k in ("batch_id", "output_batch_id")}],
    }


def trace_unit(unit_id: str) -> dict[str, Any]:
    unit = db.query_one("SELECT * FROM product_units WHERE unit_id = ?;", (unit_id,))
    if unit is None:
        raise _err(404, "No such product unit")
    packaging = dict(db.query_one("SELECT packaging_id, batch_id, unit_size_g, unit_count, packed_at, "
                                  "best_before, units_digest FROM packaging_events WHERE packaging_id = ?;",
                                  (unit["packaging_id"],)))
    return {"unit_id": unit_id, "status": unit["status"], "packaging": packaging,
            "batch": trace_batch(unit["batch_id"])}
