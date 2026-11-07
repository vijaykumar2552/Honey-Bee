"""
honeychain.harvests
===================
Secure harvest workflow (Phase 5).

    authenticated actor (ADMIN / BEEKEEPER)
      -> hive exists, is owned by an ACTIVE organization the actor may act for
      -> validated harvest event (immutable; canonical SHA-256 event_hash)
      -> ledger block for the resulting batch (the existing hash-linked ledger)
      -> batch_harvest_links provenance row
      all in ONE transaction (ledger.add_batch's in_transaction hook), so a
      harvest can never exist without its batch, or a batch without its harvest.

Hashing reuses ``ledger.canonical_json`` -- the same serialiser as ledger
blocks -- so there is exactly one off-chain hashing scheme. The block hash
itself is unchanged (it predates harvest events and covers the batch fields);
the harvest event is committed to by its own event_hash, and the link table
ties the two together. Phase 6's chain-event service is the place where both
hashes get anchored together.

The organization of a harvest is ALWAYS the hive's organization. A client
cannot choose it; a client that claims a different one is rejected.
"""

from __future__ import annotations

import re
import secrets as _secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import chain_events, db, ledger as ledger_module, validation

HARVEST_ID_RE = re.compile(r"^HRV-[A-Z0-9][A-Z0-9\-]{3,40}$")
PHOTO_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

#: Fields covered by a harvest event's hash, in canonical form.
HARVEST_HASHED_FIELDS: tuple[str, ...] = (
    "harvest_id", "hive_id", "organization_id", "harvested_by", "harvest_date",
    "harvested_at", "quantity_kg", "floral_source", "latitude", "longitude",
    "frame_count", "photo_sha256", "notes", "created_at",
)

#: How far back a harvest may be registered (late data entry is normal;
#: a harvest "from" years ago is not).
MAX_HARVEST_AGE_DAYS = 366
#: Timezone tolerance for "today" (a user at UTC+14 is a day ahead of UTC).
FUTURE_TOLERANCE_DAYS = 1
MAX_NOTES_LENGTH = 1000
UNIT_TO_KG = {"kg": 1.0, "g": 0.001}


class HarvestConflict(Exception):
    """Duplicate / conflicting request -> HTTP 409."""


def _err(message: str, field: str, issue: str, **extra: Any) -> validation.ValidationError:
    return validation.ValidationError(message, [{"field": field, "issue": issue, **extra}])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_harvest_time(value: Any, *, today: date | None = None) -> tuple[str, str | None]:
    """Return ``(harvest_date, harvested_at)``.

    Accepts exactly ``YYYY-MM-DD`` or a full ISO-8601 datetime (then
    ``harvested_at`` is its canonical UTC form). Missing -> today (UTC).
    Rejects trailing garbage, future dates beyond a one-day timezone
    tolerance, and dates older than MAX_HARVEST_AGE_DAYS.
    """
    today = today or datetime.now(timezone.utc).date()
    harvested_at: str | None = None
    if value is None or not str(value).strip():
        return today.isoformat(), None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise _err("harvest_date is not a real calendar date", "harvest_date", "format") from None
    else:
        parsed = validation.parse_device_timestamp(text) if "T" in text or " " in text else None
        if parsed is None:
            raise _err("harvest_date must be YYYY-MM-DD or an ISO-8601 datetime",
                       "harvest_date", "format")
        day = parsed.date()
        harvested_at = parsed.strftime("%Y-%m-%d %H:%M:%S")
    if day > today + timedelta(days=FUTURE_TOLERANCE_DAYS):
        raise _err("harvest_date cannot be in the future", "harvest_date", "future")
    if day < today - timedelta(days=MAX_HARVEST_AGE_DAYS):
        raise _err(f"harvest_date is more than {MAX_HARVEST_AGE_DAYS} days old",
                   "harvest_date", "too_old")
    return day.isoformat(), harvested_at


def validate_quantity(value: Any, unit: Any, max_kg: float) -> float:
    if value is None:
        raise _err("total_weight is required", "total_weight", "missing")
    if isinstance(value, bool):
        raise _err("total_weight must be a number", "total_weight", "type")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise _err("total_weight must be a number", "total_weight", "type") from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise _err("total_weight must be a finite number", "total_weight", "non_finite")
    unit_text = str(unit or "kg").strip().lower()
    if unit_text not in UNIT_TO_KG:
        raise _err("unit must be 'kg' or 'g'", "unit", "unsupported")
    kg = round(amount * UNIT_TO_KG[unit_text], 4)
    if not (0 < kg <= max_kg):
        raise _err(f"total_weight must be greater than 0 and at most {max_kg:.0f} kg",
                   "total_weight", "out_of_range", value=amount)
    return kg


def validate_optional_fields(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lat, lng = payload.get("latitude"), payload.get("longitude")
    if (lat is None) != (lng is None):
        raise _err("latitude and longitude must be supplied together", "latitude", "incomplete")
    if lat is not None:
        for name, raw, limit in (("latitude", lat, 90.0), ("longitude", lng, 180.0)):
            try:
                number = float(raw)
            except (TypeError, ValueError):
                raise _err(f"{name} must be a number", name, "type") from None
            if isinstance(raw, bool) or number != number or not -limit <= number <= limit:
                raise _err(f"{name} must be between -{limit:.0f} and {limit:.0f}", name,
                           "out_of_range")
            out[name] = round(number, 6)

    frames = payload.get("frame_count")
    if frames is not None:
        if isinstance(frames, bool) or not isinstance(frames, int) or not 0 <= frames <= 200:
            raise _err("frame_count must be an integer between 0 and 200", "frame_count",
                       "out_of_range")
        out["frame_count"] = frames

    notes = payload.get("notes")
    if notes is not None:
        text = str(notes).strip()
        if len(text) > MAX_NOTES_LENGTH:
            raise _err(f"notes must be at most {MAX_NOTES_LENGTH} characters", "notes", "too_long")
        out["notes"] = text or None

    photo = payload.get("photo_sha256")
    if photo is not None:
        if not PHOTO_SHA256_RE.match(str(photo)):
            raise _err("photo_sha256 must be a 64-character hex SHA-256 of the evidence photo",
                       "photo_sha256", "format")
        out["photo_sha256"] = str(photo).lower()
    return out


def validate_harvest_id(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().upper()
    if not HARVEST_ID_RE.match(text):
        raise _err("harvest_id must look like HRV-XXXX (letters, digits, hyphens)",
                   "harvest_id", "format")
    return text


def new_harvest_id(harvest_date: str) -> str:
    return f"HRV-{harvest_date[:4]}-{_secrets.token_hex(4).upper()}"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def canonical_event(event: dict[str, Any]) -> dict[str, Any]:
    """The hashed view of a harvest event, keyed by external identifiers only
    (internal primary keys never enter a hash -- they are not portable)."""
    return {field: event.get(field) for field in HARVEST_HASHED_FIELDS}


def compute_event_hash(event: dict[str, Any]) -> str:
    return ledger_module.sha256_hex(ledger_module.canonical_json(canonical_event(event)))


def _hash_view_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "harvest_id": row["harvest_id"],
        "hive_id": row["hive_external_id"],
        "organization_id": row["org_external_id"],
        "harvested_by": row["harvested_by_user_id"],
        "harvest_date": row["harvest_date"],
        "harvested_at": row.get("harvested_at"),
        "quantity_kg": row["quantity_kg"],
        "floral_source": row.get("floral_source"),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "frame_count": row.get("frame_count"),
        "photo_sha256": row.get("photo_sha256"),
        "notes": row.get("notes"),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Create / read / verify
# ---------------------------------------------------------------------------

def create(
    *,
    user: dict[str, Any],
    hive: dict[str, Any],
    organization: dict[str, Any],
    fields: dict[str, Any],
    batch_id: str,
    quality_score: float,
    quality_detail: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically create harvest event + batch block + link + BATCH_CREATED
    chain event. Returns ``(harvest_view, block, chain_event)``. Raises
    HarvestConflict on duplicate ids."""
    harvest_id = fields.get("harvest_id") or new_harvest_id(fields["harvest_date"])
    if db.harvest_exists(harvest_id):
        raise HarvestConflict(f"Harvest id '{harvest_id}' already exists")

    created_at = db.utc_now()
    hash_view = {
        "harvest_id": harvest_id,
        "hive_id": hive["hive_id"],
        "organization_id": organization["org_id"],
        "harvested_by": user["user_id"],
        "harvest_date": fields["harvest_date"],
        "harvested_at": fields.get("harvested_at"),
        "quantity_kg": fields["quantity_kg"],
        "floral_source": fields["floral_source"],
        "latitude": fields.get("latitude"),
        "longitude": fields.get("longitude"),
        "frame_count": fields.get("frame_count"),
        "photo_sha256": fields.get("photo_sha256"),
        "notes": fields.get("notes"),
        "created_at": created_at,
    }
    event_hash = compute_event_hash(hash_view)

    recorded: list[dict[str, Any]] = []

    def write_harvest(conn: Any, block: dict[str, Any]) -> None:
        db.insert_harvest_event(conn, {
            **hash_view,
            "hive_pk": int(hive["id"]),
            "beekeeper_user_pk": int(user["id"]),
            "organization_pk": int(organization["id"]),
            "status": "LINKED_TO_BATCH",
            "event_hash": event_hash,
        })
        db.link_batch_harvest(conn, block["batch_id"], harvest_id, fields["quantity_kg"])
        # Phase 6: the canonical chain event, in the SAME transaction. Payload
        # is identifiers + hashes only (no GPS, notes, names or quantities).
        recorded.append(chain_events.record_event(
            conn,
            event_type="BATCH_CREATED",
            resource_type="honey_batches",
            resource_id=block["batch_id"],
            organization_id=organization["org_id"],
            actor_id=user["user_id"],
            payload={
                "batch_id": block["batch_id"],
                "block_index": block["block_index"],
                "block_hash": block["block_hash"],
                "harvest_id": harvest_id,
                "harvest_event_hash": event_hash,
                "hive_id": hive["hive_id"],
            },
        ))

    try:
        block = ledger_module.ledger.add_batch(
            batch_id=batch_id,
            hive_id=hive["hive_id"],
            harvest_date=fields["harvest_date"],
            total_weight=fields["quantity_kg"],
            floral_type=fields["floral_source"],
            quality_score=quality_score,
            quality_detail=quality_detail,
            in_transaction=write_harvest,
        )
    except ledger_module.DuplicateBatchError as exc:
        raise HarvestConflict(str(exc)) from None
    except sqlite3.IntegrityError as exc:
        # A concurrent request won the race for the same harvest_id; the
        # whole transaction (block included) has already rolled back.
        raise HarvestConflict("Harvest or batch id already exists") from exc

    return serialize(db.get_harvest(harvest_id)), block, chain_events.serialize_event(recorded[0])


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Public operational view: external ids only, no internal primary keys."""
    view = _hash_view_from_row(row)
    view.pop("organization_id")
    return {
        **view,
        "organization_id": row["org_external_id"],
        "harvested_by_name": row.get("harvested_by_name"),
        "status": row["status"],
        "event_hash": row.get("event_hash"),
        "batches": db.batches_for_harvest(row["harvest_id"]),
    }


def verify(harvest_id: str) -> dict[str, Any]:
    """Recompute the event hash from stored fields and check every linked
    batch's ledger block. Detects edits to either record."""
    row = db.get_harvest(harvest_id)
    if row is None:
        return {"harvest_id": harvest_id, "found": False, "valid": False}
    recomputed = compute_event_hash(_hash_view_from_row(row))
    event_ok = bool(row.get("event_hash")) and recomputed == row["event_hash"]
    batch_checks = []
    for link in db.batches_for_harvest(harvest_id):
        result = ledger_module.ledger.verify_batch_authenticity(link["batch_id"])
        batch_checks.append({"batch_id": link["batch_id"],
                             "ledger_valid": bool(result.get("verified"))})
    return {
        "harvest_id": harvest_id,
        "found": True,
        "event_hash_valid": event_ok,
        "stored_event_hash": row.get("event_hash"),
        "recomputed_event_hash": recomputed,
        "batches": batch_checks,
        "valid": event_ok and bool(batch_checks) and all(b["ledger_valid"] for b in batch_checks),
    }
