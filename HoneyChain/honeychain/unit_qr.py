"""
honeychain.unit_qr
==================
Versioned, cryptographic per-unit QR codes (Phase 9).

Construction (version 1)
------------------------
Each product unit received a 192-bit random secret at packaging (Phase 8);
only SHA-256(secret) is stored. The QR carries a tag derived from it, never
the secret itself::

    tag      = base64url( HMAC-SHA256(key = unit_secret, msg = "HC1|" + unit_id) )   # 43 chars
    payload  = "HC1." + unit_id + "." + tag
    qr_text  = PUBLIC_BASE_URL + "/q#" + payload        # payload in the URL FRAGMENT

* HMAC binds the tag to the version AND the unit id: a tag copied onto a
  different unit id, or a different version, never verifies.
* The server stores only ``qr_verifier = SHA-256(tag)``, set once while the
  secret is legitimately available (at packaging, or when the packaging
  organization re-presents the secret). Verification is a constant-time
  comparison of SHA-256(tag) with that verifier. A leaked verifier cannot be
  turned back into a working tag, and the raw secret is never stored or
  embedded anywhere.
* The payload sits in the URL fragment, which browsers do not send in HTTP
  requests, so it does not end up in web-server access logs; the landing
  page posts it to the verification API.

Hashing reuses ``ledger.sha256_hex`` -- no new hashing scheme. A physical
label can still be photographed and copied; per-unit scan counts (recorded
here, analysed in Phase 10) are the signal for that, not this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from . import config, ledger as ledger_module

QR_VERSION = 1
_PREFIX = f"HC{QR_VERSION}"
_UNIT_RE = r"UNIT-[A-Z2-7]{16}"
_PAYLOAD_RE = re.compile(rf"^HC(\d{{1,3}})\.({_UNIT_RE})\.([A-Za-z0-9_-]{{43}})$")
MAX_INPUT_LENGTH = 512


class QRFormatError(ValueError):
    """``code`` is safe to return publicly: MALFORMED or UNSUPPORTED_VERSION."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def tag(unit_id: str, unit_secret: str) -> str:
    mac = hmac.new(unit_secret.encode("utf-8"), f"{_PREFIX}|{unit_id}".encode("utf-8"),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def verifier(tag_value: str) -> str:
    return ledger_module.sha256_hex(tag_value)


def payload(unit_id: str, unit_secret: str) -> str:
    return f"{_PREFIX}.{unit_id}.{tag(unit_id, unit_secret)}"


def url(payload_value: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/q#{payload_value}"


def secret_matches(unit_secret: str, stored_secret_hash: str | None) -> bool:
    """Constant-time check of a re-presented secret against Phase 8's hash."""
    if not stored_secret_hash or not isinstance(unit_secret, str) or not unit_secret:
        return False
    return hmac.compare_digest(ledger_module.sha256_hex(unit_secret), stored_secret_hash)


def parse(qr_text: object) -> tuple[int, str, str]:
    """Return ``(version, unit_id, tag)`` or raise QRFormatError.

    Accepts the bare payload or a full URL containing it after ``#``.
    Strict whitelist parsing: anything else is MALFORMED, a well-formed
    payload with another version is UNSUPPORTED_VERSION.
    """
    if not isinstance(qr_text, str) or not qr_text or len(qr_text) > MAX_INPUT_LENGTH:
        raise QRFormatError("MALFORMED", "QR content is missing, not text, or too long")
    text = qr_text.strip()
    if "#" in text:
        text = text.split("#", 1)[1]
    match = _PAYLOAD_RE.match(text)
    if not match:
        raise QRFormatError("MALFORMED", "QR content is not a HoneyChain unit code")
    version = int(match.group(1))
    if version != QR_VERSION:
        raise QRFormatError("UNSUPPORTED_VERSION", f"QR version {version} is not supported")
    return version, match.group(2), match.group(3)


def tag_matches(tag_value: str, stored_verifier: str | None) -> bool:
    if not stored_verifier:
        return False
    return hmac.compare_digest(verifier(tag_value), stored_verifier)
