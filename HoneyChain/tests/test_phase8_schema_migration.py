"""
Phase 8 schema migration: a pre-Phase-8 database (Phase 7 state) holding a
real ledger batch upgrades in place. The batch has NO batch_state row after
migration (history is not rewritten); participation is still derived from
the hive's owner; the first custody action lazily creates its state with
origin = holder = hive owner and remaining = the ledger weight; the new
partial unique index blocks a second pending transfer; idempotent.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tests.base  # noqa: E402,F401  (sets the test AUTH_SECRET etc. before honeychain loads)
from honeychain import auth, config, db, ledger, supply_chain  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402
from tests.test_phase7_schema_migration import PRE_PHASE7_SCHEMA  # noqa: E402

PRE_PHASE8_SCHEMA = PRE_PHASE7_SCHEMA + """
ALTER TABLE health_evaluations ADD COLUMN ml_band TEXT;
ALTER TABLE health_evaluations ADD COLUMN model_version TEXT;
ALTER TABLE devices ADD COLUMN data_origin TEXT NOT NULL DEFAULT 'PHYSICAL'
    CHECK (data_origin IN ('PHYSICAL', 'SIMULATED'));
"""


class TestPhase8MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p8-"))
        cls.db_path = cls._tmp / "pre_phase8.db"
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE8_SCHEMA)
        conn.execute("INSERT INTO organizations (org_id, name, org_type) VALUES ('ORG-BK8','B','BEEKEEPER');")
        conn.execute("INSERT INTO organizations (org_id, name, org_type) VALUES ('ORG-PR8','P','PROCESSOR');")
        conn.execute("INSERT INTO hives (hive_id, status, organization_id) VALUES ('HIVE-P8','ACTIVE',1);")
        block = {"block_index": 0, "batch_id": "HNY-2026-0800", "hive_id": "HIVE-P8",
                 "harvest_date": "2026-09-01", "total_weight": 14.0, "floral_type": "Neem",
                 "quality_score": 88.0, "timestamp": "2026-09-01T10:00:00Z",
                 "previous_hash": ledger.GENESIS_PREVIOUS_HASH if hasattr(ledger, "GENESIS_PREVIOUS_HASH") else "0" * 64}
        block["block_hash"] = ledger.compute_block_hash(block)
        conn.execute("INSERT INTO honey_batches (block_index, batch_id, hive_id, harvest_date, total_weight, "
                     " floral_type, quality_score, timestamp, previous_hash, block_hash) VALUES "
                     "(:block_index, :batch_id, :hive_id, :harvest_date, :total_weight, :floral_type, "
                     " :quality_score, :timestamp, :previous_hash, :block_hash);", block)
        conn.commit()
        cls.before = dict(conn.execute("SELECT * FROM honey_batches;").fetchone())
        conn.close()
        cls._orig = config.DATABASE_PATH
        config.DATABASE_PATH = cls.db_path
        db.close_connection()
        db.initialise_database()

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        config.DATABASE_PATH = cls._orig
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_batch_untouched_and_no_state_rewritten(self) -> None:
        self.assertEqual(dict(db.query_one("SELECT * FROM honey_batches;")), self.before)
        self.assertIsNone(db.query_one("SELECT * FROM batch_state;"))
        for table, col in (("lab_results", "result_hash"), ("shipments", "from_org_id"),
                           ("retail_records", "shipment_id"), ("custody_transfers", "initiated_by_user_id")):
            cols = {r["name"] for r in db.query_all(f"PRAGMA table_info({table});")}
            self.assertIn(col, cols, f"{table}.{col}")

    def test_participation_derived_from_hive_owner_without_state(self) -> None:
        bk = db.get_organization_by_org_id("ORG-BK8")["id"]
        self.assertIn(bk, supply_chain.viewer_org_ids("HNY-2026-0800"))

    def test_first_custody_action_creates_state_lazily_and_one_pending_rule_holds(self) -> None:
        from honeychain.app import create_app
        bk = db.get_organization_by_org_id("ORG-BK8")["id"]
        with db.transaction() as conn:
            db.create_user(conn, user_id="USR-BK8", organization_pk=bk, email="bk8@m.test",
                           password_hash=auth.hash_password("x-irrelevant"), name="BK8", role="BEEKEEPER")
        token, _ = auth.generate_token(db.get_user_by_user_id("USR-BK8"))
        client = create_app(initialise_db=False, load_model=False).test_client()
        r = client.post("/api/custody/transfers", headers={"Authorization": f"Bearer {token}"},
                        json={"batch_id": "HNY-2026-0800", "to_organization_id": "ORG-PR8"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        state = dict(db.query_one("SELECT * FROM batch_state WHERE batch_id = 'HNY-2026-0800';"))
        self.assertEqual((state["origin_org_id"], state["holder_org_id"], state["remaining_kg"],
                          state["status"]), (bk, bk, 14.0, "ACTIVE"))
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO custody_transfers (transfer_id, batch_id, from_org_id, to_org_id, "
                             "status) VALUES ('CUS-DUP', 'HNY-2026-0800', 1, 2, 'PENDING');")
                conn.commit()
        finally:
            conn.close()

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM honey_batches;")["n"], 1)


if __name__ == "__main__":
    unittest.main()
