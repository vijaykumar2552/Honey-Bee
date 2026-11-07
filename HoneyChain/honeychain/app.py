"""
honeychain.app
==============
The one and only production Flask application.

Routes
------
``GET  /``                             Real-time operations dashboard (HTML)
``GET  /verify/<batch_id>``            Public consumer verification page (HTML, no auth)
``POST /api/firebase/webhook``         Authenticated telemetry ingestion (device HMAC)
``POST /api/auth/login``               User login -> bearer token (public)
``POST /api/auth/logout``              Revoke the caller's current token (authenticated)
``GET  /api/auth/me``                  The caller's own user record (authenticated)
``POST /api/auth/register``            Create a user (ADMIN only)
``GET  /api/telemetry/latest``         Most recent joined telemetry + health rows (authenticated)
``GET  /api/telemetry/stream``         Server-Sent Events feed (authenticated)
``GET  /api/summary``                  Fleet metrics computed from the database (authenticated)
``GET  /api/hives``                    Registered hives (authenticated, org-scoped)
``GET  /api/hives/<hive_id>/history``  Per-hive telemetry history (authenticated, org-scoped)
``POST /api/harvest``                  Register a harvest -> ledger block + QR (BEEKEEPER/ADMIN, org-scoped)
``GET  /api/batches``                  Every ledger block (authenticated)
``GET  /api/verify/<batch_id>``        JSON cryptographic verification (public)
``GET  /api/chain``                    Whole-ledger integrity check (public)
``GET  /api/ml/status``                Live model state (authenticated)
``GET  /api/audit-logs``               Security audit trail (ADMIN/AUTHORITY only)
``GET  /api/health``                   Service / database / subsystem health (public)

Error handling: ``/api/*`` always answers with structured JSON; browser routes
render an error page. Stack traces are never returned to a client.

Authentication (Phase 3): every route above marked authenticated/role-
restricted is protected by ``honeychain.auth`` bearer tokens, independent of
the device-webhook HMAC scheme in ``honeychain.security``, which only ever
covers ``/api/firebase/webhook``. The two must not be confused: a valid user
token does not authenticate a device, and a valid device signature does not
authenticate a user.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets
from datetime import date, datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import auth, chain_anchor, config, db, device_auth, devices, firebase_bridge, ratelimit
from . import harvests as harvests_module
from . import chain_events
from . import supply_chain
from .supply_routes import supply_bp
from . import ledger as ledger_module
from . import quality as quality_module
from . import qrgen, security, validation
from .events import broker
from .ml import predictor
from .pipeline import ingest_batch, ingest_reading

logger = logging.getLogger(__name__)

#: Batch identifiers appear in URLs and filenames -- keep them to a safe alphabet.
BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: Upper bound on a single harvest, guarding against fat-fingered input.
MAX_BATCH_WEIGHT_KG = 10_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wants_json() -> bool:
    """True when the current request should receive a JSON error body."""
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _error(status: int, error: str, message: str, details: Any = None) -> Response:
    body: dict[str, Any] = {"error": error, "message": message}
    if details:
        body["details"] = details
    response = jsonify(body)
    response.status_code = status
    return response


def _serialize_user(user: dict[str, Any]) -> dict[str, Any]:
    """A user dict safe to return over the API -- never the password hash,
    and the organization is exposed by its external org_id, never the raw
    internal integer primary key."""
    org_external_id = None
    if user.get("organization_id") is not None:
        org = db.get_organization_by_pk(user["organization_id"])
        org_external_id = org["org_id"] if org else None
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "organization_id": org_external_id,
        "status": user["status"],
        "last_login_at": user.get("last_login_at"),
    }


def _parse_json_body() -> Any:
    """Strictly parse the request body as JSON.

    ``request.get_json`` is not used with ``force=True`` because a caller that
    sends the wrong content type should be told so explicitly rather than having
    the body reinterpreted silently.
    """
    raw = request.get_data(cache=True)
    if not raw:
        raise validation.ValidationError("Request body is empty")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise validation.ValidationError("Request body must be UTF-8 encoded") from None
    except json.JSONDecodeError as exc:
        raise validation.ValidationError(
            f"Malformed JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from None


def _validate_batch_id(value: Any) -> str:
    if value is None or not str(value).strip():
        raise validation.ValidationError(
            "batch_id is required", [{"field": "batch_id", "issue": "missing"}]
        )
    batch_id = str(value).strip()
    if not BATCH_ID_PATTERN.match(batch_id):
        raise validation.ValidationError(
            "Invalid batch_id: use 1-64 characters from A-Z, a-z, 0-9, '-' and '_'",
            [{"field": "batch_id", "issue": "format"}],
        )
    return batch_id


def _validate_floral_type(value: Any) -> str:
    if value is None or not str(value).strip():
        raise validation.ValidationError(
            "floral_type is required", [{"field": "floral_type", "issue": "missing"}]
        )
    floral = str(value).strip()
    if len(floral) > 120:
        raise validation.ValidationError(
            "floral_type must be 120 characters or fewer",
            [{"field": "floral_type", "issue": "too_long"}],
        )
    return floral


def _next_batch_id() -> str:
    """Generate the next sequential batch id, e.g. HNY-2026-0007."""
    year = date.today().year
    height = ledger_module.ledger.height()
    for offset in range(1, 10_000):
        candidate = f"HNY-{year}-{height + offset:04d}"
        if not db.batch_exists(candidate):
            return candidate
    raise ValueError("Unable to allocate a free batch id")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(initialise_db: bool = True, load_model: bool = True) -> Flask:
    config.configure_logging()
    config.ensure_runtime_directories()

    app = Flask(
        __name__,
        template_folder=str(config.TEMPLATES_DIR),
        static_folder=str(config.STATIC_DIR),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.config["JSON_SORT_KEYS"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    if initialise_db:
        db.initialise_database()
    if load_model:
        predictor.load()

    if not security.is_configured():
        logger.warning(
            "HONEYCHAIN_WEBHOOK_SECRET is not set -- the telemetry webhook will "
            "reject every request with 401 until it is configured."
        )
    if not auth.is_configured():
        logger.warning(
            "HONEYCHAIN_AUTH_SECRET is not set -- every authenticated API route "
            "will reject every request with 401 until it is configured."
        )

    app.teardown_appcontext(db.close_connection)
    _register_routes(app)
    _register_error_handlers(app)

    for problem in config.configuration_problems():
        logger.error("CONFIGURATION: %s", problem)
    logger.info("HoneyChain application ready (public base URL %s)", config.PUBLIC_BASE_URL)
    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: Flask) -> None:  # noqa: C901 - a route table is naturally long

    # -- Dashboard ---------------------------------------------------------
    @app.get("/")
    def dashboard() -> str:
        return render_template(
            "dashboard.html",
            version=__import__("honeychain").__version__,
            public_base_url=config.PUBLIC_BASE_URL,
        )

    # -- Telemetry ingestion ----------------------------------------------
    @app.post("/api/firebase/webhook")
    def firebase_webhook() -> Response:
        if not config.LEGACY_WEBHOOK_ENABLED:
            # Phase 4: the fleet-wide shared secret is off unless explicitly
            # re-enabled for migrating old firmware. 410 tells a client the
            # endpoint is intentionally retired, not temporarily broken.
            return _error(
                410,
                "Gone",
                "The shared-secret webhook is disabled. Register the device and "
                "use POST /api/devices/telemetry with per-device signatures.",
            )
        raw_body = request.get_data(cache=True)

        auth = security.authenticate(
            raw_body=raw_body,
            signature_header=request.headers.get(config.SIGNATURE_HEADER),
            auth_query=request.args.get(config.AUTH_QUERY_PARAM),
        )
        if not auth.authenticated:
            # Log the reason server-side only; the client gets a generic message.
            logger.warning(
                "Webhook auth rejected (method=%s, reason=%s, remote=%s)",
                auth.method,
                auth.detail,
                request.remote_addr,
            )
            return _error(401, "Unauthorized", security.GENERIC_AUTH_ERROR)

        payload = _parse_json_body()
        candidates = validation.normalise_payload(payload)
        readings = validation.validate_batch(candidates)
        stored = ingest_batch(readings)

        logger.info(
            "Telemetry accepted: %d reading(s) via %s auth", len(stored), auth.method
        )
        response = jsonify(
            {
                "status": "accepted",
                "ingested": len(stored),
                "records": stored,
            }
        )
        response.status_code = 200
        return response

    # -- Per-device telemetry (Phase 4) ------------------------------------
    @app.post("/api/devices/telemetry")
    def device_telemetry() -> Response:
        raw_body = request.get_data(cache=True)
        result = device_auth.verify_request(request.headers, raw_body)
        if not result.authenticated:
            logger.warning(
                "Device auth rejected (device=%s, reason=%s, remote=%s)",
                (request.headers.get(device_auth.DEVICE_ID_HEADER) or "-")[:40],
                result.reason,
                request.remote_addr,
            )
            if result.device is not None:
                db.insert_audit_event(
                    actor_user_pk=None,
                    organization_pk=result.device.get("organization_id"),
                    action="DEVICE_AUTH_REPLAY" if result.replay else "DEVICE_AUTH_FAILURE",
                    resource_type="devices",
                    resource_id=result.device["device_id"],
                    metadata=result.reason,
                    ip_address=request.remote_addr,
                    result="FAILURE",
                )
            if result.replay:
                return _error(409, "Conflict", "Replay detected: this request was already accepted.")
            return _error(401, "Unauthorized", device_auth.GENERIC_DEVICE_AUTH_ERROR)

        device = result.device
        if device.get("hive_id") is None:
            return _error(403, "Forbidden", "This device is not bound to a hive.")
        hive = db.get_hive_by_pk(int(device["hive_id"]))
        if hive is None or hive.get("organization_id") != device.get("organization_id"):
            db.insert_audit_event(
                actor_user_pk=None, organization_pk=device.get("organization_id"),
                action="DEVICE_BINDING_VIOLATION", resource_type="devices",
                resource_id=device["device_id"],
                metadata="bound hive missing or owned by a different organization",
                ip_address=request.remote_addr, result="FAILURE",
            )
            return _error(403, "Forbidden", "Device and hive organization do not match.")

        payload = _parse_json_body()
        readings = validation.validate_batch(validation.normalise_payload(payload))
        now = datetime.now(timezone.utc)
        prepared: list[tuple[dict, str]] = []
        for reading in readings:
            if reading["hive_id"] != hive["hive_id"]:
                db.insert_audit_event(
                    actor_user_pk=None, organization_pk=device.get("organization_id"),
                    action="DEVICE_HIVE_MISMATCH", resource_type="devices",
                    resource_id=device["device_id"],
                    metadata=f"claimed={reading['hive_id']} bound={hive['hive_id']}",
                    ip_address=request.remote_addr, result="FAILURE",
                )
                return _error(403, "Forbidden", "A device may only report for its bound hive.")
            # The device's own event time: per-reading if supplied, otherwise
            # the (signed, freshness-checked) request timestamp. Never silently
            # replaced by server receipt time.
            raw_ts = reading.get("device_timestamp") or result.request_timestamp
            device_ts = validation.validate_device_timestamp(
                raw_ts, now=now,
                future_tolerance_seconds=config.DEVICE_FUTURE_TOLERANCE_SECONDS,
                max_backfill_seconds=config.DEVICE_MAX_BACKFILL_SECONDS,
            )
            prepared.append((reading, device_ts))

        stored = [ingest_reading(r, device=device, device_timestamp=ts) for r, ts in prepared]
        broker.publish("summary", db.dashboard_summary())

        if device["status"] == "OFFLINE":
            db.insert_audit_event(
                actor_user_pk=None, organization_pk=device.get("organization_id"),
                action="DEVICE_BACK_ONLINE", resource_type="devices",
                resource_id=device["device_id"], ip_address=request.remote_addr,
                result="SUCCESS",
            )
        return jsonify({"status": "accepted", "ingested": len(stored), "records": stored})

    # -- Hive provisioning (Phase 4) ---------------------------------------
    @app.post("/api/hives")
    @auth.require_role("ADMIN", "BEEKEEPER")
    def create_hive_route() -> Response:
        user = auth.current_user()
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Hive payload must be a JSON object")
        hive_id = validation.validate_hive_id(payload.get("hive_id"))
        if db.get_hive_by_external_id(hive_id) is not None:
            return _error(409, "Conflict", f"Hive '{hive_id}' already exists")

        if user["role"] == "ADMIN":
            org_ext = payload.get("organization_id")
            org = db.get_organization_by_org_id(str(org_ext)) if org_ext else None
            if org_ext and org is None:
                return _error(400, "Bad Request", f"Unknown organization_id '{org_ext}'")
            organization_pk = org["id"] if org else None
        else:
            # A beekeeper always creates hives in their OWN organization; a
            # client-supplied organization_id is ignored, never trusted.
            if user.get("organization_id") is None:
                return _error(403, "Forbidden", "Your account has no organization.")
            organization_pk = user["organization_id"]

        name = str(payload.get("name") or "").strip()[:120] or None
        location = str(payload.get("location") or "").strip()[:200] or None
        with db.transaction() as conn:
            db.create_hive(conn, hive_id=hive_id, organization_pk=organization_pk,
                           name=name, location=location)
        db.insert_audit_event(
            actor_user_pk=user["id"], organization_pk=organization_pk,
            action="HIVE_CREATED", resource_type="hives", resource_id=hive_id,
            ip_address=request.remote_addr, result="SUCCESS",
        )
        response = jsonify(db.get_hive_by_external_id(hive_id))
        response.status_code = 201
        return response

    @app.post("/api/hives/<hive_id>/assign")
    @auth.require_role("ADMIN")
    def assign_hive_route(hive_id: str) -> Response:
        admin = auth.current_user()
        hive_id = validation.validate_hive_id(hive_id)
        if db.get_hive_by_external_id(hive_id) is None:
            return _error(404, "Not Found", f"No hive registered with id '{hive_id}'")
        payload = _parse_json_body()
        org_ext = payload.get("organization_id") if isinstance(payload, dict) else None
        org = db.get_organization_by_org_id(str(org_ext)) if org_ext else None
        if org is None:
            return _error(400, "Bad Request", "A valid organization_id is required")
        db.assign_hive_organization(hive_id, org["id"])
        db.insert_audit_event(
            actor_user_pk=admin["id"], organization_pk=org["id"],
            action="HIVE_ASSIGNED", resource_type="hives", resource_id=hive_id,
            metadata=f"organization={org['org_id']}", ip_address=request.remote_addr,
            result="SUCCESS",
        )
        return jsonify(db.get_hive_by_external_id(hive_id))

    # -- Device management (Phase 4) ---------------------------------------
    def _load_device_for(user: dict, device_id: str):
        """Load a device and enforce organization isolation. Returns
        (device, None) or (None, error_response)."""
        if not re.match(r"^DEV-[A-Z0-9]{4,32}$", device_id or ""):
            return None, _error(404, "Not Found", "No such device")
        device = db.get_device_detail(device_id)
        if device is None:
            return None, _error(404, "Not Found", "No such device")
        if not auth.can_access_organization(user, device.get("organization_id")):
            return None, _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        return device, None

    @app.post("/api/devices")
    @auth.require_role("ADMIN", "BEEKEEPER")
    def register_device_route() -> Response:
        user = auth.current_user()
        if not device_auth.is_configured():
            return _error(503, "Service Unavailable",
                          "Device credentials cannot be issued: server master key not configured.")
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Device payload must be a JSON object")
        hive_id = validation.validate_hive_id(payload.get("hive_id"))
        hive = db.get_hive_by_external_id(hive_id)
        if hive is None:
            return _error(404, "Not Found", f"No hive registered with id '{hive_id}'")
        if not auth.can_access_organization(user, hive.get("organization_id")):
            return _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        if hive.get("organization_id") is None:
            return _error(400, "Bad Request",
                          "Assign the hive to an organization before binding a device to it.")

        interval = payload.get("expected_interval_seconds")
        if interval is not None:
            try:
                interval = int(interval)
            except (TypeError, ValueError):
                interval = -1
            if not 10 <= interval <= 86_400:
                raise validation.ValidationError(
                    "expected_interval_seconds must be an integer between 10 and 86400",
                    [{"field": "expected_interval_seconds", "issue": "out_of_range"}],
                )
        firmware = str(payload.get("firmware_version") or "").strip()[:64] or None
        # Phase 7: a simulator must be declared as one, so its readings are
        # labelled SIMULATED everywhere instead of passing as real telemetry.
        data_origin = str(payload.get("data_origin") or "PHYSICAL").strip().upper()
        if data_origin not in ("PHYSICAL", "SIMULATED"):
            raise validation.ValidationError(
                "data_origin must be PHYSICAL or SIMULATED",
                [{"field": "data_origin", "issue": "invalid"}])

        view, credentials = devices.register(hive=hive, firmware_version=firmware,
                                             expected_interval=interval,
                                             data_origin=data_origin)
        db.insert_audit_event(
            actor_user_pk=user["id"], organization_pk=hive.get("organization_id"),
            action="DEVICE_REGISTERED", resource_type="devices",
            resource_id=view["device_id"], metadata=f"hive_id={hive_id}",
            ip_address=request.remote_addr, result="SUCCESS",
        )
        response = jsonify({"device": view, "credentials": credentials})
        response.status_code = 201
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/devices")
    @auth.require_auth
    def list_devices_route() -> Response:
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            rows = db.list_devices()
        elif user.get("organization_id") is None:
            rows = []
        else:
            rows = db.list_devices(restrict_to_org_id=user["organization_id"])
        return jsonify([devices.serialize(r) for r in rows])

    @app.post("/api/devices/silence-check")
    @auth.require_role("ADMIN")
    def device_silence_check_route() -> Response:
        return jsonify({"marked_offline": devices.run_silence_check()})

    @app.get("/api/devices/<device_id>")
    @auth.require_auth
    def get_device_route(device_id: str) -> Response:
        device, error = _load_device_for(auth.current_user(), device_id)
        return error if error else jsonify(devices.serialize(device))

    @app.post("/api/devices/<device_id>/rotate-secret")
    @auth.require_role("ADMIN", "BEEKEEPER")
    def rotate_device_route(device_id: str) -> Response:
        user = auth.current_user()
        device, error = _load_device_for(user, device_id)
        if error:
            return error
        if device["status"] == "REVOKED":
            return _error(409, "Conflict", "A revoked device cannot be re-credentialed.")
        if not device_auth.is_configured():
            return _error(503, "Service Unavailable", "Server master key not configured.")
        view, credentials = devices.rotate(device)
        db.insert_audit_event(
            actor_user_pk=user["id"], organization_pk=device.get("organization_id"),
            action="DEVICE_CREDENTIAL_ROTATED", resource_type="devices",
            resource_id=device_id, metadata=f"version={view['credential_version']}",
            ip_address=request.remote_addr, result="SUCCESS",
        )
        response = jsonify({"device": view, "credentials": credentials})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/devices/<device_id>/revoke")
    @auth.require_role("ADMIN", "BEEKEEPER")
    def revoke_device_route(device_id: str) -> Response:
        user = auth.current_user()
        device, error = _load_device_for(user, device_id)
        if error:
            return error
        if device["status"] == "REVOKED":
            return _error(409, "Conflict", "Device is already revoked.")
        db.set_device_status(device_id, "REVOKED")
        db.insert_audit_event(
            actor_user_pk=user["id"], organization_pk=device.get("organization_id"),
            action="DEVICE_REVOKED", resource_type="devices", resource_id=device_id,
            ip_address=request.remote_addr, result="SUCCESS",
        )
        return jsonify(devices.serialize(db.get_device_detail(device_id)))

    @app.get("/api/notifications")
    @auth.require_auth
    def notifications_route() -> Response:
        return jsonify(db.list_notifications_for_user(int(auth.current_user()["id"])))

    @app.get("/api/telemetry/latest")
    @auth.require_auth
    def telemetry_latest() -> Response:
        try:
            limit = min(max(int(request.args.get("limit", config.LATEST_LIMIT)), 1), 500)
        except ValueError:
            limit = config.LATEST_LIMIT
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            return jsonify(db.latest_telemetry(limit))
        if user.get("organization_id") is None:
            return jsonify([])
        return jsonify(db.latest_telemetry(limit, restrict_to_org_id=user["organization_id"]))

    @app.get("/api/telemetry/stream")
    @auth.require_auth
    def telemetry_stream() -> Response:
        response = Response(broker.stream(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache, no-transform"
        response.headers["X-Accel-Buffering"] = "no"  # disable nginx buffering
        response.headers["Connection"] = "keep-alive"
        return response

    @app.get("/api/summary")
    @auth.require_auth
    def summary() -> Response:
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            return jsonify(db.dashboard_summary())
        if user.get("organization_id") is None:
            return jsonify(
                {
                    "active_hives": 0,
                    "avg_temperature": None,
                    "avg_weight": None,
                    "overall_health_percent": None,
                    "reading_count": 0,
                    "status_counts": {"HEALTHY": 0, "WARNING": 0, "CRITICAL": 0},
                }
            )
        return jsonify(db.dashboard_summary(restrict_to_org_id=user["organization_id"]))

    @app.get("/api/hives")
    @auth.require_auth
    def hives() -> Response:
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            return jsonify(db.list_hives())
        if user.get("organization_id") is None:
            return jsonify([])
        return jsonify(db.list_hives(restrict_to_org_id=user["organization_id"]))

    @app.get("/api/hives/<hive_id>/history")
    @auth.require_auth
    def hive_history(hive_id: str) -> Response:
        hive_id = validation.validate_hive_id(hive_id)
        hive = db.get_hive_by_external_id(hive_id)
        if hive is None:
            return _error(404, "Not Found", f"No hive registered with id '{hive_id}'")
        user = auth.current_user()
        if not auth.can_access_organization(user, hive.get("organization_id")):
            return _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        try:
            limit = min(max(int(request.args.get("limit", 200)), 1), 2000)
        except ValueError:
            limit = 200
        return jsonify(db.hive_history(hive_id, limit))

    # -- Harvest / ledger --------------------------------------------------
    @app.post("/api/harvest")
    @auth.require_role("ADMIN", "BEEKEEPER")
    def create_harvest() -> Response:
        """Secure harvest workflow (Phase 5) -- see honeychain/harvests.py."""
        user = auth.current_user()
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Harvest payload must be a JSON object")

        hive_id = validation.validate_hive_id(payload.get("hive_id") or payload.get("hiveId"))
        hive = db.get_hive_by_external_id(hive_id)
        if hive is None:
            return _error(404, "Not Found", f"No hive registered with id '{hive_id}'")

        def deny(reason: str) -> Response:
            db.insert_audit_event(
                actor_user_pk=user["id"], organization_pk=user.get("organization_id"),
                action="HARVEST_DENIED", resource_type="hives", resource_id=hive_id,
                metadata=reason, ip_address=request.remote_addr, result="FAILURE",
            )
            return _error(403, "Forbidden", auth.FORBIDDEN_ERROR)

        if not auth.can_access_organization(user, hive.get("organization_id")):
            return deny("hive belongs to another organization")
        if hive.get("organization_id") is None:
            return _error(400, "Bad Request",
                          "This hive is not assigned to an organization; a harvest needs an "
                          "owning organization for provenance. Assign the hive first.",
                          [{"field": "hive_id", "issue": "unassigned"}])
        organization = db.get_organization_by_pk(int(hive["organization_id"]))
        if organization is None or organization["status"] != "ACTIVE":
            return deny("owning organization is not active")
        if hive.get("status") != "ACTIVE":
            return _error(409, "Conflict", f"Hive '{hive_id}' is {hive.get('status')}, not ACTIVE")

        claimed_org = payload.get("organization_id")
        if claimed_org is not None and str(claimed_org) != organization["org_id"]:
            raise validation.ValidationError(
                "organization_id does not match the hive's owning organization",
                [{"field": "organization_id", "issue": "does_not_match_hive"}],
            )

        harvest_date, harvested_at = harvests_module.validate_harvest_time(
            payload.get("harvest_date") or payload.get("harvestDate") or payload.get("harvested_at")
        )
        quantity_kg = harvests_module.validate_quantity(
            payload.get("total_weight") if payload.get("total_weight") is not None
            else payload.get("weight"),
            payload.get("unit"),
            MAX_BATCH_WEIGHT_KG,
        )
        floral_source = _validate_floral_type(payload.get("floral_type") or payload.get("floralType"))
        fields = {
            "harvest_id": harvests_module.validate_harvest_id(payload.get("harvest_id")),
            "harvest_date": harvest_date,
            "harvested_at": harvested_at,
            "quantity_kg": quantity_kg,
            "floral_source": floral_source,
            **harvests_module.validate_optional_fields(payload),
        }
        batch_id = (_validate_batch_id(payload["batch_id"]) if payload.get("batch_id")
                    else _next_batch_id())
        if db.batch_exists(batch_id):
            return _error(409, "Conflict", f"Batch id '{batch_id}' already exists in the ledger",
                          [{"field": "batch_id", "issue": "duplicate"}])
        if fields["harvest_id"] and db.harvest_exists(fields["harvest_id"]):
            return _error(409, "Conflict", f"Harvest id '{fields['harvest_id']}' already exists",
                          [{"field": "harvest_id", "issue": "duplicate"}])

        try:
            score, breakdown = quality_module.compute_quality_score(hive_id)
        except quality_module.InsufficientTelemetry as exc:
            return _error(400, "Bad Request", str(exc))

        try:
            harvest, block, chain_event = harvests_module.create(
                user=user, hive=hive, organization=organization, fields=fields,
                batch_id=batch_id, quality_score=score, quality_detail=breakdown,
            )
        except harvests_module.HarvestConflict as exc:
            return _error(409, "Conflict", str(exc))

        qr_info: dict[str, Any] | None = None
        try:
            qr_info = qrgen.generate_batch_qr(batch_id)
            db.set_batch_qr_path(batch_id, qr_info["static_path"])
        except qrgen.QRGenerationError as exc:
            # The block is committed and verifiable; a missing PNG is degraded,
            # not fatal, so it is reported rather than turned into a 500.
            logger.error("QR generation failed for %s: %s", batch_id, exc)

        # Phase 6: no blockchain call on the request path. BATCH_CREATED was
        # recorded in the chain-event log inside the harvest transaction; it
        # reaches the chain inside the next Merkle checkpoint.
        for action, resource_type, resource_id, meta in (
            ("HARVEST_CREATED", "harvest_events", harvest["harvest_id"],
             f"hive_id={hive_id} batch_id={batch_id} event_hash={harvest['event_hash']}"),
            ("BATCH_CREATED", "honey_batches", batch_id,
             f"harvest_id={harvest['harvest_id']} block_index={block['block_index']} "
             f"block_hash={block['block_hash']}"),
        ):
            db.insert_audit_event(
                actor_user_pk=user["id"], organization_pk=organization["id"], action=action,
                resource_type=resource_type, resource_id=resource_id, metadata=meta,
                ip_address=request.remote_addr, result="SUCCESS",
            )

        broker.publish("harvest", {"batch_id": batch_id, "hive_id": hive_id,
                                   "harvest_id": harvest["harvest_id"]})

        response = jsonify({
            "status": "created",
            "harvest": harvest,
            "batch": {key: block[key] for key in (
                "batch_id", "block_index", "hive_id", "harvest_date", "total_weight",
                "floral_type", "quality_score", "timestamp", "block_hash", "previous_hash")},
            "quality_detail": breakdown,
            "qr": qr_info,
            "verify_url": config.public_verify_url(batch_id),
            "chain_event": {**chain_event, "anchoring": "PENDING_CHECKPOINT"},
        })
        response.status_code = 201
        return response

    @app.get("/api/harvest")
    @auth.require_auth
    def list_harvests_route() -> Response:
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            rows = db.list_harvests()
        elif user.get("organization_id") is None:
            rows = []
        else:
            rows = db.list_harvests(restrict_to_org_id=user["organization_id"])
        return jsonify([harvests_module.serialize(r) for r in rows])

    def _load_harvest_for(user: dict, harvest_id: str):
        if not harvests_module.HARVEST_ID_RE.match(str(harvest_id or "").upper()):
            return None, _error(404, "Not Found", "No such harvest")
        row = db.get_harvest(str(harvest_id).upper())
        if row is None:
            return None, _error(404, "Not Found", "No such harvest")
        if not auth.can_access_organization(user, row.get("organization_id")):
            return None, _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        return row, None

    @app.get("/api/harvest/<harvest_id>")
    @auth.require_auth
    def get_harvest_route(harvest_id: str) -> Response:
        row, error = _load_harvest_for(auth.current_user(), harvest_id)
        if error:
            return error
        return jsonify({**harvests_module.serialize(row),
                        "verification": harvests_module.verify(row["harvest_id"])})

    @app.get("/api/batches/<batch_id>/provenance")
    @auth.require_auth
    def batch_provenance_route(batch_id: str) -> Response:
        user = auth.current_user()
        batch_id = _validate_batch_id(batch_id)
        batch = ledger_module.ledger.get_batch(batch_id)
        if batch is None:
            return _error(404, "Not Found", "No such batch")
        if not supply_chain.can_view_batch(user, batch_id):
            return _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        harvest_rows = [db.get_harvest(h) for h in db.harvests_for_batch(batch_id)]
        return jsonify({
            "batch_id": batch_id,
            "hive_id": batch["hive_id"],
            "ledger": ledger_module.ledger.verify_batch_authenticity(batch_id),
            "harvests": [{**harvests_module.serialize(r),
                          "verification": harvests_module.verify(r["harvest_id"])}
                         for r in harvest_rows if r is not None],
        })

    app.register_blueprint(supply_bp)
    from .authority import authority_bp  # Phase 10 oversight (AUTHORITY/ADMIN, read-only)
    app.register_blueprint(authority_bp)

    # -- Chain events & checkpoints (Phase 6) ------------------------------
    _EVENT_ID_RE = re.compile(r"^EVT-[0-9A-F]{16}$")
    _CHECKPOINT_ID_RE = re.compile(r"^CHK-\d{8}-\d{8}$")

    def _user_org_external_id(user: dict) -> str | None:
        if user.get("organization_id") is None:
            return None
        org = db.get_organization_by_pk(int(user["organization_id"]))
        return org["org_id"] if org else None

    @app.get("/api/chain/events")
    @auth.require_auth
    def list_chain_events_route() -> Response:
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            rows = chain_events.list_events()
        else:
            org_ext = _user_org_external_id(user)
            rows = chain_events.list_events(organization_id=org_ext, restrict=True) if org_ext else []
        return jsonify([chain_events.serialize_event(r) for r in rows])

    @app.get("/api/chain/events/<event_id>")
    @auth.require_auth
    def get_chain_event_route(event_id: str) -> Response:
        user = auth.current_user()
        if not _EVENT_ID_RE.match(event_id or ""):
            return _error(404, "Not Found", "No such event")
        event = chain_events.get_event(event_id)
        if event is None:
            return _error(404, "Not Found", "No such event")
        if user["role"] not in auth.ORG_UNRESTRICTED_ROLES and (
            event["organization_id"] is None or event["organization_id"] != _user_org_external_id(user)
        ):
            return _error(403, "Forbidden", auth.FORBIDDEN_ERROR)
        return jsonify({**chain_events.serialize_event(event),
                        "proof": chain_events.event_proof(event_id)})

    @app.get("/api/chain/log/verify")
    @auth.require_role("ADMIN", "AUTHORITY")
    def verify_chain_log_route() -> Response:
        return jsonify(chain_events.verify_log())

    @app.get("/api/chain/checkpoints")
    @auth.require_role("ADMIN", "AUTHORITY")
    def list_checkpoints_route() -> Response:
        return jsonify(chain_events.list_checkpoints())

    @app.post("/api/chain/checkpoints")
    @auth.require_role("ADMIN")
    def create_checkpoint_route() -> Response:
        checkpoint = chain_events.create_checkpoint()
        if checkpoint is None:
            return jsonify({"created": None, "message": "No new events since the last checkpoint"})
        db.insert_audit_event(
            actor_user_pk=auth.current_user()["id"], organization_pk=None,
            action="CHECKPOINT_CREATED", resource_type="blockchain_checkpoints",
            resource_id=checkpoint["checkpoint_id"],
            metadata=f"seq {checkpoint['from_seq']}-{checkpoint['to_seq']} root={checkpoint['merkle_root']}",
            ip_address=request.remote_addr, result="SUCCESS",
        )
        response = jsonify({"created": checkpoint})
        response.status_code = 201
        return response

    @app.post("/api/chain/checkpoints/submit")
    @auth.require_role("ADMIN")
    def submit_checkpoints_route() -> Response:
        return jsonify({"results": chain_events.submit_checkpoints()})

    @app.get("/api/chain/checkpoints/<checkpoint_id>/verify")
    def verify_checkpoint_route(checkpoint_id: str) -> Response:
        """Public: returns only hashes and transaction references."""
        if not _CHECKPOINT_ID_RE.match(checkpoint_id or ""):
            return _error(404, "Not Found", "No such checkpoint")
        result = chain_events.verify_checkpoint(checkpoint_id)
        if not result["found"]:
            return _error(404, "Not Found", "No such checkpoint")
        return jsonify(result)

    # -- Authentication ------------------------------------------------
    _login_limit = ratelimit.limiter(config.LOGIN_PER_MINUTE_PER_ADDRESS, 60)

    @app.post("/api/auth/login")
    def auth_login() -> Response:
        # Per client address (the per-email throttle below cannot stop one
        # address spraying many different emails).
        allowed, retry_after = _login_limit.hit(request.remote_addr or "unknown")
        if not allowed:
            response = _error(429, "Too Many Requests", "Too many login attempts; try again shortly.")
            response.headers["Retry-After"] = str(retry_after)
            return response
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Login payload must be a JSON object")

        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        if not email or not password:
            return _error(400, "Bad Request", "email and password are required")

        if (
            db.recent_login_failures(email, auth.LOGIN_FAILURE_WINDOW_SECONDS)
            >= auth.LOGIN_FAILURE_LIMIT
        ):
            db.insert_audit_event(
                actor_user_pk=None,
                organization_pk=None,
                action="LOGIN_THROTTLED",
                metadata=email,
                ip_address=request.remote_addr,
                result="FAILURE",
            )
            return _error(
                429,
                "Too Many Requests",
                "Too many failed login attempts. Try again later.",
            )

        user = db.get_user_by_email(email)
        password_ok = user is not None and auth.verify_password(
            password, user["password_hash"]
        )
        if not password_ok or user["status"] != "ACTIVE":
            db.insert_audit_event(
                actor_user_pk=user["id"] if user else None,
                organization_pk=user.get("organization_id") if user else None,
                action="LOGIN_FAILURE",
                metadata=email,
                ip_address=request.remote_addr,
                result="FAILURE",
            )
            return _error(401, "Unauthorized", auth.GENERIC_LOGIN_ERROR)

        token, _jti = auth.generate_token(user)
        db.update_last_login(user["user_id"])
        db.insert_audit_event(
            actor_user_pk=user["id"],
            organization_pk=user.get("organization_id"),
            action="LOGIN_SUCCESS",
            metadata=email,
            ip_address=request.remote_addr,
            result="SUCCESS",
        )

        return jsonify(
            {
                "token": token,
                "token_type": "Bearer",
                "expires_in": config.AUTH_TOKEN_TTL_SECONDS,
                "user": _serialize_user(user),
            }
        )

    @app.post("/api/auth/logout")
    @auth.require_auth
    def auth_logout() -> Response:
        user = auth.current_user()
        jti = auth.current_token_jti()
        if jti:
            db.revoke_token(jti)
        db.insert_audit_event(
            actor_user_pk=user["id"],
            organization_pk=user.get("organization_id"),
            action="LOGOUT",
            ip_address=request.remote_addr,
            result="SUCCESS",
        )
        return jsonify({"status": "logged_out"})

    @app.get("/api/auth/me")
    @auth.require_auth
    def auth_me() -> Response:
        return jsonify(_serialize_user(auth.current_user()))

    # -- Organization management (ADMIN) ------------------------------------
    # Acceptance step 1 ("Admin creates organization") previously had no API:
    # organizations could only be created by writing to the database.
    _ORG_TYPES = ("BEEKEEPER", "FPO_AGGREGATOR", "PROCESSOR", "LAB", "LOGISTICS",
                  "RETAILER", "AUTHORITY", "ADMIN")
    _ORG_ID_RE = re.compile(r"^ORG-[A-Z0-9][A-Z0-9-]{1,40}$")

    def _serialize_org(row: dict) -> dict:
        return {k: row[k] for k in ("org_id", "name", "org_type", "status", "created_at")}

    @app.get("/api/admin/organizations")
    @auth.require_role("ADMIN", "AUTHORITY")
    def list_organizations_route() -> Response:
        rows = db.query_all("SELECT * FROM organizations ORDER BY org_id;")
        return jsonify([_serialize_org(dict(r)) for r in rows])

    @app.post("/api/admin/organizations")
    @auth.require_role("ADMIN")
    def create_organization_route() -> Response:
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Organization payload must be a JSON object")
        name = str(payload.get("name") or "").strip()
        org_type = str(payload.get("org_type") or "").strip().upper()
        org_id = str(payload.get("org_id") or "").strip().upper() or f"ORG-{_secrets.token_hex(4).upper()}"
        if not 2 <= len(name) <= 120:
            raise validation.ValidationError("name (2-120 characters) is required",
                                             [{"field": "name", "issue": "length"}])
        if org_type not in _ORG_TYPES:
            raise validation.ValidationError(f"org_type must be one of {', '.join(_ORG_TYPES)}",
                                             [{"field": "org_type", "issue": "invalid"}])
        if not _ORG_ID_RE.match(org_id):
            raise validation.ValidationError("org_id must look like ORG-XXXX",
                                             [{"field": "org_id", "issue": "format"}])
        if db.get_organization_by_org_id(org_id) is not None:
            return _error(409, "Conflict", f"Organization '{org_id}' already exists")
        with db.transaction() as conn:
            db.create_organization(conn, org_id=org_id, name=name, org_type=org_type)
        created = db.get_organization_by_org_id(org_id)
        db.insert_audit_event(
            actor_user_pk=auth.current_user()["id"], organization_pk=created["id"],
            action="ORGANIZATION_CREATED", resource_type="organizations", resource_id=org_id,
            metadata=f"type={org_type}", ip_address=request.remote_addr, result="SUCCESS")
        response = jsonify(_serialize_org(created))
        response.status_code = 201
        return response

    @app.post("/api/admin/organizations/<org_id>/status")
    @auth.require_role("ADMIN")
    def set_organization_status_route(org_id: str) -> Response:
        org = db.get_organization_by_org_id(str(org_id).upper()) if _ORG_ID_RE.match(str(org_id).upper()) else None
        if org is None:
            return _error(404, "Not Found", "No such organization")
        payload = _parse_json_body()
        status = str((payload or {}).get("status") or "").strip().upper() if isinstance(payload, dict) else ""
        if status not in ("ACTIVE", "SUSPENDED", "DISABLED"):
            raise validation.ValidationError("status must be ACTIVE, SUSPENDED or DISABLED",
                                             [{"field": "status", "issue": "invalid"}])
        with db.transaction() as conn:
            conn.execute("UPDATE organizations SET status = ?, updated_at = ? WHERE id = ?;",
                         (status, db.utc_now(), org["id"]))
        db.insert_audit_event(
            actor_user_pk=auth.current_user()["id"], organization_pk=org["id"],
            action="ORGANIZATION_STATUS_CHANGED", resource_type="organizations", resource_id=org["org_id"],
            metadata=f"{org['status']}->{status}", ip_address=request.remote_addr, result="SUCCESS")
        return jsonify(_serialize_org(db.get_organization_by_org_id(org["org_id"])))

    @app.post("/api/auth/register")
    @auth.require_role("ADMIN")
    def auth_register() -> Response:
        admin = auth.current_user()
        payload = _parse_json_body()
        if not isinstance(payload, dict):
            raise validation.ValidationError("Register payload must be a JSON object")

        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        name = str(payload.get("name") or "").strip()
        role = str(payload.get("role") or "").strip().upper()
        org_external_id = payload.get("organization_id")

        if not email or "@" not in email:
            return _error(400, "Bad Request", "A valid email is required")
        if len(password) < 8:
            return _error(400, "Bad Request", "Password must be at least 8 characters")
        if not name:
            return _error(400, "Bad Request", "name is required")
        if role not in auth.ROLES:
            return _error(
                400, "Bad Request", f"role must be one of: {', '.join(auth.ROLES)}"
            )
        if db.get_user_by_email(email) is not None:
            return _error(400, "Bad Request", "A user with this email already exists")

        organization_pk = None
        if org_external_id:
            org = db.get_organization_by_org_id(str(org_external_id))
            if org is None:
                return _error(
                    400, "Bad Request", f"Unknown organization_id '{org_external_id}'"
                )
            organization_pk = org["id"]

        new_user_id = f"USR-{_secrets.token_hex(4).upper()}"
        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id=new_user_id,
                organization_pk=organization_pk,
                email=email,
                password_hash=auth.hash_password(password),
                name=name,
                role=role,
                phone=payload.get("phone"),
            )
        created = db.get_user_by_user_id(new_user_id)

        db.insert_audit_event(
            actor_user_pk=admin["id"],
            organization_pk=admin.get("organization_id"),
            action="USER_CREATED",
            resource_type="users",
            resource_id=new_user_id,
            metadata=f"role={role}",
            ip_address=request.remote_addr,
            result="SUCCESS",
        )

        response = jsonify(_serialize_user(created))
        response.status_code = 201
        return response

    @app.get("/api/audit-logs")
    @auth.require_role("ADMIN", "AUTHORITY")
    def audit_logs() -> Response:
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 1000)
        except ValueError:
            limit = 100
        return jsonify(db.list_audit_events(limit))

    @app.get("/api/batches")
    @auth.require_auth
    def list_batches() -> Response:
        # Organization-scoped (a pre-Phase-5 cross-organization leak: every
        # authenticated user used to receive every organization's batches).
        user = auth.current_user()
        if user["role"] in auth.ORG_UNRESTRICTED_ROLES:
            allowed = None
        elif user.get("organization_id") is None:
            allowed = set()
        else:
            allowed = db.batch_ids_for_org(int(user["organization_id"]))
        batches = []
        for row in ledger_module.ledger.all_batches(ascending=False):
            if allowed is not None and row["batch_id"] not in allowed:
                continue
            record = dict(row)
            detail = record.pop("quality_detail", None)
            if detail:
                try:
                    record["quality_detail"] = json.loads(detail)
                except (TypeError, json.JSONDecodeError):
                    record["quality_detail"] = None
            record["verify_url"] = config.public_verify_url(record["batch_id"])
            batches.append(record)
        return jsonify(batches)

    @app.get("/api/verify/<batch_id>")
    def verify_batch_api(batch_id: str) -> Response:
        batch_id = _validate_batch_id(batch_id)
        result = ledger_module.ledger.verify_batch_authenticity(batch_id)
        if not result["found"]:
            response = jsonify(result)
            response.status_code = 404
            return response

        record = db.get_batch(batch_id)
        result["batch"] = {
            "batch_id": record["batch_id"],
            "hive_id": record["hive_id"],
            "harvest_date": record["harvest_date"],
            "total_weight": record["total_weight"],
            "floral_type": record["floral_type"],
            "quality_score": record["quality_score"],
        }
        result["production_audit"] = quality_module.production_health_audit(
            record["hive_id"]
        )
        return jsonify(result)

    @app.get("/api/chain")
    def chain_status() -> Response:
        return jsonify(ledger_module.ledger.verify_chain())

    # -- Public consumer verification page --------------------------------
    @app.get("/verify/<batch_id>")
    def verify_batch_page(batch_id: str):
        try:
            batch_id = _validate_batch_id(batch_id)
        except validation.ValidationError:
            return (
                render_template(
                    "verify.html",
                    found=False,
                    batch_id=str(batch_id)[:64],
                    message="That batch reference is not a valid HoneyChain identifier.",
                ),
                404,
            )

        verification = ledger_module.ledger.verify_batch_authenticity(batch_id)
        if not verification["found"]:
            return (
                render_template(
                    "verify.html",
                    found=False,
                    batch_id=batch_id,
                    message=(
                        "No honey batch with this reference exists in the HoneyChain "
                        "ledger. Treat this product as unverified."
                    ),
                ),
                404,
            )

        record = db.get_batch(batch_id)
        audit = quality_module.production_health_audit(record["hive_id"])
        return render_template(
            "verify.html",
            found=True,
            batch_id=batch_id,
            batch=record,
            verification=verification,
            audit=audit,
            qr_static_path=record.get("qr_path"),
        )

    # -- Diagnostics -------------------------------------------------------
    @app.get("/api/ml/status")
    @auth.require_auth
    def ml_status() -> Response:
        from . import rules as rules_module
        status = predictor.status()
        status["fusion_policy"] = {
            "rules_authoritative": True,
            "ml_max_escalation": config.ML_MAX_ESCALATION,
            "ml_suppressed_when": ["any feature imputed", "sensor data-quality flags present"],
        }
        status["rule_thresholds"] = {
            name: {"value": value,
                   "source": "env override" if name in config.RULE_THRESHOLDS else "default"}
            for name, value in rules_module.active_thresholds().items()
        }
        return jsonify(status)

    @app.get("/api/health")
    def service_health() -> Response:
        try:
            pragmas = db.pragma_report()
            database_ok = True
        except Exception as exc:  # pragma: no cover - only on a broken DB file
            logger.error("Database health check failed: %s", type(exc).__name__)
            pragmas, database_ok = {}, False

        return jsonify(
            {
                "status": "ok" if database_ok else "degraded",
                "version": __import__("honeychain").__version__,
                "database": {"ok": database_ok, "path": str(config.DATABASE_PATH), **pragmas},
                "webhook_secret_configured": security.is_configured(),
                "auth_secret_configured": auth.is_configured(),
                "device_master_key_configured": device_auth.is_configured(),
                "legacy_webhook_enabled": config.LEGACY_WEBHOOK_ENABLED,
                "configuration_ok": not config.configuration_problems(),
                "ml": predictor.status(),
                "sse": {
                    "clients": broker.client_count,
                    "events_published": broker.published_count,
                },
                "ledger": {"height": ledger_module.ledger.height()},
                "chain_anchor": chain_anchor.status(),
                "firebase_bridge": firebase_bridge.status(),
                "public_base_url": config.PUBLIC_BASE_URL,
            }
        )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

def _register_error_handlers(app: Flask) -> None:

    @app.errorhandler(validation.ValidationError)
    def handle_validation_error(exc: validation.ValidationError):
        if _wants_json():
            return _error(400, "Bad Request", exc.message, exc.errors)
        return render_template("error.html", code=400, message=exc.message), 400

    @app.errorhandler(404)
    def handle_not_found(_exc):
        message = "The requested resource does not exist."
        if _wants_json():
            return _error(404, "Not Found", message)
        return render_template("error.html", code=404, message=message), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(_exc):
        message = "That HTTP method is not allowed on this endpoint."
        if _wants_json():
            return _error(405, "Method Not Allowed", message)
        return render_template("error.html", code=405, message=message), 405

    @app.errorhandler(413)
    def handle_payload_too_large(_exc):
        message = (
            f"Request body exceeds the {config.MAX_CONTENT_LENGTH} byte limit."
        )
        if _wants_json():
            return _error(413, "Payload Too Large", message)
        return render_template("error.html", code=413, message=message), 413

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        message = exc.description or "Request could not be processed."
        if _wants_json():
            return _error(exc.code or 500, exc.name, message)
        return render_template("error.html", code=exc.code or 500, message=message), (
            exc.code or 500
        )

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Full detail to the server log; a generic message to the client. No
        # stack trace, exception text or file path ever crosses the boundary.
        logger.exception("Unhandled error on %s %s", request.method, request.path)
        message = "An internal error occurred. The incident has been logged."
        if _wants_json():
            return _error(500, "Internal Server Error", message)
        return render_template("error.html", code=500, message=message), 500
