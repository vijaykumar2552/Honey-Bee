"""Dashboard rendering, SSE streaming, error handling and diagnostics endpoints."""

from __future__ import annotations

import json
import re
import threading
import time
import unittest

from tests.base import HoneyChainTestCase

from honeychain import db
from honeychain.events import EventBroker


class TestDashboard(HoneyChainTestCase):

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("HoneyChain", html)
        self.assertIn("bootstrap@5.3.2", html)

    def test_dashboard_contains_the_four_summary_cards(self):
        html = self.client.get("/").get_data(as_text=True)
        for element_id in (
            "card-total-hives",
            "card-avg-temp",
            "card-avg-weight",
            "card-health-score",
        ):
            self.assertIn(element_id, html)

    def test_dashboard_ships_no_hardcoded_metrics(self):
        """Cards must start at '--' and be filled from the API, never baked in."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="card-total-hives">--<', html)
        self.assertIn('id="card-avg-temp">--<', html)
        self.assertIn('id="card-avg-weight">--<', html)
        self.assertIn('id="card-health-score">--<', html)

    def test_static_assets_are_served(self):
        for path in ("/static/css/honeychain.css", "/static/js/dashboard.js"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()

    def test_stylesheet_defines_the_critical_pulse(self):
        css = self.client.get("/static/css/honeychain.css").get_data(as_text=True)
        self.assertIn("@keyframes hc-pulse-red", css)
        self.assertIn(".pulse-critical", css)
        self.assertIn("badge-healthy", css)
        self.assertIn("badge-warning", css)
        self.assertIn("badge-critical", css)
        self.assertIn("prefers-reduced-motion", css)

    def test_client_script_implements_the_transport_contract(self):
        # Final audit: every data endpoint requires a bearer token (Phase 3),
        # and EventSource cannot send an Authorization header, so the previous
        # SSE + reconnect-backoff contract could only ever receive 401. The
        # dashboard now uses ONE authenticated polling timer.
        js = self.client.get("/static/js/dashboard.js").get_data(as_text=True)
        self.assertIn("/api/telemetry/latest", js)
        self.assertIn("POLL_INTERVAL_MS = 3000", js)      # 3-second poll
        self.assertIn("headers.Authorization = 'Bearer ' + token", js)
        self.assertIn("if (response.status === 401) { requireSignIn(); }", js)
        self.assertNotIn("new EventSource", js)
        self.assertIn("escapeHtml", js)
        self.assertIn("stopPolling", js)

    def test_client_script_never_assigns_innerhtml(self):
        """Rendering is textContent-only, so telemetry cannot inject nodes.

        Comments are stripped first: the file *mentions* innerHTML to explain
        why it is avoided, which is not the same as using it.
        """
        js = self.client.get("/static/js/dashboard.js").get_data(as_text=True)
        without_block_comments = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
        code = re.sub(r"//[^\n]*", "", without_block_comments)
        self.assertNotRegex(code, r"innerHTML\s*=")
        self.assertNotIn("insertAdjacentHTML", code)
        self.assertNotIn("document.write", code)


class TestTelemetryEndpoints(HoneyChainTestCase):

    def test_latest_is_empty_safe(self):
        response = self.client.get("/api/telemetry/latest", headers=self.admin_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])

    def test_latest_returns_newest_first_and_caps_at_twenty(self):
        self.seed_hive("HIVE-001", count=25)
        rows = self.client.get("/api/telemetry/latest", headers=self.admin_headers()).get_json()
        self.assertEqual(len(rows), 20)
        timestamps = [row["timestamp"] for row in rows]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_latest_joins_health_information(self):
        self.seed_hive("HIVE-001", count=2)
        row = self.client.get("/api/telemetry/latest", headers=self.admin_headers()).get_json()[0]
        for field in ("hive_id", "timestamp", "temperature", "humidity", "weight",
                      "audio_frequency", "battery_level", "health_status", "reason"):
            self.assertIn(field, row)

    def test_limit_parameter_is_clamped(self):
        self.seed_hive("HIVE-001", count=5)
        headers = self.admin_headers()
        self.assertEqual(
            len(self.client.get("/api/telemetry/latest?limit=3", headers=headers).get_json()), 3
        )
        # Nonsense values fall back to the default rather than erroring.
        self.assertEqual(
            self.client.get("/api/telemetry/latest?limit=abc", headers=headers).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/telemetry/latest?limit=-1", headers=headers).status_code, 200
        )

    def test_hive_history_endpoint(self):
        self.seed_hive("HIVE-001", count=4)
        headers = self.admin_headers()
        rows = self.client.get("/api/hives/HIVE-001/history", headers=headers).get_json()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            self.client.get("/api/hives/HIVE-GHOST/history", headers=headers).status_code, 404
        )

    def test_summary_endpoint_matches_the_database(self):
        self.seed_hive("HIVE-001", count=6)
        body = self.client.get("/api/summary", headers=self.admin_headers()).get_json()
        self.assertEqual(body, db.dashboard_summary())


class TestEventBroker(unittest.TestCase):
    """Broker mechanics, exercised without an HTTP server."""

    def test_publish_reaches_every_subscriber(self):
        broker = EventBroker(queue_size=10, max_clients=10)
        first = broker.subscribe()
        second = broker.subscribe()
        broker.publish("telemetry", {"hive_id": "HIVE-001"})

        self.assertEqual(first.get_nowait()["payload"]["hive_id"], "HIVE-001")
        self.assertEqual(second.get_nowait()["payload"]["hive_id"], "HIVE-001")

    def test_unsubscribe_stops_delivery(self):
        broker = EventBroker(queue_size=10, max_clients=10)
        client = broker.subscribe()
        broker.unsubscribe(client)
        broker.publish("telemetry", {"x": 1})
        self.assertEqual(broker.client_count, 0)
        self.assertTrue(client.empty())

    def test_client_cap_is_enforced(self):
        broker = EventBroker(queue_size=5, max_clients=2)
        self.assertIsNotNone(broker.subscribe())
        self.assertIsNotNone(broker.subscribe())
        self.assertIsNone(broker.subscribe())

    def test_slow_client_is_dropped_not_buffered_forever(self):
        broker = EventBroker(queue_size=3, max_clients=10)
        broker.subscribe()  # never drained
        for index in range(10):
            broker.publish("telemetry", {"n": index})
        self.assertEqual(broker.client_count, 0)

    def test_publish_never_raises_with_no_subscribers(self):
        broker = EventBroker(queue_size=5, max_clients=5)
        broker.publish("telemetry", {"x": 1})
        self.assertEqual(broker.published_count, 1)

    def test_concurrent_publishers_are_safe(self):
        broker = EventBroker(queue_size=10_000, max_clients=10)
        client = broker.subscribe()

        def worker():
            for index in range(200):
                broker.publish("telemetry", {"n": index})

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(broker.published_count, 800)
        self.assertEqual(client.qsize(), 800)

    def test_stream_emits_retry_ready_and_keepalive(self):
        broker = EventBroker(queue_size=5, max_clients=5)
        generator = broker.stream()

        self.assertIn("retry:", next(generator))
        ready = next(generator)
        self.assertIn('"event": "ready"', ready)

        # With no traffic, the next frame must be a comment keepalive.
        keepalive = next(generator)
        self.assertTrue(keepalive.startswith(": keepalive"))
        generator.close()
        self.assertEqual(broker.client_count, 0)

    def test_stream_delivers_a_published_event(self):
        broker = EventBroker(queue_size=5, max_clients=5)
        generator = broker.stream()
        next(generator)  # retry
        next(generator)  # ready

        broker.publish("telemetry", {"hive_id": "HIVE-042"})
        frame = next(generator)
        self.assertTrue(frame.startswith("data: "))
        payload = json.loads(frame[len("data: "):].strip())
        self.assertEqual(payload["event"], "telemetry")
        self.assertEqual(payload["payload"]["hive_id"], "HIVE-042")
        generator.close()

    def test_closing_the_generator_unsubscribes(self):
        broker = EventBroker(queue_size=5, max_clients=5)
        generator = broker.stream()
        next(generator)
        self.assertEqual(broker.client_count, 1)
        generator.close()
        self.assertEqual(broker.client_count, 0)


class TestSseEndpoint(HoneyChainTestCase):

    def test_stream_has_the_right_headers(self):
        response = self.client.get(
            "/api/telemetry/stream", headers=self.admin_headers(), buffered=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/event-stream"))
        self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        response.close()

    def test_ingested_telemetry_reaches_a_connected_client(self):
        """End-to-end: webhook POST -> broker -> SSE frame."""
        response = self.client.get(
            "/api/telemetry/stream", headers=self.admin_headers(), buffered=False
        )
        # The Werkzeug test client yields bytes; decode each frame.
        stream = (chunk.decode("utf-8") for chunk in response.response)

        self.assertIn("retry:", next(stream))
        self.assertIn('"event": "ready"', next(stream))

        received: list = []

        def reader():
            for frame in stream:
                if frame.startswith("data: "):
                    received.append(json.loads(frame[len("data: "):].strip()))
                    if len(received) >= 1:
                        return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.1)

        self.post_telemetry(
            {"hive_id": "HIVE-SSE", "temperature": 34.5, "humidity": 62.0, "weight": 42.0}
        )
        thread.join(timeout=5)
        response.close()

        self.assertTrue(received, "no SSE frame was delivered")
        self.assertEqual(received[0]["event"], "telemetry")
        self.assertEqual(received[0]["payload"]["hive_id"], "HIVE-SSE")


class TestErrorHandling(HoneyChainTestCase):

    def test_api_404_is_json(self):
        response = self.client.get("/api/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json()["error"], "Not Found")

    def test_browser_404_is_html(self):
        response = self.client.get("/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("404", response.get_data(as_text=True))

    def test_method_not_allowed(self):
        response = self.client.get("/api/firebase/webhook")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.get_json()["error"], "Method Not Allowed")

    def test_oversized_body_is_rejected(self):
        oversized = b'{"hive_id":"H","note":"' + b"a" * 2_000_000 + b'"}'
        response = self.client.post(
            "/api/firebase/webhook", data=oversized, content_type="application/json"
        )
        self.assertIn(response.status_code, (401, 413))

    def test_no_stack_traces_are_ever_returned(self):
        for path in ("/api/does-not-exist", "/does-not-exist", "/verify/UNKNOWN"):
            body = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("File \"/", body)


class TestDiagnostics(HoneyChainTestCase):

    def test_health_endpoint_reports_pragmas(self):
        body = self.client.get("/api/health").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database"]["ok"])
        self.assertEqual(body["database"]["journal_mode"].lower(), "wal")
        self.assertTrue(body["database"]["foreign_keys"])
        self.assertTrue(body["webhook_secret_configured"])

    def test_health_never_leaks_the_secret(self):
        from tests.base import TEST_SECRET

        body = self.client.get("/api/health").get_data(as_text=True)
        self.assertNotIn(TEST_SECRET, body)

    def test_optional_subsystems_report_disabled_not_broken(self):
        body = self.client.get("/api/health").get_json()
        self.assertFalse(body["chain_anchor"]["enabled"])
        self.assertFalse(body["firebase_bridge"]["enabled"])

    def test_ml_status_endpoint(self):
        body = self.client.get("/api/ml/status", headers=self.admin_headers()).get_json()
        self.assertIn("available", body)
        self.assertEqual(
            body["feature_columns"],
            ["temperature", "humidity", "weight", "audio_frequency"],
        )
        if body["available"]:
            self.assertEqual(body["contamination"], 0.05)
            self.assertEqual(body["random_state"], 42)

    def test_hives_endpoint(self):
        self.seed_hive("HIVE-001", count=2)
        hives = self.client.get("/api/hives", headers=self.admin_headers()).get_json()
        self.assertEqual(len(hives), 1)
        self.assertEqual(hives[0]["hive_id"], "HIVE-001")
        self.assertEqual(hives[0]["status"], "ACTIVE")


class TestRuleEngineOnlyMode(HoneyChainTestCase):
    """The whole service must work with no model artefacts at all."""

    load_model = False

    def test_service_operates_without_the_model(self):
        response = self.post_telemetry(
            {"hive_id": "HIVE-NOML", "temperature": 34.5, "humidity": 62.0, "weight": 42.0}
        )
        self.assertEqual(response.status_code, 200)
        record = response.get_json()["records"][0]
        self.assertEqual(record["health_status"], "HEALTHY")

    def test_rules_still_catch_critical_conditions(self):
        response = self.post_telemetry(
            {"hive_id": "HIVE-NOML", "temperature": 25.0, "humidity": 62.0, "weight": 42.0}
        )
        self.assertEqual(response.get_json()["records"][0]["health_status"], "CRITICAL")

    def test_dashboard_and_health_still_render(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
