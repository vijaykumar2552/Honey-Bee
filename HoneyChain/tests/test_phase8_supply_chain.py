"""
Phase 8 -- complete honey supply chain, through the real API and database.

Cast: BK (beekeeper), PROC (processor), LAB (laboratory), DIST (logistics /
distributor), RET (retailer), AUTH (authority) -- plus PROC2, LAB2, RET2 as
unrelated organizations for isolation / IDOR attacks.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unittest
from datetime import date, timedelta
from unittest import mock

from tests.base import HoneyChainTestCase
from tests.test_phase6_chain_events import FakeAnchor

from honeychain import chain_events, db

TODAY = date.today().isoformat()
BEST_BEFORE = (date.today() + timedelta(days=365)).isoformat()


class SupplyCase(HoneyChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.h = {}
        for key, org_type, role in (("BK", "BEEKEEPER", "BEEKEEPER"), ("PROC", "PROCESSOR", "PROCESSOR"),
                                    ("LAB", "LAB", "LAB"), ("DIST", "LOGISTICS", "LOGISTICS"),
                                    ("RET", "RETAILER", "RETAILER"), ("PROC2", "PROCESSOR", "PROCESSOR"),
                                    ("LAB2", "LAB", "LAB"), ("RET2", "RETAILER", "RETAILER"),
                                    ("AUTH", "AUTHORITY", "AUTHORITY")):
            pk = self.create_org(org_id=f"ORG-{key}", name=key, org_type=org_type)
            user = self.create_user(user_id=f"USR-{key}", email=f"{key.lower()}@sc.test", role=role,
                                    organization_pk=pk)
            self.h[key] = self.auth_headers_for(user)
        self.seed_hive("HIVE-A", count=3)
        self.own_hive("HIVE-A", "ORG-BK")
        self.seed_hive("HIVE-B", count=3)
        self.own_hive("HIVE-B", "ORG-BK")

    # -- helpers -----------------------------------------------------------
    def call(self, who, method, path, body=None, expect=None):
        r = getattr(self.client, method)(path, json=body, headers=self.h[who] if who else {})
        if expect is not None:
            self.assertEqual(r.status_code, expect, f"{method.upper()} {path}: {r.get_data(as_text=True)[:300]}")
        return r

    def harvest(self, hive="HIVE-A", kg=20.0) -> str:
        r = self.call("BK", "post", "/api/harvest", {"hive_id": hive, "harvest_date": TODAY,
                                                      "total_weight": kg, "floral_type": f"Flora-{hive}"}, 201)
        return r.get_json()["batch"]["batch_id"]

    def transfer(self, batch, frm, to_org, to_who, accept=True) -> str:
        t = self.call(frm, "post", "/api/custody/transfers",
                      {"batch_id": batch, "to_organization_id": to_org}, 201).get_json()["transfer_id"]
        if accept:
            self.call(to_who, "post", f"/api/custody/transfers/{t}/accept", expect=200)
        return t

    def at_processor(self, hive="HIVE-A", kg=20.0) -> str:
        batch = self.harvest(hive, kg)
        self.transfer(batch, "BK", "ORG-PROC", "PROC")
        return batch

    def lab_pass(self, batch, holder="PROC", lab="LAB", status="PASSED") -> str:
        rid = self.call(holder, "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": f"ORG-{lab}"}, 201).get_json()["lab_result_id"]
        self.call(lab, "post", f"/api/lab/results/{rid}", {"status": status, "test_date": TODAY,
                                                           "moisture_pct": 17.2, "hmf_mg_kg": 12.5}, 201)
        return rid

    def packaged(self, count=4) -> tuple[str, dict]:
        batch = self.at_processor()
        self.lab_pass(batch)
        body = self.call("PROC", "post", f"/api/batches/{batch}/packaging",
                         {"unit_size_g": 500, "unit_count": count, "best_before": BEST_BEFORE}, 201).get_json()
        return batch, body

    def to_retailer(self, batch, retailer="RET") -> tuple[str, str, str]:
        self.transfer(batch, "PROC", "ORG-DIST", "DIST")
        t = self.transfer(batch, "DIST", f"ORG-{retailer}", retailer, accept=False)
        s = self.call("DIST", "post", "/api/shipments", {"custody_transfer_id": t, "origin": "Kolar",
                                                         "destination": "Hyderabad"}, 201).get_json()
        return t, s["shipment_id"], s["retail_record_id"]

    def ship(self, sid, *statuses):
        for status in statuses:
            body = {"status": status}
            if status == "EXCEPTION":   # Phase 10: an exception must state its reason
                body["exception_reason"] = "Delayed at checkpost"
            self.call("DIST", "post", f"/api/shipments/{sid}/status", body, 200)

    def state(self, batch):
        return dict(db.query_one("SELECT * FROM batch_state WHERE batch_id = ?;", (batch,)))

    def actions(self):
        return [e["action"] for e in db.list_audit_events(500)]


# ---------------------------------------------------------------------------
class TestEndToEnd(SupplyCase):
    def test_hive_to_shelf_and_backward_trace(self) -> None:
        batch = self.harvest(kg=20.0)
        self.transfer(batch, "BK", "ORG-PROC", "PROC")
        prc = self.call("PROC", "post", f"/api/batches/{batch}/processing",
                        {"process_type": "FILTERING", "input_kg": 20.0, "output_kg": 19.5,
                         "equipment": "Mesh filter 200um"}, 201).get_json()["processing_id"]
        self.lab_pass(batch)
        pkg = self.call("PROC", "post", f"/api/batches/{batch}/packaging",
                        {"unit_size_g": 500, "unit_count": 10, "best_before": BEST_BEFORE}, 201).get_json()
        unit = pkg["units"][0]["unit_id"]
        t, sid, rid = self.to_retailer(batch)
        self.ship(sid, "IN_TRANSIT", "DELIVERED")
        self.call("RET", "post", f"/api/retail/{rid}/receive", expect=200)
        shelf = self.call("RET", "post", f"/api/retail/{rid}/shelve", expect=200).get_json()
        self.assertEqual(shelf["status"], "ON_SHELF")
        self.assertEqual(self.state(batch)["holder_org_id"],
                         db.get_organization_by_org_id("ORG-RET")["id"])
        self.call("RET", "post", f"/api/retail/units/{unit}/sell", expect=200)

        # Backward: Product Unit -> Batch -> Processing -> Harvest -> Hive
        trace = self.call("RET", "get", f"/api/units/{unit}/trace", expect=200).get_json()
        self.assertEqual(trace["status"], "SOLD")
        self.assertEqual(trace["packaging"]["packaging_id"], pkg["packaging_id"])
        b = trace["batch"]
        self.assertEqual(b["batch_id"], batch)
        self.assertTrue(b["ledger_verified"])
        self.assertEqual([p["processing_id"] for p in b["processing"]], [prc])
        self.assertEqual(b["harvests"][0]["hive_id"], "HIVE-A")
        self.assertEqual(b["lab"]["status"], "PASSED")
        self.assertEqual([c["status"] for c in b["custody"]], ["ACCEPTED", "ACCEPTED", "ACCEPTED"])
        self.assertEqual(b["shipments"][0]["status"], "DELIVERED")
        types = {e["event_type"] for e in b["chain_events"]}
        self.assertTrue({"BATCH_CREATED", "PROCESSING_RECORDED", "LAB_RESULT_RECORDED", "PACKAGED"} <= types)
        all_types = {e["event_type"] for e in chain_events.list_events()}
        self.assertTrue({"CUSTODY_TRANSFERRED", "SHIPPED", "RECEIVED", "RETAIL_REGISTERED"} <= all_types)
        self.assertTrue(chain_events.verify_log()["valid"])
        for action in ("PROCESSING_RECORDED", "LAB_TEST_REQUESTED", "LAB_RESULT_RECORDED", "BATCH_PACKAGED",
                       "PRODUCT_UNITS_CREATED", "CUSTODY_TRANSFER_INITIATED", "CUSTODY_TRANSFER_ACCEPTED",
                       "SHIPMENT_CREATED", "SHIPMENT_STATUS_CHANGED", "RETAIL_RECEIVED",
                       "RETAIL_ON_SHELF", "UNIT_SOLD"):
            self.assertIn(action, self.actions(), action)

        # Every supply-chain event is covered by a real Merkle checkpoint.
        cp = chain_events.create_checkpoint()
        fake = FakeAnchor()
        chain_events.submit_checkpoints(anchor=fake)
        self.assertEqual(chain_events.get_checkpoint(cp["checkpoint_id"])["status"], "CONFIRMED")
        for e in chain_events.list_events():
            self.assertTrue(chain_events.event_proof(e["event_id"])["proof_valid"])


# ---------------------------------------------------------------------------
class TestProcessing(SupplyCase):
    def test_records_event_and_updates_remaining_without_touching_ledger(self) -> None:
        batch = self.at_processor(kg=20.0)
        block_before = db.get_batch(batch)["block_hash"]
        self.call("PROC", "post", f"/api/batches/{batch}/processing",
                  {"process_type": "SETTLING", "input_kg": 10, "output_kg": 9.5}, 201)
        self.assertAlmostEqual(self.state(batch)["remaining_kg"], 19.5)
        self.assertEqual(db.get_batch(batch)["block_hash"], block_before)
        self.assertTrue(self.call("PROC", "get", f"/api/batches/{batch}/trace", expect=200)
                        .get_json()["ledger_verified"])

    def test_authorization(self) -> None:
        batch = self.harvest()
        self.call("BK", "post", f"/api/batches/{batch}/processing", {"process_type": "FILTERING"}, 403)
        self.call("PROC", "post", f"/api/batches/{batch}/processing", {"process_type": "FILTERING"}, 403)
        self.transfer(batch, "BK", "ORG-PROC", "PROC")
        self.call("PROC2", "post", f"/api/batches/{batch}/processing", {"process_type": "FILTERING"}, 403)
        self.call("LAB", "post", f"/api/batches/{batch}/processing", {"process_type": "FILTERING"}, 403)

    def test_validation(self) -> None:
        batch = self.at_processor(kg=10)
        for body in ({"process_type": "BLENDING"}, {"process_type": "MAGIC"},
                     {"process_type": "FILTERING", "input_kg": 11},
                     {"process_type": "FILTERING", "input_kg": 5, "output_kg": 6},
                     {"process_type": "HEATING", "temperature_c": 500},
                     {"process_type": "FILTERING", "started_at": "2026-01-02T00:00:00Z",
                      "completed_at": "2026-01-01T00:00:00Z"}):
            self.call("PROC", "post", f"/api/batches/{batch}/processing", body, 400)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM processing_events;")["n"], 0)

    def test_forged_organization_in_body_is_ignored(self) -> None:
        batch = self.at_processor()
        self.call("PROC", "post", f"/api/batches/{batch}/processing",
                  {"process_type": "FILTERING", "processor_org_id": 999, "organization_id": "ORG-PROC2"}, 201)
        row = db.query_one("SELECT processor_org_id FROM processing_events;")
        self.assertEqual(row["processor_org_id"], db.get_organization_by_org_id("ORG-PROC")["id"])

    def test_chain_event_failure_rolls_the_action_back(self) -> None:
        batch = self.at_processor(kg=10)
        with mock.patch.object(chain_events, "record_event",
                               side_effect=chain_events.ChainEventError("boom")):
            r = self.client.post(f"/api/batches/{batch}/processing", headers=self.h["PROC"],
                                 json={"process_type": "FILTERING", "input_kg": 5, "output_kg": 4})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM processing_events;")["n"], 0)
        self.assertAlmostEqual(self.state(batch)["remaining_kg"], 10.0)


# ---------------------------------------------------------------------------
class TestBlending(SupplyCase):
    def test_blend_preserves_every_source_and_traces_to_every_hive(self) -> None:
        a, b = self.at_processor("HIVE-A", 10), self.at_processor("HIVE-B", 6)
        r = self.call("PROC", "post", "/api/blends", {"inputs": [{"batch_id": a, "weight_kg": 10},
                                                                 {"batch_id": b, "weight_kg": 4}]}, 201).get_json()
        out = r["batch_id"]
        self.assertEqual(r["total_kg"], 14)
        ledger_row = db.get_batch(out)
        self.assertEqual(ledger_row["hive_id"], "HIVE-A+HIVE-B")
        self.assertEqual(self.state(a)["status"], "CONSUMED")
        self.assertAlmostEqual(self.state(b)["remaining_kg"], 2.0)
        trace = self.call("PROC", "get", f"/api/batches/{out}/trace", expect=200).get_json()
        sources = {x["batch_id"]: x for x in trace["blended_from"]}
        self.assertEqual(set(sources), {a, b})
        self.assertEqual(sources[a]["weight_kg"], 10)
        self.assertEqual({x["trace"]["harvests"][0]["hive_id"] for x in trace["blended_from"]},
                         {"HIVE-A", "HIVE-B"})
        self.assertIsNotNone(db.get_batch(a))                   # source batches stay in the ledger
        self.assertTrue(chain_events.verify_log()["valid"])
        self.assertIn("BATCH_BLENDED", self.actions())

    def test_nested_blend_still_reaches_original_hives(self) -> None:
        a, b, c = self.at_processor("HIVE-A", 5), self.at_processor("HIVE-B", 5), self.at_processor("HIVE-A", 5)
        first = self.call("PROC", "post", "/api/blends", {"inputs": [{"batch_id": a, "weight_kg": 5},
                                                                     {"batch_id": b, "weight_kg": 5}]}, 201).get_json()["batch_id"]
        second = self.call("PROC", "post", "/api/blends", {"inputs": [{"batch_id": first, "weight_kg": 10},
                                                                      {"batch_id": c, "weight_kg": 5}]}, 201).get_json()["batch_id"]
        trace = self.call("PROC", "get", f"/api/batches/{second}/trace", expect=200).get_json()
        inner = next(x for x in trace["blended_from"] if x["batch_id"] == first)["trace"]
        self.assertEqual({x["batch_id"] for x in inner["blended_from"]}, {a, b})

    def test_blend_rejections(self) -> None:
        a, b = self.at_processor("HIVE-A", 5), self.at_processor("HIVE-B", 5)
        foreign = self.harvest("HIVE-A", 5)  # still held by the beekeeper
        cases = [({"inputs": [{"batch_id": a, "weight_kg": 1}]}, 400),
                 ({"inputs": [{"batch_id": a, "weight_kg": 1}, {"batch_id": a, "weight_kg": 1}]}, 400),
                 ({"inputs": [{"batch_id": a, "weight_kg": 9}, {"batch_id": b, "weight_kg": 1}]}, 400),
                 ({"inputs": [{"batch_id": a, "weight_kg": 1}, {"batch_id": foreign, "weight_kg": 1}]}, 403),
                 ({"inputs": [{"batch_id": a, "weight_kg": 1}, {"batch_id": "NOPE-1", "weight_kg": 1}]}, 404),
                 ({"inputs": [{"batch_id": a, "weight_kg": 1}, {"batch_id": b, "weight_kg": 1}],
                   "batch_id": a}, 409)]
        for body, status in cases:
            self.call("PROC", "post", "/api/blends", body, status)
        self.call("BK", "post", "/api/blends", {"inputs": [{"batch_id": a, "weight_kg": 1},
                                                           {"batch_id": b, "weight_kg": 1}]}, 403)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM batch_blend_inputs;")["n"], 0)
        self.assertAlmostEqual(self.state(a)["remaining_kg"], 5.0)


# ---------------------------------------------------------------------------
class TestLaboratory(SupplyCase):
    def test_status_progression_and_immutability(self) -> None:
        batch = self.at_processor()
        lab = lambda: self.call("PROC", "get", f"/api/batches/{batch}/lab-results", expect=200).get_json()  # noqa: E731
        self.assertEqual(lab()["status"], "NOT_TESTED")
        rid = self.call("PROC", "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB"}, 201).get_json()["lab_result_id"]
        self.assertEqual(lab()["status"], "PENDING")
        self.call("PROC", "post", f"/api/batches/{batch}/lab-requests", {"lab_organization_id": "ORG-LAB"}, 409)
        body = {"status": "PASSED", "test_date": TODAY, "moisture_pct": 17.1, "hmf_mg_kg": 9.0,
                "c4_sugar_pct": 1.2, "certificate_id": "CERT/2026-001"}
        result = self.call("LAB", "post", f"/api/lab/results/{rid}", body, 201).get_json()
        self.assertEqual((result["status"], result["moisture_pct"]), ("PASSED", 17.1))
        self.assertEqual(result["units"]["hmf_mg_kg"], "mg/kg")
        self.assertEqual(lab()["status"], "PASSED")
        self.call("LAB", "post", f"/api/lab/results/{rid}", {**body, "status": "FAILED"}, 409)

    def test_only_the_requested_lab_may_record(self) -> None:
        batch = self.at_processor()
        rid = self.call("PROC", "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB"}, 201).get_json()["lab_result_id"]
        body = {"status": "PASSED", "test_date": TODAY, "moisture_pct": 17}
        self.call("LAB2", "post", f"/api/lab/results/{rid}", body, 403)
        self.call("PROC", "post", f"/api/lab/results/{rid}", body, 403)
        self.call("BK", "post", f"/api/batches/{batch}/lab-requests", {"lab_organization_id": "ORG-LAB"}, 403)
        self.call("PROC", "post", f"/api/batches/{batch}/lab-requests", {"lab_organization_id": "ORG-PROC2"}, 400)

    def test_result_validation(self) -> None:
        batch = self.at_processor()
        rid = self.call("PROC", "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB"}, 201).get_json()["lab_result_id"]
        future = (date.today() + timedelta(days=10)).isoformat()
        for body in ({"status": "PASSED", "test_date": TODAY},
                     {"status": "APPROVED", "test_date": TODAY, "moisture_pct": 17},
                     {"status": "PASSED", "test_date": future, "moisture_pct": 17},
                     {"status": "PASSED", "test_date": TODAY, "moisture_pct": 120},
                     {"status": "PASSED", "test_date": TODAY, "moisture_pct": 17,
                      "certificate_url": "http://insecure.example/cert.pdf"}):
            self.call("LAB", "post", f"/api/lab/results/{rid}", body, 400)
        self.call("LAB", "post", f"/api/lab/results/{rid}", {"status": "REVIEW", "test_date": TODAY}, 201)

    def test_tampered_lab_result_is_detected(self) -> None:
        batch = self.at_processor()
        rid = self.lab_pass(batch)
        with db.transaction() as conn:
            conn.execute("UPDATE lab_results SET moisture_pct = 15.0 WHERE lab_result_id = ?;", (rid,))
        problems = chain_events.verify_log()["problems"]
        self.assertTrue(any("lab result" in p["issue"] for p in problems), problems)

    def test_failed_result_blocks_packaging(self) -> None:
        batch = self.at_processor()
        self.lab_pass(batch, status="FAILED")
        self.call("PROC", "post", f"/api/batches/{batch}/packaging",
                  {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE}, 409)


# ---------------------------------------------------------------------------
class TestPackaging(SupplyCase):
    def test_units_are_unique_random_and_secrets_are_not_stored(self) -> None:
        batch, body = self.packaged(count=20)
        ids = [u["unit_id"] for u in body["units"]]
        self.assertEqual(len(set(ids)), 20)
        self.assertTrue(all(re.fullmatch(r"UNIT-[A-Z2-7]{16}", i) for i in ids))
        stored = {r["unit_id"]: r["secret_hash"] for r in db.query_all("SELECT * FROM product_units;")}
        for unit in body["units"]:
            self.assertEqual(stored[unit["unit_id"]], hashlib.sha256(unit["unit_secret"].encode()).hexdigest())
            self.assertNotEqual(stored[unit["unit_id"]], unit["unit_secret"])
        listing = self.call("PROC", "get", f"/api/batches/{batch}/units", expect=200).get_data(as_text=True)
        self.assertNotIn("secret", listing)
        self.assertAlmostEqual(self.state(batch)["remaining_kg"], 10.0)  # 20 kg - 20 x 0.5 kg

    def test_packaging_rules(self) -> None:
        batch = self.at_processor(kg=2)
        self.call("PROC", "post", f"/api/batches/{batch}/packaging",
                  {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE}, 409)  # not tested
        self.lab_pass(batch)
        for body in ({"unit_size_g": 500, "unit_count": 5, "best_before": BEST_BEFORE},   # 2.5 kg > 2 kg
                     {"unit_size_g": 10, "unit_count": 1, "best_before": BEST_BEFORE},
                     {"unit_size_g": 500, "unit_count": "2", "best_before": BEST_BEFORE},
                     {"unit_size_g": 500, "unit_count": 1, "best_before": TODAY}):
            self.call("PROC", "post", f"/api/batches/{batch}/packaging", body, 400)
        self.call("PROC2", "post", f"/api/batches/{batch}/packaging",
                  {"unit_size_g": 500, "unit_count": 1, "best_before": BEST_BEFORE}, 403)

    def test_deleted_or_injected_unit_is_detected(self) -> None:
        batch, body = self.packaged(count=3)
        with db.transaction() as conn:
            conn.execute("DELETE FROM product_units WHERE unit_id = ?;", (body["units"][0]["unit_id"],))
        self.assertTrue(any("product units" in p["issue"] for p in chain_events.verify_log()["problems"]))


# ---------------------------------------------------------------------------
class TestCustody(SupplyCase):
    def test_initiation_rules(self) -> None:
        batch = self.harvest()
        self.call("PROC", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-DIST"}, 403)
        for org, status in (("ORG-BK", 400), ("ORG-LAB", 400), ("ORG-AUTH", 400), ("ORG-NOPE", 400)):
            self.call("BK", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": org}, status)
        self.call("BK", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-PROC"}, 201)
        self.call("BK", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-PROC2"}, 409)
        self.call("BK", "post", "/api/custody/transfers", {"batch_id": "NOPE-9", "to_organization_id": "ORG-PROC"}, 404)

    def test_only_the_receiver_accepts_or_rejects(self) -> None:
        batch = self.harvest()
        t = self.transfer(batch, "BK", "ORG-PROC", "PROC", accept=False)
        self.call("BK", "post", f"/api/custody/transfers/{t}/accept", expect=403)     # sender
        self.call("PROC2", "post", f"/api/custody/transfers/{t}/accept", expect=403)  # other org
        self.call("DIST", "post", f"/api/custody/transfers/{t}/accept", expect=403)   # distributor, not party
        self.call("PROC", "post", f"/api/custody/transfers/{t}/cancel", expect=403)   # receiver can't cancel
        self.call("PROC", "post", f"/api/custody/transfers/{t}/reject", expect=200)
        self.assertEqual(self.state(batch)["holder_org_id"], db.get_organization_by_org_id("ORG-BK")["id"])
        self.call("PROC", "post", f"/api/custody/transfers/{t}/accept", expect=409)   # replay after reject

    def test_accept_moves_custody_once(self) -> None:
        batch = self.harvest()
        t = self.transfer(batch, "BK", "ORG-PROC", "PROC")
        self.call("PROC", "post", f"/api/custody/transfers/{t}/accept", expect=409)   # replay
        self.assertEqual(self.state(batch)["holder_org_id"], db.get_organization_by_org_id("ORG-PROC")["id"])
        self.call("BK", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-DIST"}, 403)
        self.assertIn("CUSTODY_TRANSFERRED", {e["event_type"] for e in chain_events.list_events()})

    def test_sender_cancel(self) -> None:
        batch = self.harvest()
        t = self.transfer(batch, "BK", "ORG-PROC", "PROC", accept=False)
        self.call("BK", "post", f"/api/custody/transfers/{t}/cancel", expect=200)
        self.call("BK", "post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-PROC"}, 201)

    def test_one_pending_transfer_is_enforced_by_the_database(self) -> None:
        batch = self.harvest()
        self.transfer(batch, "BK", "ORG-PROC", "PROC", accept=False)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO custody_transfers (transfer_id, batch_id, status) "
                             "VALUES ('CUS-RAW', ?, 'PENDING');", (batch,))


# ---------------------------------------------------------------------------
class TestShipmentsAndRetail(SupplyCase):
    def test_lifecycle_rules(self) -> None:
        batch, _ = self.packaged()
        t, sid, rid = self.to_retailer(batch)
        self.call("DIST", "post", "/api/shipments", {"custody_transfer_id": t}, 409)          # duplicate
        self.call("RET", "post", f"/api/shipments/{sid}/status", {"status": "IN_TRANSIT"}, 403)
        self.call("DIST", "post", f"/api/shipments/{sid}/status", {"status": "DELIVERED"}, 409)
        self.call("DIST", "post", f"/api/shipments/{sid}/status",
                  {"status": "IN_TRANSIT", "min_temperature_c": 30, "max_temperature_c": 20}, 400)
        self.ship(sid, "IN_TRANSIT", "EXCEPTION", "IN_TRANSIT")
        self.call("RET", "post", f"/api/custody/transfers/{t}/accept", expect=409)          # not delivered
        self.call("RET", "post", f"/api/retail/{rid}/receive", expect=409)                  # not delivered
        self.ship(sid, "DELIVERED")
        self.call("DIST", "post", f"/api/shipments/{sid}/status", {"status": "IN_TRANSIT"}, 409)
        shipped = [e for e in chain_events.list_events() if e["event_type"] == "SHIPPED"]
        self.assertEqual(len(shipped), 1)                                                    # not re-emitted
        self.call("RET2", "post", f"/api/retail/{rid}/receive", expect=403)
        self.call("RET", "post", f"/api/retail/{rid}/shelve", expect=409)                   # before receive
        self.call("RET", "post", f"/api/retail/{rid}/receive", expect=200)
        self.call("RET", "post", f"/api/retail/{rid}/receive", expect=409)                  # replay
        self.call("RET", "post", f"/api/retail/{rid}/shelve", expect=200)

    def test_unrelated_retailer_sees_nothing(self) -> None:
        batch, body = self.packaged()
        _, sid, _ = self.to_retailer(batch)
        self.call("RET2", "get", f"/api/shipments/{sid}", expect=403)
        self.assertEqual(self.call("RET2", "get", "/api/shipments", expect=200).get_json(), [])
        self.assertEqual(self.call("RET2", "get", "/api/retail", expect=200).get_json(), [])
        self.call("RET2", "get", f"/api/batches/{batch}/trace", expect=403)
        self.call("RET2", "get", f"/api/units/{body['units'][0]['unit_id']}/trace", expect=403)
        self.call("RET", "get", f"/api/shipments/{sid}", expect=200)

    def test_selling(self) -> None:
        batch, body = self.packaged(count=2)
        _, sid, rid = self.to_retailer(batch)
        unit = body["units"][0]["unit_id"]
        self.ship(sid, "IN_TRANSIT", "DELIVERED")
        self.call("RET", "post", f"/api/retail/{rid}/receive", expect=200)
        self.call("RET", "post", f"/api/retail/units/{unit}/sell", expect=409)   # not on shelf yet
        self.call("RET", "post", f"/api/retail/{rid}/shelve", expect=200)
        self.call("RET2", "post", f"/api/retail/units/{unit}/sell", expect=403)
        self.call("RET", "post", "/api/retail/units/UNIT-AAAAAAAAAAAAAAAA/sell", expect=404)
        self.call("RET", "post", f"/api/retail/units/{unit}/sell", expect=200)
        self.call("RET", "post", f"/api/retail/units/{unit}/sell", expect=409)   # double sale
        self.call("RET", "post", f"/api/retail/units/{body['units'][1]['unit_id']}/sell", expect=200)
        self.assertEqual(self.call("RET", "get", "/api/retail", expect=200).get_json()[0]["status"], "SOLD")


# ---------------------------------------------------------------------------
class TestRecall(SupplyCase):
    def test_recall_propagates_to_blends_units_and_retail(self) -> None:
        a, b = self.at_processor("HIVE-A", 6), self.at_processor("HIVE-B", 6)
        out = self.call("PROC", "post", "/api/blends", {"inputs": [{"batch_id": a, "weight_kg": 3},
                                                                   {"batch_id": b, "weight_kg": 3}]}, 201).get_json()["batch_id"]
        self.lab_pass(out)
        units = self.call("PROC", "post", f"/api/batches/{out}/packaging",
                          {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE}, 201).get_json()["units"]
        self.call("BK", "post", "/api/recalls", {"batch_id": a, "reason": "Pesticide residue"}, 403)
        r = self.call("AUTH", "post", "/api/recalls", {"batch_id": a, "reason": "Pesticide residue found"}, 201).get_json()
        self.assertEqual(set(r["affected_batches"]), {a, out})
        self.assertEqual(r["recalled_units"], 2)
        self.assertEqual(self.state(out)["status"], "RECALLED")
        self.assertEqual({u["status"] for u in self.call("PROC", "get", f"/api/batches/{out}/units",
                                                         expect=200).get_json()}, {"RECALLED"})
        self.assertEqual(self.state(b)["status"], "ACTIVE")         # the other source is not recalled
        self.call("PROC", "post", f"/api/batches/{out}/processing", {"process_type": "FILTERING"}, 409)
        self.call("AUTH", "post", "/api/recalls", {"batch_id": a, "reason": "Duplicate recall"}, 409)
        trace = self.call("PROC", "get", f"/api/batches/{out}/trace", expect=200).get_json()
        self.assertEqual(trace["recall"]["root_batch_id"], a)
        self.assertIn("RECALLED", {e["event_type"] for e in chain_events.list_events()})
        self.assertIn(units[0]["unit_id"], [u["unit_id"] for u in self.call(
            "PROC", "get", f"/api/batches/{out}/units", expect=200).get_json()])


# ---------------------------------------------------------------------------
class TestInputsAndIsolation(SupplyCase):
    def test_malformed_ids_are_rejected(self) -> None:
        for path in ("/api/batches/'%3B--/trace", "/api/units/UNIT-1/trace",
                     "/api/units/not-a-unit/trace", "/api/shipments/%3Cscript%3E"):
            self.assertIn(self.call("ADMIN" if False else "PROC", "get", path).status_code, (400, 404), path)

    def test_unauthenticated_requests_are_refused(self) -> None:
        for method, path in (("post", "/api/blends"), ("post", "/api/custody/transfers"),
                             ("post", "/api/shipments"), ("get", "/api/retail"), ("post", "/api/recalls"),
                             ("get", "/api/custody/transfers")):
            self.assertEqual(getattr(self.client, method)(path, json={}).status_code, 401, path)

    def test_lab_sees_only_its_own_queue(self) -> None:
        batch = self.at_processor()
        self.call("PROC", "post", f"/api/batches/{batch}/lab-requests", {"lab_organization_id": "ORG-LAB"}, 201)
        self.assertEqual(len(self.call("LAB", "get", "/api/lab/requests", expect=200).get_json()), 1)
        self.assertEqual(self.call("LAB2", "get", "/api/lab/requests", expect=200).get_json(), [])
        self.call("LAB", "get", f"/api/batches/{batch}/trace", expect=200)   # requested lab may view
        self.call("LAB2", "get", f"/api/batches/{batch}/trace", expect=403)


if __name__ == "__main__":
    unittest.main()
