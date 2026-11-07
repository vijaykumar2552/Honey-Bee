"""
Phase 6 schema migration: a pre-Phase-6 database (Phase 5 state) holding a
batch and an old entity-level blockchain_checkpoints row upgrades in place.
The old row keeps its values, gains NULL Merkle columns / attempts=0, and is
never mistaken for a Merkle checkpoint; chain_events and the range-uniqueness
index appear; the migration is idempotent.
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

from honeychain import chain_events, config, db  # noqa: E402
from tests.test_phase2_schema_migration import _raw_connect  # noqa: E402
from tests.test_phase5_schema_migration import PRE_PHASE5_SCHEMA  # noqa: E402

PRE_PHASE6_SCHEMA = PRE_PHASE5_SCHEMA + """
ALTER TABLE harvest_events ADD COLUMN harvested_at TEXT;
ALTER TABLE harvest_events ADD COLUMN photo_sha256 TEXT;
ALTER TABLE harvest_events ADD COLUMN event_hash TEXT;
"""


class TestPhase6MigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = Path(tempfile.mkdtemp(prefix="hc-migration-p6-"))
        cls.db_path = cls._tmp / "pre_phase6.db"
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE6_SCHEMA)
        conn.execute(
            "INSERT INTO blockchain_checkpoints (checkpoint_id, checkpoint_type, entity_type, "
            " entity_id, tx_hash, status) VALUES ('CHK-OLD-1', 'BATCH_ANCHOR', 'honey_batches', "
            " 'HNY-OLD', '0xabc', 'CONFIRMED');")
        conn.commit()
        cls.before = dict(conn.execute("SELECT * FROM blockchain_checkpoints;").fetchone())
        assert "merkle_root" not in cls.before, "setup bug: already migrated"
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

    def test_old_checkpoint_row_survives_and_is_not_a_merkle_checkpoint(self) -> None:
        row = dict(db.query_one("SELECT * FROM blockchain_checkpoints WHERE checkpoint_id='CHK-OLD-1';"))
        for key, value in self.before.items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(row["attempts"], 0)
        for col in ("from_seq", "to_seq", "merkle_root", "last_error"):
            self.assertIsNone(row[col])
        self.assertEqual(chain_events.list_checkpoints(), [])

    def test_chain_events_table_and_constraints_exist(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            conn.execute("INSERT INTO chain_events (seq, event_id, event_type, resource_type, "
                         "resource_id, occurred_at, payload, payload_hash, previous_hash, event_hash) "
                         "VALUES (1,'EVT-1','BATCH_CREATED','honey_batches','HNY-1','t','{}','p','0','h1');")
            conn.commit()
            for bad in ("(2,'EVT-2','NOT_A_TYPE','x','y','t','{}','p','0','h2')",          # CHECK
                        "(3,'EVT-3','BATCH_CREATED','honey_batches','HNY-1','t','{}','p','0','h3')"):  # UNIQUE triple
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO chain_events (seq, event_id, event_type, resource_type, "
                                 "resource_id, occurred_at, payload, payload_hash, previous_hash, "
                                 f"event_hash) VALUES {bad};")
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';")}
            self.assertIn("idx_checkpoint_range", names)
        finally:
            conn.execute("DELETE FROM chain_events;"); conn.commit(); conn.close()

    def test_idempotent(self) -> None:
        db.close_connection()
        db.initialise_database()
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM blockchain_checkpoints;")["n"], 1)


if __name__ == "__main__":
    unittest.main()
