"""
Phase 8 review fixes -- regression tests for two defects found while
reviewing the supply-chain implementation (not covered by the original
Phase 8 tests):

1. A holder could keep processing / blending / packaging (or re-offer) a
   batch AFTER offering it in a custody transfer. The receiver would then
   accept a batch whose contents no longer matched what was offered.
   Now: 409 while a transfer is PENDING; sampling for a lab test is still
   allowed; after cancellation the holder can operate again.

2. A laboratory could date a result BEFORE the sample was requested
   (backdating). Now: 400.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from tests.test_phase8_supply_chain import BEST_BEFORE, TODAY, SupplyCase

from honeychain import db


class TestNoOperationsDuringPendingHandOff(SupplyCase):
    def setUp(self) -> None:
        super().setUp()
        self.batch = self.at_processor(kg=20.0)
        self.other = self.at_processor(hive="HIVE-B", kg=10.0)
        self.transfer_id = self.transfer(self.batch, "PROC", "ORG-DIST", "DIST", accept=False)

    def remaining(self) -> float:
        return db.query_one("SELECT remaining_kg FROM batch_state WHERE batch_id = ?;",
                            (self.batch,))["remaining_kg"]

    def test_consuming_operations_are_refused_and_change_nothing(self) -> None:
        before = self.remaining()
        self.call("PROC", "post", f"/api/batches/{self.batch}/processing",
                  {"process_type": "FILTERING", "output_kg": 19.0}, 409)
        self.call("PROC", "post", f"/api/batches/{self.batch}/packaging",
                  {"unit_size_g": 500, "unit_count": 2, "best_before": BEST_BEFORE}, 409)
        self.call("PROC", "post", "/api/blends",
                  {"inputs": [{"batch_id": self.batch, "weight_kg": 5},
                              {"batch_id": self.other, "weight_kg": 5}]}, 409)
        self.call("PROC", "post", "/api/custody/transfers",
                  {"batch_id": self.batch, "to_organization_id": "ORG-RET"}, 409)
        self.assertEqual(self.remaining(), before)
        for table in ("processing_events", "packaging_events", "product_units"):
            self.assertEqual(db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE "
                                          + ("input_batch_id" if table == "processing_events" else "batch_id")
                                          + " = ?;", (self.batch,))["n"], 0, table)

    def test_receiver_gets_exactly_what_was_offered(self) -> None:
        offered = db.query_one("SELECT quantity_kg FROM custody_transfers WHERE transfer_id = ?;",
                               (self.transfer_id,))["quantity_kg"]
        self.call("DIST", "post", f"/api/custody/transfers/{self.transfer_id}/accept", expect=200)
        self.assertEqual(self.remaining(), offered)

    def test_lab_sampling_is_still_allowed_during_hand_off(self) -> None:
        self.call("PROC", "post", f"/api/batches/{self.batch}/lab-requests",
                  {"lab_organization_id": "ORG-LAB"}, 201)

    def test_after_cancellation_the_holder_may_operate_again(self) -> None:
        self.call("PROC", "post", f"/api/custody/transfers/{self.transfer_id}/cancel", expect=200)
        self.call("PROC", "post", f"/api/batches/{self.batch}/processing",
                  {"process_type": "FILTERING", "output_kg": 19.0}, 201)


class TestLabResultCannotBeBackdated(SupplyCase):
    def test_result_dated_before_the_request_is_rejected(self) -> None:
        batch = self.at_processor()
        rid = self.call("PROC", "post", f"/api/batches/{batch}/lab-requests",
                        {"lab_organization_id": "ORG-LAB"}, 201).get_json()["lab_result_id"]
        yesterday = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
        r = self.call("LAB", "post", f"/api/lab/results/{rid}",
                      {"status": "PASSED", "test_date": yesterday, "moisture_pct": 17.0}, 400)
        self.assertIn("before the sample was requested", r.get_data(as_text=True))
        self.assertEqual(db.query_one("SELECT status FROM lab_results WHERE lab_result_id = ?;",
                                      (rid,))["status"], "PENDING")
        self.call("LAB", "post", f"/api/lab/results/{rid}",
                  {"status": "PASSED", "test_date": TODAY, "moisture_pct": 17.0}, 201)


if __name__ == "__main__":
    unittest.main()
