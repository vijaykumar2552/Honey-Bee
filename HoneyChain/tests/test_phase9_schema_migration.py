"""
Phase 9 schema migration: a pre-Phase-9 database (the Phase 8 schema, i.e.
without the four QR columns) holding a packaged unit upgrades in place. The
unit keeps every value, gains NULL qr_verifier/qr_version/qr_revoked_at/
last_scanned_at, and -- having no verifier -- is reported INVALID by public
verification until its packager re-presents the secret (never silently
treated as authentic).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from honeychain import config, consumer, db, unit_qr  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402

PHASE9_COLUMNS = ("qr_verifier", "qr_version", "qr_revoked_at", "last_scanned_at")
UNIT = "UNIT-ABCDEFGHIJKLMNOP"
SECRET = "pre-phase-9-unit-secret"


class TestPhase9MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p9-"))
        cls.db_path = cls._tmp / "pre_phase9.db"
        cls._orig = config.DATABASE_PATH
        config.DATABASE_PATH = cls.db_path
        db.close_connection()
        db.initialise_database()
        db.close_connection()
        conn = _raw_connect(cls.db_path)
        for col in PHASE9_COLUMNS:                      # back to the Phase 8 shape
            conn.execute(f"ALTER TABLE product_units DROP COLUMN {col};")
        conn.execute("INSERT INTO honey_batches (block_index, batch_id, hive_id, harvest_date, "
                     "total_weight, floral_type, quality_score, timestamp, previous_hash, block_hash) "
                     "VALUES (0,'HNY-P9','HIVE-P9','2026-09-01',10,'Mustard',90,'t','0','h');")
        conn.execute("INSERT INTO packaging_events (packaging_id, batch_id, unit_size_g, unit_count) "
                     "VALUES ('PKG-P9','HNY-P9',500,1);")
        conn.execute("INSERT INTO product_units (unit_id, packaging_id, batch_id, secret_hash, status) "
                     "VALUES (?, 'PKG-P9', 'HNY-P9', ?, 'ISSUED');",
                     (UNIT, unit_qr.ledger_module.sha256_hex(SECRET)))
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(product_units);")}
        assert not cols & set(PHASE9_COLUMNS), "setup bug: Phase 9 columns still present"
        cls.before = dict(conn.execute("SELECT * FROM product_units;").fetchone())
        conn.close()
        db.initialise_database()                         # the real migration path

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        config.DATABASE_PATH = cls._orig
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_unit_survives_with_null_qr_columns(self) -> None:
        row = dict(db.query_one("SELECT * FROM product_units WHERE unit_id = ?;", (UNIT,)))
        for key, value in self.before.items():
            self.assertEqual(row[key], value, key)
        for col in PHASE9_COLUMNS:
            self.assertIsNone(row[col], col)

    def test_legacy_unit_is_not_authentic_until_reissued(self) -> None:
        payload = unit_qr.payload(UNIT, SECRET)
        self.assertEqual(consumer.verify(payload)["result"], "INVALID")
        self.assertTrue(unit_qr.secret_matches(SECRET, self.before["secret_hash"]))

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM product_units;")["n"], 1)
