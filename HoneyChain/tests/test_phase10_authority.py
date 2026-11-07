"""
Phase 10 -- authority oversight: access separation, figures cross-checked
against the database, region filtering, compliance views, suspicious-scan
signals (each type + threshold boundaries), recall view, hotspots, coarse
geography, and scan-log privacy.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tests.test_phase8_supply_chain import BEST_BEFORE, TODAY, SupplyCase

from honeychain import config, db

ENDPOINTS = ("/api/authority/overview", "/api/authority/hotspots",
             "/api/authority/signals", "/api/authority/recalls")


def _wrong_tag(payload: str) -> str:
    tag = payload.split(".")[2]
    return payload[: -len(tag)] + (("A" if tag[0] != "A" else "B") + tag[1:])


class OversightCase(SupplyCase):
    def get(self, who, path, expect=200):
        return self.call(who, "get", path, expect=expect).get_json()

    def scan(self, qr):
        return self.client.post("/api/public/verify", json={"qr": qr}).get_json()

    def signal_types(self):
        return {s["type"]: s for s in self.get("AUTH", "/api/authority/signals")["signals"]}


class TestAccessSeparation(OversightCase):
    def test_only_authority_and_admin_can_read(self) -> None:
        admin = self.admin_headers()
        for path in ENDPOINTS:
            self.assertEqual(self.client.get(path, headers=admin).status_code, 200, path)
            self.get("AUTH", path)
            for who in ("BK", "PROC", "LAB", "DIST", "RET"):
                self.call(who, "get", path, expect=403)
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_authority_is_not_an_admin(self) -> None:
        self.call("AUTH", "post", "/api/auth/register",
                  {"email": "x@y.test", "password": "Password123", "name": "X", "role": "BEEKEEPER"}, 403)
        self.call("AUTH", "post", "/api/devices/silence-check", expect=403)
        self.call("AUTH", "post", "/api/hives/HIVE-A/assign", {"organization_id": "ORG-BK"}, 403)
        self.call("AUTH", "post", "/api/harvest", {"hive_id": "HIVE-A", "total_weight": 1,
                                                   "floral_type": "X"}, 403)


class TestOverviewFigures(OversightCase):
    def test_figures_match_the_database(self) -> None:
        self.harvest("HIVE-A", 20.0)
        self.harvest("HIVE-B", 5.5)
        ov = self.get("AUTH", "/api/authority/overview")
        p = ov["production"]
        self.assertEqual(p["harvests"], 2)
        self.assertEqual(p["harvest_kg"], 25.5)
        self.assertEqual(p["active_beekeepers"],
                         db.query_one("SELECT COUNT(*) AS n FROM users WHERE role='BEEKEEPER' "
                                      "AND status='ACTIVE';")["n"])
        self.assertEqual(ov["supply_chain"]["batches"], 2)

    def test_region_filter_and_unspecified_bucket(self) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE hives SET location = 'Kolar' WHERE hive_id = 'HIVE-A';")
        self.harvest("HIVE-A", 20.0)
        self.harvest("HIVE-B", 5.0)
        kolar = self.get("AUTH", "/api/authority/overview?region=Kolar")["production"]
        self.assertEqual((kolar["harvests"], kolar["harvest_kg"], kolar["active_hives"]), (1, 20.0, 1))
        rest = self.get("AUTH", "/api/authority/overview?region=Unspecified")["production"]
        self.assertEqual((rest["harvests"], rest["harvest_kg"]), (1, 5.0))
        regions = {r["region"]: r["harvest_kg"] for r in
                   self.get("AUTH", "/api/authority/overview")["production"]["by_region"]}
        self.assertEqual(regions["Kolar"], 20.0)
        self.assertEqual(regions["Unspecified"], 5.0)

    def test_compliance_coverage_failures_and_exceptions(self) -> None:
        batch, body = self.packaged(count=2)
        comp = self.get("AUTH", "/api/authority/overview")["compliance"]
        self.assertEqual(comp["units_total"], 2)
        self.assertEqual(comp["units_traceable_to_harvest_pct"], 100.0)
        self.assertEqual(comp["batches_with_passed_lab_pct"], 100.0)
        rid = self.call("PROC", "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB2"}, 201).get_json()["lab_result_id"]
        self.call("LAB2", "post", f"/api/lab/results/{rid}",
                  {"status": "FAILED", "test_date": TODAY, "c4_sugar_pct": 11.0}, 201)
        t, sid, _ = self.to_retailer(batch)
        self.call("DIST", "post", f"/api/shipments/{sid}/status", {"status": "EXCEPTION"}, 400)
        self.call("DIST", "post", f"/api/shipments/{sid}/status",
                  {"status": "EXCEPTION", "exception_reason": "Broken seal"}, 200)
        comp = self.get("AUTH", "/api/authority/overview")["compliance"]
        self.assertEqual(comp["failed_lab_batches"][0]["batch_id"], batch)
        self.assertEqual(comp["failed_lab_batches"][0]["laboratory"], "LAB2")
        self.assertEqual(comp["shipment_exceptions"][0]["exception_reason"], "Broken seal")


class TestSignals(OversightCase):
    def setUp(self) -> None:
        super().setUp()
        self.batch, body = self.packaged(count=3)
        self.units = body["units"]

    def test_forged_tag_attempts_flagged_at_threshold_only(self) -> None:
        bad = _wrong_tag(self.units[0]["qr_payload"])
        for _ in range(config.SIGNAL_FORGERY_ATTEMPTS - 1):
            self.assertEqual(self.scan(bad)["result"], "INVALID")
        self.assertNotIn("FORGED_TAG_ATTEMPTS", self.signal_types())
        self.scan(bad)
        sig = self.signal_types()["FORGED_TAG_ATTEMPTS"]
        self.assertEqual((sig["severity"], sig["classification"], sig["unit_id"], sig["batch_id"]),
                         ("HIGH", "SIGNAL", self.units[0]["unit_id"], self.batch))
        self.assertEqual(sig["occurrences"], config.SIGNAL_FORGERY_ATTEMPTS)
        self.assertIn("NOT a confirmed counterfeit", self.get("AUTH", "/api/authority/signals")["note"])

    def test_scan_velocity(self) -> None:
        with mock.patch.object(config, "SIGNAL_SCAN_VELOCITY_24H", 3):
            for _ in range(2):
                self.scan(self.units[1]["qr_payload"])
            self.assertNotIn("HIGH_SCAN_VELOCITY", self.signal_types())
            self.scan(self.units[1]["qr_payload"])
            self.assertEqual(self.signal_types()["HIGH_SCAN_VELOCITY"]["occurrences"], 3)

    def test_before_retail_after_recall_and_revoked(self) -> None:
        self.scan(self.units[0]["qr_payload"])                 # still ISSUED
        self.assertEqual(self.signal_types()["SCANNED_BEFORE_RETAIL"]["severity"], "LOW")
        self.call("PROC", "post", f"/api/units/{self.units[2]['unit_id']}/revoke-qr", expect=200)
        self.scan(self.units[2]["qr_payload"])
        self.assertEqual(self.signal_types()["REVOKED_LABEL_SCANNED"]["unit_id"], self.units[2]["unit_id"])
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Residue found"}, 201)
        self.scan(self.units[1]["qr_payload"])
        sig = self.signal_types()["SCANNED_AFTER_RECALL"]
        self.assertEqual((sig["severity"], sig["unit_id"]), ("HIGH", self.units[1]["unit_id"]))
        types = [s["type"] for s in self.get("AUTH", "/api/authority/signals")["signals"]]
        self.assertEqual(types[0], "SCANNED_AFTER_RECALL")        # HIGH sorted first

    def test_junk_scans_are_counted_without_inventing_units(self) -> None:
        self.scan("garbage")
        self.scan("HC1.UNIT-ZZZZZZZZZZZZZZZZ." + "a" * 43)
        body = self.get("AUTH", "/api/authority/signals")
        self.assertEqual(body["invalid_scans_without_a_real_unit"],
                         {"MALFORMED": 1, "NOT_RECOGNISED": 1})
        self.assertEqual(body["signals"], [])

    def test_scan_log_stores_no_client_data_and_public_answer_is_unchanged(self) -> None:
        cols = {r["name"] for r in db.query_all("PRAGMA table_info(consumer_scans);")}
        self.assertEqual(cols, {"id", "unit_id", "result", "reason", "scanned_at"})
        bad = _wrong_tag(self.units[0]["qr_payload"])
        unknown = "HC1.UNIT-ZZZZZZZZZZZZZZZZ." + bad.split(".")[2]
        self.assertEqual(self.scan(bad), self.scan(unknown))    # still no enumeration


class TestRecallsAndHotspots(OversightCase):
    def test_recall_view_lists_blend_descendants_units_and_scans(self) -> None:
        a = self.at_processor("HIVE-A", 10.0)
        b = self.at_processor("HIVE-B", 8.0)
        out = self.call("PROC", "post", "/api/blends",
                        {"inputs": [{"batch_id": a, "weight_kg": 6}, {"batch_id": b, "weight_kg": 4}]},
                        201).get_json()["batch_id"]
        self.lab_pass(out)
        units = self.call("PROC", "post", f"/api/batches/{out}/packaging",
                          {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE},
                          201).get_json()["units"]
        self.call("AUTH", "post", "/api/recalls", {"batch_id": a, "reason": "Source contaminated"}, 201)
        self.scan(units[0]["qr_payload"])
        view = self.get("AUTH", "/api/authority/recalls")[0]
        self.assertEqual(view["root_batch_id"], a)
        self.assertIn(out, view["affected_batches"])
        self.assertEqual(view["units_by_status"], {"RECALLED": 2})
        self.assertEqual(view["consumer_scans_since_recall"], 1)

    def test_hotspots_rank_critical_regions_and_geo_is_coarse(self) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE hives SET location = 'Kolar' WHERE hive_id = 'HIVE-A';")
            conn.execute("UPDATE hives SET location = 'Tumkur' WHERE hive_id = 'HIVE-B';")
        self.post_telemetry({"hive_id": "HIVE-A", "temperature": 20.0, "humidity": 60, "weight": 40})
        self.call("BK", "post", "/api/harvest", {"hive_id": "HIVE-B", "harvest_date": TODAY,
                                                 "total_weight": 5, "floral_type": "Neem",
                                                 "latitude": 13.13674, "longitude": 78.12921}, 201)
        body = self.get("AUTH", "/api/authority/hotspots")
        self.assertEqual(body["regions"][0]["region"], "Kolar")
        self.assertEqual(body["regions"][0]["CRITICAL"], 1)
        self.assertEqual(body["harvest_geo_cells"], [{"lat": 13.1, "lng": 78.1, "harvests": 1,
                                                      "harvest_kg": 5.0}])
        text = json.dumps(body)
        self.assertNotIn("13.13674", text)
        self.assertNotIn("78.12921", text)


if __name__ == "__main__":
    unittest.main()


class TestFrontendServing(OversightCase):
    """Flask serves the built React app at /app/ (tests use a temporary dist
    so they do not depend on a local `npm run build`)."""

    def setUp(self) -> None:
        super().setUp()
        import tempfile
        from pathlib import Path
        self.dist = Path(tempfile.mkdtemp(prefix="hc-dist-"))
        self._orig = config.FRONTEND_DIST

    def tearDown(self) -> None:
        import shutil
        config.FRONTEND_DIST = self._orig
        shutil.rmtree(self.dist, ignore_errors=True)
        super().tearDown()

    def test_unbuilt_frontend_gives_instructions_not_a_crash(self) -> None:
        config.FRONTEND_DIST = self.dist / "missing"
        r = self.client.get("/app/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("npm run build", r.get_json()["message"])

    def test_built_frontend_is_served_with_security_headers_and_spa_fallback(self) -> None:
        (self.dist / "assets").mkdir()
        (self.dist / "index.html").write_text("<!doctype html><div id=root></div>")
        (self.dist / "assets" / "app.js").write_text("console.log(1)")
        config.FRONTEND_DIST = self.dist

        def fetch(path):
            """GET and close (file responses are streamed; close releases the file)."""
            response = self.client.get(path)
            result = (response.status_code, response.get_data(as_text=True), dict(response.headers))
            response.close()
            return result

        status, _, headers = fetch("/app/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(fetch("/app/assets/app.js")[0], 200)
        self.assertIn("id=root", fetch("/app/some/client/route")[1])
        self.assertEqual(fetch("/app/assets/missing.js")[0], 404)
        for probe in ("/app/../honeychain/config.py", "/app/..%2f..%2fhoneychain%2fconfig.py",
                      "/app/%2e%2e/%2e%2e/.env.example"):
            self.assertNotIn("HONEYCHAIN", fetch(probe)[1], probe)

    def test_real_build_has_no_inline_script_so_the_csp_holds(self) -> None:
        index = self._orig / "index.html"
        if not index.is_file():
            self.skipTest("frontend not built locally (cd frontend && npm run build)")
        html = index.read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
