"""
Phase 4 -- per-device IoT security.

Every scenario in the Phase 4 spec is exercised against the real HTTP
endpoints (Flask test client) and the real database, not mocks:
registration, one-time secret disclosure, organization isolation / IDOR on
device records, valid/invalid/unknown/revoked/wrong-device signatures, body
tampering, device<->hive binding, cross-organization binding, exact replay,
duplicate nonce, stale/future request timestamps, malformed nonce, credential
rotation, per-reading device-timestamp preservation and drift rejection,
device-silence detection (including no repeated alerting), sensor-fault
flags, the legacy webhook's disabled-by-default state, and fail-closed
behaviour when the master key is missing or changed.
"""

from __future__ import annotations

import json
import secrets
import time
import unittest

from tests.base import HoneyChainTestCase, TEST_DEVICE_MASTER_KEY

from honeychain import config, db, device_auth


def _nonce() -> str:
    return secrets.token_hex(16)


class DeviceTestCase(HoneyChainTestCase):
    """Shared fixtures: org A with a beekeeper, hive and registered device;
    org B with its own beekeeper and hive."""

    def setUp(self) -> None:
        super().setUp()
        self.org_a = self.create_org(org_id="ORG-A", name="Org A")
        self.org_b = self.create_org(org_id="ORG-B", name="Org B")
        self.bk_a = self.create_user(user_id="USR-BK-A", email="bka@hc.test",
                                     role="BEEKEEPER", organization_pk=self.org_a)
        self.bk_b = self.create_user(user_id="USR-BK-B", email="bkb@hc.test",
                                     role="BEEKEEPER", organization_pk=self.org_b)
        self.h_a = self.auth_headers_for(self.bk_a)
        self.h_b = self.auth_headers_for(self.bk_b)
        self.assertEqual(self.client.post("/api/hives", json={"hive_id": "HIVE-A"},
                                          headers=self.h_a).status_code, 201)
        self.assertEqual(self.client.post("/api/hives", json={"hive_id": "HIVE-B"},
                                          headers=self.h_b).status_code, 201)
        self.device_id, self.secret = self.register("HIVE-A", self.h_a)

    # -- helpers -----------------------------------------------------------
    def register(self, hive_id: str, headers: dict) -> tuple[str, str]:
        response = self.client.post("/api/devices", json={"hive_id": hive_id}, headers=headers)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        creds = response.get_json()["credentials"]
        return creds["device_id"], creds["device_secret"]

    def device_post(self, payload, *, device_id=None, secret=None, ts=None, nonce=None,
                    raw=None, sign_body=None):
        device_id = device_id or self.device_id
        secret = secret or self.secret
        body = raw if raw is not None else json.dumps(payload).encode()
        ts = str(int(time.time()) if ts is None else ts)
        nonce = nonce or _nonce()
        signature = device_auth.sign_request(
            device_id, secret, ts, nonce, sign_body if sign_body is not None else body
        )
        return self.client.post(
            "/api/devices/telemetry", data=body, content_type="application/json",
            headers={device_auth.DEVICE_ID_HEADER: device_id,
                     device_auth.TIMESTAMP_HEADER: ts,
                     device_auth.NONCE_HEADER: nonce,
                     device_auth.SIGNATURE_HEADER: signature},
        )

    def good_reading(self, **extra):
        reading = {"hive_id": "HIVE-A", "temperature": 34.6, "humidity": 60.0, "weight": 41.0}
        reading.update(extra)
        return reading

    def telemetry_count(self) -> int:
        return db.query_one("SELECT COUNT(*) AS n FROM telemetry_readings;")["n"]

    def audit_actions(self) -> list[str]:
        return [e["action"] for e in db.list_audit_events(200)]


# ---------------------------------------------------------------------------
class TestCredentialDerivation(unittest.TestCase):
    def test_deterministic_per_device_and_version(self) -> None:
        a1 = device_auth.derive_device_secret("DEV-AAAA", 1, TEST_DEVICE_MASTER_KEY)
        self.assertEqual(a1, device_auth.derive_device_secret("DEV-AAAA", 1, TEST_DEVICE_MASTER_KEY))
        self.assertNotEqual(a1, device_auth.derive_device_secret("DEV-BBBB", 1, TEST_DEVICE_MASTER_KEY))
        self.assertNotEqual(a1, device_auth.derive_device_secret("DEV-AAAA", 2, TEST_DEVICE_MASTER_KEY))
        self.assertNotEqual(a1, device_auth.derive_device_secret("DEV-AAAA", 1, "other-master"))

    def test_fingerprint_is_not_the_secret(self) -> None:
        secret = device_auth.derive_device_secret("DEV-AAAA", 1, TEST_DEVICE_MASTER_KEY)
        self.assertNotEqual(device_auth.credential_fingerprint(secret), secret)


# ---------------------------------------------------------------------------
class TestRegistrationAndIsolation(DeviceTestCase):
    def test_secret_returned_once_and_never_again(self) -> None:
        stored = db.get_device_by_device_id(self.device_id)
        self.assertNotEqual(stored["credential_hash"], self.secret)
        body = self.client.get(f"/api/devices/{self.device_id}", headers=self.h_a).get_data(as_text=True)
        self.assertNotIn(self.secret, body)
        self.assertNotIn("credential_hash", body)
        listing = self.client.get("/api/devices", headers=self.h_a).get_data(as_text=True)
        self.assertNotIn(self.secret, listing)
        self.assertNotIn(self.secret, str(db.list_audit_events(200)))

    def test_new_device_is_provisioned_bound_to_hive_and_org(self) -> None:
        view = self.client.get(f"/api/devices/{self.device_id}", headers=self.h_a).get_json()
        self.assertEqual(view["hive_id"], "HIVE-A")
        self.assertEqual(view["organization_id"], "ORG-A")
        self.assertEqual(view["status"], "PROVISIONED")
        self.assertIn("DEVICE_REGISTERED", self.audit_actions())

    def test_cannot_register_device_on_another_orgs_hive(self) -> None:
        r = self.client.post("/api/devices", json={"hive_id": "HIVE-B"}, headers=self.h_a)
        self.assertEqual(r.status_code, 403)

    def test_consumer_cannot_register_devices(self) -> None:
        consumer = self.create_user(user_id="USR-C", email="c@hc.test", role="CONSUMER")
        r = self.client.post("/api/devices", json={"hive_id": "HIVE-A"},
                             headers=self.auth_headers_for(consumer))
        self.assertEqual(r.status_code, 403)

    def test_unknown_and_unassigned_hive(self) -> None:
        self.assertEqual(self.client.post("/api/devices", json={"hive_id": "HIVE-NOPE"},
                                          headers=self.admin_headers()).status_code, 404)
        self.client.post("/api/hives", json={"hive_id": "HIVE-UNOWNED"}, headers=self.admin_headers())
        self.assertEqual(self.client.post("/api/devices", json={"hive_id": "HIVE-UNOWNED"},
                                          headers=self.admin_headers()).status_code, 400)

    def test_idor_on_device_record(self) -> None:
        self.assertEqual(self.client.get(f"/api/devices/{self.device_id}",
                                         headers=self.h_b).status_code, 403)
        ids = [d["device_id"] for d in self.client.get("/api/devices", headers=self.h_b).get_json()]
        self.assertNotIn(self.device_id, ids)
        self.assertEqual(self.client.post(f"/api/devices/{self.device_id}/revoke",
                                          headers=self.h_b).status_code, 403)
        self.assertEqual(self.client.post(f"/api/devices/{self.device_id}/rotate-secret",
                                          headers=self.h_b).status_code, 403)

    def test_unknown_or_malformed_device_id_is_404(self) -> None:
        for bad in ("DEV-DOESNOTEXIST", "not-a-device", "'; DROP TABLE devices;--"):
            self.assertEqual(self.client.get(f"/api/devices/{bad}", headers=self.h_a).status_code,
                             404, bad)

    def test_registration_fails_closed_without_master_key(self) -> None:
        original = config.DEVICE_MASTER_KEY
        try:
            config.DEVICE_MASTER_KEY = None
            r = self.client.post("/api/devices", json={"hive_id": "HIVE-A"}, headers=self.h_a)
            self.assertEqual(r.status_code, 503)
        finally:
            config.DEVICE_MASTER_KEY = original


# ---------------------------------------------------------------------------
class TestDeviceRequestAuthentication(DeviceTestCase):
    def test_valid_device_request_is_accepted_and_attributed(self) -> None:
        r = self.device_post(self.good_reading())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        record = r.get_json()["records"][0]
        self.assertEqual(record["device_id"], self.device_id)
        self.assertEqual(record["hive_id"], "HIVE-A")
        device = db.get_device_by_device_id(self.device_id)
        self.assertEqual(device["status"], "ACTIVE")
        self.assertIsNotNone(device["last_seen_at"])

    def test_missing_headers_rejected(self) -> None:
        before = self.telemetry_count()
        r = self.client.post("/api/devices/telemetry", json=self.good_reading())
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.telemetry_count(), before)

    def test_bad_signature_rejected(self) -> None:
        r = self.device_post(self.good_reading(), secret="0" * 64)
        self.assertEqual(r.status_code, 401)
        self.assertIn("DEVICE_AUTH_FAILURE", self.audit_actions())

    def test_tampered_body_rejected(self) -> None:
        signed = json.dumps(self.good_reading()).encode()
        tampered = json.dumps(self.good_reading(temperature=10.0)).encode()
        r = self.device_post(None, raw=tampered, sign_body=signed)
        self.assertEqual(r.status_code, 401)

    def test_wrong_device_identity_rejected(self) -> None:
        """Device B's secret cannot authenticate a request claiming to be A."""
        dev_b, secret_b = self.register("HIVE-B", self.h_b)
        r = self.device_post(self.good_reading(), device_id=self.device_id, secret=secret_b)
        self.assertEqual(r.status_code, 401)

    def test_unknown_device_rejected(self) -> None:
        r = self.device_post(self.good_reading(), device_id="DEV-FFFFFFFFFFFF", secret=self.secret)
        self.assertEqual(r.status_code, 401)

    def test_revoked_device_rejected(self) -> None:
        self.assertEqual(self.client.post(f"/api/devices/{self.device_id}/revoke",
                                          headers=self.h_a).status_code, 200)
        self.assertEqual(self.device_post(self.good_reading()).status_code, 401)
        self.assertEqual(self.client.post(f"/api/devices/{self.device_id}/revoke",
                                          headers=self.h_a).status_code, 409)

    def test_rotation_invalidates_the_old_secret(self) -> None:
        r = self.client.post(f"/api/devices/{self.device_id}/rotate-secret", headers=self.h_a)
        self.assertEqual(r.status_code, 200)
        new_secret = r.get_json()["credentials"]["device_secret"]
        self.assertNotEqual(new_secret, self.secret)
        self.assertEqual(self.device_post(self.good_reading()).status_code, 401)
        self.assertEqual(self.device_post(self.good_reading(), secret=new_secret).status_code, 200)
        self.assertIn("DEVICE_CREDENTIAL_ROTATED", self.audit_actions())

    def test_revoked_device_cannot_be_rotated(self) -> None:
        self.client.post(f"/api/devices/{self.device_id}/revoke", headers=self.h_a)
        r = self.client.post(f"/api/devices/{self.device_id}/rotate-secret", headers=self.h_a)
        self.assertEqual(r.status_code, 409)

    def test_fails_closed_without_master_key(self) -> None:
        original = config.DEVICE_MASTER_KEY
        try:
            config.DEVICE_MASTER_KEY = None
            self.assertEqual(self.device_post(self.good_reading()).status_code, 401)
        finally:
            config.DEVICE_MASTER_KEY = original

    def test_changed_master_key_fails_closed_via_fingerprint(self) -> None:
        original = config.DEVICE_MASTER_KEY
        try:
            config.DEVICE_MASTER_KEY = "a-different-master-key"
            forged = device_auth.derive_device_secret(self.device_id, 1)
            self.assertEqual(self.device_post(self.good_reading(), secret=forged).status_code, 401)
        finally:
            config.DEVICE_MASTER_KEY = original


# ---------------------------------------------------------------------------
class TestBinding(DeviceTestCase):
    def test_device_cannot_report_for_another_hive(self) -> None:
        before = self.telemetry_count()
        r = self.device_post(self.good_reading(hive_id="HIVE-B"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.telemetry_count(), before)
        self.assertIn("DEVICE_HIVE_MISMATCH", self.audit_actions())

    def test_multi_hive_payload_with_foreign_hive_is_rejected_entirely(self) -> None:
        before = self.telemetry_count()
        r = self.device_post([self.good_reading(), self.good_reading(hive_id="HIVE-B")])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.telemetry_count(), before)

    def test_hive_moved_to_other_org_breaks_binding(self) -> None:
        db.assign_hive_organization("HIVE-A", self.org_b)
        self.assertEqual(self.device_post(self.good_reading()).status_code, 403)
        self.assertIn("DEVICE_BINDING_VIOLATION", self.audit_actions())

    def test_device_path_never_auto_creates_hives(self) -> None:
        self.device_post(self.good_reading(hive_id="HIVE-INVENTED"))
        self.assertIsNone(db.get_hive_by_external_id("HIVE-INVENTED"))


# ---------------------------------------------------------------------------
class TestReplayProtection(DeviceTestCase):
    def test_exact_replay_is_rejected(self) -> None:
        body = json.dumps(self.good_reading()).encode()
        ts, nonce = str(int(time.time())), _nonce()
        first = self.device_post(None, raw=body, ts=ts, nonce=nonce)
        self.assertEqual(first.status_code, 200)
        before = self.telemetry_count()
        replay = self.device_post(None, raw=body, ts=ts, nonce=nonce)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.telemetry_count(), before)
        self.assertIn("DEVICE_AUTH_REPLAY", self.audit_actions())

    def test_duplicate_nonce_with_new_body_is_rejected(self) -> None:
        nonce = _nonce()
        self.assertEqual(self.device_post(self.good_reading(), nonce=nonce).status_code, 200)
        r = self.device_post(self.good_reading(temperature=35.0), nonce=nonce)
        self.assertEqual(r.status_code, 409)

    def test_stale_request_rejected(self) -> None:
        old = int(time.time()) - config.DEVICE_REQUEST_MAX_SKEW_SECONDS - 60
        self.assertEqual(self.device_post(self.good_reading(), ts=old).status_code, 401)

    def test_future_request_rejected(self) -> None:
        future = int(time.time()) + config.DEVICE_REQUEST_MAX_SKEW_SECONDS + 60
        self.assertEqual(self.device_post(self.good_reading(), ts=future).status_code, 401)

    def test_malformed_nonce_rejected(self) -> None:
        for bad in ("short", "has spaces in it ok", "x" * 200, "<script>alert(1)</script>xxxx"):
            self.assertEqual(self.device_post(self.good_reading(), nonce=bad).status_code, 401, bad)

    def test_same_nonce_different_devices_is_allowed(self) -> None:
        """Nonces are unique per device, not globally."""
        dev_b, secret_b = self.register("HIVE-B", self.h_b)
        nonce = _nonce()
        self.assertEqual(self.device_post(self.good_reading(), nonce=nonce).status_code, 200)
        r = self.device_post(self.good_reading(hive_id="HIVE-B"), device_id=dev_b,
                             secret=secret_b, nonce=nonce)
        self.assertEqual(r.status_code, 200)

    def test_rejected_unsigned_request_does_not_burn_a_nonce(self) -> None:
        nonce = _nonce()
        self.assertEqual(self.device_post(self.good_reading(), secret="0" * 64,
                                          nonce=nonce).status_code, 401)
        self.assertEqual(self.device_post(self.good_reading(), nonce=nonce).status_code, 200)


# ---------------------------------------------------------------------------
class TestDeviceTimestamps(DeviceTestCase):
    def test_per_reading_device_time_is_preserved_not_replaced(self) -> None:
        event_time = int(time.time()) - 3600  # buffered for an hour, flushed now
        r = self.device_post(self.good_reading(timestamp=event_time))
        self.assertEqual(r.status_code, 200)
        record = r.get_json()["records"][0]
        expected = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(event_time))
        self.assertEqual(record["device_timestamp"], expected)
        self.assertNotEqual(record["device_timestamp"], record["timestamp"])

    def test_request_time_used_when_reading_has_none(self) -> None:
        ts = int(time.time())
        record = self.device_post(self.good_reading(), ts=ts).get_json()["records"][0]
        self.assertEqual(record["device_timestamp"],
                         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)))

    def test_iso8601_reading_timestamp_accepted(self) -> None:
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        self.assertEqual(self.device_post(self.good_reading(timestamp=iso)).status_code, 200)

    def test_reading_from_the_future_rejected(self) -> None:
        future = int(time.time()) + config.DEVICE_FUTURE_TOLERANCE_SECONDS + 600
        before = self.telemetry_count()
        self.assertEqual(self.device_post(self.good_reading(timestamp=future)).status_code, 400)
        self.assertEqual(self.telemetry_count(), before)

    def test_reading_older_than_backfill_window_rejected(self) -> None:
        ancient = int(time.time()) - config.DEVICE_MAX_BACKFILL_SECONDS - 3600
        self.assertEqual(self.device_post(self.good_reading(timestamp=ancient)).status_code, 400)

    def test_unparseable_reading_timestamp_rejected(self) -> None:
        self.assertEqual(self.device_post(self.good_reading(timestamp="yesterday-ish")).status_code, 400)


# ---------------------------------------------------------------------------
class TestDeviceSilence(DeviceTestCase):
    def _go_silent(self, seconds_ago: int) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_at = DATETIME('now', ?) WHERE device_id = ?;",
                (f"-{seconds_ago} seconds", self.device_id),
            )

    def test_silent_device_is_marked_offline_audited_and_notified_once(self) -> None:
        self.assertEqual(self.device_post(self.good_reading()).status_code, 200)
        limit = config.DEVICE_DEFAULT_EXPECTED_INTERVAL_SECONDS * config.DEVICE_SILENCE_MULTIPLIER
        self._go_silent(int(limit) + 120)

        r = self.client.post("/api/devices/silence-check", headers=self.admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual([d["device_id"] for d in r.get_json()["marked_offline"]], [self.device_id])
        self.assertEqual(db.get_device_by_device_id(self.device_id)["status"], "OFFLINE")
        self.assertEqual(self.audit_actions().count("DEVICE_SILENT"), 1)
        notes = self.client.get("/api/notifications", headers=self.h_a).get_json()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["notification_type"], "DEVICE_SILENT")
        # Org B's beekeeper is NOT notified about org A's device.
        self.assertEqual(self.client.get("/api/notifications", headers=self.h_b).get_json(), [])

        # Unchanged condition -> no second alert.
        again = self.client.post("/api/devices/silence-check", headers=self.admin_headers())
        self.assertEqual(again.get_json()["marked_offline"], [])
        self.assertEqual(self.audit_actions().count("DEVICE_SILENT"), 1)

    def test_device_recovers_when_it_reports_again(self) -> None:
        self.device_post(self.good_reading())
        self._go_silent(100_000)
        self.client.post("/api/devices/silence-check", headers=self.admin_headers())
        self.assertEqual(self.device_post(self.good_reading()).status_code, 200)
        self.assertEqual(db.get_device_by_device_id(self.device_id)["status"], "ACTIVE")
        self.assertIn("DEVICE_BACK_ONLINE", self.audit_actions())

    def test_recently_seen_and_never_seen_devices_are_not_flagged(self) -> None:
        self.device_post(self.good_reading())      # recently seen
        self.register("HIVE-A", self.h_a)          # provisioned, never reported
        r = self.client.post("/api/devices/silence-check", headers=self.admin_headers())
        self.assertEqual(r.get_json()["marked_offline"], [])

    def test_per_device_interval_is_respected(self) -> None:
        r = self.client.post("/api/devices", headers=self.h_a,
                             json={"hive_id": "HIVE-A", "expected_interval_seconds": 60})
        dev = r.get_json()["credentials"]
        self.device_post(self.good_reading(), device_id=dev["device_id"], secret=dev["device_secret"])
        with db.transaction() as conn:
            conn.execute("UPDATE devices SET last_seen_at = DATETIME('now', '-400 seconds') "
                         "WHERE device_id = ?;", (dev["device_id"],))
        marked = self.client.post("/api/devices/silence-check",
                                  headers=self.admin_headers()).get_json()["marked_offline"]
        self.assertEqual([d["device_id"] for d in marked], [dev["device_id"]])

    def test_only_admin_can_run_silence_check(self) -> None:
        self.assertEqual(self.client.post("/api/devices/silence-check",
                                          headers=self.h_a).status_code, 403)


# ---------------------------------------------------------------------------
class TestSensorFaults(DeviceTestCase):
    def flags(self, response) -> str:
        return response.get_json()["records"][0]["sensor_flags"] or ""

    def test_missing_core_channel_is_flagged(self) -> None:
        r = self.device_post({"hive_id": "HIVE-A", "temperature": 34.5, "humidity": 60.0})
        self.assertIn("MISSING_WEIGHT", self.flags(r))

    def test_clean_reading_has_no_flags(self) -> None:
        self.assertIsNone(self.device_post(self.good_reading()).get_json()["records"][0]["sensor_flags"])

    def test_implausible_temperature_jump_is_flagged(self) -> None:
        self.device_post(self.good_reading(temperature=34.0))
        r = self.device_post(self.good_reading(temperature=46.0))
        self.assertIn("IMPLAUSIBLE_JUMP_TEMPERATURE", self.flags(r))

    def test_flatline_is_flagged_only_after_enough_identical_readings(self) -> None:
        responses = [self.device_post(self.good_reading(humidity=55.5))
                     for _ in range(config.SENSOR_FLATLINE_COUNT)]
        self.assertNotIn("FLATLINE_HUMIDITY", self.flags(responses[-2]))
        self.assertIn("FLATLINE_HUMIDITY", self.flags(responses[-1]))

    def test_flags_never_change_the_health_verdict(self) -> None:
        r = self.device_post({"hive_id": "HIVE-A", "temperature": 34.5, "humidity": 60.0})
        self.assertEqual(r.get_json()["records"][0]["health_status"], "HEALTHY")

    def test_impossible_values_are_rejected_not_stored(self) -> None:
        before = self.telemetry_count()
        self.assertEqual(self.device_post(self.good_reading(temperature=999.0)).status_code, 400)
        self.assertEqual(self.device_post(None, raw=b"{not json").status_code, 400)
        self.assertEqual(self.telemetry_count(), before)


# ---------------------------------------------------------------------------
class TestHiveProvisioningAndLegacyWebhook(DeviceTestCase):
    def test_beekeeper_hive_goes_to_own_org_regardless_of_payload(self) -> None:
        r = self.client.post("/api/hives", headers=self.h_a,
                             json={"hive_id": "HIVE-A2", "organization_id": "ORG-B"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["organization_id"], self.org_a)

    def test_duplicate_hive_is_409(self) -> None:
        self.assertEqual(self.client.post("/api/hives", json={"hive_id": "HIVE-A"},
                                          headers=self.h_a).status_code, 409)

    def test_consumer_cannot_create_hives(self) -> None:
        consumer = self.create_user(user_id="USR-C2", email="c2@hc.test", role="CONSUMER")
        self.assertEqual(self.client.post("/api/hives", json={"hive_id": "HIVE-X"},
                                          headers=self.auth_headers_for(consumer)).status_code, 403)

    def test_only_admin_assigns_hive_ownership(self) -> None:
        self.client.post("/api/hives", json={"hive_id": "HIVE-FREE"}, headers=self.admin_headers())
        self.assertEqual(self.client.post("/api/hives/HIVE-FREE/assign", headers=self.h_a,
                                          json={"organization_id": "ORG-A"}).status_code, 403)
        r = self.client.post("/api/hives/HIVE-FREE/assign", headers=self.admin_headers(),
                             json={"organization_id": "ORG-A"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["organization_id"], self.org_a)

    def test_legacy_shared_secret_webhook_is_disabled_by_default(self) -> None:
        original = config.LEGACY_WEBHOOK_ENABLED
        try:
            config.LEGACY_WEBHOOK_ENABLED = False
            before = self.telemetry_count()
            r = self.post_telemetry({"hive_id": "HIVE-LEGACY", "temperature": 34.5})
            self.assertEqual(r.status_code, 410)
            self.assertEqual(self.telemetry_count(), before)
            self.assertIsNone(db.get_hive_by_external_id("HIVE-LEGACY"))
        finally:
            config.LEGACY_WEBHOOK_ENABLED = original

    def test_development_default_for_legacy_flag_is_on(self) -> None:
        """Development mode keeps the legacy simulator endpoint available."""
        import os
        import subprocess
        import sys
        from tests.base import REPO_ROOT
        env = {k: v for k, v in os.environ.items() if k != "HONEYCHAIN_LEGACY_WEBHOOK_ENABLED"}
        out = subprocess.run(
            [sys.executable, "-c",
             "from honeychain import config; print(config.LEGACY_WEBHOOK_ENABLED)"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

if __name__ == "__main__":
    unittest.main()
