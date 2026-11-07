"""
Phase 5 -- secure harvest workflow.

Exercised end-to-end through the real API and database: authorization
(role, organization, IDOR on write AND read), hive/organization validity,
input validation (quantity, unit, time, GPS, frames, notes, photo hash, ids),
duplicate/conflicting requests, atomic hive -> harvest -> batch provenance,
canonical event hashing, ledger integrity, tamper detection, audit logging,
and the pre-Phase-5 batch-listing cross-organization leak.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from unittest import mock

from tests.base import HoneyChainTestCase

from honeychain import db, harvests, ledger


class HarvestTestCase(HoneyChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.org_a = self.create_org(org_id="ORG-A", name="Org A")
        self.org_b = self.create_org(org_id="ORG-B", name="Org B")
        self.bk_a = self.create_user(user_id="USR-BK-A", email="bka@hc.test", role="BEEKEEPER",
                                     organization_pk=self.org_a)
        self.bk_b = self.create_user(user_id="USR-BK-B", email="bkb@hc.test", role="BEEKEEPER",
                                     organization_pk=self.org_b)
        self.h_a = self.auth_headers_for(self.bk_a)
        self.h_b = self.auth_headers_for(self.bk_b)
        self.seed_hive("HIVE-A", count=3)
        self.own_hive("HIVE-A", "ORG-A")
        self.seed_hive("HIVE-B", count=3)
        self.own_hive("HIVE-B", "ORG-B")

    def harvest(self, headers=None, **overrides):
        payload = {"hive_id": "HIVE-A", "harvest_date": date.today().isoformat(),
                   "total_weight": 12.5, "floral_type": "Mustard"}
        payload.update(overrides)
        return self.client.post("/api/harvest", json=payload,
                                headers=self.h_a if headers is None else headers)

    def counts(self) -> tuple[int, int, int]:
        q = lambda t: db.query_one(f"SELECT COUNT(*) AS n FROM {t};")["n"]  # noqa: E731
        return q("harvest_events"), q("honey_batches"), q("batch_harvest_links")

    def actions(self) -> list[str]:
        return [e["action"] for e in db.list_audit_events(200)]


# ---------------------------------------------------------------------------
class TestAuthorizedHarvest(HarvestTestCase):
    def test_creates_harvest_batch_link_and_ledger_block_atomically(self) -> None:
        r = self.harvest(latitude=17.385, longitude=78.4867, frame_count=8,
                         notes="Super 2", photo_sha256="ab" * 32)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        harvest, batch = body["harvest"], body["batch"]
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(harvest["hive_id"], "HIVE-A")
        self.assertEqual(harvest["organization_id"], "ORG-A")
        self.assertEqual(harvest["harvested_by"], "USR-BK-A")
        self.assertEqual(harvest["status"], "LINKED_TO_BATCH")
        self.assertEqual(harvest["batches"], [{"batch_id": batch["batch_id"], "weight_kg": 12.5}])
        self.assertEqual((harvest["latitude"], harvest["frame_count"]), (17.385, 8))
        self.assertEqual(len(harvest["event_hash"]), 64)
        self.assertEqual(batch["total_weight"], 12.5)
        self.assertEqual(batch["floral_type"], "Mustard")

    def test_response_exposes_no_internal_primary_keys(self) -> None:
        harvest = self.harvest().get_json()["harvest"]
        for leaked in ("id", "hive_pk", "beekeeper_user_id", "organization_pk"):
            self.assertNotIn(leaked, harvest)

    def test_actor_comes_from_token_not_payload(self) -> None:
        harvest = self.harvest(harvested_by="USR-BK-B", beekeeper="Someone Else").get_json()["harvest"]
        self.assertEqual(harvest["harvested_by"], "USR-BK-A")

    def test_admin_may_harvest_any_owned_hive(self) -> None:
        r = self.harvest(headers=self.admin_headers(), hive_id="HIVE-B")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["harvest"]["organization_id"], "ORG-B")

    def test_unit_grams_is_converted_to_kg(self) -> None:
        r = self.harvest(total_weight=12500, unit="g")
        self.assertEqual(r.get_json()["harvest"]["quantity_kg"], 12.5)

    def test_iso_datetime_is_kept_as_harvested_at(self) -> None:
        when = (date.today() - timedelta(days=1)).isoformat() + "T06:30:00Z"
        harvest = self.harvest(harvest_date=when).get_json()["harvest"]
        self.assertEqual(harvest["harvested_at"], when[:10] + " 06:30:00")
        self.assertEqual(harvest["harvest_date"], when[:10])

    def test_client_supplied_ids_are_honoured(self) -> None:
        r = self.harvest(harvest_id="HRV-2026-FIELD01", batch_id="HNY-2026-9001")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["harvest"]["harvest_id"], "HRV-2026-FIELD01")
        self.assertEqual(r.get_json()["batch"]["batch_id"], "HNY-2026-9001")


# ---------------------------------------------------------------------------
class TestHarvestAuthorization(HarvestTestCase):
    def test_unauthenticated_is_401_and_writes_nothing(self) -> None:
        r = self.client.post("/api/harvest", json={"hive_id": "HIVE-A", "total_weight": 1,
                                                   "floral_type": "X"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_wrong_roles_are_403(self) -> None:
        for role in ("PROCESSOR", "LAB", "LOGISTICS", "RETAILER", "CONSUMER", "AUTHORITY",
                     "FPO_AGGREGATOR"):
            user = self.create_user(user_id=f"USR-{role}", email=f"{role}@hc.test", role=role,
                                    organization_pk=self.org_a)
            self.assertEqual(self.harvest(headers=self.auth_headers_for(user)).status_code, 403, role)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_cross_organization_harvest_is_403_audited_and_writes_nothing(self) -> None:
        r = self.harvest(headers=self.h_b, hive_id="HIVE-A")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertIn("HARVEST_DENIED", self.actions())

    def test_suspended_organization_cannot_harvest(self) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE organizations SET status = 'SUSPENDED' WHERE id = ?;", (self.org_a,))
        self.assertEqual(self.harvest().status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_claimed_organization_must_match_the_hive(self) -> None:
        r = self.harvest(organization_id="ORG-B")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["details"][0]["issue"], "does_not_match_hive")
        self.assertEqual(self.harvest(organization_id="ORG-A").status_code, 201)


# ---------------------------------------------------------------------------
class TestHiveValidity(HarvestTestCase):
    def test_unknown_hive_is_404(self) -> None:
        self.assertEqual(self.harvest(hive_id="HIVE-NOPE").status_code, 404)

    def test_malformed_or_injection_hive_id_is_400(self) -> None:
        for bad in ("'; DROP TABLE hives;--", "<script>", "", "x" * 200):
            self.assertEqual(self.harvest(hive_id=bad).status_code, 400, bad)

    def test_unassigned_hive_is_rejected_even_for_admin(self) -> None:
        self.seed_hive("HIVE-ORPHAN", count=3)
        r = self.harvest(headers=self.admin_headers(), hive_id="HIVE-ORPHAN")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["details"][0]["issue"], "unassigned")

    def test_retired_hive_is_409(self) -> None:
        with db.transaction() as conn:
            conn.execute("UPDATE hives SET status = 'RETIRED' WHERE hive_id = 'HIVE-A';")
        self.assertEqual(self.harvest().status_code, 409)

    def test_hive_without_telemetry_cannot_get_a_quality_score(self) -> None:
        self.client.post("/api/hives", json={"hive_id": "HIVE-SILENT"}, headers=self.h_a)
        self.assertEqual(self.harvest(hive_id="HIVE-SILENT").status_code, 400)
        self.assertEqual(self.counts(), (0, 0, 0))


# ---------------------------------------------------------------------------
class TestInputValidation(HarvestTestCase):
    def assert_400(self, **overrides) -> None:
        r = self.harvest(**overrides)
        self.assertEqual(r.status_code, 400, (overrides, r.get_data(as_text=True)))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_invalid_quantities(self) -> None:
        for value in (0, -5, "abc", "NaN", "Infinity", True, None, 10_001):
            self.assert_400(total_weight=value)

    def test_unsupported_unit(self) -> None:
        self.assert_400(unit="lb")

    def test_invalid_harvest_times(self) -> None:
        future = (date.today() + timedelta(days=3)).isoformat()
        ancient = (date.today() - timedelta(days=400)).isoformat()
        for value in (future, ancient, "2026-09-01garbage", "2026-02-30", "yesterday", "01/09/2026"):
            self.assert_400(harvest_date=value)

    def test_missing_date_defaults_to_today(self) -> None:
        r = self.harvest(harvest_date=None)
        self.assertEqual(r.get_json()["harvest"]["harvest_date"], date.today().isoformat())

    def test_invalid_gps(self) -> None:
        self.assert_400(latitude=17.0)                    # without longitude
        self.assert_400(latitude=91, longitude=78)
        self.assert_400(latitude=17, longitude=-181)
        self.assert_400(latitude="north", longitude=78)

    def test_invalid_optional_fields(self) -> None:
        for kwargs in ({"frame_count": -1}, {"frame_count": "5"}, {"frame_count": True},
                       {"frame_count": 500}, {"notes": "x" * 1001},
                       {"photo_sha256": "not-a-hash"}, {"harvest_id": "bad id"},
                       {"floral_type": ""}):
            self.assert_400(**kwargs)

    def test_malformed_and_non_object_bodies(self) -> None:
        for raw in (b"{broken", b"[1, 2]", b'"string"'):
            r = self.client.post("/api/harvest", data=raw, content_type="application/json",
                                 headers=self.h_a)
            self.assertEqual(r.status_code, 400, raw)

    def test_markup_in_text_fields_is_stored_inert(self) -> None:
        payload = "<script>alert(1)</script>"
        r = self.harvest(notes=payload)
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.content_type.startswith("application/json"))
        self.assertEqual(r.get_json()["harvest"]["notes"], payload)  # stored verbatim, as data
        hid = r.get_json()["harvest"]["harvest_id"]
        self.assertEqual(db.get_harvest(hid)["notes"], payload)      # parameterised, not executed


# ---------------------------------------------------------------------------
class TestDuplicatesAndAtomicity(HarvestTestCase):
    def test_duplicate_harvest_id_is_409_and_creates_no_batch(self) -> None:
        self.assertEqual(self.harvest(harvest_id="HRV-DUP-0001").status_code, 201)
        r = self.harvest(harvest_id="HRV-DUP-0001")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_duplicate_batch_id_is_409_and_creates_no_harvest(self) -> None:
        self.assertEqual(self.harvest(batch_id="HNY-DUP-1").status_code, 201)
        self.assertEqual(self.harvest(batch_id="HNY-DUP-1").status_code, 409)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_race_on_harvest_id_rolls_back_the_ledger_block_too(self) -> None:
        """If the harvest insert fails INSIDE the transaction (a concurrent
        request won the race after the pre-check), the ledger block must not
        survive on its own -- no orphan batch without provenance."""
        self.assertEqual(self.harvest(harvest_id="HRV-RACE-0001").status_code, 201)
        height = ledger.ledger.height()
        with mock.patch.object(db, "harvest_exists", return_value=False):
            r = self.harvest(harvest_id="HRV-RACE-0001")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(ledger.ledger.height(), height)
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertTrue(self.client.get("/api/chain").get_json()["valid"])


# ---------------------------------------------------------------------------
class TestHashingLedgerAndTamperDetection(HarvestTestCase):
    def test_event_hash_is_deterministic_and_field_sensitive(self) -> None:
        base = {f: None for f in harvests.HARVEST_HASHED_FIELDS}
        base.update(harvest_id="HRV-X-0001", hive_id="HIVE-A", quantity_kg=12.5,
                    created_at="2026-09-01 00:00:00")
        h = harvests.compute_event_hash(base)
        self.assertEqual(h, harvests.compute_event_hash(dict(reversed(list(base.items())))))
        for field, value in (("quantity_kg", 12.6), ("hive_id", "HIVE-B"), ("notes", "x")):
            self.assertNotEqual(h, harvests.compute_event_hash({**base, field: value}), field)
        self.assertEqual(h, harvests.compute_event_hash({**base, "unrelated": "ignored"}))

    def test_stored_event_hash_matches_and_verification_passes(self) -> None:
        hid = self.harvest().get_json()["harvest"]["harvest_id"]
        v = self.client.get(f"/api/harvest/{hid}", headers=self.h_a).get_json()["verification"]
        self.assertTrue(v["event_hash_valid"])
        self.assertTrue(v["valid"])
        self.assertEqual(v["stored_event_hash"], v["recomputed_event_hash"])

    def test_editing_a_harvest_row_is_detected(self) -> None:
        hid = self.harvest().get_json()["harvest"]["harvest_id"]
        with db.transaction() as conn:
            conn.execute("UPDATE harvest_events SET quantity_kg = 99 WHERE harvest_id = ?;", (hid,))
        v = harvests.verify(hid)
        self.assertFalse(v["event_hash_valid"])
        self.assertFalse(v["valid"])

    def test_editing_the_linked_batch_block_is_detected(self) -> None:
        body = self.harvest().get_json()
        with db.transaction() as conn:
            conn.execute("UPDATE honey_batches SET total_weight = 99 WHERE batch_id = ?;",
                         (body["batch"]["batch_id"],))
        v = harvests.verify(body["harvest"]["harvest_id"])
        self.assertTrue(v["event_hash_valid"])
        self.assertFalse(v["batches"][0]["ledger_valid"])
        self.assertFalse(v["valid"])

    def test_successive_harvests_extend_one_valid_chain(self) -> None:
        blocks = [self.harvest().get_json()["batch"] for _ in range(3)]
        self.assertEqual([b["block_index"] for b in blocks], [0, 1, 2])
        self.assertEqual(blocks[1]["previous_hash"], blocks[0]["block_hash"])
        chain = self.client.get("/api/chain").get_json()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["height"], 3)


# ---------------------------------------------------------------------------
class TestRetrievalProvenanceAndIsolation(HarvestTestCase):
    def test_provenance_links_batch_to_harvest_to_hive(self) -> None:
        body = self.harvest().get_json()
        batch_id = body["batch"]["batch_id"]
        p = self.client.get(f"/api/batches/{batch_id}/provenance", headers=self.h_a).get_json()
        self.assertEqual(p["hive_id"], "HIVE-A")
        self.assertTrue(p["ledger"]["verified"])
        self.assertEqual([h["harvest_id"] for h in p["harvests"]], [body["harvest"]["harvest_id"]])
        self.assertTrue(p["harvests"][0]["verification"]["valid"])

    def test_read_idor_is_blocked(self) -> None:
        body = self.harvest().get_json()
        hid, bid = body["harvest"]["harvest_id"], body["batch"]["batch_id"]
        self.assertEqual(self.client.get(f"/api/harvest/{hid}", headers=self.h_b).status_code, 403)
        self.assertEqual(self.client.get(f"/api/batches/{bid}/provenance",
                                         headers=self.h_b).status_code, 403)
        self.assertEqual(self.client.get("/api/harvest", headers=self.h_b).get_json(), [])

    def test_batch_listing_is_organization_scoped(self) -> None:
        """Pre-Phase-5, every authenticated user received every organization's batches."""
        a = self.harvest().get_json()["batch"]["batch_id"]
        b = self.harvest(headers=self.h_b, hive_id="HIVE-B").get_json()["batch"]["batch_id"]
        ids = lambda h: {x["batch_id"] for x in self.client.get("/api/batches", headers=h).get_json()}  # noqa: E731
        self.assertEqual(ids(self.h_a), {a})
        self.assertEqual(ids(self.h_b), {b})
        self.assertEqual(ids(self.admin_headers()), {a, b})
        consumer = self.create_user(user_id="USR-CON", email="con@hc.test", role="CONSUMER")
        self.assertEqual(ids(self.auth_headers_for(consumer)), set())

    def test_unknown_and_malformed_ids_are_404(self) -> None:
        for bad in ("HRV-NOPE-0000", "not-a-harvest", "HRV-'--"):
            self.assertEqual(self.client.get(f"/api/harvest/{bad}", headers=self.h_a).status_code,
                             404, bad)

    def test_public_verification_still_works_for_new_batches(self) -> None:
        bid = self.harvest().get_json()["batch"]["batch_id"]
        self.assertEqual(self.client.get(f"/verify/{bid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/verify/{bid}").get_json()["status"], "VERIFIED")


# ---------------------------------------------------------------------------
class TestHarvestAudit(HarvestTestCase):
    def test_success_writes_harvest_and_batch_events_with_actor_and_org(self) -> None:
        body = self.harvest().get_json()
        events = {e["action"]: e for e in db.list_audit_events(50)}
        self.assertIn("HARVEST_CREATED", events)
        self.assertIn("BATCH_CREATED", events)
        h = events["HARVEST_CREATED"]
        self.assertEqual(h["actor_external_id"], "USR-BK-A")
        self.assertEqual(h["resource_id"], body["harvest"]["harvest_id"])
        self.assertIn(body["harvest"]["event_hash"], h["metadata"])
        self.assertEqual(events["BATCH_CREATED"]["resource_id"], body["batch"]["batch_id"])

    def test_audit_never_contains_the_bearer_token(self) -> None:
        self.harvest()
        token = self.h_a["Authorization"].split()[1]
        self.assertNotIn(token, json.dumps(db.list_audit_events(200)))


if __name__ == "__main__":
    unittest.main()
