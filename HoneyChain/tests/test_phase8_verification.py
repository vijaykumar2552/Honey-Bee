"""
Phase 8 verification suite (independent review of the supply-chain code).

Written after the review found that ``supply_chain.can_view_batch`` returned
True for every user -- a cross-organization read bypass that the original
per-feature tests only partly covered. This module adds SYSTEMATIC checks:

* every read endpoint, probed by unrelated organizations (IDOR sweep);
* every write endpoint, attempted by the wrong organization;
* every state transition replayed (idempotency / invalid transitions);
* every Phase 8 audit row names its authenticated actor AND organization;
* blockchain failure never makes the database claim an anchor exists, and
  a failed chain-event write rolls the supply-chain action back.
"""

from __future__ import annotations

from unittest import mock

from tests.test_phase6_chain_events import FakeAnchor
from tests.test_phase8_supply_chain import BEST_BEFORE, TODAY, SupplyCase

from honeychain import chain_anchor, chain_events, db


class FullChainCase(SupplyCase):
    """A complete chain for ORG-BK -> PROC -> LAB -> DIST -> RET, left at ON_SHELF."""

    def setUp(self) -> None:
        super().setUp()
        self.batch = self.at_processor()
        self.lab_id = self.lab_pass(self.batch)
        pkg = self.call("PROC", "post", f"/api/batches/{self.batch}/packaging",
                        {"unit_size_g": 500, "unit_count": 3, "best_before": BEST_BEFORE}, 201).get_json()
        self.pkg, self.units = pkg, [u["unit_id"] for u in pkg["units"]]
        self.secrets = [u.get("unit_secret") or u.get("secret") for u in pkg["units"]]
        self.transfer_id, self.sid, self.rid = self.to_retailer(self.batch)
        self.ship(self.sid, "IN_TRANSIT", "DELIVERED")
        self.call("RET", "post", f"/api/retail/{self.rid}/receive", expect=200)
        self.call("RET", "post", f"/api/retail/{self.rid}/shelve", expect=200)


# ---------------------------------------------------------------------------
class TestIdorSweep(FullChainCase):
    OUTSIDERS = ("PROC2", "LAB2", "RET2")

    def test_every_batch_scoped_read_is_forbidden_to_outsiders(self) -> None:
        paths = [f"/api/batches/{self.batch}/trace", f"/api/batches/{self.batch}/lab-results",
                 f"/api/batches/{self.batch}/units", f"/api/batches/{self.batch}/provenance",
                 f"/api/shipments/{self.sid}", f"/api/units/{self.units[0]}/trace"]
        for who in self.OUTSIDERS:
            for path in paths:
                self.assertEqual(self.call(who, "get", path).status_code, 403, f"{who} {path}")

    def test_every_listing_hides_other_organizations_records(self) -> None:
        for who in self.OUTSIDERS:
            for path, needle in (("/api/shipments", self.sid), ("/api/custody/transfers", self.transfer_id),
                                 ("/api/retail", self.rid), ("/api/batches", self.batch),
                                 ("/api/chain/events", self.batch)):
                body = self.call(who, "get", path, expect=200).get_data(as_text=True)
                self.assertNotIn(needle, body, f"{who} {path}")
        self.assertNotIn(self.lab_id, self.call("LAB2", "get", "/api/lab/requests", expect=200)
                         .get_data(as_text=True))

    def test_participants_can_read(self) -> None:
        for who in ("BK", "PROC", "DIST", "RET", "LAB", "AUTH"):
            self.assertEqual(self.call(who, "get", f"/api/batches/{self.batch}/trace").status_code, 200, who)

    def test_every_write_is_refused_to_the_wrong_organization(self) -> None:
        attempts = [
            ("PROC2", "post", f"/api/batches/{self.batch}/processing",
             {"process_type": "FILTERING", "input_kg": 1, "output_kg": 1}),
            ("PROC2", "post", f"/api/batches/{self.batch}/packaging",
             {"unit_size_g": 500, "unit_count": 1, "best_before": BEST_BEFORE}),
            ("PROC2", "post", f"/api/batches/{self.batch}/lab-requests", {"lab_organization_id": "ORG-LAB2"}),
            ("PROC2", "post", "/api/custody/transfers", {"batch_id": self.batch, "to_organization_id": "ORG-PROC2"}),
            ("RET2", "post", f"/api/retail/units/{self.units[0]}/sell", None),
            ("RET2", "post", f"/api/retail/{self.rid}/shelve", None),
        ]
        for who, method, path, body in attempts:
            self.assertIn(self.call(who, method, path, body).status_code, (403, 404), f"{who} {path}")
        self.assertEqual(self.state(self.batch)["holder_org_id"],
                         db.get_organization_by_org_id("ORG-RET")["id"])

    def test_unit_secrets_never_leave_the_packaging_response(self) -> None:
        secrets = [s for s in self.secrets if s]
        self.assertTrue(secrets, "packaging must return per-unit secrets once")
        for path in (f"/api/batches/{self.batch}/units", f"/api/units/{self.units[0]}/trace",
                     f"/api/batches/{self.batch}/trace"):
            body = self.call("PROC", "get", path, expect=200).get_data(as_text=True)
            for secret in secrets:
                self.assertNotIn(secret, body, path)
        stored = [r["secret_hash"] for r in db.query_all("SELECT secret_hash FROM product_units;")]
        self.assertTrue(all(h and h not in secrets for h in stored))


# ---------------------------------------------------------------------------
class TestReplayedTransitions(FullChainCase):
    def test_terminal_and_repeated_transitions_are_rejected_without_side_effects(self) -> None:
        events_before = len(chain_events.list_events())
        replays = [
            ("RET", "post", f"/api/custody/transfers/{self.transfer_id}/accept", None),
            ("RET", "post", f"/api/custody/transfers/{self.transfer_id}/reject", None),
            ("DIST", "post", f"/api/shipments/{self.sid}/status", {"status": "DELIVERED"}),
            ("DIST", "post", f"/api/shipments/{self.sid}/status", {"status": "IN_TRANSIT"}),
            # DELIVERED is terminal: it cannot be re-opened via an EXCEPTION either
            # (added after a mutation test showed this transition was unguarded by tests).
            ("DIST", "post", f"/api/shipments/{self.sid}/status",
             {"status": "EXCEPTION", "exception_reason": "late complaint"}),
            ("DIST", "post", f"/api/shipments/{self.sid}/status", {"status": "CREATED"}),
            ("RET", "post", f"/api/retail/{self.rid}/receive", None),
            ("RET", "post", f"/api/retail/{self.rid}/shelve", None),
            ("LAB", "post", f"/api/lab/results/{self.lab_id}",
             {"status": "FAILED", "test_date": TODAY, "moisture_pct": 30.0}),
        ]
        for who, method, path, body in replays:
            self.assertEqual(self.call(who, method, path, body).status_code, 409, path)
        self.call("RET", "post", f"/api/retail/units/{self.units[0]}/sell", expect=200)
        self.call("RET", "post", f"/api/retail/units/{self.units[0]}/sell", expect=409)
        self.assertEqual(len(chain_events.list_events()), events_before)   # no duplicate events
        lab = self.call("PROC", "get", f"/api/batches/{self.batch}/lab-results", expect=200).get_json()
        self.assertEqual(lab["status"], "PASSED")                        # lab result not overwritten

    def test_recall_is_not_repeatable_and_blocks_sale(self) -> None:
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Residue found"}, 201)
        self.assertEqual(self.call("AUTH", "post", "/api/recalls",
                                   {"batch_id": self.batch, "reason": "again"}).status_code, 409)
        self.assertEqual(self.call("RET", "post", f"/api/retail/units/{self.units[1]}/sell").status_code, 409)


# ---------------------------------------------------------------------------
class TestAuditIdentity(FullChainCase):
    def test_every_phase8_audit_row_names_actor_and_matching_organization(self) -> None:
        self.call("RET", "post", f"/api/retail/units/{self.units[0]}/sell", expect=200)
        b2 = self.at_processor(hive="HIVE-B", kg=10.0)
        t = self.call("PROC", "post", "/api/custody/transfers",
                      {"batch_id": b2, "to_organization_id": "ORG-DIST"}, 201).get_json()["transfer_id"]
        self.call("DIST", "post", f"/api/custody/transfers/{t}/reject", {"reason": "No capacity"}, 200)
        self.call("AUTH", "post", "/api/recalls", {"batch_id": self.batch, "reason": "Audit test"}, 201)

        system_actions = {"CHECKPOINT_ANCHORED", "CHECKPOINT_ANCHOR_FAILED", "DEVICE_SILENT"}
        rows = [dict(r) for r in db.query_all(
            "SELECT a.action, a.actor_user_id, a.organization_id, u.organization_id AS actor_org, "
            "       u.user_id AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id;")]
        phase8 = [r for r in rows if r["action"] not in system_actions
                  and r["action"] not in ("LOGIN_SUCCESS", "USER_CREATED")]
        self.assertGreater(len(phase8), 15)
        for r in phase8:
            self.assertIsNotNone(r["actor_user_id"], r)
            self.assertIsNotNone(r["organization_id"], r)
            if r["actor_org"] is not None:
                self.assertEqual(r["organization_id"], r["actor_org"], r)
        who = {r["action"]: r["actor"] for r in rows}
        self.assertEqual(who["LAB_RESULT_RECORDED"], "USR-LAB")
        self.assertEqual(who["UNIT_SOLD"], "USR-RET")
        self.assertEqual(who["SHIPMENT_CREATED"], "USR-DIST")
        self.assertIn("USR-DIST", {r["actor"] for r in rows if "REJECT" in r["action"]})
        self.assertIn("USR-AUTH", {r["actor"] for r in rows if "RECALL" in r["action"]})


# ---------------------------------------------------------------------------
class TestBlockchainHonesty(FullChainCase):
    def test_failed_anchoring_never_marks_anything_as_anchored(self) -> None:
        cp = chain_events.create_checkpoint()
        fake = FakeAnchor()
        fake.fail_with = chain_anchor.AnchorFailed("transaction reverted on-chain", "0xbad")
        chain_events.submit_checkpoints(anchor=fake)
        self.assertEqual(chain_events.get_checkpoint(cp["checkpoint_id"])["status"], "FAILED")
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM honey_batches "
                                      "WHERE anchor_tx_hash IS NOT NULL;")["n"], 0)
        v = self.client.get(f"/api/chain/checkpoints/{cp['checkpoint_id']}/verify").get_json()
        self.assertEqual(v["onchain"], "NOT_ANCHORED")
        # Supply-chain state is unaffected by the anchoring failure.
        self.assertEqual(self.call("RET", "get", f"/api/batches/{self.batch}/trace", expect=200)
                         .get_json()["lab"]["status"], "PASSED")
        for body in (self.pkg,):
            self.assertNotIn("CONFIRMED", str(body.get("chain_event", "")))

    def test_chain_event_failure_rolls_back_every_kind_of_action(self) -> None:
        batch = self.at_processor(hive="HIVE-B", kg=12.0)
        boom = chain_events.ChainEventError("simulated event-log failure")
        with mock.patch.object(chain_events, "record_event", side_effect=boom):
            self.assertGreaterEqual(self.call("PROC", "post", f"/api/batches/{batch}/packaging",
                                              {"unit_size_g": 500, "unit_count": 2,
                                               "best_before": BEST_BEFORE}).status_code, 400)
            t = self.call("PROC", "post", "/api/custody/transfers",
                          {"batch_id": batch, "to_organization_id": "ORG-DIST"}, 201).get_json()["transfer_id"]
            self.assertGreaterEqual(self.call("DIST", "post", f"/api/custody/transfers/{t}/accept")
                                    .status_code, 400)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM product_units WHERE batch_id = ?;",
                                      (batch,))["n"], 0)
        self.assertEqual(self.state(batch)["holder_org_id"], db.get_organization_by_org_id("ORG-PROC")["id"])
        self.assertEqual(db.query_one("SELECT status FROM custody_transfers WHERE transfer_id = ?;",
                                      (t,))["status"], "PENDING")
        self.assertTrue(chain_events.verify_log()["valid"])
