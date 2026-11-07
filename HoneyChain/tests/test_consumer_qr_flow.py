"""
Consumer QR flow: /q -> React page routing, the consumer-facing suspicious
activity signal, and a drift guard between the real public API and the
response fixtures the React page is tested against.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from tests.base import REPO_ROOT
from tests.test_phase9_consumer import ConsumerCase

from honeychain import config

FIXTURES = json.loads((Path(REPO_ROOT) / "frontend/src/__fixtures__/verify-responses.json").read_text())


def _keys(node, prefix=""):
    """Every key path in a JSON value (list items merged)."""
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(prefix + k)
            out |= _keys(v, prefix + k + ".")
    elif isinstance(node, list):
        for item in node:
            out |= _keys(item, prefix)
    return out


class TestQRLandingRouting(ConsumerCase):
    def test_q_redirects_to_the_react_page_when_built(self) -> None:
        dist = Path(tempfile.mkdtemp())
        (dist / "index.html").write_text("<div id=root></div>")
        with mock.patch.object(config, "FRONTEND_DIST", dist):
            r = self.client.get("/q")
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/app/verify"))
            self.assertNotIn("#", r.headers["Location"])      # fragment preserved by the browser
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            page = self.client.get("/app/verify")
            self.assertEqual(page.status_code, 200)                # SPA shell, no login
            page.close()

    def test_q_falls_back_to_the_template_page_without_a_build(self) -> None:
        with mock.patch.object(config, "FRONTEND_DIST", Path("/nonexistent-frontend-dist")):
            r = self.client.get("/q")
        self.assertEqual(r.status_code, 200)
        self.assertIn("textContent", r.get_data(as_text=True))


class TestConsumerScanSignal(ConsumerCase):
    def test_normal_scan_is_not_suspicious(self) -> None:
        sig = self.verify(self.qr)["scan_signal"]
        self.assertFalse(sig["suspicious"])
        self.assertIn("not proof", sig["note"])

    def test_velocity_makes_the_unit_suspicious_at_the_threshold(self) -> None:
        with mock.patch.object(config, "SIGNAL_SCAN_VELOCITY_24H", 3):
            self.assertFalse(self.verify(self.qr)["scan_signal"]["suspicious"])
            self.assertFalse(self.verify(self.qr)["scan_signal"]["suspicious"])
            body = self.verify(self.qr)
        self.assertTrue(body["scan_signal"]["suspicious"])
        self.assertTrue(any("SUSPICIOUS ACTIVITY DETECTED" in w for w in body["warnings"]))
        self.assertEqual(body["result"], "AUTHENTIC")                 # a signal, not a rejection

    def test_forged_copies_of_a_real_unit_are_reported_to_the_genuine_holder(self) -> None:
        tag = self.qr.split(".")[2]
        forged = self.qr[: -len(tag)] + ("A" if tag[0] != "A" else "B") + tag[1:]
        for _ in range(config.SIGNAL_FORGERY_ATTEMPTS):
            self.assertEqual(self.verify(forged)["result"], "INVALID")
        sig = self.verify(self.qr)["scan_signal"]
        self.assertTrue(sig["suspicious"])
        self.assertTrue(any("Altered copies" in r for r in sig["reasons"]))

    def test_invalid_answers_are_unchanged_and_carry_no_signal(self) -> None:
        tag = self.qr.split(".")[2]
        unknown = self.verify(f"HC1.UNIT-ZZZZZZZZZZZZZZZZ.{tag}")
        wrong = self.verify(self.qr[: -len(tag)] + ("A" if tag[0] != "A" else "B") + tag[1:])
        self.assertEqual(unknown, wrong)                              # still no enumeration
        self.assertNotIn("scan_signal", unknown)


class TestFixtureDriftGuard(ConsumerCase):
    """The React page is tested against captured responses. This fails if the
    real API's response shape diverges from them, so the UI tests cannot keep
    passing against a stale contract."""

    def test_fixtures_match_the_live_response_shapes(self) -> None:
        live_authentic = self.verify(self.qr)
        self.assertEqual(_keys(live_authentic), _keys(FIXTURES["authentic"]))
        tag = self.qr.split(".")[2]
        self.assertEqual(set(self.verify(f"HC1.UNIT-ZZZZZZZZZZZZZZZZ.{tag}")),
                         set(FIXTURES["invalid_not_recognised"]))
        self.call("PROC", "post", f"/api/units/{self.units[0]['unit_id']}/revoke-qr", expect=200)
        self.assertEqual(set(self.verify(self.qr)), set(FIXTURES["revoked_with_recall"]))
        for name in ("authentic", "suspicious", "recalled", "revoked_with_recall",
                     "invalid_not_recognised", "invalid_malformed", "invalid_unsupported_version"):
            self.assertIn(name, FIXTURES)
