#!/usr/bin/env python3
"""
scripts/seed_demo.py -- one-command, clearly-labelled DEMO dataset.

    HONEYCHAIN_AUTH_SECRET=... HONEYCHAIN_DEVICE_MASTER_KEY=... python scripts/seed_demo.py

Drives the REAL HTTP API (Flask test client against the configured database)
through the complete flow, so the demo exercises exactly the code a user
does:

  admin -> organizations -> users -> hive -> SIMULATED device -> signed
  telemetry -> harvest -> custody to processor -> processing -> lab test ->
  packaging (per-unit QR) -> custody to distributor -> shipment -> retailer
  receipt -> on shelf -> public consumer verification

Honesty rules:
* Every organization is named "DEMO ..." with an ORG-DEMO-* id, the device is
  registered as data_origin=SIMULATED (its readings are labelled so), and
  the lab result carries certificate reference "DEMO-NOT-A-REAL-CERTIFICATE".
  The lab values are demonstration inputs, not measurements.
* Passwords are random and printed ONCE; nothing is hard-coded.
* Only the bootstrap admin is written directly to the database (as
  scripts/create_admin_user.py does); everything else goes through the API.
* Refuses to run twice against the same database.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from honeychain import auth, config, db, device_auth  # noqa: E402
from honeychain.app import create_app  # noqa: E402

ROLES = {  # key: (org_id, org name, org_type, user role)
    "beekeeper": ("ORG-DEMO-APIARY", "DEMO Kolar Apiary", "BEEKEEPER", "BEEKEEPER"),
    "processor": ("ORG-DEMO-PROCESSOR", "DEMO Honey Processor", "PROCESSOR", "PROCESSOR"),
    "lab": ("ORG-DEMO-LAB", "DEMO Testing Laboratory", "LAB", "LAB"),
    "distributor": ("ORG-DEMO-LOGISTICS", "DEMO Logistics", "LOGISTICS", "LOGISTICS"),
    "retailer": ("ORG-DEMO-RETAIL", "DEMO Retail Store", "RETAILER", "RETAILER"),
    "authority": ("ORG-DEMO-AUTHORITY", "DEMO Apiculture Board", "AUTHORITY", "AUTHORITY"),
}


class SeedError(RuntimeError):
    pass


def main() -> int:
    if not (config.AUTH_SECRET and config.DEVICE_MASTER_KEY):
        print("Set HONEYCHAIN_AUTH_SECRET and HONEYCHAIN_DEVICE_MASTER_KEY first.", file=sys.stderr)
        return 2
    app = create_app(load_model=True)
    app.config["TESTING"] = True
    client = app.test_client()
    if db.get_organization_by_org_id("ORG-DEMO-APIARY") is not None:
        print("DEMO data already exists in this database; refusing to seed twice.", file=sys.stderr)
        return 1

    def call(method, path, body=None, token=None, expect=(200, 201), raw=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        kwargs = {"data": raw, "content_type": "application/json"} if raw is not None else {"json": body}
        r = getattr(client, method)(path, headers=h, **kwargs)
        if r.status_code not in expect:
            raise SeedError(f"{method.upper()} {path} -> {r.status_code}: {r.get_data(as_text=True)[:300]}")
        return r.get_json()

    passwords: dict[str, str] = {}

    def login(email: str, password: str) -> str:
        return call("post", "/api/auth/login", {"email": email, "password": password})["token"]

    # Bootstrap admin (direct, like scripts/create_admin_user.py).
    passwords["admin"] = secrets.token_urlsafe(12)
    with db.transaction() as conn:
        db.create_user(conn, user_id="USR-DEMO-ADMIN", organization_pk=None,
                       email="admin@demo.honeychain.test",
                       password_hash=auth.hash_password(passwords["admin"]),
                       name="DEMO Administrator", role="ADMIN")
    admin = login("admin@demo.honeychain.test", passwords["admin"])

    tokens: dict[str, str] = {}
    for key, (org_id, name, org_type, role) in ROLES.items():
        call("post", "/api/admin/organizations", {"org_id": org_id, "name": name, "org_type": org_type}, admin)
        passwords[key] = secrets.token_urlsafe(12)
        email = f"{key}@demo.honeychain.test"
        call("post", "/api/auth/register", {"email": email, "password": passwords[key],
                                            "name": f"DEMO {key.title()}", "role": role,
                                            "organization_id": org_id}, admin)
        tokens[key] = login(email, passwords[key])
    bk, proc, lab, dist, ret = (tokens[k] for k in ("beekeeper", "processor", "lab", "distributor", "retailer"))

    call("post", "/api/hives", {"hive_id": "HIVE-DEMO-01", "name": "DEMO hive", "location": "Kolar"}, bk)
    creds = call("post", "/api/devices", {"hive_id": "HIVE-DEMO-01", "data_origin": "SIMULATED"}, bk)["credentials"]
    for i in range(6):  # quality scoring needs telemetry
        raw = json.dumps({"hive_id": "HIVE-DEMO-01", "temperature": 34.4 + 0.05 * i, "humidity": 61,
                          "weight": 42.0 + 0.1 * i, "audio_frequency": 255}).encode()
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        call("post", "/api/devices/telemetry", raw=raw, headers={
            device_auth.DEVICE_ID_HEADER: creds["device_id"], device_auth.TIMESTAMP_HEADER: ts,
            device_auth.NONCE_HEADER: nonce,
            device_auth.SIGNATURE_HEADER: device_auth.sign_request(creds["device_id"], creds["device_secret"],
                                                                   ts, nonce, raw)})

    today = date.today().isoformat()
    batch = call("post", "/api/harvest", {"hive_id": "HIVE-DEMO-01", "harvest_date": today,
                                          "total_weight": 24.0, "floral_type": "Eucalyptus",
                                          "latitude": 13.13, "longitude": 78.13}, bk)["batch"]["batch_id"]

    def hand_over(sender, receiver_org, receiver):
        t = call("post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": receiver_org},
                 sender)["transfer_id"]
        call("post", f"/api/custody/transfers/{t}/accept", token=receiver)

    hand_over(bk, "ORG-DEMO-PROCESSOR", proc)
    call("post", f"/api/batches/{batch}/processing", {"process_type": "FILTERING", "output_kg": 23.5}, proc)
    rid = call("post", f"/api/batches/{batch}/lab-requests", {"lab_organization_id": "ORG-DEMO-LAB"},
               proc)["lab_result_id"]
    call("post", f"/api/lab/results/{rid}", {"status": "PASSED", "test_date": today, "moisture_pct": 17.8,
                                             "hmf_mg_kg": 12.0, "c4_sugar_pct": 2.1,
                                             "certificate_id": "DEMO-NOT-A-REAL-CERTIFICATE"}, lab)
    units = call("post", f"/api/batches/{batch}/packaging",
                 {"unit_size_g": 500, "unit_count": 6,
                  "best_before": (date.today() + timedelta(days=540)).isoformat()}, proc)["units"]
    hand_over(proc, "ORG-DEMO-LOGISTICS", dist)
    t = call("post", "/api/custody/transfers", {"batch_id": batch, "to_organization_id": "ORG-DEMO-RETAIL"},
             dist)["transfer_id"]
    shipment = call("post", "/api/shipments", {"custody_transfer_id": t, "origin": "Kolar",
                                               "destination": "Bengaluru"}, dist)
    for status in ("IN_TRANSIT", "DELIVERED"):
        call("post", f"/api/shipments/{shipment['shipment_id']}/status", {"status": status}, dist)
    call("post", f"/api/retail/{shipment['retail_record_id']}/receive", token=ret)
    call("post", f"/api/retail/{shipment['retail_record_id']}/shelve", token=ret)

    verdict = call("post", "/api/public/verify", {"qr": units[0]["qr_payload"]})
    if verdict["result"] != "AUTHENTIC":
        raise SeedError(f"consumer verification did not pass: {verdict}")

    print("DEMO dataset created (every organization is named 'DEMO ...').\n")
    print("Sign-in (passwords shown once):")
    print(f"  admin@demo.honeychain.test  {passwords['admin']}")
    for key in ROLES:
        print(f"  {key}@demo.honeychain.test  {passwords[key]}")
    print(f"\nBatch {batch} is ON_SHELF. Consumer verification: {verdict['result']}")
    print(f"Scan / open this unit QR URL: {units[0]['qr_url']}")
    print("Oversight console: /app/ (sign in as the authority user)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        print(f"Seeding failed: {exc}", file=sys.stderr)
        sys.exit(1)
