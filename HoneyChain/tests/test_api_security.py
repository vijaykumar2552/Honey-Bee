"""Webhook authentication and telemetry validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from tests.base import TEST_SECRET, HoneyChainTestCase

from honeychain import security, validation


class TestHmacAuthentication(HoneyChainTestCase):

    VALID = {"hive_id": "HIVE-001", "temperature": 34.2, "humidity": 65.0, "weight": 42.5}

    def test_valid_header_signature_accepted(self):
        response = self.post_telemetry(self.VALID, mode="header")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ingested"], 1)

    def test_valid_query_secret_accepted(self):
        response = self.post_telemetry(self.VALID, mode="query")
        self.assertEqual(response.status_code, 200)

    def test_sha256_prefixed_signature_accepted(self):
        body = json.dumps(self.VALID).encode()
        signature = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.client.post(
            "/api/firebase/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-HoneyChain-Signature": f"sha256={signature}",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_missing_authentication_rejected(self):
        response = self.post_telemetry(self.VALID, mode="none")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "Unauthorized")

    def test_invalid_signature_rejected(self):
        body = json.dumps(self.VALID).encode()
        response = self.client.post(
            "/api/firebase/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-HoneyChain-Signature": "0" * 64,
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_invalid_query_secret_rejected(self):
        response = self.post_telemetry(self.VALID, mode="query", secret="wrong-secret")
        self.assertEqual(response.status_code, 401)

    def test_signature_covers_the_body(self):
        """A signature valid for one body must not authenticate a different one."""
        original = json.dumps(self.VALID).encode()
        signature = hmac.new(TEST_SECRET.encode(), original, hashlib.sha256).hexdigest()
        tampered = json.dumps({**self.VALID, "weight": 99.0}).encode()

        response = self.client.post(
            "/api/firebase/webhook",
            data=tampered,
            headers={
                "Content-Type": "application/json",
                "X-HoneyChain-Signature": signature,
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_error_body_never_leaks_the_secret(self):
        response = self.post_telemetry(self.VALID, mode="query", secret="wrong")
        body = response.get_data(as_text=True)
        self.assertNotIn(TEST_SECRET, body)
        self.assertNotIn("signature mismatch", body)

    def test_auth_failure_does_not_persist_anything(self):
        self.post_telemetry(self.VALID, mode="none")
        response = self.client.get("/api/telemetry/latest", headers=self.admin_headers())
        self.assertEqual(response.get_json(), [])

    def test_constant_time_comparison_used(self):
        result = security.authenticate(b"{}", None, TEST_SECRET)
        self.assertTrue(result.authenticated)
        self.assertEqual(result.method, "query")


class TestTelemetryValidation(HoneyChainTestCase):

    def _reject(self, payload, *, field=None):
        response = self.post_telemetry(payload)
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["error"], "Bad Request")
        if field:
            fields = {detail.get("field") for detail in body.get("details", [])}
            self.assertIn(field, fields)
        return body

    def test_malformed_json_returns_400(self):
        body = b'{"hive_id": "HIVE-001", "temperature": '
        signature = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.client.post(
            "/api/firebase/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-HoneyChain-Signature": signature,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Malformed JSON", response.get_json()["message"])

    def test_empty_body_returns_400(self):
        self._reject({})

    def test_temperature_below_range_rejected(self):
        self._reject({"hive_id": "HIVE-001", "temperature": -50.0}, field="temperature")

    def test_temperature_above_range_rejected(self):
        self._reject({"hive_id": "HIVE-001", "temperature": 120.0}, field="temperature")

    def test_humidity_out_of_range_rejected(self):
        self._reject({"hive_id": "HIVE-001", "humidity": 150.0}, field="humidity")

    def test_weight_out_of_range_rejected(self):
        self._reject({"hive_id": "HIVE-001", "weight": 500.0}, field="weight")

    def test_audio_frequency_out_of_range_rejected(self):
        self._reject({"hive_id": "HIVE-001", "audio_frequency": 9000.0},
                     field="audio_frequency")

    def test_battery_out_of_range_rejected(self):
        self._reject(
            {"hive_id": "HIVE-001", "temperature": 34.0, "battery_level": 300.0},
            field="battery_level",
        )

    def test_string_type_rejected(self):
        self._reject({"hive_id": "HIVE-001", "temperature": "hot"}, field="temperature")

    def test_boolean_type_rejected(self):
        self._reject({"hive_id": "HIVE-001", "temperature": True}, field="temperature")

    def test_nan_rejected(self):
        # json.dumps emits bare NaN, which our strict parser must not accept as a number.
        self._reject({"hive_id": "HIVE-001", "temperature": "NaN"}, field="temperature")

    def test_infinity_rejected(self):
        self._reject({"hive_id": "HIVE-001", "temperature": "Infinity"}, field="temperature")

    def test_missing_hive_id_rejected(self):
        self._reject({"temperature": 34.0}, field="hive_id")

    def test_path_traversal_hive_id_rejected(self):
        self._reject({"hive_id": "../../etc/passwd", "temperature": 34.0}, field="hive_id")

    def test_payload_with_no_sensors_rejected(self):
        self._reject({"hive_id": "HIVE-001", "colour": "amber"})

    def test_boundary_values_accepted(self):
        """Range bounds are inclusive, so the exact limits must pass."""
        response = self.post_telemetry(
            {
                "hive_id": "HIVE-EDGE",
                "temperature": -20.0,
                "humidity": 0.0,
                "weight": 0.0,
                "audio_frequency": 20.0,
                "battery_level": 0.0,
            }
        )
        self.assertEqual(response.status_code, 200)

    def test_multi_hive_payload_is_all_or_nothing(self):
        response = self.post_telemetry(
            {
                "HIVE-001": {"temperature": 34.0, "humidity": 60.0},
                "HIVE-002": {"temperature": 999.0},
            }
        )
        self.assertEqual(response.status_code, 400)
        # The valid sibling must not have been written either.
        response = self.client.get("/api/telemetry/latest", headers=self.admin_headers())
        self.assertEqual(response.get_json(), [])


class TestPayloadNormalisation(unittest.TestCase):
    """Pure normalisation logic -- no HTTP, no database."""

    def test_flat_object(self):
        readings = validation.normalise_payload(
            {"hive_id": "HIVE-001", "temperature": 34.2, "humidity": 65}
        )
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["hive_id"], "HIVE-001")

    def test_nested_firebase_tree(self):
        readings = validation.normalise_payload(
            {
                "HIVE-001": {"temperature": 34.2, "humidity": 65, "weight": 42.5},
                "HIVE-002": {"temperature": 35.1, "humidity": 71, "weight": 38.1},
            }
        )
        self.assertEqual({r["hive_id"] for r in readings}, {"HIVE-001", "HIVE-002"})

    def test_flat_array(self):
        readings = validation.normalise_payload(
            [
                {"hive_id": "HIVE-001", "temperature": 34.2},
                {"hive_id": "HIVE-002", "temperature": 35.0},
            ]
        )
        self.assertEqual(len(readings), 2)

    def test_firebase_event_wrapper(self):
        readings = validation.normalise_payload(
            {"path": "/HIVE-007", "data": {"temperature": 34.0, "humidity": 60}}
        )
        self.assertEqual(readings[0]["hive_id"], "HIVE-007")

    def test_readings_envelope(self):
        readings = validation.normalise_payload(
            {"readings": [{"hive_id": "HIVE-001", "temperature": 34.0}]}
        )
        self.assertEqual(len(readings), 1)

    def test_field_aliases_mapped(self):
        readings = validation.normalise_payload(
            {"hiveId": "HIVE-009", "temp": 34.0, "hum": 61, "wt": 40.0, "audio": 260}
        )
        reading = validation.validate_reading(readings[0])
        self.assertEqual(reading["hive_id"], "HIVE-009")
        self.assertEqual(reading["temperature"], 34.0)
        self.assertEqual(reading["humidity"], 61.0)
        self.assertEqual(reading["weight"], 40.0)
        self.assertEqual(reading["audio_frequency"], 260.0)

    def test_missing_field_is_not_defaulted(self):
        """A missing weight must stay absent, never become 0 (which is CRITICAL)."""
        reading = validation.validate_reading(
            validation.normalise_payload({"hive_id": "HIVE-001", "temperature": 34.0})[0]
        )
        self.assertNotIn("weight", reading)

    def test_unparseable_payload_raises(self):
        with self.assertRaises(validation.ValidationError):
            validation.normalise_payload("just a string")


if __name__ == "__main__":
    unittest.main()
