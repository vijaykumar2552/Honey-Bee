"""
honeychain.supply_routes
========================
HTTP layer for the Phase 8 supply chain. Every route is a thin wrapper over
honeychain.supply_chain; authorization happens in two layers: the role
decorator here (coarse) and the holder / party / organization checks inside
the service (fine-grained, inside the write transaction).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request

from . import auth, config, db, ratelimit
from . import supply_chain as sc

supply_bp = Blueprint("supply_chain", __name__)
_PUBLIC_VERIFY_LIMIT = ratelimit.limiter(config.PUBLIC_VERIFY_PER_MINUTE, 60)
_UNIT_RE = re.compile(r"^UNIT-[A-Z2-7]{16}$")


def _error(status: int, message: str, details: Any = None) -> Response:
    from .app import _error as app_error  # local: app imports this module
    names = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}
    return app_error(status, names.get(status, "Error"), message, details)


def _body() -> dict:
    from .app import _parse_json_body
    payload = _parse_json_body()
    if not isinstance(payload, dict):
        raise sc.SupplyChainError(400, "Request body must be a JSON object")
    return payload


def _run(fn: Callable[[], Any], status: int = 200) -> Response:
    try:
        result = fn()
    except sc.SupplyChainError as exc:
        return _error(exc.status, exc.message, exc.details)
    response = jsonify(result)
    response.status_code = status
    return response


def _viewable(batch_id: str) -> Response | None:
    if not sc.ID_RE.match(batch_id or "") or db.get_batch(batch_id) is None:
        return _error(404, "No such batch")
    if not sc.can_view_batch(auth.current_user(), batch_id):
        return _error(403, auth.FORBIDDEN_ERROR)
    return None


def _ip() -> str | None:
    return request.remote_addr


# -- processing / blending ----------------------------------------------------
@supply_bp.post("/api/batches/<batch_id>/processing")
@auth.require_role("PROCESSOR")
def processing_route(batch_id: str) -> Response:
    return _run(lambda: sc.record_processing(auth.current_user(), batch_id, _body(), _ip()), 201)


@supply_bp.post("/api/blends")
@auth.require_role("PROCESSOR")
def blend_route() -> Response:
    return _run(lambda: sc.blend(auth.current_user(), _body(), _ip()), 201)


# -- laboratory -----------------------------------------------------------------
@supply_bp.post("/api/batches/<batch_id>/lab-requests")
@auth.require_role(*sc.HOLDER_ROLES)
def lab_request_route(batch_id: str) -> Response:
    return _run(lambda: sc.request_lab_test(auth.current_user(), batch_id, _body(), _ip()), 201)


@supply_bp.get("/api/lab/requests")
@auth.require_role("LAB")
def lab_queue_route() -> Response:
    rows = db.query_all("SELECT * FROM lab_results WHERE lab_org_id = ? ORDER BY id DESC LIMIT 500;",
                        (auth.current_user().get("organization_id"),))
    return jsonify([sc.serialize_lab(dict(r)) for r in rows])


@supply_bp.post("/api/lab/results/<lab_result_id>")
@auth.require_role("LAB")
def lab_result_route(lab_result_id: str) -> Response:
    return _run(lambda: sc.complete_lab_result(auth.current_user(), lab_result_id, _body(), _ip()), 201)


@supply_bp.get("/api/batches/<batch_id>/lab-results")
@auth.require_auth
def lab_results_route(batch_id: str) -> Response:
    return _viewable(batch_id) or jsonify(sc.lab_summary(batch_id))


# -- packaging / units ----------------------------------------------------------
@supply_bp.post("/api/batches/<batch_id>/packaging")
@auth.require_role("PROCESSOR")
def packaging_route(batch_id: str) -> Response:
    response = _run(lambda: sc.package(auth.current_user(), batch_id, _body(), _ip()), 201)
    response.headers["Cache-Control"] = "no-store"  # contains one-time unit secrets
    return response


@supply_bp.get("/api/batches/<batch_id>/units")
@auth.require_auth
def units_route(batch_id: str) -> Response:
    denied = _viewable(batch_id)
    if denied:
        return denied
    rows = db.query_all("SELECT unit_id, packaging_id, status, created_at FROM product_units "
                        "WHERE batch_id = ? ORDER BY id;", (batch_id,))
    return jsonify([dict(r) for r in rows])  # never the secret hash


# -- custody ---------------------------------------------------------------------
@supply_bp.post("/api/custody/transfers")
@auth.require_role(*sc.HOLDER_ROLES)
def transfer_route() -> Response:
    return _run(lambda: sc.initiate_transfer(auth.current_user(), _body(), _ip()), 201)


@supply_bp.get("/api/custody/transfers")
@auth.require_auth
def transfers_route() -> Response:
    return jsonify(sc.list_transfers(auth.current_user()))


@supply_bp.post("/api/custody/transfers/<transfer_id>/<action>")
@auth.require_role(*sc.HOLDER_ROLES)
def resolve_route(transfer_id: str, action: str) -> Response:
    if action not in ("accept", "reject", "cancel"):
        return _error(404, "Unknown action")
    return _run(lambda: sc.resolve_transfer(auth.current_user(), transfer_id, action, _ip()))


# -- shipments -----------------------------------------------------------------
@supply_bp.post("/api/shipments")
@auth.require_role(*sc.HOLDER_ROLES)
def create_shipment_route() -> Response:
    return _run(lambda: sc.public_shipment(sc.create_shipment(auth.current_user(), _body(), _ip())), 201)


@supply_bp.get("/api/shipments")
@auth.require_auth
def shipments_route() -> Response:
    return jsonify(sc.list_shipments(auth.current_user()))


@supply_bp.get("/api/shipments/<shipment_id>")
@auth.require_auth
def shipment_route(shipment_id: str) -> Response:
    user = auth.current_user()
    if not sc.ID_RE.match(shipment_id or ""):
        return _error(404, "No such shipment")
    try:
        view = sc.get_shipment(shipment_id)
    except sc.SupplyChainError as exc:
        return _error(exc.status, exc.message)
    if user["role"] not in auth.ORG_UNRESTRICTED_ROLES and \
            user.get("organization_id") not in (view["_from_org_pk"], view["_to_org_pk"]):
        return _error(403, auth.FORBIDDEN_ERROR)
    return jsonify(sc.public_shipment(view))


@supply_bp.post("/api/shipments/<shipment_id>/status")
@auth.require_role(*sc.HOLDER_ROLES)
def shipment_status_route(shipment_id: str) -> Response:
    return _run(lambda: sc.public_shipment(
        sc.update_shipment(auth.current_user(), shipment_id, _body(), _ip())))


# -- retail ----------------------------------------------------------------------
@supply_bp.get("/api/retail")
@auth.require_auth
def retail_list_route() -> Response:
    return jsonify(sc.list_retail(auth.current_user()))


@supply_bp.post("/api/retail/<retail_record_id>/receive")
@auth.require_role("RETAILER")
def retail_receive_route(retail_record_id: str) -> Response:
    return _run(lambda: sc.receive_retail(auth.current_user(), retail_record_id, _ip()))


@supply_bp.post("/api/retail/<retail_record_id>/shelve")
@auth.require_role("RETAILER")
def retail_shelve_route(retail_record_id: str) -> Response:
    return _run(lambda: sc.shelve_retail(auth.current_user(), retail_record_id, _ip()))


@supply_bp.post("/api/retail/units/<unit_id>/sell")
@auth.require_role("RETAILER")
def retail_sell_route(unit_id: str) -> Response:
    return _run(lambda: sc.sell_unit(auth.current_user(), unit_id, _ip()))


# -- recall ----------------------------------------------------------------------
@supply_bp.post("/api/recalls")
@auth.require_role("AUTHORITY", "ADMIN")
def recall_route() -> Response:
    return _run(lambda: sc.issue_recall(auth.current_user(), _body(), _ip()), 201)


@supply_bp.get("/api/recalls")
@auth.require_auth
def recalls_route() -> Response:
    user = auth.current_user()
    rows = [dict(r) for r in db.query_all(
        "SELECT recall_id, batch_id, reason, status, issued_at FROM recalls ORDER BY id DESC LIMIT 500;")]
    if user["role"] not in auth.ORG_UNRESTRICTED_ROLES:
        rows = [r for r in rows if sc.can_view_batch(user, r["batch_id"])]
    return jsonify(rows)


# -- trace -------------------------------------------------------------------------
@supply_bp.get("/api/batches/<batch_id>/trace")
@auth.require_auth
def batch_trace_route(batch_id: str) -> Response:
    return _viewable(batch_id) or _run(lambda: sc.trace_batch(batch_id))


@supply_bp.get("/api/units/<unit_id>/trace")
@auth.require_auth
def unit_trace_route(unit_id: str) -> Response:
    if not _UNIT_RE.match(unit_id or ""):
        return _error(404, "No such product unit")
    unit = db.query_one("SELECT batch_id FROM product_units WHERE unit_id = ?;", (unit_id,))
    if unit is None:
        return _error(404, "No such product unit")
    if not sc.can_view_batch(auth.current_user(), unit["batch_id"]):
        return _error(403, auth.FORBIDDEN_ERROR)
    return _run(lambda: sc.trace_unit(unit_id))


# -- Phase 9: per-unit cryptographic QR + public consumer verification -------
@supply_bp.post("/api/public/verify")
def public_verify() -> Response:
    """PUBLIC (no login). Body: {"qr": "<scanned text>"}. POST keeps the QR tag
    out of URLs and access logs. Always 200 with an explicit result; error
    details never reveal whether a unit id exists."""
    from . import consumer
    allowed, retry_after = _PUBLIC_VERIFY_LIMIT.hit(request.remote_addr or "unknown")
    if not allowed:
        # Refused BEFORE verification: a flood never reaches the scan log, so
        # it cannot inflate the Phase 10 signals.
        response = jsonify({"result": "RATE_LIMITED",
                            "message": "Too many verification requests; try again shortly."})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    raw = request.get_json(silent=True)
    qr_text = raw.get("qr") if isinstance(raw, dict) else None
    response = jsonify(consumer.verify(qr_text))
    response.headers["Cache-Control"] = "no-store"   # recall must never be served stale
    return response


@supply_bp.get("/q")
def qr_landing_page() -> Response:
    """PUBLIC landing page encoded in every unit QR (payload in the #fragment).

    When the React app is built, redirect to its verification page. Browsers
    carry the #fragment across a redirect whose Location has none, and the
    fragment is never sent to the server, so the code still stays out of logs.
    Without a build, the self-contained template page is served instead, so
    printed QR codes keep working either way."""
    from flask import redirect, render_template
    if (config.FRONTEND_DIST / "index.html").is_file():
        response = redirect("/app/verify", code=302)
        response.headers["Cache-Control"] = "no-store"
        return response
    response = Response(render_template("unit_verify.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


def _unit_for_packager(unit_id: str):
    """(unit_row, packaging_org_pk) if the caller may manage this unit's label."""
    if not _UNIT_RE.match(unit_id or ""):
        return None, _error(404, "No such product unit")
    unit = db.query_one("SELECT u.*, p.organization_id AS packaging_org_id FROM product_units u "
                        "JOIN packaging_events p ON p.packaging_id = u.packaging_id "
                        "WHERE u.unit_id = ?;", (unit_id,))
    if unit is None:
        return None, _error(404, "No such product unit")
    user = auth.current_user()
    if user["role"] != "ADMIN" and user.get("organization_id") != unit["packaging_org_id"]:
        return None, _error(403, auth.FORBIDDEN_ERROR)
    return dict(unit), None


@supply_bp.post("/api/units/<unit_id>/qr")
@auth.require_role("PROCESSOR", "ADMIN")
def reissue_unit_qr(unit_id: str) -> Response:
    """Re-render a unit's QR. The packager must re-present the unit secret it
    received once at packaging; the server never stored it. Also completes
    QR issuance for units packaged before Phase 9 (no verifier yet)."""
    import base64
    from . import qrgen, unit_qr
    unit, error = _unit_for_packager(unit_id)
    if error:
        return error
    user = auth.current_user()
    secret = _body().get("unit_secret")
    if not unit_qr.secret_matches(secret if isinstance(secret, str) else "", unit["secret_hash"]):
        db.insert_audit_event(actor_user_pk=user["id"], organization_pk=user.get("organization_id"),
                              action="UNIT_QR_SECRET_MISMATCH", resource_type="product_units",
                              resource_id=unit_id, ip_address=_ip(), result="FAILURE")
        return _error(403, "The unit secret does not match this unit")
    if unit["qr_revoked_at"]:
        return _error(409, "This unit's label has been revoked")
    payload = unit_qr.payload(unit_id, secret)
    verifier = unit_qr.verifier(unit_qr.tag(unit_id, secret))
    with db.transaction() as conn:
        conn.execute("UPDATE product_units SET qr_verifier = ?, qr_version = ? "
                     "WHERE unit_id = ? AND (qr_verifier IS NULL OR qr_verifier = ?);",
                     (verifier, unit_qr.QR_VERSION, unit_id, verifier))
    db.insert_audit_event(actor_user_pk=user["id"], organization_pk=user.get("organization_id"),
                          action="UNIT_QR_ISSUED", resource_type="product_units", resource_id=unit_id,
                          ip_address=_ip(), result="SUCCESS")
    png = qrgen.render_qr_png_bytes(unit_qr.url(payload))   # in memory: no file, no path
    response = jsonify({"unit_id": unit_id, "qr_payload": payload, "qr_url": unit_qr.url(payload),
                        "qr_png_base64": base64.b64encode(png).decode("ascii")})
    response.headers["Cache-Control"] = "no-store"
    return response


@supply_bp.post("/api/units/<unit_id>/revoke-qr")
@auth.require_role("PROCESSOR", "ADMIN")
def revoke_unit_qr(unit_id: str) -> Response:
    """Revoke a unit's label (e.g. stolen or misprinted label stock)."""
    unit, error = _unit_for_packager(unit_id)
    if error:
        return error
    if unit["qr_revoked_at"]:
        return _error(409, "This unit's label is already revoked")
    with db.transaction() as conn:
        conn.execute("UPDATE product_units SET qr_revoked_at = ? WHERE unit_id = ? "
                     "AND qr_revoked_at IS NULL;", (db.utc_now(), unit_id))
    user = auth.current_user()
    db.insert_audit_event(actor_user_pk=user["id"], organization_pk=user.get("organization_id"),
                          action="UNIT_QR_REVOKED", resource_type="product_units", resource_id=unit_id,
                          ip_address=_ip(), result="SUCCESS")
    return jsonify({"unit_id": unit_id, "qr_revoked": True})
