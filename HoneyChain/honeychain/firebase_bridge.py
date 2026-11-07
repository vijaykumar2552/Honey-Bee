"""
honeychain.firebase_bridge
==========================
**Optional** Firebase Realtime Database bridge.

The primary, and recommended, path from Firebase into HoneyChain is the *push*
direction: a Cloud Function (or the RTDB webhook) posts telemetry to
``POST /api/firebase/webhook`` with HMAC authentication. That path needs no
Firebase credentials on the HoneyChain host at all.

This module covers the *pull* direction -- reading a Firebase tree directly --
which is useful for backfilling history from an existing ESP32 deployment and
for the ``research/inspect_hive_data.py`` tool.

The original ``backend/firebase.py`` called ``firebase_admin.initialize_app()``
at import time, so importing anything that touched it crashed when the service
account JSON was absent. Initialisation here is lazy, guarded and reports
failure through a return value instead of an exception.

Enable with::

    HONEYCHAIN_FIREBASE=true
    FIREBASE_DATABASE_URL=https://<project>-default-rtdb.firebaseio.com
    FIREBASE_CREDENTIALS_PATH=/secure/path/service-account.json
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {"initialised": False, "ready": False, "error": None}


def is_enabled() -> bool:
    return bool(config.FIREBASE_ENABLED)


def _initialise() -> bool:
    if _state["initialised"]:
        return bool(_state["ready"])

    with _lock:
        if _state["initialised"]:
            return bool(_state["ready"])
        _state["initialised"] = True

        if not is_enabled():
            _state["error"] = "disabled (set HONEYCHAIN_FIREBASE=true)"
            return False
        if not config.FIREBASE_CREDENTIALS_PATH or not config.FIREBASE_DATABASE_URL:
            _state["error"] = (
                "FIREBASE_CREDENTIALS_PATH and FIREBASE_DATABASE_URL must both be set"
            )
            logger.warning("Firebase bridge unavailable: %s", _state["error"])
            return False

        try:
            import firebase_admin  # type: ignore
            from firebase_admin import credentials  # type: ignore
        except ImportError:
            _state["error"] = "firebase-admin is not installed"
            logger.warning("Firebase bridge unavailable: %s", _state["error"])
            return False

        try:
            if not firebase_admin._apps:  # noqa: SLF001 - documented module attribute
                certificate = credentials.Certificate(config.FIREBASE_CREDENTIALS_PATH)
                firebase_admin.initialize_app(
                    certificate, {"databaseURL": config.FIREBASE_DATABASE_URL}
                )
        except Exception as exc:
            _state["error"] = f"initialisation failed: {type(exc).__name__}"
            logger.warning("Firebase bridge unavailable: %s", _state["error"])
            return False

        _state["ready"] = True
        _state["error"] = None
        logger.info("Firebase Admin bridge initialised")
        return True


def status() -> dict[str, Any]:
    return {
        "enabled": is_enabled(),
        "ready": bool(_state["ready"]),
        "database_url_configured": bool(config.FIREBASE_DATABASE_URL),
        "credentials_configured": bool(config.FIREBASE_CREDENTIALS_PATH),
        "error": _state["error"],
    }


def read_path(path: str) -> Any | None:
    """Read a node from the Realtime Database, or ``None`` if unavailable."""
    if not _initialise():
        return None
    try:
        from firebase_admin import db as firebase_db  # type: ignore

        return firebase_db.reference(path).get()
    except Exception as exc:
        logger.warning("Firebase read of %s failed (%s)", path, type(exc).__name__)
        return None


def write_path(path: str, value: Any) -> bool:
    """Write a node. Returns False when the bridge is unavailable or the write fails."""
    if not _initialise():
        return False
    try:
        from firebase_admin import db as firebase_db  # type: ignore

        firebase_db.reference(path).set(value)
        return True
    except Exception as exc:
        logger.warning("Firebase write to %s failed (%s)", path, type(exc).__name__)
        return False


def update_path(path: str, values: dict[str, Any]) -> bool:
    if not _initialise():
        return False
    try:
        from firebase_admin import db as firebase_db  # type: ignore

        firebase_db.reference(path).update(values)
        return True
    except Exception as exc:
        logger.warning("Firebase update of %s failed (%s)", path, type(exc).__name__)
        return False


def save_batch_record(batch_id: str, batch_data: dict[str, Any]) -> bool:
    """Mirror an off-chain batch record into Firebase (best effort)."""
    return write_path(f"honey_batches/{batch_id}", batch_data)


def save_batch_blockchain_info(
    batch_id: str, tx_hash: str, contract_address: str | None, status_text: str
) -> bool:
    """Link a Firebase batch record to its on-chain anchor (best effort)."""
    return update_path(
        f"honey_batches/{batch_id}",
        {
            "blockchainTxHash": tx_hash,
            "contractAddress": contract_address,
            "blockchainStatus": status_text,
        },
    )
