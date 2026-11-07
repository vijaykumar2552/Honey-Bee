"""
honeychain.consumer
===================
Public consumer verification of a product unit (Phase 9). No login.

``verify(qr_text)`` returns one of:

* ``AUTHENTIC`` -- the QR was issued by HoneyChain for this exact unit and has
  not been revoked. Returned together with truthful provenance and any
  active recall. Authentic says who issued the label; it is NOT a statement
  about safety or quality (the lab section says what was, or was not, tested).
* ``REVOKED``   -- a genuine code whose label was revoked by its packager.
  No provenance is shown.
* ``INVALID``   -- malformed, unsupported version, unknown unit, wrong or
  modified tag. Unknown unit and wrong tag get the SAME response, so the
  endpoint cannot be used to discover which unit ids exist.

The public view is built from database rows through explicit allow-lists:
organization NAMES (not ids), dates, statuses, quantities, lab verdicts and
measurements, and anchoring status. It never contains internal ids (database
keys, user/organization/transfer/shipment/event ids), people's names, GPS,
notes, vehicles/drivers, secrets, verifiers or audit data.
"""

from __future__ import annotations

import json
from typing import Any

from . import chain_events, config, db, ledger as ledger_module, unit_qr
from . import supply_chain as sc

AUTHENTICITY_NOTE = (
    "AUTHENTIC means this code was issued by HoneyChain for this specific unit and has not "
    "been revoked. It does not by itself mean the product is safe or of a particular quality; "
    "see the laboratory section and any recall notice."
)
INVALID_NOTE = ("This code is not a valid HoneyChain product code. Treat this product as "
                "unverified and contact the seller.")


def _org_name(pk: int | None) -> str | None:
    if pk is None:
        return None
    row = db.query_one("SELECT name FROM organizations WHERE id = ?;", (pk,))
    return row["name"] if row else None


def _anchoring(batch_id: str) -> list[dict[str, Any]]:
    """Chain events about this batch, each with its REAL anchoring state: a
    transaction hash is shown only for a CONFIRMED checkpoint."""
    out = []
    for e in db.query_all("SELECT event_id, event_type, payload FROM chain_events ORDER BY seq;"):
        payload = json.loads(e["payload"])
        if batch_id not in {payload.get(k) for k in ("batch_id", "output_batch_id")}:
            continue
        proof = chain_events.event_proof(e["event_id"]) or {}
        confirmed = proof.get("checkpoint_status") == "CONFIRMED"
        out.append({
            "event": e["event_type"],
            "anchored_on_chain": confirmed,
            "status": "ANCHORED" if confirmed else "RECORDED_NOT_YET_ANCHORED",
            "tx_hash": proof.get("tx_hash") if confirmed else None,
            "chain_id": proof.get("chain_id") if confirmed else None,
            "inclusion_proof_valid": bool(proof.get("proof_valid")) if confirmed else None,
        })
    return out


def _batch_view(batch_id: str, depth: int = 0) -> dict[str, Any]:
    batch = db.get_batch(batch_id)
    if batch is None:
        return {"batch_id": batch_id, "missing": True}
    ledger_ok = bool(ledger_module.ledger.verify_batch_authenticity(batch_id).get("verified"))
    harvests = []
    for hid in db.harvests_for_batch(batch_id):
        h = db.get_harvest(hid)
        if h:
            harvests.append({"hive": h["hive_external_id"], "harvest_date": h["harvest_date"],
                             "producer": _org_name(h["organization_id"]),
                             "floral_source": h.get("floral_source")})
    processing = [{"process": r["process_type"], "date": r["created_at"][:10],
                   "input_kg": r["input_kg"], "output_kg": r["output_kg"],
                   "processor": _org_name(r["processor_org_id"])}
                  for r in db.query_all("SELECT * FROM processing_events WHERE input_batch_id = ? "
                                        "OR output_batch_id = ? ORDER BY id;", (batch_id, batch_id))]
    blended_from = []
    if depth < 20:
        for r in db.query_all("SELECT i.input_batch_id, i.weight_kg FROM batch_blend_inputs i JOIN "
                              "processing_events p ON p.processing_id = i.processing_id "
                              "WHERE p.output_batch_id = ? ORDER BY i.id;", (batch_id,)):
            blended_from.append({"weight_kg": r["weight_kg"],
                                 "source": _batch_view(r["input_batch_id"], depth + 1)})
    lab_rows = [dict(r) for r in db.query_all("SELECT * FROM lab_results WHERE batch_id = ? "
                                              "ORDER BY id;", (batch_id,))]
    lab = {
        "status": sc.lab_summary(batch_id)["status"],
        "results": [{"verdict": r["status"], "test_date": r["test_date"],
                     "laboratory": _org_name(r["lab_org_id"]),
                     "moisture_pct": r["moisture_pct"], "hmf_mg_kg": r["hmf_mg_kg"],
                     "c4_sugar_pct": r["c4_sugar_pct"], "acidity_meq_kg": r["acidity"],
                     "certificate_reference": r["certificate_id"]}
                    for r in lab_rows if r["status"] != "PENDING"],
        "pending_tests": sum(1 for r in lab_rows if r["status"] == "PENDING"),
    }
    custody = [{"from": _org_name(r["from_org_id"]), "to": _org_name(r["to_org_id"]),
                "date": (r["resolved_at"] or r["initiated_at"])[:10]}
               for r in db.query_all("SELECT * FROM custody_transfers WHERE batch_id = ? AND "
                                     "status = 'ACCEPTED' ORDER BY id;", (batch_id,))]
    shipments = [{"from": _org_name(r["from_org_id"]), "to": _org_name(r["to_org_id"]),
                  "status": r["status"], "departed": r["departure_at"], "arrived": r["arrival_at"]}
                 for r in db.query_all("SELECT * FROM shipments WHERE batch_id = ? ORDER BY id;",
                                       (batch_id,))]
    retail = [{"retailer": _org_name(r["retailer_org_id"]), "status": r["status"]}
              for r in db.query_all("SELECT * FROM retail_records WHERE batch_id = ? ORDER BY id;",
                                    (batch_id,))]
    return {"batch_id": batch_id, "floral_type": batch["floral_type"],
            "harvest_date": batch["harvest_date"], "ledger_record_intact": ledger_ok,
            "harvests": harvests, "processing": processing, "blended_from": blended_from,
            "lab": lab, "custody": custody, "shipments": shipments, "retail": retail,
            "blockchain": _anchoring(batch_id)}


def _invalid(reason: str) -> dict[str, Any]:
    return {"result": "INVALID", "reason": reason, "message": INVALID_NOTE}


def _log_scan(unit_id: str | None, result: str, reason: str | None) -> None:
    """Append-only scan log for Phase 10 oversight. No client data stored."""
    with db.transaction() as conn:
        conn.execute("INSERT INTO consumer_scans (unit_id, result, reason, scanned_at) "
                     "VALUES (?, ?, ?, ?);", (unit_id, result, reason, db.utc_now()))


def _scan_signal(unit_id: str) -> dict[str, Any]:
    """Consumer-facing suspicious-activity signal for an AUTHENTIC unit, from
    the same scan log and thresholds as the Phase 10 authority signals. Only
    attached to AUTHENTIC answers, so it reveals nothing about which unit ids
    exist (INVALID answers are unchanged)."""
    recent = db.query_one(
        "SELECT COUNT(*) AS n FROM consumer_scans WHERE unit_id = ? AND result = 'AUTHENTIC' "
        "AND scanned_at >= DATETIME('now', '-1 day');", (unit_id,))["n"]
    forged = db.query_one(
        "SELECT COUNT(*) AS n FROM consumer_scans WHERE unit_id = ? AND result = 'INVALID';",
        (unit_id,))["n"]
    reasons = []
    if recent >= config.SIGNAL_SCAN_VELOCITY_24H:
        reasons.append(f"This code was verified {recent} times in the last 24 hours, which is "
                       "unusual for a single jar and can indicate a copied label.")
    if forged >= config.SIGNAL_FORGERY_ATTEMPTS:
        reasons.append(f"Altered copies of this code have been presented {forged} times.")
    return {"suspicious": bool(reasons), "reasons": reasons,
            "note": "A risk signal from scan patterns, not proof of counterfeiting."}


def verify(qr_text: object) -> dict[str, Any]:
    try:
        _, unit_id, tag = unit_qr.parse(qr_text)
    except unit_qr.QRFormatError as exc:
        _log_scan(None, "INVALID", exc.code)
        return _invalid(exc.code)

    unit = db.query_one("SELECT * FROM product_units WHERE unit_id = ?;", (unit_id,))
    # Unknown unit and wrong/modified tag are deliberately indistinguishable
    # to the caller; internally a wrong tag on a REAL unit is logged against
    # it, because repeated forged attempts on one unit are a Phase 10 signal.
    if unit is None or not unit_qr.tag_matches(tag, unit["qr_verifier"]):
        _log_scan(unit_id if unit is not None else None, "INVALID", "NOT_RECOGNISED")
        return _invalid("NOT_RECOGNISED")
    if unit["qr_revoked_at"]:
        _log_scan(unit_id, "REVOKED", None)
        # No provenance for a revoked label -- but a recall is safety
        # information and is shown regardless of the label's status.
        recall = sc.recall_status(unit["batch_id"])
        return {"result": "REVOKED", "unit_id": unit_id,
                "message": "This label was revoked by its packager and must not be trusted.",
                "recall": ({"active": True, "reason": recall["reason"], "issued_at": recall["issued_at"]}
                           if recall else {"active": unit["status"] == "RECALLED"})}

    now = db.utc_now()
    with db.transaction() as conn:
        conn.execute("UPDATE product_units SET scan_count = scan_count + 1, last_scanned_at = ?, "
                     "first_scanned_at = COALESCE(first_scanned_at, ?) WHERE unit_id = ?;",
                     (now, now, unit_id))
    _log_scan(unit_id, "AUTHENTIC", unit["status"])   # unit status at scan time
    unit = dict(db.query_one("SELECT * FROM product_units WHERE unit_id = ?;", (unit_id,)))
    pkg = db.query_one("SELECT * FROM packaging_events WHERE packaging_id = ?;", (unit["packaging_id"],))
    recall = sc.recall_status(unit["batch_id"])
    batch = _batch_view(unit["batch_id"])

    warnings = []
    if recall or unit["status"] == "RECALLED":
        warnings.append("RECALLED: do not consume. See the recall notice.")
    if batch["lab"]["status"] == "FAILED":
        warnings.append("A laboratory test of this batch FAILED.")
    elif batch["lab"]["status"] in ("NOT_TESTED", "PENDING"):
        warnings.append(f"Laboratory status: {batch['lab']['status']} (no passed test on record).")
    if not batch["ledger_record_intact"]:
        warnings.append("The batch record failed its integrity check.")
    scan_signal = _scan_signal(unit_id)
    if scan_signal["suspicious"]:
        warnings.append("SUSPICIOUS ACTIVITY DETECTED: " + " ".join(scan_signal["reasons"]))

    return {
        "result": "AUTHENTIC",
        "authenticity_note": AUTHENTICITY_NOTE,
        "recall": ({"active": True, "reason": recall["reason"], "issued_at": recall["issued_at"]}
                   if recall else {"active": unit["status"] == "RECALLED"}),
        "warnings": warnings,
        "scan_signal": scan_signal,
        "unit": {"unit_id": unit_id, "status": unit["status"],
                 "size_g": pkg["unit_size_g"] if pkg else None,
                 "packed_at": pkg["packed_at"] if pkg else None,
                 "best_before": pkg["best_before"] if pkg else None,
                 "packager": _org_name(pkg["organization_id"]) if pkg else None,
                 "times_verified": unit["scan_count"],
                 "first_verified_at": unit["first_scanned_at"]},
        "provenance": batch,
    }
