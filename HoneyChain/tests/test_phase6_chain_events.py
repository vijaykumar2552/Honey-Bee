"""
Phase 6 -- canonical chain-event service, Merkle checkpoints, anchoring.

Blockchain-client failure modes (RPC down, revert, timeout, already
anchored, root mismatch) are driven through ``FakeAnchor`` below: an
in-process TEST DOUBLE with the same two functions as
``honeychain.chain_anchor``. It exists only in this test module and proves
the service's state handling. The REAL client against a REAL chain is
exercised separately in tests/test_phase6_live_chain.py.
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

from tests.base import HoneyChainTestCase

from honeychain import chain_anchor, chain_events, config, db, merkle


def _h(i: int) -> str:
    import hashlib
    return hashlib.sha256(f"event-{i}".encode()).hexdigest()


# ---------------------------------------------------------------------------
class TestMerkle(unittest.TestCase):
    def test_every_proof_verifies_for_tree_sizes_1_to_17(self) -> None:
        for n in range(1, 18):
            leaves = [_h(i) for i in range(n)]
            root = merkle.merkle_root(leaves)
            self.assertEqual(root, merkle.merkle_root(list(leaves)))  # deterministic
            for i in range(n):
                self.assertTrue(merkle.verify_proof(leaves[i], merkle.merkle_proof(leaves, i), root),
                                (n, i))

    def test_tampering_breaks_verification(self) -> None:
        leaves = [_h(i) for i in range(7)]
        root, proof = merkle.merkle_root(leaves), merkle.merkle_proof(leaves, 3)
        self.assertFalse(merkle.verify_proof(_h(99), proof, root))           # wrong leaf
        self.assertFalse(merkle.verify_proof(leaves[3], proof, _h(98)))      # wrong root
        bad = [dict(s) for s in proof]; bad[0]["hash"] = _h(97)
        self.assertFalse(merkle.verify_proof(leaves[3], bad, root))          # wrong sibling
        flipped = [dict(s) for s in proof]; flipped[0]["side"] = "L" if proof[0]["side"] == "R" else "R"
        self.assertFalse(merkle.verify_proof(leaves[3], flipped, root))      # wrong order
        self.assertNotEqual(root, merkle.merkle_root(leaves[:3] + [leaves[4], leaves[3]] + leaves[5:]))

    def test_construction_matches_the_documented_spec_exactly(self) -> None:
        """Known-answer test, computed independently with hashlib from the
        spec in honeychain/merkle.py. Roots already anchored on-chain can
        only ever be re-verified if this construction never drifts."""
        import hashlib
        L = lambda h: hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest()  # noqa: E731
        N = lambda a, b: hashlib.sha256(b"\x01" + a + b).digest()          # noqa: E731
        a, b, c = _h(1), _h(2), _h(3)
        self.assertEqual(merkle.merkle_root([a]), L(a).hex())
        self.assertEqual(merkle.merkle_root([a, b]), N(L(a), L(b)).hex())
        self.assertEqual(merkle.merkle_root([a, b, c]), N(N(L(a), L(b)), L(c)).hex())  # c promoted

    def test_internal_node_value_is_not_provable_as_a_leaf(self) -> None:
        """The node over (a, b) is not accepted as a leaf of the same tree."""
        a, b = _h(1), _h(2)
        node = merkle.node_hash(merkle.leaf_hash(a), merkle.leaf_hash(b)).hex()
        self.assertFalse(merkle.verify_proof(node, [], merkle.merkle_root([a, b])))

    def test_odd_level_promotes_rather_than_duplicates(self) -> None:
        """[a,b,c] and [a,b,c,c] must NOT share a root (CVE-2012-2459 class)."""
        a, b, c = _h(1), _h(2), _h(3)
        self.assertNotEqual(merkle.merkle_root([a, b, c]), merkle.merkle_root([a, b, c, c]))

    def test_malformed_input_is_rejected_safely(self) -> None:
        with self.assertRaises(ValueError):
            merkle.merkle_root([])
        with self.assertRaises(ValueError):
            merkle.merkle_root(["not-hex"])
        self.assertFalse(merkle.verify_proof("zz", [], _h(1)))
        self.assertFalse(merkle.verify_proof(_h(1), [{"side": "X", "hash": _h(2)}], _h(3)))
        self.assertFalse(merkle.verify_proof(_h(1), [{"nope": 1}], _h(3)))


# ---------------------------------------------------------------------------
class FakeAnchor:
    """TEST DOUBLE for honeychain.chain_anchor (same function names/semantics)."""

    def __init__(self) -> None:
        self.onchain: dict[str, dict] = {}
        self.fail_with: Exception | None = None
        self.calls = 0

    def read_checkpoint(self, cid):
        if isinstance(self.fail_with, chain_anchor.AnchorUnavailable):
            raise self.fail_with
        return self.onchain.get(cid)

    def anchor_checkpoint(self, cid, from_seq, to_seq, root):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if cid in self.onchain:
            raise chain_anchor.AnchorRejected("already exists")
        block = 100 + len(self.onchain)
        self.onchain[cid] = {"from_seq": from_seq, "to_seq": to_seq, "merkle_root": root,
                             "timestamp": 1, "block_number": block}
        return {"tx_hash": "0x" + _h(block), "block_number": block, "chain_id": 31337}


class ChainTestCase(HoneyChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.org = self.create_org(org_id="ORG-A", name="Org A")
        self.bk = self.create_user(user_id="USR-BK-A", email="bka@hc.test", role="BEEKEEPER",
                                   organization_pk=self.org)
        self.h = self.auth_headers_for(self.bk)
        self.seed_hive("HIVE-A", count=3)
        self.own_hive("HIVE-A", "ORG-A")
        self._chain_id = config.CHAIN_ID
        config.CHAIN_ID = 31337

    def tearDown(self) -> None:
        config.CHAIN_ID = self._chain_id
        super().tearDown()

    def harvest(self, **kw):
        payload = {"hive_id": "HIVE-A", "harvest_date": date.today().isoformat(),
                   "total_weight": 10, "floral_type": "Mustard", "latitude": 13.1,
                   "longitude": 78.1, "notes": "private field note"}
        payload.update(kw)
        r = self.client.post("/api/harvest", json=payload, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def events(self):
        return [dict(r) for r in db.query_all("SELECT * FROM chain_events ORDER BY seq;")]


# ---------------------------------------------------------------------------
class TestEventRecording(ChainTestCase):
    def test_harvest_records_batch_created_atomically_with_ids_and_hashes_only(self) -> None:
        body = self.harvest()
        ev = body["chain_event"]
        self.assertEqual(ev["event_type"], "BATCH_CREATED")
        self.assertEqual(ev["anchoring"], "PENDING_CHECKPOINT")
        self.assertEqual((ev["seq"], ev["previous_hash"]), (1, chain_events.GENESIS_PREVIOUS_HASH))
        self.assertEqual((ev["organization_id"], ev["actor_id"]), ("ORG-A", "USR-BK-A"))
        p = ev["payload"]
        self.assertEqual(p["block_hash"], body["batch"]["block_hash"])
        self.assertEqual(p["harvest_event_hash"], body["harvest"]["event_hash"])
        raw = json.dumps(p)
        for private in ("private field note", "13.1", "78.1", "Mustard"):
            self.assertNotIn(private, raw)

    def test_sequence_and_linkage_across_events(self) -> None:
        for _ in range(3):
            self.harvest()
        ev = self.events()
        self.assertEqual([e["seq"] for e in ev], [1, 2, 3])
        self.assertEqual(ev[1]["previous_hash"], ev[0]["event_hash"])
        self.assertEqual(ev[2]["previous_hash"], ev[1]["event_hash"])

    def test_duplicate_event_is_idempotent(self) -> None:
        bid = self.harvest()["batch"]["batch_id"]
        first = self.events()[0]
        with db.transaction() as conn:
            again = chain_events.record_event(
                conn, event_type="BATCH_CREATED", resource_type="honey_batches", resource_id=bid,
                organization_id="ORG-A", actor_id="USR-X", payload={"batch_id": bid})
        self.assertEqual(again["event_hash"], first["event_hash"])
        self.assertEqual(len(self.events()), 1)

    def test_invalid_events_are_rejected(self) -> None:
        cases = [dict(event_type="NOT_A_TYPE", payload={"x": 1}),
                 dict(event_type="RECALLED", payload={"owner_email": "a@b.c"}),
                 dict(event_type="RECALLED", payload={"latitude": 1.0}),
                 dict(event_type="RECALLED", payload={"nested": {"a": 1}}),
                 dict(event_type="RECALLED", payload={})]
        for case in cases:
            with self.assertRaises(chain_events.ChainEventError, msg=str(case)):
                with db.transaction() as conn:
                    chain_events.record_event(conn, resource_type="honey_batches",
                                              resource_id="HNY-X", organization_id=None,
                                              actor_id=None, **case)
        self.assertEqual(self.events(), [])

    def test_event_failure_rolls_back_the_whole_harvest(self) -> None:
        with mock.patch.object(chain_events, "record_event",
                               side_effect=chain_events.ChainEventError("boom")):
            r = self.client.post("/api/harvest", headers=self.h, json={
                "hive_id": "HIVE-A", "total_weight": 5, "floral_type": "X"})
        self.assertGreaterEqual(r.status_code, 400)
        for table in ("honey_batches", "harvest_events", "batch_harvest_links", "chain_events"):
            self.assertEqual(db.query_one(f"SELECT COUNT(*) AS n FROM {table};")["n"], 0, table)


# ---------------------------------------------------------------------------
class TestLogVerification(ChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bodies = [self.harvest() for _ in range(3)]

    def test_untouched_log_is_valid(self) -> None:
        self.assertEqual(chain_events.verify_log(), {"valid": True, "event_count": 3, "problems": []})

    def _issue_after(self, sql: str, params=()) -> str:
        with db.transaction() as conn:
            conn.execute(sql, params)
        result = chain_events.verify_log()
        self.assertFalse(result["valid"])
        return result["problems"][0]["issue"]

    def test_payload_tamper(self) -> None:
        self.assertIn("payload", self._issue_after(
            "UPDATE chain_events SET payload = replace(payload, 'HIVE-A', 'HIVE-Z') WHERE seq = 2;"))

    def test_event_field_tamper(self) -> None:
        self.assertIn("event_hash", self._issue_after(
            "UPDATE chain_events SET actor_id = 'USR-EVIL' WHERE seq = 2;"))

    def test_deleted_event_is_a_gap(self) -> None:
        self.assertIn("gap", self._issue_after("DELETE FROM chain_events WHERE seq = 2;"))

    def test_rewritten_batch_block_is_caught_by_domain_check(self) -> None:
        self.assertIn("block", self._issue_after(
            "UPDATE honey_batches SET total_weight = 999 WHERE batch_id = ?;",
            (self.bodies[0]["batch"]["batch_id"],)))

    def test_rewritten_harvest_is_caught_by_domain_check(self) -> None:
        self.assertIn("harvest", self._issue_after(
            "UPDATE harvest_events SET event_hash = ? WHERE harvest_id = ?;",
            ("f" * 64, self.bodies[1]["harvest"]["harvest_id"])))


# ---------------------------------------------------------------------------
class TestCheckpointsAndAnchoring(ChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        for _ in range(3):
            self.harvest()
        self.fake = FakeAnchor()

    def status(self, cid):
        return chain_events.get_checkpoint(cid)["status"]

    def test_checkpoint_root_covers_exactly_the_new_events(self) -> None:
        cp = chain_events.create_checkpoint()
        self.assertEqual((cp["checkpoint_id"], cp["from_seq"], cp["to_seq"], cp["status"]),
                         ("CHK-00000001-00000003", 1, 3, "PENDING"))
        self.assertEqual(cp["merkle_root"], merkle.merkle_root([e["event_hash"] for e in self.events()]))
        self.assertIsNone(chain_events.create_checkpoint())       # nothing new
        self.harvest()
        cp2 = chain_events.create_checkpoint()
        self.assertEqual((cp2["from_seq"], cp2["to_seq"]), (4, 4))

    def test_successful_anchor_is_recorded_with_real_fields(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        out = chain_events.submit_checkpoints(anchor=self.fake)
        self.assertEqual(out[0]["status"], "CONFIRMED")
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual((cp["status"], cp["chain_id"], cp["block_number"]), ("CONFIRMED", 31337, 100))
        self.assertTrue(cp["tx_hash"].startswith("0x"))
        anchored = db.query_all("SELECT anchor_tx_hash FROM honey_batches;")
        self.assertTrue(all(r["anchor_tx_hash"] == cp["tx_hash"] for r in anchored))
        self.assertIn("CHECKPOINT_ANCHORED", [e["action"] for e in db.list_audit_events(50)])

    def test_rpc_unavailable_is_failed_not_confirmed_then_retry_succeeds(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        self.fake.fail_with = chain_anchor.AnchorUnavailable("RPC endpoint unreachable")
        chain_events.submit_checkpoints(anchor=self.fake)
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual((cp["status"], cp["attempts"]), ("FAILED", 1))
        self.assertIn("unreachable", cp["last_error"])
        self.assertIsNone(cp["tx_hash"])
        self.assertTrue(all(r["anchor_tx_hash"] is None
                            for r in db.query_all("SELECT anchor_tx_hash FROM honey_batches;")))
        self.fake.fail_with = None
        chain_events.submit_checkpoints(anchor=self.fake)
        self.assertEqual(self.status(cid), "CONFIRMED")

    def test_reverted_transaction_is_never_reported_as_anchored(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        self.fake.fail_with = chain_anchor.AnchorFailed("transaction reverted on-chain", "0xdead")
        chain_events.submit_checkpoints(anchor=self.fake)
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual(cp["status"], "FAILED")
        self.assertEqual(cp["tx_hash"], "0xdead")          # kept for investigation...
        self.assertIsNone(cp["confirmed_at"])              # ...but never confirmed
        self.assertTrue(all(r["anchor_tx_hash"] is None
                            for r in db.query_all("SELECT anchor_tx_hash FROM honey_batches;")))

    def test_retry_budget_is_respected(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        self.fake.fail_with = chain_anchor.AnchorFailed("timeout")
        for _ in range(config.CHECKPOINT_MAX_ATTEMPTS + 3):
            chain_events.submit_checkpoints(anchor=self.fake)
        self.assertEqual(self.fake.calls, config.CHECKPOINT_MAX_ATTEMPTS)
        self.assertEqual(chain_events.get_checkpoint(cid)["attempts"], config.CHECKPOINT_MAX_ATTEMPTS)

    def test_already_anchored_same_root_confirms_without_a_new_transaction(self) -> None:
        cp = chain_events.create_checkpoint()
        self.fake.onchain[cp["checkpoint_id"]] = {"merkle_root": cp["merkle_root"], "block_number": 7}
        out = chain_events.submit_checkpoints(anchor=self.fake)
        self.assertTrue(out[0]["already_on_chain"])
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(chain_events.get_checkpoint(cp["checkpoint_id"])["block_number"], 7)

    def test_already_anchored_different_root_is_flagged_not_overwritten(self) -> None:
        cp = chain_events.create_checkpoint()
        self.fake.onchain[cp["checkpoint_id"]] = {"merkle_root": "0" * 63 + "1", "block_number": 7}
        chain_events.submit_checkpoints(anchor=self.fake)
        got = chain_events.get_checkpoint(cp["checkpoint_id"])
        self.assertEqual(got["status"], "FAILED")
        self.assertIn("DIFFERS", got["last_error"])
        self.assertEqual(self.fake.calls, 0)

    def test_checkpoints_anchor_in_order_and_stop_at_first_failure(self) -> None:
        first = chain_events.create_checkpoint()["checkpoint_id"]
        self.harvest()
        second = chain_events.create_checkpoint()["checkpoint_id"]
        self.fake.fail_with = chain_anchor.AnchorFailed("timeout")
        chain_events.submit_checkpoints(anchor=self.fake)
        self.assertEqual((self.status(first), self.status(second)), ("FAILED", "PENDING"))

    def test_log_edited_after_checkpoint_blocks_anchoring(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        with db.transaction() as conn:
            conn.execute("UPDATE chain_events SET event_hash = ? WHERE seq = 2;", ("a" * 64,))
        chain_events.submit_checkpoints(anchor=self.fake)
        self.assertEqual(self.status(cid), "FAILED")
        self.assertEqual(self.fake.calls, 0)

    def test_verify_checkpoint_states(self) -> None:
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        self.assertEqual(chain_events.verify_checkpoint(cid, anchor=self.fake)["onchain"], "NOT_ANCHORED")
        chain_events.submit_checkpoints(anchor=self.fake)
        v = chain_events.verify_checkpoint(cid, anchor=self.fake)
        self.assertEqual((v["onchain"], v["local_root_valid"]), ("VERIFIED", True))
        self.fake.onchain[cid]["merkle_root"] = "0" * 63 + "1"
        self.assertEqual(chain_events.verify_checkpoint(cid, anchor=self.fake)["onchain"], "MISMATCH")

    def test_event_proofs_verify_against_the_checkpoint_root(self) -> None:
        chain_events.create_checkpoint()
        for e in self.events():
            proof = chain_events.event_proof(e["event_id"])
            self.assertTrue(proof["proof_valid"])
            self.assertTrue(merkle.verify_proof(e["event_hash"], proof["proof"], proof["merkle_root"]))

    def test_real_client_with_anchoring_disabled_reports_failure_honestly(self) -> None:
        """Production default: HONEYCHAIN_CHAIN_ANCHOR off -> FAILED with the
        reason, never a fabricated CONFIRMED."""
        chain_anchor.reset()
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual(cp["status"], "FAILED")
        self.assertIn("disabled", cp["last_error"])
        self.assertIsNone(cp["tx_hash"])


# ---------------------------------------------------------------------------
class TestChainApi(ChainTestCase):
    def test_event_reads_are_organization_scoped(self) -> None:
        self.harvest()
        org_b = self.create_org(org_id="ORG-B", name="Org B")
        eve = self.auth_headers_for(self.create_user(user_id="USR-EVE", email="e@hc.test",
                                                     role="BEEKEEPER", organization_pk=org_b))
        self.assertEqual(len(self.client.get("/api/chain/events", headers=self.h).get_json()), 1)
        self.assertEqual(self.client.get("/api/chain/events", headers=eve).get_json(), [])
        eid = self.events()[0]["event_id"]
        self.assertEqual(self.client.get(f"/api/chain/events/{eid}", headers=self.h).status_code, 200)
        self.assertEqual(self.client.get(f"/api/chain/events/{eid}", headers=eve).status_code, 403)
        self.assertEqual(self.client.get("/api/chain/events/EVT-NOPE", headers=self.h).status_code, 404)
        self.assertEqual(self.client.get("/api/chain/events", ).status_code, 401)

    def test_admin_operations_are_restricted(self) -> None:
        self.harvest()
        for method, path in (("get", "/api/chain/log/verify"), ("get", "/api/chain/checkpoints"),
                             ("post", "/api/chain/checkpoints"), ("post", "/api/chain/checkpoints/submit")):
            self.assertEqual(getattr(self.client, method)(path, headers=self.h).status_code, 403, path)
        admin = self.admin_headers()
        r = self.client.post("/api/chain/checkpoints", headers=admin)
        self.assertEqual(r.status_code, 201)
        self.assertTrue(self.client.get("/api/chain/log/verify", headers=admin).get_json()["valid"])
        self.assertIn("CHECKPOINT_CREATED", [e["action"] for e in db.list_audit_events(50)])

    def test_public_checkpoint_verification_needs_no_auth_and_leaks_nothing(self) -> None:
        self.harvest()
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        r = self.client.get(f"/api/chain/checkpoints/{cid}/verify")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        for private in ("USR-BK-A", "private field note", "13.1"):
            self.assertNotIn(private, text)
        for bad in ("CHK-1", "CHK-00000009-00000009", "../etc/passwd"):
            self.assertEqual(self.client.get(f"/api/chain/checkpoints/{bad}/verify").status_code, 404)

    def test_health_reports_chain_configuration_without_secrets(self) -> None:
        body = self.client.get("/api/health").get_data(as_text=True)
        self.assertIn('"chain_id"', body)
        self.assertNotIn("PRIVATE_KEY", body)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
class TestClientReceiptHandling(unittest.TestCase):
    """The pre-Phase-6 client returned a result for a REVERTED transaction
    (receipt status 0) and the caller stored it as an anchor. These drive
    the real chain_anchor.anchor_checkpoint with a stubbed web3 connection."""

    def setUp(self) -> None:
        import os
        self._state = dict(chain_anchor._state)
        self._key = os.environ.get("PRIVATE_KEY")
        os.environ["PRIVATE_KEY"] = "0x" + "11" * 32  # stub only; never reaches a network
        self.w3, self.contract = mock.MagicMock(), mock.MagicMock()
        self.w3.eth.send_raw_transaction.return_value = bytes.fromhex("ab" * 32)
        chain_anchor._state.update(initialised=True, w3=self.w3, contract=self.contract, error=None)

    def tearDown(self) -> None:
        import os
        chain_anchor._state.clear(); chain_anchor._state.update(self._state)
        if self._key is None:
            os.environ.pop("PRIVATE_KEY", None)
        else:
            os.environ["PRIVATE_KEY"] = self._key

    def _anchor(self):
        return chain_anchor.anchor_checkpoint("CHK-00000001-00000001", 1, 1, "cd" * 32)

    def test_status_zero_receipt_raises_and_keeps_tx_hash_for_investigation(self) -> None:
        self.w3.eth.wait_for_transaction_receipt.return_value = mock.Mock(status=0, blockNumber=5)
        with self.assertRaises(chain_anchor.AnchorFailed) as ctx:
            self._anchor()
        self.assertEqual(ctx.exception.tx_hash, "ab" * 32)

    def test_receipt_timeout_raises(self) -> None:
        self.w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError("no receipt")
        with self.assertRaises(chain_anchor.AnchorFailed):
            self._anchor()

    def test_dry_run_revert_is_rejected_before_sending(self) -> None:
        self.contract.functions.anchorCheckpoint.return_value.call.side_effect = RuntimeError("revert")
        with self.assertRaises(chain_anchor.AnchorRejected):
            self._anchor()
        self.w3.eth.send_raw_transaction.assert_not_called()

    def test_status_one_receipt_succeeds(self) -> None:
        self.w3.eth.wait_for_transaction_receipt.return_value = mock.Mock(status=1, blockNumber=9)
        out = self._anchor()
        self.assertEqual((out["tx_hash"], out["block_number"]), ("0x" + "ab" * 32, 9))

    def test_missing_private_key_is_unavailable(self) -> None:
        import os
        os.environ.pop("PRIVATE_KEY", None)
        with self.assertRaises(chain_anchor.AnchorUnavailable):
            self._anchor()


class TestConsistentForgery(ChainTestCase):
    def test_attacker_who_recomputes_hashes_still_breaks_the_link(self) -> None:
        """Edit event 2 AND recompute its payload_hash/event_hash so it is
        internally consistent: event 3's previous_hash no longer links."""
        for _ in range(3):
            self.harvest()
        row = self.events()[1]
        payload = json.loads(row["payload"]); payload["hive_id"] = "HIVE-FORGED"
        row["payload"] = json.dumps(payload); row["payload_hash"] = chain_events.payload_hash(payload)
        row["event_hash"] = chain_events.compute_event_hash(row)
        with db.transaction() as conn:
            conn.execute("UPDATE chain_events SET payload = ?, payload_hash = ?, event_hash = ? "
                         "WHERE seq = 2;", (row["payload"], row["payload_hash"], row["event_hash"]))
        problems = chain_events.verify_log()["problems"]
        self.assertTrue(any(p["seq"] == 3 and "previous_hash" in p["issue"] for p in problems), problems)
