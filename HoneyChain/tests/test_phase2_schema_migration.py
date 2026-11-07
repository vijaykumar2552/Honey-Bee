"""
Phase 2 schema migration tests.

These tests are deliberately independent of ``tests/base.py``: that harness
always creates a fresh database from the *current* schema file, which cannot
prove anything about migrating an *existing, already-populated* database --
which is the actual risk in a schema change. This module builds a real
pre-Phase-2 database by hand (the exact schema HoneyChain shipped with before
this change), puts real rows in it, then runs the current
``honeychain.db.initialise_database()`` against that file and checks what
happened.

Five things must all be true:
  1. Migration completes without error against a populated pre-Phase-2 DB.
  2. Every pre-existing row in every original table is still there, unchanged.
  3. Every new Phase 2 table now exists with the expected shape.
  4. Foreign keys on the new tables are actually enforced.
  5. Unique constraints on the new tables are actually enforced.

A sixth check proves the migration is idempotent, and a seventh proves the
existing Flask API still serves correctly against a database that was
migrated (not just freshly created) from pre-existing data.

Implementation note: this module reuses the SAME ``honeychain.config`` /
``honeychain.db`` module objects the rest of the test suite already imported,
via monkeypatching ``config.DATABASE_PATH`` for the duration of each test
class and restoring it afterwards. An earlier version of this file instead
popped ``honeychain.*`` out of ``sys.modules`` to force a "fresh" reimport
with a different ``HONEYCHAIN_DB_PATH`` -- that corrupted shared module state
for every test module that ran afterward in the same process (a real bug,
caught by running the full suite, not just this file, in isolation). Directly
patching the one attribute the code actually reads has no such side effect.
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

# This is a snapshot of database/schema.sql exactly as it existed at the end
# of Phase 1, byte-for-byte -- not imported from the live file, on purpose.
# The whole point of this test is to simulate a real user's existing database
# that was created *before* this migration existed, so it must not silently
# track whatever the schema file happens to say today.
PRE_PHASE2_SCHEMA = """
CREATE TABLE IF NOT EXISTS hives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id     TEXT    NOT NULL UNIQUE,
    name        TEXT,
    location    TEXT,
    status      TEXT    NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'INACTIVE', 'RETIRED')),
    created_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);
CREATE INDEX IF NOT EXISTS idx_hives_hive_id ON hives (hive_id);
CREATE INDEX IF NOT EXISTS idx_hives_status  ON hives (status);

CREATE TABLE IF NOT EXISTS telemetry_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id         INTEGER NOT NULL REFERENCES hives (id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    temperature     REAL,
    humidity        REAL,
    weight          REAL,
    audio_frequency REAL,
    battery_level   REAL,
    received_at     TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW'))
);
CREATE INDEX IF NOT EXISTS idx_telemetry_hive_time
    ON telemetry_readings (hive_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_time
    ON telemetry_readings (timestamp DESC);

CREATE TABLE IF NOT EXISTS health_evaluations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id       INTEGER NOT NULL REFERENCES hives (id) ON DELETE CASCADE,
    telemetry_id  INTEGER NOT NULL UNIQUE
                          REFERENCES telemetry_readings (id) ON DELETE CASCADE,
    timestamp     TEXT    NOT NULL,
    health_status TEXT    NOT NULL
                  CHECK (health_status IN ('HEALTHY', 'WARNING', 'CRITICAL')),
    anomaly_score REAL,
    prediction    TEXT,
    rule_status   TEXT,
    source        TEXT    NOT NULL DEFAULT 'RULE',
    reason        TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_hive_time
    ON health_evaluations (hive_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_health_status
    ON health_evaluations (health_status);
CREATE INDEX IF NOT EXISTS idx_health_telemetry
    ON health_evaluations (telemetry_id);

CREATE TABLE IF NOT EXISTS honey_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_index   INTEGER NOT NULL UNIQUE,
    batch_id      TEXT    NOT NULL UNIQUE,
    hive_id       TEXT    NOT NULL,
    harvest_date  TEXT    NOT NULL,
    total_weight  REAL    NOT NULL,
    floral_type   TEXT    NOT NULL,
    quality_score REAL    NOT NULL,
    quality_detail TEXT,
    timestamp     TEXT    NOT NULL,
    previous_hash TEXT    NOT NULL,
    block_hash    TEXT    NOT NULL,
    qr_path       TEXT,
    anchor_tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_batch_id ON honey_batches (batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_index    ON honey_batches (block_index);
CREATE INDEX IF NOT EXISTS idx_batches_hive     ON honey_batches (hive_id);
"""

ORIGINAL_TABLES = ("hives", "telemetry_readings", "health_evaluations", "honey_batches")

PHASE2_TABLES = (
    "organizations",
    "users",
    "devices",
    "harvest_events",
    "batch_harvest_links",
    "processing_events",
    "batch_blend_inputs",
    "lab_results",
    "packaging_events",
    "product_units",
    "custody_transfers",
    "shipments",
    "retail_records",
    "recalls",
    "blockchain_checkpoints",
    "audit_log",
    "notifications",
)


def _raw_connect(path: Path) -> sqlite3.Connection:
    """A bare connection -- deliberately not honeychain.db.get_connection(),
    since that's per-thread and cached; this test wants a fresh, disposable
    connection to a file it fully controls."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


class TestExistingDatabaseMigratesSafely(unittest.TestCase):
    """Simulates a real user's pre-Phase-2 database and migrates it in place."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_dir = Path(tempfile.mkdtemp(prefix="hc-migration-"))
        cls.db_path = cls._tmp_dir / "pre_existing.db"

        # Build a genuine pre-Phase-2 database and put real rows in it --
        # exactly what a user upgrading from Phase 1 actually has on disk.
        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE2_SCHEMA)
        conn.execute(
            "INSERT INTO hives (hive_id, name, location, status) "
            "VALUES ('HIVE-001', 'North apiary hive 1', 'Warangal', 'ACTIVE');"
        )
        conn.execute(
            "INSERT INTO telemetry_readings "
            "(hive_id, timestamp, temperature, humidity, weight, audio_frequency) "
            "VALUES (1, '2026-09-01 08:00:00', 34.6, 61.0, 42.1, 258.0);"
        )
        conn.execute(
            "INSERT INTO health_evaluations "
            "(hive_id, telemetry_id, timestamp, health_status, source) "
            "VALUES (1, 1, '2026-09-01 08:00:00', 'HEALTHY', 'RULE');"
        )
        conn.execute(
            "INSERT INTO honey_batches "
            "(block_index, batch_id, hive_id, harvest_date, total_weight, "
            " floral_type, quality_score, timestamp, previous_hash, block_hash) "
            "VALUES (0, 'HNY-2026-0001', 'HIVE-001', '2026-09-01', 12.5, "
            " 'Mustard', 91.4, '2026-09-01 09:00:00', "
            " '0000000000000000000000000000000000000000000000000000000000000000', "
            " 'abc123deadbeef00000000000000000000000000000000000000000000000000');"
        )
        conn.commit()
        conn.close()

        # Record the pre-migration state so we can compare byte-for-byte later.
        conn = _raw_connect(cls.db_path)
        cls.pre_migration_rows = {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table};").fetchall()]
            for table in ORIGINAL_TABLES
        }
        conn.close()

        # Point the real application config at this exact file -- monkeypatch
        # the one attribute the code reads, don't touch sys.modules.
        cls._original_database_path = config.DATABASE_PATH
        config.DATABASE_PATH = cls.db_path
        db.close_connection()  # drop any cached connection to the old path
        db.initialise_database()  # the same function Flask calls at startup

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        config.DATABASE_PATH = cls._original_database_path
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    # -- 1. migration completes without error is implied by setUpClass not
    #       raising; every test below runs only if it succeeded.

    def test_original_tables_still_exist(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            for table in ORIGINAL_TABLES:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
                    (table,),
                ).fetchone()
                self.assertIsNotNone(row, f"{table} disappeared after migration")
        finally:
            conn.close()

    def test_existing_rows_survive_unchanged(self) -> None:
        """Every field that existed *before* migration must still hold the
        same value afterward. This intentionally does NOT require the row's
        set of columns to stay identical: a later, purely additive migration
        (e.g. Phase 3 adding a nullable ``hives.organization_id`` column) is
        expected and correct, and must not make this test fail forever after.
        What must never happen is an existing field's value changing or
        disappearing -- that's what is actually checked here.
        """
        conn = _raw_connect(self.db_path)
        try:
            for table in ORIGINAL_TABLES:
                after_rows = {
                    r["id"]: dict(r)
                    for r in conn.execute(f"SELECT * FROM {table};").fetchall()
                }
                for before_row in self.pre_migration_rows[table]:
                    after_row = after_rows.get(before_row["id"])
                    self.assertIsNotNone(
                        after_row, f"{table} row id={before_row['id']} disappeared"
                    )
                    for key, value in before_row.items():
                        self.assertEqual(
                            after_row.get(key),
                            value,
                            f"{table}.{key} changed for row id={before_row['id']} "
                            f"({value!r} -> {after_row.get(key)!r})",
                        )
        finally:
            conn.close()

    def test_new_phase2_tables_were_created(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            for table in PHASE2_TABLES:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
                    (table,),
                ).fetchone()
                self.assertIsNotNone(row, f"{table} was not created by the migration")
        finally:
            conn.close()

    def test_foreign_keys_on_new_tables_are_enforced(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                # hive_id 999999 does not exist -- must be rejected, not silently accepted.
                conn.execute(
                    "INSERT INTO harvest_events "
                    "(harvest_id, hive_id, harvest_date, quantity_kg) "
                    "VALUES ('HRV-TEST-001', 999999, '2026-09-15', 10.0);"
                )
                conn.commit()
        finally:
            conn.close()

    def test_foreign_key_succeeds_against_a_real_row(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            # hive pk 1 genuinely exists (it's the pre-existing seeded hive) --
            # a *valid* reference must be accepted, not just rejection tested.
            conn.execute(
                "INSERT INTO harvest_events "
                "(harvest_id, hive_id, harvest_date, quantity_kg) "
                "VALUES ('HRV-TEST-OK', 1, '2026-09-15', 10.0);"
            )
            conn.commit()
            row = conn.execute(
                "SELECT hive_id FROM harvest_events WHERE harvest_id = 'HRV-TEST-OK';"
            ).fetchone()
            self.assertEqual(row["hive_id"], 1)
        finally:
            conn.execute("DELETE FROM harvest_events WHERE harvest_id = 'HRV-TEST-OK';")
            conn.commit()
            conn.close()

    def test_unique_constraints_on_new_tables_are_enforced(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO organizations (org_id, name, org_type) "
                "VALUES ('ORG-DUPTEST', 'Test Org', 'BEEKEEPER');"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO organizations (org_id, name, org_type) "
                    "VALUES ('ORG-DUPTEST', 'Different Name', 'PROCESSOR');"
                )
                conn.commit()
        finally:
            conn.execute("DELETE FROM organizations WHERE org_id = 'ORG-DUPTEST';")
            conn.commit()
            conn.close()

    def test_check_constraint_on_role_is_enforced(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users (user_id, email, password_hash, name, role) "
                    "VALUES ('USR-BAD', 'bad@example.com', 'x', 'Bad Role User', "
                    " 'NOT_A_REAL_ROLE');"
                )
                conn.commit()
        finally:
            conn.close()

    def test_migration_is_idempotent(self) -> None:
        """Running the migration a second time must not error or duplicate anything."""
        conn = _raw_connect(self.db_path)
        before = {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table};").fetchall()]
            for table in ORIGINAL_TABLES + PHASE2_TABLES
        }
        conn.close()

        db.close_connection()
        db.initialise_database()  # run it again, against the same file

        conn = _raw_connect(self.db_path)
        try:
            for table in ORIGINAL_TABLES + PHASE2_TABLES:
                after = [dict(r) for r in conn.execute(f"SELECT * FROM {table};").fetchall()]
                self.assertEqual(
                    after, before[table], f"{table} changed after re-running the migration"
                )
        finally:
            conn.close()

    def test_existing_api_still_works_against_the_migrated_database(self) -> None:
        """The Flask app, pointed at this genuinely-migrated (not freshly
        created) database, must still serve its existing routes correctly.

        /api/hives and /api/batches became authenticated in Phase 3, so this
        test creates a real admin user directly in the migrated database
        (there were none before -- this pre-existing DB predates the users
        table entirely) and authenticates as them, exactly as a real deployer
        upgrading from Phase 1 would have to.
        """
        from honeychain import auth as auth_module
        from honeychain.app import create_app

        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id="USR-MIGRATION-ADMIN",
                organization_pk=None,
                email="migration-admin@honeychain.test",
                password_hash=auth_module.hash_password("irrelevant-not-logged-in-with"),
                name="Migration Test Admin",
                role="ADMIN",
            )
        admin_row = db.get_user_by_user_id("USR-MIGRATION-ADMIN")
        token, _jti = auth_module.generate_token(admin_row)
        auth_headers = {"Authorization": f"Bearer {token}"}

        app = create_app(initialise_db=False, load_model=False)
        app.config["TESTING"] = True
        client = app.test_client()

        # Public endpoints: no auth required, must still be 200.
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)

        response = client.get("/api/chain")
        self.assertEqual(response.status_code, 200)

        # Authenticated endpoints: 401 with no token, 200 with a valid one.
        response = client.get("/api/hives")
        self.assertEqual(response.status_code, 401)

        response = client.get("/api/hives", headers=auth_headers)
        self.assertEqual(response.status_code, 200)
        hives = response.get_json()
        self.assertTrue(
            any(h["hive_id"] == "HIVE-001" for h in hives),
            "the pre-existing seeded hive should still be visible through the API",
        )

        response = client.get("/api/batches", headers=auth_headers)
        self.assertEqual(response.status_code, 200)
        batches = response.get_json()
        self.assertTrue(
            any(b["batch_id"] == "HNY-2026-0001" for b in batches),
            "the pre-existing seeded batch should still be visible through the API",
        )


if __name__ == "__main__":
    unittest.main()
