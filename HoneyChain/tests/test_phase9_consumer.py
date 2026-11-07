"""
Phase 9 -- cryptographic per-unit QR + public consumer verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unittest

from tests.test_phase6_chain_events import FakeAnchor
from tests.test_phase8_supply_chain import BEST_BEFORE, TODAY, SupplyCase

from honeychain import chain_events, config, db, unit_qr

SENSITIVE_KEYS = re.compile(r"(secret|verifier|password|token|email|phone|latitude|longitude|"
                            r"notes|driver|vehicle|user_id|actor|audit|_org_id|organization_id|"
                            r"transfer_id|shipment_id|event_id|processing_id|lab_result_id|"
                            r"packaging_id|retail_record_id|recall_id|harvest_id|\bid\b)", re.I)


def _walk_keys(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(k)
            _walk_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, out)
    return out


class TestQRConstruction(unittest.TestCase):
    UNIT = "UNIT-ABCDEFGHIJKLMNOP"

    def test_tag_is_standard_hmac_sha256_bound_to_version_and_unit(self) -> None:
        secret = "s3cret-value"
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), f"HC1|{self.UNIT}".encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        self.assertEqual(unit_qr.tag(self.UNIT, secret), expected)
        self.assertNotEqual(unit_qr.tag("UNIT-ABCDEFGHIJKLMNOQ", secret), expected)
        self.assertEqual(unit_qr.payload(self.UNIT, secret), f"HC1.{self.UNIT}.{expected}")

    def test_payload_never_contains_the_secret_and_url_uses_fragment(self) -> None:
        secret = "very-distinctive-secret-XYZ"
        p = unit_qr.payload(self.UNIT, secret)
        self.assertNotIn(secret, p)
        self.assertEqual(unit_qr.url(p), f"{config.PUBLIC_BASE_URL}/q#{p}")

    def test_strict_parsing(self) -> None:
        good = unit_qr.payload(self.UNIT, "x")
        self.assertEqual(unit_qr.parse(good)[1], self.UNIT)
        self.assertEqual(unit_qr.parse(unit_qr.url(good))[1], self.UNIT)
        for bad in (None, 123, "", "x" * 600, "HC1", good + "=", good[:-1],
                    good.replace("UNIT-", "unit-"), "HC1.UNIT-ABC.x", "<script>alert(1)</script>",
                    "HC1.UNIT-ABCDEFGHIJKLMNOP.' OR 1=1 --aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "../../etc/passwd"):
            with self.assertRaises(unit_qr.QRFormatError, msg=repr(bad)) as ctx:
                unit_qr.parse(bad)
            self.assertEqual(ctx.exception.code, "MALFORMED")
        with self.assertRaises(unit_qr.QRFormatError) as ctx:
            unit_qr.parse(good.replace("HC1.", "HC2.", 1))
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_VERSION")


class ConsumerCase(SupplyCase):
    def setUp(self) -> None:
        super().setUp()
        self.batch, body = self.packaged(count=3)
        self.units = body["units"]
        self.qr = self.units[0]["qr_payload"]

    def verify(self, qr, status=200):
        r = self.client.post("/api/public/verify", json={"qr": qr})   # no auth header
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:300])
        return r.get_json()

    def assert_no_leaks(self, body_text: str, body: dict) -> None:
        for unit in self.units:
            self.assertNotIn(unit["unit_secret"], body_text)
        for row in db.query_all("SELECT secret_hash, qr_verifier FROM product_units;"):
            self.assertNotIn(row["secret_hash"], body_text)
            if row["qr_verifier"]:
                self.assertNotIn(row["qr_verifier"], body_text)
        for org in db.query_all("SELECT org_id FROM organizations;"):
            self.assertNotIn(org["org_id"], body_text)
        for user in db.query_all("SELECT user_id, email, name FROM users;"):
            for value in (user["user_id"], user["email"]):
                self.assertNotIn(value, body_text)
        bad = [k for k in _walk_keys(body, []) if SENSITIVE_KEYS.search(k)]
        self.assertEqual(bad, [], f"sensitive keys in public response: {bad}")


class TestIssuanceAndStorage(ConsumerCase):
    def test_packaging_stores_only_a_verifier(self) -> None:
        for unit in self.units:
            row = db.query_one("SELECT * FROM product_units WHERE unit_id = ?;", (unit["unit_id"],))
            tag = unit["qr_payload"].split(".")[2]
            self.assertEqual(row["qr_verifier"], hashlib.sha256(tag.encode()).hexdigest())
            self.assertEqual(row["qr_version"], 1)
            for column in row.keys():
                self.assertNotEqual(row[column], unit["unit_secret"], column)
                self.assertNotEqual(row[column], tag, column)

    def test_every_unit_has_a_distinct_qr(self) -> None:
        payloads = {u["qr_payload"] for u in self.units}
        self.assertEqual(len(payloads), len(self.units))


class TestPublicVerification(ConsumerCase):
    def test_authentic_unit_needs_no_login_and_is_truthful(self) -> None:
        body = self.verify(self.qr)
        self.assertEqual(body["result"], "AUTHENTIC")
        self.assertIn("does not by itself mean the product is safe", body["authenticity_note"])
        self.assertEqual(body["unit"]["unit_id"], self.units[0]["unit_id"])
        self.assertEqual(body["unit"]["packager"], "PROC")
        prov = body["provenance"]
        self.assertEqual(prov["batch_id"], self.batch)
        self.assertEqual(prov["harvests"][0]["producer"], "BK")
        self.assertEqual(prov["lab"]["status"], "PASSED")
        self.assertTrue(prov["ledger_record_intact"])
        self.assertEqual(prov["custody"][0], {"from": "BK", "to": "PROC", "date": TODAY})
        self.assertFalse(body["recall"]["active"])

    def test_full_url_form_also_verifies(self) -> None:
        self.assertEqual(self.verify(self.units[1]["qr_url"])["result"], "AUTHENTIC")

    def test_rejections_are_strict_and_uniform(self) -> None:
        other = self.units[1]["unit_id"]
        tag = self.qr.split(".")[2]
        flipped = tag[:-1] + ("A" if tag[-1] != "A" else "B")
        cases = {
            "wrong unit id": f"HC1.{other}.{tag}",
            "modified tag": f"HC1.{self.units[0]['unit_id']}.{flipped}",
            "unknown unit": f"HC1.UNIT-ZZZZZZZZZZZZZZZZ.{tag}",
        }
        responses = {name: self.verify(qr) for name, qr in cases.items()}
        for name, body in responses.items():
            self.assertEqual(body["result"], "INVALID", name)
            self.assertNotIn("provenance", body, name)
        # unknown unit and wrong tag must be indistinguishable (no enumeration)
        self.assertEqual(responses["unknown unit"], responses["modified tag"])
        self.assertEqual(self.verify(self.qr.replace("HC1.", "HC7.", 1))["reason"], "UNSUPPORTED_VERSION")
        for junk in ("", "garbage", None, 42, ["x"], "HC1.." + "a" * 43):
            self.assertEqual(self.verify(junk)["reason"], "MALFORMED")
        r = self.client.post("/api/public/verify", data=b"{not json", content_type="application/json")
        self.assertEqual(r.get_json()["result"], "INVALID")

    def test_scans_are_counted_only_for_authentic_codes(self) -> None:
        self.verify(self.qr)
        self.verify(self.qr)
        self.verify("HC1.UNIT-ZZZZZZZZZZZZZZZZ." + "a" * 43)
        row = db.query_one("SELECT scan_count, first_scanned_at FROM product_units WHERE unit_id = ?;",
                           (self.units[0]["unit_id"],))
        self.assertEqual(row["scan_count"], 2)
        self.assertIsNotNone(row["first_scanned_at"])

    def test_public_response_leaks_nothing_internal(self) -> None:
        r = self.client.post("/api/public/verify", json={"qr": self.qr})
        self.assert_no_leaks(r.get_data(as_text=True), r.get_json())
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_only_post_is_accepted(self) -> None:
        self.assertEqual(self.client.get("/api/public/verify").status_code, 405)


class TestRevocationRecallAndLab(ConsumerCase):
    def test_revoked_label_is_rejected_without_provenance(self) -> None:
        uid = self.units[0]["unit_id"]
        self.call("PROC", "post", f"/api/units/{uid}/revoke-qr", expect=200)
        body = self.verify(self.qr)
        self.assertEqual(body["result"], "REVOKED")
        self.assertNotIn("provenance", body)
        self.call("PROC", "post", f"/api/units/{uid}/revoke-qr", expect=409)
        self.assertEqual(self.verify(self.units[1]["qr_payload"])["result"], "AUTHENTIC")
        self.assertIn("UNIT_QR_REVOKED", self.actions())

    def test_recall_is_shown_even_for_a_revoked_label(self) -> None:
        self.call("PROC", "post", f"/api/units/{self.units[0]['unit_id']}/revoke-qr", expect=200)
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Residue found"}, 201)
        body = self.verify(self.qr)
        self.assertEqual(body["result"], "REVOKED")
        self.assertTrue(body["recall"]["active"])
        self.assertNotIn("provenance", body)

    def test_recall_is_visible_even_though_the_code_is_authentic(self) -> None:
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Residue found"}, 201)
        body = self.verify(self.qr)
        self.assertEqual(body["result"], "AUTHENTIC")
        self.assertTrue(body["recall"]["active"])
        self.assertEqual(body["recall"]["reason"], "Residue found")
        self.assertEqual(body["unit"]["status"], "RECALLED")
        self.assertTrue(any("RECALLED" in w for w in body["warnings"]))

    def test_recall_is_never_served_stale(self) -> None:
        self.assertFalse(self.verify(self.qr)["recall"]["active"])
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Late-detected residue"}, 201)
        self.assertTrue(self.verify(self.qr)["recall"]["active"])

    def test_lab_picture_is_truthful_as_it_changes_after_packaging(self) -> None:
        # (Packaging itself requires a PASSED result -- a Phase 8 rule -- so
        # an untested unit cannot exist through the normal flow.)
        rid = self.call("PROC", "post", f"/api/batches/{self.batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB2"}, 201).get_json()["lab_result_id"]
        lab = self.verify(self.qr)["provenance"]["lab"]
        self.assertEqual((lab["status"], lab["pending_tests"], len(lab["results"])), ("PASSED", 1, 1))
        self.call("LAB2", "post", f"/api/lab/results/{rid}",
                  {"status": "FAILED", "test_date": TODAY, "c4_sugar_pct": 12.0}, 201)
        body = self.verify(self.qr)
        self.assertEqual(body["provenance"]["lab"]["status"], "FAILED")
        self.assertEqual(sorted(r["verdict"] for r in body["provenance"]["lab"]["results"]),
                         ["FAILED", "PASSED"])
        self.assertTrue(any("FAILED" in w for w in body["warnings"]))
        self.assertEqual(body["result"], "AUTHENTIC")   # authentic label, failed product test


class TestBlendProvenance(SupplyCase):
    def test_blended_unit_traces_to_every_source_hive_and_upstream_recall(self) -> None:
        a = self.at_processor("HIVE-A", 10.0)
        b = self.at_processor("HIVE-B", 8.0)
        blend = self.call("PROC", "post", "/api/blends",
                          {"inputs": [{"batch_id": a, "weight_kg": 6}, {"batch_id": b, "weight_kg": 4}],
                           "floral_type": "Mixed"}, 201).get_json()
        out = blend["batch_id"]
        self.lab_pass(out)
        units = self.call("PROC", "post", f"/api/batches/{out}/packaging",
                          {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE}, 201).get_json()["units"]
        body = self.client.post("/api/public/verify", json={"qr": units[0]["qr_payload"]}).get_json()
        sources = body["provenance"]["blended_from"]
        self.assertEqual(sorted(s["source"]["batch_id"] for s in sources), sorted([a, b]))
        hives = sorted(h["hive"] for s in sources for h in s["source"]["harvests"])
        self.assertEqual(hives, ["HIVE-A", "HIVE-B"])
        self.assertEqual(sorted(s["weight_kg"] for s in sources), [4.0, 6.0])
        # recall of ONE source batch reaches the blend unit's consumer view
        self.call("AUTH", "post", "/api/recalls", {"batch_id": a, "reason": "Source contaminated"}, 201)
        body = self.client.post("/api/public/verify", json={"qr": units[0]["qr_payload"]}).get_json()
        self.assertEqual(body["result"], "AUTHENTIC")
        self.assertTrue(body["recall"]["active"])


class TestReissueAndIDOR(ConsumerCase):
    def test_packager_can_reissue_with_the_secret_same_payload(self) -> None:
        uid, secret = self.units[0]["unit_id"], self.units[0]["unit_secret"]
        r = self.call("PROC", "post", f"/api/units/{uid}/qr", {"unit_secret": secret}, 200).get_json()
        self.assertEqual(r["qr_payload"], self.qr)
        self.assertTrue(base64.b64decode(r["qr_png_base64"]).startswith(b"\x89PNG"))
        self.assertNotIn(secret, json.dumps({k: v for k, v in r.items() if k != "qr_png_base64"}))

    def test_wrong_secret_other_org_and_unauthenticated_are_refused(self) -> None:
        uid = self.units[0]["unit_id"]
        self.call("PROC", "post", f"/api/units/{uid}/qr", {"unit_secret": "guess"}, 403)
        self.assertIn("UNIT_QR_SECRET_MISMATCH", self.actions())
        self.call("PROC2", "post", f"/api/units/{uid}/qr", {"unit_secret": self.units[0]["unit_secret"]}, 403)
        self.call("PROC2", "post", f"/api/units/{uid}/revoke-qr", expect=403)
        for who in ("BK", "LAB", "RET", "DIST"):
            self.call(who, "post", f"/api/units/{uid}/revoke-qr", expect=403)
        self.assertEqual(self.client.post(f"/api/units/{uid}/revoke-qr").status_code, 401)
        for bad in ("UNIT-NOPE", "../etc", "UNIT-ZZZZZZZZZZZZZZZZ"):
            self.call("PROC", "post", f"/api/units/{bad}/revoke-qr", expect=404)
        self.assertEqual(self.verify(self.qr)["result"], "AUTHENTIC")   # nothing changed

    def test_legacy_unit_without_verifier_gets_a_code_only_via_the_secret(self) -> None:
        uid, secret = self.units[2]["unit_id"], self.units[2]["unit_secret"]
        with db.transaction() as conn:
            conn.execute("UPDATE product_units SET qr_verifier = NULL WHERE unit_id = ?;", (uid,))
        self.assertEqual(self.verify(self.units[2]["qr_payload"])["result"], "INVALID")
        self.call("PROC", "post", f"/api/units/{uid}/qr", {"unit_secret": secret}, 200)
        self.assertEqual(self.verify(self.units[2]["qr_payload"])["result"], "AUTHENTIC")


class TestLandingPageAndAnchoring(ConsumerCase):
    def test_landing_page_is_public_and_renders_safely(self) -> None:
        # Without a React build, /q serves the self-contained template page
        # (with a build it redirects to /app/verify -- see the QR flow tests).
        from pathlib import Path
        from unittest import mock
        with mock.patch.object(config, "FRONTEND_DIST", Path("/nonexistent-frontend-dist")):
            r = self.client.get("/q")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn('name="referrer" content="no-referrer"', html)

    def test_blockchain_status_is_truthful(self) -> None:
        events = self.verify(self.qr)["provenance"]["blockchain"]
        self.assertTrue(events)
        self.assertTrue(all(e["status"] == "RECORDED_NOT_YET_ANCHORED" and e["tx_hash"] is None
                            for e in events))
        chain_events.create_checkpoint()
        chain_events.submit_checkpoints(anchor=FakeAnchor())
        events = self.verify(self.qr)["provenance"]["blockchain"]
        self.assertTrue(all(e["anchored_on_chain"] and e["tx_hash"] and e["inclusion_proof_valid"]
                            for e in events))


if __name__ == "__main__":
    unittest.main()
