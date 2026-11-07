"""
honeychain.security
===================
Authentication for the telemetry webhook.

Two accepted modes, both backed by the same ``HONEYCHAIN_WEBHOOK_SECRET``:

**A. Signature header (preferred)** -- ``X-HoneyChain-Signature`` carries the
hex HMAC-SHA256 of the *raw request body* keyed with the shared secret. This is
what the ESP32 firmware and the bundled simulator use, and it authenticates the
payload itself, not just the caller.

**B. Query parameter** -- ``?auth=<secret>``. Firebase Realtime Database
webhooks and some Cloud Functions triggers cannot attach custom headers, so the
secret is presented directly. It is compared in constant time and must only ever
travel over HTTPS in production.

Nothing in this module logs, echoes or returns the secret, a prefix of it, or
the expected signature. Failures return a fixed, generic reason string.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from . import config

logger = logging.getLogger(__name__)

#: Single generic message for every failure mode. Distinguishing "missing" from
#: "invalid" in the response body would tell an attacker which half of the
#: scheme they got right.
GENERIC_AUTH_ERROR = "Unauthorized: valid webhook authentication required"


@dataclass(frozen=True)
class AuthResult:
    authenticated: bool
    method: str  # "header" | "query" | "none"
    detail: str = ""


def compute_signature(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of ``body`` under ``secret``.

    Shared with simulate_hardware.py so the client and server can never drift.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _constant_time_equals(left: str, right: str) -> bool:
    """Constant-time string comparison (never short-circuits on first mismatch)."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def authenticate(
    raw_body: bytes,
    signature_header: str | None,
    auth_query: str | None,
) -> AuthResult:
    """Validate a webhook request. Never raises."""
    secret = config.WEBHOOK_SECRET

    if not secret:
        # Fail closed. An unset secret must not degrade into "allow everything".
        logger.error(
            "HONEYCHAIN_WEBHOOK_SECRET is not configured; rejecting webhook request"
        )
        return AuthResult(False, "none", "server secret not configured")

    if signature_header:
        candidate = signature_header.strip()
        # Tolerate the common "sha256=<hex>" prefix used by other webhook
        # providers so existing Cloud Function code can be reused unchanged.
        if candidate.lower().startswith("sha256="):
            candidate = candidate[7:]
        expected = compute_signature(raw_body, secret)
        if _constant_time_equals(candidate, expected):
            return AuthResult(True, "header")
        return AuthResult(False, "header", "signature mismatch")

    if auth_query:
        if _constant_time_equals(auth_query, secret):
            return AuthResult(True, "query")
        return AuthResult(False, "query", "shared secret mismatch")

    return AuthResult(False, "none", "no credentials presented")


def is_configured() -> bool:
    """Whether a webhook secret is present (never reveals its value)."""
    return bool(config.WEBHOOK_SECRET)
