"""
Phase 10 schema migration: a pre-Phase-10 database (no consumer_scans table,
no shipments.exception_reason) holding a shipment in EXCEPTION upgrades in
place. The shipment keeps every value and gains a NULL reason (an old
exception is never back-filled with an invented reason); consumer_scans is
created and its foreign key and result CHECK are enforced; idempotent.
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

from honeychain import authority, config, db  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402


class TestPhase10MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p10-"))
        cls.db_path = cls._tmp / "pre_phase10.db"
        cls._orig = config.DATABASE_PATH
        config.DATABASE_PATH = cls.db_path
        db.close_connection()
        db.initialise_database()
        db.close_connection()
        conn = _raw_connect(cls.db_path)
        conn.execute("DROP TABLE consumer_scans;")
        conn.execute("ALTER TABLE shipments DROP COLUMN exception_reason;")
        conn.execute("INSERT INTO honey_batches (block_index, batch_id, hive_id, harvest_date, "
                     "total_weight, floral_type, quality_score, timestamp, previous_hash, block_hash) "
                     "VALUES (0,'HNY-P10','HIVE-P10','2026-09-01',10,'Mustard',90,'t','0','h');")
        conn.execute("INSERT INTO shipments (shipment_id, batch_id, status) "
                     "VALUES ('SHP-P10','HNY-P10','EXCEPTION');")
        conn.commit()
        cls.before = dict(conn.execute("SELECT * FROM shipments;").fetchone())
        assert "exception_reason" not in cls.before
        conn.close()
        db.initialise_database()

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        config.DATABASE_PATH = cls._orig
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_shipment_survives_without_an_invented_reason(self) -> None:
        row = dict(db.query_one("SELECT * FROM shipments;"))
        for key, value in self.before.items():
            self.assertEqual(row[key], value, key)
        self.assertIsNone(row["exception_reason"])

    def test_consumer_scans_exists_with_constraints(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):      # FK to product_units
                conn.execute("INSERT INTO consumer_scans (unit_id, result, scanned_at) "
                             "VALUES ('UNIT-NOPE', 'INVALID', 't');")
                conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):      # result CHECK
                conn.execute("INSERT INTO consumer_scans (result, scanned_at) VALUES ('MAYBE', 't');")
                conn.commit()
        finally:
            conn.close()

    def test_oversight_views_work_on_the_migrated_database(self) -> None:
        self.assertEqual(authority.overview()["compliance"]["shipment_exceptions"][0]["batch_id"], "HNY-P10")
        self.assertEqual(authority.signals()["signals"], [])

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM shipments;")["n"], 1)
