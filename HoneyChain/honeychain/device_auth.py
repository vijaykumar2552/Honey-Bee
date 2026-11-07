"""
honeychain.device_auth
======================
Per-device IoT authentication (Phase 4). Replaces the fleet-wide shared
secret for all new telemetry ingestion.

Credential model
----------------
HMAC is symmetric, so the server must be able to *recompute* a device's
secret to verify its signatures -- a stored password-style hash could not do
that. Instead of storing raw secrets (or adding an encryption dependency),
every device secret is DERIVED:

    device_secret = HMAC-SHA256(master_key, "honeychain-device-v1|<device_id>|<version>")

* The database stores no raw secret, only ``credential_hash`` =
  SHA-256(device_secret), used as a fingerprint that must match on every
  request (so a master-key change without re-provisioning fails closed
  instead of silently authenticating against the wrong key).
* HMAC-SHA256 is a PRF: one device's leaked secret reveals nothing about any
  other device's secret, and nothing about the master key.
* Rotation = increment ``credential_version``; the old secret stops working
  immediately because the server now derives a different one.
* Revocation = ``status = 'REVOKED'``, checked on every request.

Request signing (see docs/IOT_PROTOCOL.md)
------------------------------------------
Headers: X-Device-Id, X-Device-Timestamp (unix seconds), X-Device-Nonce,
X-Device-Signature. The signature covers::

    "<device_id>\\n<timestamp>\\n<nonce>\\n<sha256_hex(raw_body)>"

so the body, the identity, the time and the one-time nonce are all bound
together; changing any of them invalidates the signature.

Replay protection = signed timestamp within DEVICE_REQUEST_MAX_SKEW_SECONDS
AND a (device, nonce) pair never seen before (UNIQUE constraint, so this is
race-safe even under concurrent requests).

Nothing in this module logs or returns a secret, a signature, or the master
key. Every rejection returns the same generic message to the client; the
specific reason is logged server-side only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import config, db

logger = logging.getLogger(__name__)

GENERIC_DEVICE_AUTH_ERROR = "Unauthorized: valid device authentication required"

DEVICE_ID_HEADER = "X-Device-Id"
TIMESTAMP_HEADER = "X-Device-Timestamp"
NONCE_HEADER = "X-Device-Nonce"
SIGNATURE_HEADER = "X-Device-Signature"

_DERIVATION_LABEL = "honeychain-device-v1"
_DEVICE_ID_RE = re.compile(r"^DEV-[A-Z0-9]{4,32}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    return bool(config.DEVICE_MASTER_KEY)


def derive_device_secret(device_id: str, version: int, master_key: str | None = None) -> str:
    key = master_key if master_key is not None else config.DEVICE_MASTER_KEY
    if not key:
        raise RuntimeError("HONEYCHAIN_DEVICE_MASTER_KEY is not configured")
    message = f"{_DERIVATION_LABEL}|{device_id}|{int(version)}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def credential_fingerprint(device_secret: str) -> str:
    return hashlib.sha256(device_secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Signing (shared by the server, the simulator and the protocol docs)
# ---------------------------------------------------------------------------

def canonical_string(device_id: str, timestamp: str, nonce: str, raw_body: bytes) -> str:
    body_digest = hashlib.sha256(raw_body).hexdigest()
    return f"{device_id}\n{timestamp}\n{nonce}\n{body_digest}"


def sign_request(device_id: str, device_secret: str, timestamp: str, nonce: str,
                 raw_body: bytes) -> str:
    return hmac.new(
        device_secret.encode("utf-8"),
        canonical_string(device_id, timestamp, nonce, raw_body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceAuthResult:
    authenticated: bool
    device: dict[str, Any] | None = None
    reason: str = ""          # server-side log only, never sent to the client
    replay: bool = False      # distinguishes an exact replay (409) from bad auth (401)
    request_timestamp: int | None = None


def _reject(reason: str, *, replay: bool = False, device: dict | None = None) -> DeviceAuthResult:
    return DeviceAuthResult(False, device=device, reason=reason, replay=replay)


def verify_request(headers: Any, raw_body: bytes, *, now: float | None = None) -> DeviceAuthResult:
    """Authenticate one device request. Never raises; always fails closed.

    Order: configuration -> header shape -> device exists -> not revoked ->
    fingerprint matches -> signature -> freshness -> nonce uniqueness. The
    nonce is recorded only after everything else passed, so an attacker
    cannot burn a legitimate device's future nonces with unsigned requests.
    """
    if not is_configured():
        logger.error("HONEYCHAIN_DEVICE_MASTER_KEY is not configured; rejecting device request")
        return _reject("server master key not configured")

    device_id = (headers.get(DEVICE_ID_HEADER) or "").strip()
    timestamp = (headers.get(TIMESTAMP_HEADER) or "").strip()
    nonce = (headers.get(NONCE_HEADER) or "").strip()
    signature = (headers.get(SIGNATURE_HEADER) or "").strip()

    if not (device_id and timestamp and nonce and signature):
        return _reject("missing device authentication header(s)")
    if not _DEVICE_ID_RE.match(device_id):
        return _reject("malformed device id")
    if not _NONCE_RE.match(nonce):
        return _reject("malformed nonce")
    if not _HEX64_RE.match(signature):
        return _reject("malformed signature")
    if not timestamp.isdigit() or len(timestamp) > 12:
        return _reject("malformed timestamp")

    device = db.get_device_by_device_id(device_id)
    if device is None:
        return _reject("unknown device")
    if device["status"] == "REVOKED":
        return _reject("device revoked", device=device)
    if not device.get("credential_hash"):
        return _reject("device has no provisioned credential", device=device)

    secret = derive_device_secret(device_id, int(device["credential_version"]))
    if not hmac.compare_digest(credential_fingerprint(secret), device["credential_hash"]):
        # Master key changed without re-provisioning: refuse rather than guess.
        logger.error("Credential fingerprint mismatch for %s (master key rotated?)", device_id)
        return _reject("credential fingerprint mismatch", device=device)

    expected = sign_request(device_id, secret, timestamp, nonce, raw_body)
    if not hmac.compare_digest(expected.lower(), signature.lower()):
        return _reject("signature mismatch", device=device)

    request_ts = int(timestamp)
    current = int(now if now is not None else time.time())
    if abs(current - request_ts) > config.DEVICE_REQUEST_MAX_SKEW_SECONDS:
        return _reject("request timestamp outside allowed window", device=device)

    try:
        db.record_device_nonce(device["id"], nonce)
    except sqlite3.IntegrityError:
        return _reject("nonce already used (replay)", replay=True, device=device)

    db.purge_device_nonces(config.DEVICE_REQUEST_MAX_SKEW_SECONDS * 2)
    return DeviceAuthResult(True, device=device, request_timestamp=request_ts)
