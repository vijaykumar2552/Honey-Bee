"""
honeychain.devices
==================
Device lifecycle (Phase 4): registration, credential rotation, revocation,
serialisation, and device-silence detection. Routes in honeychain.app stay
thin and call into here; the cryptography lives in honeychain.device_auth.

A device secret is returned exactly ONCE -- in the response to registration
or rotation -- and is never stored, logged, audited or returned again.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Any

from . import config, db, device_auth
from .events import broker

logger = logging.getLogger(__name__)

SECRET_NOTICE = (
    "Store this secret on the device now. It is shown only once and cannot be "
    "retrieved again; rotate the credential if it is lost."
)


def new_device_id() -> str:
    return f"DEV-{_secrets.token_hex(6).upper()}"


def serialize(device: dict[str, Any]) -> dict[str, Any]:
    """Public view of a device. Never includes credential_hash or any secret,
    and exposes hive/organization by external id, never internal pk."""
    return {
        "device_id": device["device_id"],
        "hive_id": device.get("hive_external_id"),
        "organization_id": device.get("org_external_id"),
        "status": device["status"],
        "firmware_version": device.get("firmware_version"),
        "data_origin": device.get("data_origin", "PHYSICAL"),
        "credential_version": device.get("credential_version"),
        "expected_interval_seconds": device.get("expected_interval_seconds")
        or config.DEVICE_DEFAULT_EXPECTED_INTERVAL_SECONDS,
        "last_seen_at": device.get("last_seen_at"),
        "battery_level": device.get("battery_level"),
        "created_at": device.get("created_at"),
        "updated_at": device.get("updated_at"),
    }


def _credentials_payload(device_id: str, version: int, secret: str) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "device_secret": secret,
        "credential_version": version,
        "signature_algorithm": "HMAC-SHA256",
        "signed_string": "<device_id>\\n<unix_timestamp>\\n<nonce>\\n<sha256_hex(body)>",
        "notice": SECRET_NOTICE,
    }


def register(
    *, hive: dict[str, Any], firmware_version: str | None, expected_interval: int | None,
    data_origin: str = "PHYSICAL",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a device bound to ``hive`` (and to the hive's organization).
    Returns ``(device_view, one_time_credentials)``."""
    device_id = new_device_id()
    while db.get_device_by_device_id(device_id) is not None:  # pragma: no cover
        device_id = new_device_id()
    secret = device_auth.derive_device_secret(device_id, 1)
    with db.transaction() as conn:
        db.create_device(
            conn,
            device_id=device_id,
            hive_pk=int(hive["id"]),
            organization_pk=int(hive["organization_id"]),
            credential_hash=device_auth.credential_fingerprint(secret),
            firmware_version=firmware_version,
            expected_interval_seconds=expected_interval,
            data_origin=data_origin,
        )
    return serialize(db.get_device_detail(device_id)), _credentials_payload(device_id, 1, secret)


def rotate(device: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    version = int(device["credential_version"]) + 1
    secret = device_auth.derive_device_secret(device["device_id"], version)
    db.update_device_credentials(
        device["device_id"], version, device_auth.credential_fingerprint(secret)
    )
    return (
        serialize(db.get_device_detail(device["device_id"])),
        _credentials_payload(device["device_id"], version, secret),
    )


def run_silence_check() -> list[dict[str, Any]]:
    """Mark silent ACTIVE devices OFFLINE, audit + notify once per transition.

    Idempotent for an unchanged condition: an already-OFFLINE device is not a
    candidate again until it reports (which makes it ACTIVE) and then goes
    silent anew.
    """
    silent = db.find_silent_devices(
        config.DEVICE_DEFAULT_EXPECTED_INTERVAL_SECONDS, config.DEVICE_SILENCE_MULTIPLIER
    )
    transitioned: list[dict[str, Any]] = []
    for device in silent:
        db.set_device_status(device["device_id"], "OFFLINE")
        db.insert_audit_event(
            actor_user_pk=None,
            organization_pk=device.get("organization_id"),
            action="DEVICE_SILENT",
            resource_type="devices",
            resource_id=device["device_id"],
            metadata=f"last_seen_at={device.get('last_seen_at')}",
            result="SUCCESS",
        )
        title = f"Device {device['device_id']} stopped reporting"
        body = (
            f"No telemetry from {device['device_id']} (hive "
            f"{device.get('hive_external_id')}) since {device.get('last_seen_at')} UTC. "
            "A silent device and a healthy hive look identical on the dashboard -- "
            "check power and connectivity."
        )
        if device.get("organization_id") is not None:
            for user in db.active_users_in_organization(int(device["organization_id"])):
                db.create_notification(
                    notification_id=f"NTF-{_secrets.token_hex(6).upper()}",
                    recipient_user_pk=int(user["id"]),
                    notification_type="DEVICE_SILENT",
                    title=title,
                    body=body,
                )
        broker.publish(
            "alert",
            {"type": "DEVICE_SILENT", "device_id": device["device_id"],
             "hive_id": device.get("hive_external_id"),
             "last_seen_at": device.get("last_seen_at")},
        )
        logger.warning("Device %s marked OFFLINE (silent since %s)",
                       device["device_id"], device.get("last_seen_at"))
        transitioned.append({"device_id": device["device_id"],
                             "hive_id": device.get("hive_external_id"),
                             "last_seen_at": device.get("last_seen_at")})
    return transitioned
