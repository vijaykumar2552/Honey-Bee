"""
honeychain.authority
====================
Phase 10 -- regulator / authority oversight (read-only), served to AUTHORITY
and ADMIN only. AUTHORITY is not "admin with another label": these
endpoints expose aggregates, compliance and risk views across organizations,
while user/organization/device management stays ADMIN-only (enforced by the
existing routes). Issuing recalls (Phase 8) is the one authority write.

Every figure is computed from the database at request time -- nothing is
hard-coded or estimated. Where a dimension does not exist in the data, that
is stated rather than invented:

* "Region" is the hive's free-text ``location``; hives without one are
  grouped as "Unspecified". Organizations carry no region, so supply-chain
  volumes (processing, shipments, retail) are national totals, not regional.
* Geography is aggregated to 0.1-degree cells (~11 km); precise harvest
  coordinates are never returned.
* Suspicious scans are RISK SIGNALS with their evidence -- never presented
  as confirmed counterfeits.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, request

from . import auth, config, db

authority_bp = Blueprint("authority", __name__)

SIGNAL_NOTE = ("Risk signal derived from scan patterns. It is NOT a confirmed counterfeit; "
               "it indicates where investigation may be warranted.")
UNSPECIFIED = "Unspecified"


def _counts(sql: str, params: tuple = ()) -> dict[str, int]:
    return {r[0]: r[1] for r in db.query_all(sql, params)}


def _one(sql: str, params: tuple = ()) -> Any:
    row = db.query_one(sql, params)
    return row[0] if row else None


def _region_clause(alias: str, region: str | None) -> tuple[str, tuple]:
    if not region:
        return "", ()
    if region == UNSPECIFIED:
        return f" AND ({alias}.location IS NULL OR TRIM({alias}.location) = '')", ()
    return f" AND {alias}.location = ?", (region,)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

_ANCESTOR_CTE = """
WITH RECURSIVE ancestry(root, batch_id) AS (
    SELECT batch_id, batch_id FROM honey_batches
    UNION
    SELECT a.root, i.input_batch_id FROM ancestry a
    JOIN processing_events p ON p.output_batch_id = a.batch_id
    JOIN batch_blend_inputs i ON i.processing_id = p.processing_id
)
"""


def overview(region: str | None = None) -> dict[str, Any]:
    rc, rp = _region_clause("h", region)
    harvest = db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(e.quantity_kg), 0) AS kg FROM harvest_events e "
        f"JOIN hives h ON h.id = e.hive_id WHERE 1=1{rc};", rp)
    by_region = [
        {"region": r["region"], "harvests": r["n"], "harvest_kg": round(r["kg"], 3),
         "hives": r["hives"]}
        for r in db.query_all(
            "SELECT COALESCE(NULLIF(TRIM(h.location), ''), ?) AS region, COUNT(e.id) AS n, "
            "COALESCE(SUM(e.quantity_kg), 0) AS kg, COUNT(DISTINCT h.id) AS hives "
            "FROM hives h LEFT JOIN harvest_events e ON e.hive_id = h.id "
            "GROUP BY region ORDER BY kg DESC;", (UNSPECIFIED,))]
    units_total = _one("SELECT COUNT(*) FROM product_units;") or 0
    units_traceable = _one(
        _ANCESTOR_CTE + "SELECT COUNT(*) FROM product_units u WHERE EXISTS ("
        "SELECT 1 FROM ancestry a JOIN batch_harvest_links l ON l.batch_id = a.batch_id "
        "WHERE a.root = u.batch_id);") or 0
    batches_total = _one("SELECT COUNT(*) FROM honey_batches;") or 0
    batches_passed = _one("SELECT COUNT(DISTINCT batch_id) FROM lab_results WHERE status = 'PASSED';") or 0

    def pct(part: int, whole: int) -> float | None:
        return round(100.0 * part / whole, 1) if whole else None

    return {
        "region_filter": region,
        "note": "Region = hive location; supply-chain volumes are national (organizations "
                "have no region).",
        "production": {
            "active_beekeepers": _one("SELECT COUNT(*) FROM users WHERE role = 'BEEKEEPER' "
                                      "AND status = 'ACTIVE';"),
            "active_hives": _one(f"SELECT COUNT(*) FROM hives h WHERE h.status = 'ACTIVE'{rc};", rp),
            "harvests": harvest["n"],
            "harvest_kg": round(harvest["kg"], 3),
            "by_region": by_region,
        },
        "supply_chain": {
            "batches": batches_total,
            "processing_events": _one("SELECT COUNT(*) FROM processing_events;"),
            "shipments_by_status": _counts("SELECT status, COUNT(*) FROM shipments GROUP BY status;"),
            "retail_by_status": _counts("SELECT status, COUNT(*) FROM retail_records GROUP BY status;"),
            "units_by_status": _counts("SELECT status, COUNT(*) FROM product_units GROUP BY status;"),
        },
        "compliance": {
            "lab_results_by_status": _counts("SELECT status, COUNT(*) FROM lab_results GROUP BY status;"),
            "failed_lab_batches": [dict(r) for r in db.query_all(
                "SELECT l.batch_id, l.test_date, o.name AS laboratory FROM lab_results l "
                "LEFT JOIN organizations o ON o.id = l.lab_org_id WHERE l.status = 'FAILED' "
                "ORDER BY l.id DESC LIMIT 100;")],
            "batches_with_passed_lab_pct": pct(batches_passed, batches_total),
            "units_traceable_to_harvest_pct": pct(units_traceable, units_total),
            "units_total": units_total,
            "active_recalls": _one("SELECT COUNT(*) FROM recalls WHERE status = 'ACTIVE';"),
            "shipment_exceptions": [dict(r) for r in db.query_all(
                "SELECT s.batch_id, s.exception_reason, s.updated_at, f.name AS sender, t.name AS receiver "
                "FROM shipments s LEFT JOIN organizations f ON f.id = s.from_org_id "
                "LEFT JOIN organizations t ON t.id = s.to_org_id WHERE s.status = 'EXCEPTION' "
                "ORDER BY s.id DESC LIMIT 100;")],
        },
    }


# ---------------------------------------------------------------------------
# Hive-health hotspots and coarse geography
# ---------------------------------------------------------------------------

def hotspots(region: str | None = None) -> dict[str, Any]:
    rc, rp = _region_clause("h", region)
    latest = db.query_all(
        "SELECT COALESCE(NULLIF(TRIM(h.location), ''), ?) AS region, h.hive_id, "
        "(SELECT e.health_status FROM health_evaluations e WHERE e.hive_id = h.id "
        " ORDER BY e.id DESC LIMIT 1) AS latest_status "
        f"FROM hives h WHERE h.status = 'ACTIVE'{rc};", (UNSPECIFIED, *rp))
    regions: dict[str, dict[str, Any]] = {}
    for row in latest:
        r = regions.setdefault(row["region"], {"region": row["region"], "hives": 0, "CRITICAL": 0,
                                               "WARNING": 0, "HEALTHY": 0, "NO_DATA": 0})
        r["hives"] += 1
        r[row["latest_status"] or "NO_DATA"] += 1
    offline = db.query_all(
        "SELECT COALESCE(NULLIF(TRIM(h.location), ''), ?) AS region, COUNT(*) AS n FROM devices d "
        f"JOIN hives h ON h.id = d.hive_id WHERE d.status = 'OFFLINE'{rc} GROUP BY region;",
        (UNSPECIFIED, *rp))
    for row in offline:
        regions.setdefault(row["region"], {"region": row["region"], "hives": 0, "CRITICAL": 0,
                                           "WARNING": 0, "HEALTHY": 0, "NO_DATA": 0})
        regions[row["region"]]["devices_offline"] = row["n"]
    items = sorted(regions.values(), key=lambda r: (-r["CRITICAL"], -r["WARNING"], r["region"]))
    for r in items:
        r.setdefault("devices_offline", 0)
    geo = [{"lat": r["lat"], "lng": r["lng"], "harvests": r["n"], "harvest_kg": round(r["kg"], 3)}
           for r in db.query_all(
               "SELECT ROUND(latitude, 1) AS lat, ROUND(longitude, 1) AS lng, COUNT(*) AS n, "
               "SUM(quantity_kg) AS kg FROM harvest_events WHERE latitude IS NOT NULL "
               "GROUP BY lat, lng ORDER BY n DESC;")]
    return {"regions": items, "harvest_geo_cells": geo,
            "geo_precision": "0.1 degree cells (~11 km); precise coordinates are never exposed"}


# ---------------------------------------------------------------------------
# Suspicious-scan signals
# ---------------------------------------------------------------------------

def _unit_batch(unit_id: str) -> str | None:
    return _one("SELECT batch_id FROM product_units WHERE unit_id = ?;", (unit_id,))


def signals() -> dict[str, Any]:
    out: list[dict[str, Any]] = []

    def add(kind: str, severity: str, row, extra: str) -> None:
        out.append({"type": kind, "severity": severity, "classification": "SIGNAL",
                    "unit_id": row["unit_id"], "batch_id": _unit_batch(row["unit_id"]),
                    "occurrences": row["n"], "first_seen": row["first"], "last_seen": row["last"],
                    "evidence": extra})

    agg = ("SELECT unit_id, COUNT(*) AS n, MIN(scanned_at) AS first, MAX(scanned_at) AS last "
           "FROM consumer_scans WHERE unit_id IS NOT NULL AND ")
    for row in db.query_all(agg + "result = 'INVALID' GROUP BY unit_id HAVING n >= ?;",
                            (config.SIGNAL_FORGERY_ATTEMPTS,)):
        add("FORGED_TAG_ATTEMPTS", "HIGH", row,
            f"{row['n']} scans named this real unit with a wrong or modified tag")
    for row in db.query_all(agg + "result = 'AUTHENTIC' AND reason = 'RECALLED' GROUP BY unit_id;"):
        add("SCANNED_AFTER_RECALL", "HIGH", row, "a recalled unit is still being scanned by consumers")
    for row in db.query_all(agg + "result = 'REVOKED' GROUP BY unit_id;"):
        add("REVOKED_LABEL_SCANNED", "MEDIUM", row, "a revoked label is still in circulation")
    for row in db.query_all(agg + "result = 'AUTHENTIC' AND scanned_at >= DATETIME('now', '-1 day') "
                            "GROUP BY unit_id HAVING n >= ?;", (config.SIGNAL_SCAN_VELOCITY_24H,)):
        add("HIGH_SCAN_VELOCITY", "MEDIUM", row,
            f"{row['n']} authentic scans in 24 h -- possible copied label")
    for row in db.query_all(agg + "result = 'AUTHENTIC' AND reason = 'ISSUED' GROUP BY unit_id;"):
        add("SCANNED_BEFORE_RETAIL", "LOW", row,
            "scanned while the unit had not yet reached a retail shelf (may be staff checks)")
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    out.sort(key=lambda s: s["last_seen"] or "", reverse=True)   # newest first...
    out.sort(key=lambda s: rank[s["severity"]])                   # ...within severity (stable)
    return {
        "note": SIGNAL_NOTE,
        "thresholds": {"forged_tag_attempts": config.SIGNAL_FORGERY_ATTEMPTS,
                       "scan_velocity_24h": config.SIGNAL_SCAN_VELOCITY_24H},
        "signals": out,
        "invalid_scans_without_a_real_unit": _counts(
            "SELECT reason, COUNT(*) FROM consumer_scans WHERE unit_id IS NULL AND result = 'INVALID' "
            "GROUP BY reason;"),
        "scans_total": _one("SELECT COUNT(*) FROM consumer_scans;"),
    }


# ---------------------------------------------------------------------------
# Recalls with affected products
# ---------------------------------------------------------------------------

def recalls_view() -> list[dict[str, Any]]:
    from . import supply_chain as sc
    conn = db.get_connection()
    views = []
    for r in db.query_all("SELECT * FROM recalls ORDER BY id DESC;"):
        affected = [r["batch_id"], *sc.descendants(conn, r["batch_id"])]
        marks = ",".join("?" * len(affected))
        views.append({
            "recall_id": r["recall_id"], "root_batch_id": r["batch_id"], "status": r["status"],
            "reason": r["reason"], "issued_at": r["issued_at"],
            "affected_batches": affected,
            "units_by_status": _counts(f"SELECT status, COUNT(*) FROM product_units WHERE batch_id "
                                       f"IN ({marks}) GROUP BY status;", tuple(affected)),
            "retailers": [x["name"] for x in db.query_all(
                f"SELECT DISTINCT o.name FROM retail_records rr JOIN organizations o "
                f"ON o.id = rr.retailer_org_id WHERE rr.batch_id IN ({marks});", tuple(affected))],
            "consumer_scans_since_recall": _one(
                f"SELECT COUNT(*) FROM consumer_scans s JOIN product_units u ON u.unit_id = s.unit_id "
                f"WHERE u.batch_id IN ({marks}) AND s.scanned_at >= ?;", (*affected, r["issued_at"])),
        })
    return views


# ---------------------------------------------------------------------------
# Routes (AUTHORITY + ADMIN, read-only)
# ---------------------------------------------------------------------------

def _region() -> str | None:
    value = (request.args.get("region") or "").strip()
    return value[:120] or None


@authority_bp.get("/api/authority/overview")
@auth.require_role("AUTHORITY", "ADMIN")
def overview_route() -> Response:
    return jsonify(overview(_region()))


@authority_bp.get("/api/authority/hotspots")
@auth.require_role("AUTHORITY", "ADMIN")
def hotspots_route() -> Response:
    return jsonify(hotspots(_region()))


@authority_bp.get("/api/authority/signals")
@auth.require_role("AUTHORITY", "ADMIN")
def signals_route() -> Response:
    return jsonify(signals())


@authority_bp.get("/api/authority/recalls")
@auth.require_role("AUTHORITY", "ADMIN")
def recalls_route() -> Response:
    return jsonify(recalls_view())


# ---------------------------------------------------------------------------
# The built React frontend (Phase 10), served at /app/
# ---------------------------------------------------------------------------
_APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _frontend_response(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = _APP_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@authority_bp.get("/app/")
@authority_bp.get("/app/<path:filename>")
def frontend(filename: str = "index.html") -> Response:
    """Serve frontend/dist. send_from_directory rejects path traversal; any
    unknown path falls back to index.html (client-side routing)."""
    from flask import send_from_directory
    from werkzeug.exceptions import NotFound
    dist = config.FRONTEND_DIST
    if not (dist / "index.html").is_file():
        response = jsonify({"error": "Not Found", "message": "The frontend has not been built. "
                            "Run: cd frontend && npm install && npm run build"})
        response.status_code = 404
        return response
    try:
        return _frontend_response(send_from_directory(dist, filename))
    except NotFound:
        if filename.startswith("assets/"):
            raise                      # a missing asset is a real 404, not the app shell
        return _frontend_response(send_from_directory(dist, "index.html"))
