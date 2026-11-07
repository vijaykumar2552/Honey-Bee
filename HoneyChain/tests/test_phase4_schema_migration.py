"""
Phase 4 schema migration: a genuine pre-Phase-4 database (Phase 3 state,
with real telemetry already in it) must upgrade in place -- existing readings
keep every original value and gain NULL device columns; devices gain a
credential_version defaulting to 1; device_nonces appears and its composite
PRIMARY KEY is enforced; the migration is idempotent.
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
from tests.test_phase3_schema_migration import PRE_PHASE3_SCHEMA  # noqa: E402

# Phase 3's additions, frozen: revoked_tokens + hives.organization_id.
PRE_PHASE4_SCHEMA = PRE_PHASE3_SCHEMA + """
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY, revoked_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    expires_at TEXT
);
ALTER TABLE hives ADD COLUMN organization_id INTEGER REFERENCES organizations (id);
"""


class TestPhase4MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p4-"))
        cls.db_path = cls._tmp / "pre_phase4.db"
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE4_SCHEMA)
        conn.execute("INSERT INTO organizations (org_id, name, org_type) VALUES ('ORG-P4','O','BEEKEEPER');")
        conn.execute("INSERT INTO hives (hive_id, status, organization_id) VALUES ('HIVE-P4','ACTIVE',1);")
        conn.execute(
            "INSERT INTO telemetry_readings (hive_id, timestamp, temperature, humidity, weight) "
            "VALUES (1, '2026-09-01 08:00:00', 34.6, 61.0, 42.1);"
        )
        conn.execute(
            "INSERT INTO devices (device_id, hive_id, organization_id, status) "
            "VALUES ('DEV-PRE4', 1, 1, 'PROVISIONED');"
        )
        conn.commit()
        cls.pre_reading = dict(conn.execute("SELECT * FROM telemetry_readings;").fetchone())
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(telemetry_readings);")}
        assert "device_timestamp" not in cols, "setup bug: pre-Phase-4 schema already migrated"
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

    def test_existing_reading_survives_with_null_device_columns(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            row = dict(conn.execute("SELECT * FROM telemetry_readings;").fetchone())
            for key, value in self.pre_reading.items():
                self.assertEqual(row[key], value, key)
            for new_col in ("device_timestamp", "device_id", "sensor_flags"):
                self.assertIn(new_col, row)
                self.assertIsNone(row[new_col])
        finally:
            conn.close()

    def test_existing_device_gets_default_credential_version(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM devices WHERE device_id = 'DEV-PRE4';").fetchone()
            self.assertEqual(row["credential_version"], 1)
            self.assertIsNone(row["expected_interval_seconds"])
        finally:
            conn.close()

    def test_device_nonces_primary_key_enforced(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            conn.execute("INSERT INTO device_nonces (device_id, nonce, seen_at) "
                         "VALUES (1, 'nonce-abc', '2026-09-01 00:00:00');")
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO device_nonces (device_id, nonce, seen_at) "
                             "VALUES (1, 'nonce-abc', '2026-09-01 00:00:01');")
                conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):  # FK to devices
                conn.execute("INSERT INTO device_nonces (device_id, nonce, seen_at) "
                             "VALUES (999, 'nonce-xyz', '2026-09-01 00:00:00');")
                conn.commit()
        finally:
            conn.close()

    def test_new_indexes_exist(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';")}
            for idx in ("idx_telemetry_device", "idx_hives_org", "idx_device_nonces_seen"):
                self.assertIn(idx, names)
        finally:
            conn.close()

    def test_legacy_reading_still_served_by_api_read_query(self) -> None:
        record = db.telemetry_by_id(1)
        self.assertEqual(record["hive_id"], "HIVE-P4")
        self.assertIsNone(record["device_id"])

    def test_idempotent(self) -> None:
        conn = _raw_connect(self.db_path)
        before = [dict(r) for r in conn.execute("SELECT * FROM telemetry_readings;")]
        conn.close()
        db.close_connection()
        db.initialise_database()
        conn = _raw_connect(self.db_path)
        try:
            self.assertEqual([dict(r) for r in conn.execute("SELECT * FROM telemetry_readings;")], before)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
