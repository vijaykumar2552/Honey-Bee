"""Blockchain ledger, quality scoring, QR generation and consumer verification."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.base import HoneyChainTestCase

from honeychain import config, db, qrgen, quality
from honeychain.ledger import (
    GENESIS_PREVIOUS_HASH,
    compute_block_hash,
    canonical_payload,
    ledger,
)


class TestCanonicalHashing(unittest.TestCase):

    BLOCK = {
        "block_index": 0,
        "batch_id": "HNY-2026-0001",
        "hive_id": "HIVE-001",
        "harvest_date": "2026-09-01",
        "total_weight": 12.5,
        "floral_type": "Acacia",
        "quality_score": 87.25,
        "timestamp": "2026-09-01T10:00:00Z",
        "previous_hash": GENESIS_PREVIOUS_HASH,
    }

    def test_hash_is_deterministic(self):
        self.assertEqual(compute_block_hash(self.BLOCK), compute_block_hash(self.BLOCK))

    def test_hash_is_sha256_hex(self):
        digest = compute_block_hash(self.BLOCK)
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises if not hex

    def test_key_order_does_not_change_the_hash(self):
        reversed_block = dict(reversed(list(self.BLOCK.items())))
        self.assertEqual(compute_block_hash(self.BLOCK), compute_block_hash(reversed_block))

    def test_changing_any_field_changes_the_hash(self):
        baseline = compute_block_hash(self.BLOCK)
        for field in ("batch_id", "hive_id", "harvest_date", "total_weight",
                      "floral_type", "quality_score", "previous_hash"):
            mutated = dict(self.BLOCK)
            mutated[field] = "TAMPERED" if isinstance(mutated[field], str) else 99.99
            self.assertNotEqual(baseline, compute_block_hash(mutated), f"{field} not covered")

    def test_derived_fields_are_excluded_from_the_digest(self):
        """Recording a QR path or an anchor tx must not invalidate the block."""
        baseline = compute_block_hash(self.BLOCK)
        with_extras = {**self.BLOCK, "qr_path": "qrcodes/x.png", "anchor_tx_hash": "0xabc"}
        self.assertEqual(baseline, compute_block_hash(with_extras))

    def test_float_precision_is_normalised(self):
        drifted = {**self.BLOCK, "total_weight": 12.50000000001}
        self.assertEqual(compute_block_hash(self.BLOCK), compute_block_hash(drifted))

    def test_canonical_payload_has_no_incidental_whitespace(self):
        payload = canonical_payload(self.BLOCK)
        self.assertNotIn(", ", payload)
        self.assertNotIn(": ", payload)


class TestLedger(HoneyChainTestCase):

    def _add(self, batch_id: str, hive_id: str = "HIVE-001"):
        return ledger.add_batch(
            batch_id=batch_id,
            hive_id=hive_id,
            harvest_date="2026-09-01",
            total_weight=12.5,
            floral_type="Acacia",
            quality_score=88.0,
        )

    def test_genesis_block_links_to_zero_hash(self):
        block = self._add("BATCH-A")
        self.assertEqual(block["block_index"], 0)
        self.assertEqual(block["previous_hash"], GENESIS_PREVIOUS_HASH)

    def test_blocks_chain_to_their_predecessor(self):
        first = self._add("BATCH-A")
        second = self._add("BATCH-B")
        third = self._add("BATCH-C")
        self.assertEqual(second["previous_hash"], first["block_hash"])
        self.assertEqual(third["previous_hash"], second["block_hash"])
        self.assertEqual([b["block_index"] for b in (first, second, third)], [0, 1, 2])

    def test_duplicate_batch_id_rejected(self):
        self._add("BATCH-A")
        from honeychain.ledger import LedgerError

        with self.assertRaises(LedgerError):
            self._add("BATCH-A")

    def test_verification_passes_on_an_untouched_chain(self):
        self._add("BATCH-A")
        self._add("BATCH-B")
        result = ledger.verify_batch_authenticity("BATCH-B")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(all(check["passed"] for check in result["checks"]))

    def test_unknown_batch_reports_not_found(self):
        result = ledger.verify_batch_authenticity("NO-SUCH-BATCH")
        self.assertFalse(result["found"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "NOT_FOUND")

    def test_editing_a_field_is_detected(self):
        self._add("BATCH-A")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE honey_batches SET total_weight = 999.0 WHERE batch_id = ?;",
                ("BATCH-A",),
            )
        result = ledger.verify_batch_authenticity("BATCH-A")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "TAMPERED")
        integrity = next(c for c in result["checks"] if c["name"] == "block_hash_integrity")
        self.assertFalse(integrity["passed"])

    def test_editing_an_earlier_block_invalidates_later_ones(self):
        """The property a row-exists check cannot provide."""
        self._add("BATCH-A")
        self._add("BATCH-B")
        self._add("BATCH-C")

        with db.transaction() as conn:
            conn.execute(
                "UPDATE honey_batches SET floral_type = 'Forged' WHERE batch_id = ?;",
                ("BATCH-A",),
            )

        # The edited block itself fails...
        self.assertFalse(ledger.verify_batch_authenticity("BATCH-A")["verified"])
        # ...and so does every descendant, via the ancestor walk.
        for descendant in ("BATCH-B", "BATCH-C"):
            result = ledger.verify_batch_authenticity(descendant)
            self.assertFalse(result["verified"], f"{descendant} should be invalidated")
            ancestors = next(c for c in result["checks"] if c["name"] == "ancestor_chain")
            self.assertFalse(ancestors["passed"])
            self.assertEqual(ancestors["broken_at_block"], 0)

    def test_rewriting_a_stored_hash_is_also_detected(self):
        """Recomputing the hash to match forged data still breaks the linkage."""
        self._add("BATCH-A")
        self._add("BATCH-B")

        forged = dict(db.get_batch("BATCH-A"))
        forged["total_weight"] = 999.0
        forged_hash = compute_block_hash(forged)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE honey_batches SET total_weight = ?, block_hash = ? WHERE batch_id = ?;",
                (999.0, forged_hash, "BATCH-A"),
            )

        # BATCH-A now hashes consistently, but BATCH-B still points at the old hash.
        self.assertTrue(ledger.verify_batch_authenticity("BATCH-A")["verified"])
        self.assertFalse(ledger.verify_batch_authenticity("BATCH-B")["verified"])
        self.assertFalse(ledger.verify_chain()["valid"])

    def test_whole_chain_verification(self):
        self._add("BATCH-A")
        self._add("BATCH-B")
        chain = ledger.verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["height"], 2)
        self.assertEqual(chain["broken_blocks"], [])

    def test_empty_chain_is_valid(self):
        chain = ledger.verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["height"], 0)


class TestQualityScoring(HoneyChainTestCase):

    def test_score_requires_real_telemetry(self):
        with self.assertRaises(quality.InsufficientTelemetry):
            quality.compute_quality_score("HIVE-GHOST")

    def test_score_is_deterministic(self):
        self.seed_hive("HIVE-001", count=10)
        first, _ = quality.compute_quality_score("HIVE-001")
        second, _ = quality.compute_quality_score("HIVE-001")
        self.assertEqual(first, second)

    def test_score_is_within_bounds(self):
        self.seed_hive("HIVE-001", count=10)
        score, _ = quality.compute_quality_score("HIVE-001")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_healthy_hive_scores_higher_than_a_stressed_one(self):
        self.seed_hive("HIVE-GOOD", count=12)
        for index in range(12):
            self.post_telemetry(
                {
                    "hive_id": "HIVE-BAD",
                    "temperature": 29.0 + (index % 5) * 1.8,   # unstable, off-optimum
                    "humidity": 90.0,                          # outside the curing band
                    "weight": 20.0 + (index % 6) * 4.0,        # very unstable
                    "audio_frequency": 430.0,
                }
            )
        good, _ = quality.compute_quality_score("HIVE-GOOD")
        bad, _ = quality.compute_quality_score("HIVE-BAD")
        self.assertGreater(good, bad)

    def test_breakdown_reports_component_availability(self):
        self.seed_hive("HIVE-001", count=6)
        _, breakdown = quality.compute_quality_score("HIVE-001")
        self.assertIn("components", breakdown)
        self.assertEqual(breakdown["readings_analysed"], 6)
        for component in breakdown["components"].values():
            self.assertIn("available", component)

    def test_audit_reports_unavailable_rather_than_inventing_values(self):
        audit = quality.production_health_audit("HIVE-GHOST")
        self.assertFalse(audit["available"])
        self.assertIsNone(audit["avg_temperature"])
        self.assertIsNone(audit["quality_score"])


class TestQrGeneration(HoneyChainTestCase):

    def test_qr_png_is_written(self):
        info = qrgen.generate_batch_qr("HNY-2026-0001")
        path = Path(info["absolute_path"])
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_qr_encodes_the_verification_url(self):
        info = qrgen.generate_batch_qr("HNY-2026-0002")
        self.assertEqual(info["url"], config.public_verify_url("HNY-2026-0002"))
        self.assertIn("/verify/HNY-2026-0002", info["url"])

    def test_qr_decodes_back_to_the_url(self):
        """Verified with an independent decoder, not just by trusting the encoder."""
        try:
            import cv2  # type: ignore
        except ImportError:
            self.skipTest("OpenCV not installed; skipping independent QR decode check")

        info = qrgen.generate_batch_qr("HNY-2026-0003")
        image = cv2.imread(info["absolute_path"])
        self.assertIsNotNone(image, "QR PNG could not be read back")
        decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
        self.assertEqual(decoded, info["url"])

    def test_filename_cannot_escape_the_qr_directory(self):
        filename = qrgen.safe_filename("../../etc/passwd")
        self.assertNotIn("/", filename)
        self.assertNotIn("..", filename)
        self.assertTrue(filename.endswith(".png"))

    def test_unusable_batch_id_raises(self):
        with self.assertRaises(qrgen.QRGenerationError):
            qrgen.safe_filename("///")


class TestHarvestApi(HoneyChainTestCase):

    def _harvest(self, **overrides):
        payload = {
            "hive_id": "HIVE-001",
            "harvest_date": "2026-09-01",
            "total_weight": 12.5,
            "floral_type": "Wildflower blend",
        }
        payload.update(overrides)
        self.own_hive(payload["hive_id"])
        return self.client.post(
            "/api/harvest",
            data=json.dumps(payload),
            content_type="application/json",
            headers=self.admin_headers(),
        )

    def test_harvest_creates_a_block_and_a_qr(self):
        self.seed_hive("HIVE-001")
        response = self._harvest(batch_id="HNY-2026-0001")
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

        body = response.get_json()
        self.assertEqual(body["batch"]["batch_id"], "HNY-2026-0001")
        self.assertEqual(body["batch"]["block_index"], 0)
        self.assertEqual(len(body["batch"]["block_hash"]), 64)
        self.assertIsNotNone(body["qr"])
        self.assertTrue(Path(body["qr"]["absolute_path"]).is_file())
        self.assertIn("/verify/HNY-2026-0001", body["verify_url"])

    def test_quality_score_comes_from_telemetry(self):
        self.seed_hive("HIVE-001", count=10)
        body = self._harvest().get_json()
        expected, _ = quality.compute_quality_score("HIVE-001")
        self.assertEqual(body["batch"]["quality_score"], expected)
        self.assertEqual(body["quality_detail"]["readings_analysed"], 10)

    def test_batch_id_is_auto_generated_when_omitted(self):
        self.seed_hive("HIVE-001")
        body = self._harvest().get_json()
        self.assertRegex(body["batch"]["batch_id"], r"^HNY-\d{4}-\d{4}$")

    def test_harvest_for_unknown_hive_returns_404(self):
        response = self._harvest(hive_id="HIVE-GHOST")
        self.assertEqual(response.status_code, 404)

    def test_duplicate_batch_id_returns_409(self):
        # Phase 5: a duplicate id is a conflict with existing state (409),
        # not a malformed request (400). Still rejected; only the code changed.
        self.seed_hive("HIVE-001")
        self.assertEqual(self._harvest(batch_id="HNY-DUP").status_code, 201)
        response = self._harvest(batch_id="HNY-DUP")
        self.assertEqual(response.status_code, 409)

    def test_invalid_weight_returns_400(self):
        self.seed_hive("HIVE-001")
        for weight in (-5, 0, "heavy", None, 999999):
            self.assertEqual(self._harvest(total_weight=weight).status_code, 400)

    def test_missing_floral_type_returns_400(self):
        self.seed_hive("HIVE-001")
        self.assertEqual(self._harvest(floral_type="").status_code, 400)

    def test_invalid_harvest_date_returns_400(self):
        self.seed_hive("HIVE-001")
        self.assertEqual(self._harvest(harvest_date="not-a-date").status_code, 400)

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            "/api/harvest",
            data=b"{broken",
            content_type="application/json",
            headers=self.admin_headers(),
        )
        self.assertEqual(response.status_code, 400)


class TestVerificationEndpoints(HoneyChainTestCase):

    def _create_batch(self, batch_id="HNY-2026-0001"):
        self.seed_hive("HIVE-001")
        self.own_hive("HIVE-001")
        response = self.client.post(
            "/api/harvest",
            data=json.dumps(
                {
                    "hive_id": "HIVE-001",
                    "batch_id": batch_id,
                    "harvest_date": "2026-09-01",
                    "total_weight": 12.5,
                    "floral_type": "Acacia",
                }
            ),
            content_type="application/json",
            headers=self.admin_headers(),
        )
        self.assertEqual(response.status_code, 201)
        return batch_id

    def test_json_verification_reports_verified(self):
        batch_id = self._create_batch()
        body = self.client.get(f"/api/verify/{batch_id}").get_json()
        self.assertTrue(body["verified"])
        self.assertEqual(body["status"], "VERIFIED")
        self.assertIn("batch", body)
        self.assertIn("production_audit", body)
        self.assertTrue(body["production_audit"]["available"])

    def test_json_verification_reports_tampered(self):
        batch_id = self._create_batch()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE honey_batches SET total_weight = 500.0 WHERE batch_id = ?;",
                (batch_id,),
            )
        body = self.client.get(f"/api/verify/{batch_id}").get_json()
        self.assertFalse(body["verified"])
        self.assertEqual(body["status"], "TAMPERED")

    def test_json_verification_unknown_batch_is_404(self):
        response = self.client.get("/api/verify/NO-SUCH-BATCH")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["status"], "NOT_FOUND")

    def test_public_page_shows_verified_authentic(self):
        batch_id = self._create_batch()
        response = self.client.get(f"/verify/{batch_id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("VERIFIED AUTHENTIC", html)
        self.assertNotIn("TAMPERED", html)
        # Batch details and cryptographic proof must both be present.
        self.assertIn("HIVE-001", html)
        self.assertIn("Acacia", html)
        self.assertIn("Cryptographic proof", html)

    def test_public_page_shows_tampered_alert(self):
        batch_id = self._create_batch()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE honey_batches SET floral_type = 'Forged' WHERE batch_id = ?;",
                (batch_id,),
            )
        html = self.client.get(f"/verify/{batch_id}").get_data(as_text=True)
        self.assertIn("TAMPERED", html)
        self.assertNotIn("VERIFIED AUTHENTIC", html)

    def test_public_page_needs_no_authentication(self):
        batch_id = self._create_batch()
        # No signature, no secret, no session -- must still render.
        self.assertEqual(self.client.get(f"/verify/{batch_id}").status_code, 200)

    def test_public_page_unknown_batch_is_404_with_guidance(self):
        response = self.client.get("/verify/NOPE-1234")
        self.assertEqual(response.status_code, 404)
        self.assertIn("BATCH NOT FOUND", response.get_data(as_text=True))

    def test_public_page_escapes_injected_markup(self):
        """A hostile batch reference must not become live markup."""
        response = self.client.get("/verify/%3Cscript%3Ealert(1)%3C/script%3E")
        html = response.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_batches_listing_includes_verify_urls(self):
        batch_id = self._create_batch()
        batches = self.client.get("/api/batches", headers=self.admin_headers()).get_json()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_id"], batch_id)
        self.assertIn(f"/verify/{batch_id}", batches[0]["verify_url"])
        self.assertIsNotNone(batches[0]["qr_path"])

    def test_chain_endpoint_reports_integrity(self):
        self._create_batch()
        body = self.client.get("/api/chain").get_json()
        self.assertTrue(body["valid"])
        self.assertEqual(body["height"], 1)


if __name__ == "__main__":
    unittest.main()
