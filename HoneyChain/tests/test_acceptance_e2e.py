"""
Final acceptance test -- the complete workflow through the REAL HTTP API on a
REAL local Hardhat chain (production blockchain client):

admin -> organizations -> users -> hive -> device -> telemetry -> health ->
harvest -> batch -> custody -> processing -> lab -> packaging -> unique QR ->
custody -> shipment -> retail -> consumer verification -> blockchain anchor
-> recall -> consumer sees recall -> authority + audit visibility.

DATA HONESTY: the telemetry includes rows from data/raw_data.txt, a REAL
ESP32 capture (temperature/humidity only). Here it is REPLAYED through the
device API by software, from a device registered as SIMULATED -- this is not
live physical-hardware verification, and the test does not claim it is. Five
extra full-feature readings (also SIMULATED) are added because the real
capture has no weight channel, which the harvest quality score needs.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import date, timedelta
from pathlib import Path

from tests.base import REPO_ROOT
from tests.test_phase6_live_chain import LiveChainTestCase

from honeychain import chain_events, device_auth

TODAY = date.today().isoformat()


def _real_rows(n: int) -> list[tuple[float, float]]:
    """(inside temperature, inside humidity) from the real capture."""
    rows = []
    for line in (Path(REPO_ROOT) / "data/raw_data.txt").read_text().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                rows.append((float(parts[-4]), float(parts[-3])))
            except ValueError:
                continue
        if len(rows) == n:
            break
    return rows


class TestAcceptanceEndToEnd(LiveChainTestCase):
    def api(self, method, path, token=None, body=None, expect=(200, 201), raw=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        kw = {"data": raw, "content_type": "application/json"} if raw is not None else {"json": body}
        r = getattr(self.client, method)(path, headers=h, **kw)
        expected = expect if isinstance(expect, tuple) else (expect,)
        self.assertIn(r.status_code, expected, f"{method.upper()} {path}: {r.get_data(as_text=True)[:300]}")
        return r.get_json()

    def test_hive_to_consumer_to_recall_on_a_real_local_chain(self) -> None:
        admin = self.admin_headers()["Authorization"].split()[1]
        tokens = {}
        for key, org_type, role in (("bk", "BEEKEEPER", "BEEKEEPER"), ("proc", "PROCESSOR", "PROCESSOR"),
                                    ("lab", "LAB", "LAB"), ("dist", "LOGISTICS", "LOGISTICS"),
                                    ("ret", "RETAILER", "RETAILER"), ("gov", "AUTHORITY", "AUTHORITY"),
                                    ("other", "BEEKEEPER", "BEEKEEPER")):
            org = f"ORG-ACC-{key.upper()}"
            self.api("post", "/api/admin/organizations", admin,
                     {"org_id": org, "name": f"Acceptance {key}", "org_type": org_type})
            pw = secrets.token_urlsafe(12)
            self.api("post", "/api/auth/register", admin, {"email": f"{key}@acc.test", "password": pw,
                                                           "name": key, "role": role, "organization_id": org})
            tokens[key] = self.api("post", "/api/auth/login", body={"email": f"{key}@acc.test",
                                                                    "password": pw})["token"]
        t = tokens

        # Hive + device + telemetry (real capture replayed, honestly labelled)
        self.api("post", "/api/hives", t["bk"], {"hive_id": "HIVE-ACC-1", "location": "Kolar"})
        creds = self.api("post", "/api/devices", t["bk"], {"hive_id": "HIVE-ACC-1",
                                                           "data_origin": "SIMULATED"})["credentials"]

        def send(reading):
            raw = json.dumps({"hive_id": "HIVE-ACC-1", **reading}).encode()
            ts, nonce = str(int(time.time())), secrets.token_hex(16)
            hdr = {device_auth.DEVICE_ID_HEADER: creds["device_id"], device_auth.TIMESTAMP_HEADER: ts,
                   device_auth.NONCE_HEADER: nonce,
                   device_auth.SIGNATURE_HEADER: device_auth.sign_request(
                       creds["device_id"], creds["device_secret"], ts, nonce, raw)}
            return raw, hdr

        real = _real_rows(20)
        self.assertEqual(len(real), 20)
        for temp, hum in real:
            raw, hdr = send({"temperature": temp, "humidity": hum})
            rec = self.api("post", "/api/devices/telemetry", raw=raw, headers=hdr)["records"][0]
            self.assertEqual(rec["data_origin"], "SIMULATED")            # replay, not live hardware
            self.assertIn("MISSING_WEIGHT", rec["sensor_flags"])
            self.assertIn("NOT applied", rec["reason"])                  # no ML conclusion on partial data
            self.assertIn(rec["health_status"], ("HEALTHY", "WARNING", "CRITICAL"))
        self.api("post", "/api/devices/telemetry", raw=raw, headers=hdr, expect=409)   # replay blocked
        for i in range(5):
            raw, hdr = send({"temperature": 34.5, "humidity": 60 + i, "weight": 42.0, "audio_frequency": 255})
            self.api("post", "/api/devices/telemetry", raw=raw, headers=hdr)

        # Harvest -> processing -> lab -> packaging
        batch = self.api("post", "/api/harvest", t["bk"], {"hive_id": "HIVE-ACC-1", "harvest_date": TODAY,
                                                           "total_weight": 20, "floral_type": "Mustard"})["batch"]["batch_id"]
        self.api("get", f"/api/batches/{batch}/trace", t["other"], expect=403)   # IDOR blocked

        def hand_over(sender, to_org, receiver):
            tid = self.api("post", "/api/custody/transfers", t[sender],
                           {"batch_id": batch, "to_organization_id": to_org})["transfer_id"]
            self.api("post", f"/api/custody/transfers/{tid}/accept", t[receiver])

        hand_over("bk", "ORG-ACC-PROC", "proc")
        self.api("post", f"/api/batches/{batch}/processing", t["proc"], {"process_type": "FILTERING", "output_kg": 19.5})
        rid = self.api("post", f"/api/batches/{batch}/lab-requests", t["proc"],
                       {"lab_organization_id": "ORG-ACC-LAB"})["lab_result_id"]
        self.api("post", f"/api/lab/results/{rid}", t["proc"], {"status": "PASSED", "test_date": TODAY,
                                                                "moisture_pct": 17.5}, expect=403)
        self.api("post", f"/api/lab/results/{rid}", t["lab"], {"status": "PASSED", "test_date": TODAY,
                                                               "moisture_pct": 17.5, "hmf_mg_kg": 11})
        units = self.api("post", f"/api/batches/{batch}/packaging", t["proc"],
                         {"unit_size_g": 500, "unit_count": 4,
                          "best_before": (date.today() + timedelta(days=365)).isoformat()})["units"]
        self.assertEqual(len({u["qr_payload"] for u in units}), 4)

        # Distribution -> shipment -> retail
        hand_over("proc", "ORG-ACC-DIST", "dist")
        tid = self.api("post", "/api/custody/transfers", t["dist"],
                       {"batch_id": batch, "to_organization_id": "ORG-ACC-RET"})["transfer_id"]
        ship = self.api("post", "/api/shipments", t["dist"], {"custody_transfer_id": tid})
        self.api("post", f"/api/custody/transfers/{tid}/accept", t["ret"], expect=409)  # not delivered yet
        for status in ("IN_TRANSIT", "DELIVERED"):
            self.api("post", f"/api/shipments/{ship['shipment_id']}/status", t["dist"], {"status": status})
        self.api("post", f"/api/retail/{ship['retail_record_id']}/receive", t["ret"])
        self.api("post", f"/api/retail/{ship['retail_record_id']}/shelve", t["ret"])

        # Consumer verification + provenance
        v = self.api("post", "/api/public/verify", body={"qr": units[0]["qr_payload"]})
        self.assertEqual(v["result"], "AUTHENTIC")
        prov = v["provenance"]
        self.assertEqual(prov["harvests"][0]["hive"], "HIVE-ACC-1")
        self.assertEqual(prov["lab"]["status"], "PASSED")
        self.assertEqual([s["status"] for s in prov["shipments"]], ["DELIVERED"])
        self.assertTrue(all(not e["anchored_on_chain"] for e in prov["blockchain"]))  # not anchored yet

        # Blockchain: checkpoint anchored on the REAL local chain, verified independently
        cp = chain_events.create_checkpoint()
        self.assertEqual(chain_events.submit_checkpoints()[0]["status"], "CONFIRMED")
        stored = chain_events.get_checkpoint(cp["checkpoint_id"])
        self.assertEqual(self.w3.eth.get_transaction_receipt(stored["tx_hash"]).status, 1)
        self.assertTrue(self.contract().functions.verifyCheckpoint(
            cp["checkpoint_id"], bytes.fromhex(cp["merkle_root"])).call())
        v = self.api("post", "/api/public/verify", body={"qr": units[1]["qr_payload"]})
        self.assertTrue(all(e["anchored_on_chain"] and e["tx_hash"] == stored["tx_hash"]
                            for e in v["provenance"]["blockchain"]))
        self.assertTrue(self.api("get", "/api/chain/log/verify", admin)["valid"])

        # Recall -> consumer sees it -> authority + audit visibility
        self.api("post", "/api/recalls", t["gov"], {"batch_id": batch, "reason": "Acceptance test recall"})
        v = self.api("post", "/api/public/verify", body={"qr": units[0]["qr_payload"]})
        self.assertEqual(v["result"], "AUTHENTIC")
        self.assertTrue(v["recall"]["active"])
        self.assertEqual(v["unit"]["status"], "RECALLED")
        recalls = self.api("get", "/api/authority/recalls", t["gov"])
        self.assertEqual(recalls[0]["root_batch_id"], batch)
        signals = {s["type"] for s in self.api("get", "/api/authority/signals", t["gov"])["signals"]}
        self.assertIn("SCANNED_AFTER_RECALL", signals)
        actions = {e["action"] for e in self.api("get", "/api/audit-logs", admin)}
        for action in ("ORGANIZATION_CREATED", "USER_CREATED", "DEVICE_REGISTERED", "DEVICE_AUTH_REPLAY",
                       "HARVEST_CREATED", "CUSTODY_TRANSFER_ACCEPTED", "PROCESSING_RECORDED",
                       "LAB_RESULT_RECORDED", "BATCH_PACKAGED", "PRODUCT_UNITS_CREATED",
                       "SHIPMENT_STATUS_CHANGED", "RETAIL_ON_SHELF", "RECALL_ISSUED",
                       "CHECKPOINT_ANCHORED", "AUTHORIZATION_DENIED"):
            self.assertIn(action, actions)
        self.api("get", "/api/authority/overview", t["bk"], expect=403)
