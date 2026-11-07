"""
Phase 5 schema migration: a pre-Phase-5 database (Phase 4 state) that already
holds a harvest_events row must upgrade in place. The row keeps every
original value, gains NULL harvested_at / photo_sha256 / event_hash, and
verification reports it honestly as unhashed (never crashes, never claims it
is valid).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from honeychain import config, db, harvests  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402
from tests.test_phase4_schema_migration import PRE_PHASE4_SCHEMA  # noqa: E402

PRE_PHASE5_SCHEMA = PRE_PHASE4_SCHEMA + """
CREATE TABLE IF NOT EXISTS device_nonces (
    device_id INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    nonce TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (device_id, nonce)
);
ALTER TABLE telemetry_readings ADD COLUMN device_timestamp TEXT;
ALTER TABLE telemetry_readings ADD COLUMN device_id INTEGER REFERENCES devices (id);
ALTER TABLE telemetry_readings ADD COLUMN sensor_flags TEXT;
ALTER TABLE devices ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE devices ADD COLUMN expected_interval_seconds INTEGER;
"""


class TestPhase5MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p5-"))
        cls.db_path = cls._tmp / "pre_phase5.db"
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE5_SCHEMA)
        conn.execute("INSERT INTO organizations (org_id, name, org_type) VALUES ('ORG-P5','O','BEEKEEPER');")
        conn.execute("INSERT INTO hives (hive_id, status, organization_id) VALUES ('HIVE-P5','ACTIVE',1);")
        conn.execute(
            "INSERT INTO harvest_events (harvest_id, hive_id, organization_id, harvest_date, "
            " quantity_kg, floral_source) VALUES ('HRV-LEGACY-0001', 1, 1, '2026-09-01', 7.5, 'Neem');"
        )
        conn.commit()
        cls.before = dict(conn.execute("SELECT * FROM harvest_events;").fetchone())
        assert "event_hash" not in cls.before, "setup bug: already migrated"
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

    def test_existing_harvest_survives_with_null_new_columns(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            row = dict(conn.execute("SELECT * FROM harvest_events;").fetchone())
        finally:
            conn.close()
        for key, value in self.before.items():
            self.assertEqual(row[key], value, key)
        for col in ("harvested_at", "photo_sha256", "event_hash"):
            self.assertIsNone(row[col])

    def test_legacy_unhashed_harvest_is_reported_honestly(self) -> None:
        result = harvests.verify("HRV-LEGACY-0001")
        self.assertTrue(result["found"])
        self.assertFalse(result["event_hash_valid"])
        self.assertFalse(result["valid"])

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        conn = _raw_connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM harvest_events;").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
