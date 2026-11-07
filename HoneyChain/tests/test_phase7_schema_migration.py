"""
Phase 7 schema migration: a pre-Phase-7 database (Phase 6 state) holding a
health evaluation and a device upgrades in place. Existing rows keep every
value; health_evaluations gains NULL ml_band / model_version (old verdicts
are never retro-labelled with a model that did not produce them); devices
gain data_origin = 'PHYSICAL' with its CHECK enforced; idempotent.
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

from honeychain import config, db  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402
from tests.test_phase6_schema_migration import PRE_PHASE6_SCHEMA  # noqa: E402

PRE_PHASE7_SCHEMA = PRE_PHASE6_SCHEMA + """
ALTER TABLE blockchain_checkpoints ADD COLUMN from_seq INTEGER;
ALTER TABLE blockchain_checkpoints ADD COLUMN to_seq INTEGER;
ALTER TABLE blockchain_checkpoints ADD COLUMN merkle_root TEXT;
ALTER TABLE blockchain_checkpoints ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE blockchain_checkpoints ADD COLUMN last_error TEXT;
ALTER TABLE blockchain_checkpoints ADD COLUMN last_attempt_at TEXT;
ALTER TABLE blockchain_checkpoints ADD COLUMN contract_address TEXT;
"""


class TestPhase7MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p7-"))
        cls.db_path = cls._tmp / "pre_phase7.db"
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE7_SCHEMA)
        conn.execute("INSERT INTO organizations (org_id, name, org_type) VALUES ('ORG-P7','O','BEEKEEPER');")
        conn.execute("INSERT INTO hives (hive_id, status, organization_id) VALUES ('HIVE-P7','ACTIVE',1);")
        conn.execute("INSERT INTO telemetry_readings (hive_id, timestamp, temperature) VALUES (1,'2026-09-01 08:00:00',34.6);")
        conn.execute("INSERT INTO health_evaluations (hive_id, telemetry_id, timestamp, health_status, "
                     " anomaly_score, prediction, source) VALUES (1,1,'2026-09-01 08:00:00','HEALTHY',0.2,'HEALTHY','RULE+ML');")
        conn.execute("INSERT INTO devices (device_id, hive_id, organization_id, status) VALUES ('DEV-PRE7',1,1,'ACTIVE');")
        conn.commit()
        cls.before_eval = dict(conn.execute("SELECT * FROM health_evaluations;").fetchone())
        cls.before_dev = dict(conn.execute("SELECT * FROM devices;").fetchone())
        assert "ml_band" not in cls.before_eval and "data_origin" not in cls.before_dev
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

    def test_existing_evaluation_survives_and_is_not_retro_labelled(self) -> None:
        row = dict(db.query_one("SELECT * FROM health_evaluations;"))
        for key, value in self.before_eval.items():
            self.assertEqual(row[key], value, key)
        self.assertIsNone(row["ml_band"])
        self.assertIsNone(row["model_version"])

    def test_existing_device_defaults_to_physical_and_check_is_enforced(self) -> None:
        row = dict(db.query_one("SELECT * FROM devices;"))
        for key, value in self.before_dev.items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(row["data_origin"], "PHYSICAL")
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE devices SET data_origin = 'MADE_UP';")
                conn.commit()
        finally:
            conn.close()

    def test_legacy_record_still_readable_through_api_query(self) -> None:
        record = db.telemetry_by_id(1)
        self.assertEqual((record["hive_id"], record["ml_band"], record["data_origin"]),
                         ("HIVE-P7", None, "UNATTRIBUTED"))

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM health_evaluations;")["n"], 1)


if __name__ == "__main__":
    unittest.main()
