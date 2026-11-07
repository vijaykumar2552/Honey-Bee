"""
Phase 3 schema migration tests.

Same reasoning and pattern as tests/test_phase2_schema_migration.py: build a
genuine PRE-PHASE-3 database by hand (the original 4 tables plus every Phase
2 table, exactly as database/schema.sql had it before this phase), seed real
rows -- including a hive with no organization_id at all, since that column
did not exist yet -- then run the real initialise_database() against it and
check what happened.

Covers:
  1. Migration completes without error against a populated pre-Phase-3 DB.
  2. The existing hive row survives, and organization_id appears as NULL
     (not an error, not a default value that pretends to know an owner).
  3. revoked_tokens now exists.
  4. hives.organization_id enforces its foreign key when set to a real value,
     and rejects a bogus one.
  5. revoked_tokens.jti PRIMARY KEY uniqueness is enforced.
  6. The migration is idempotent.
  7. The existing Flask API still works against the migrated database.
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
from tests.test_phase2_schema_migration import PRE_PHASE2_SCHEMA, _raw_connect  # noqa: E402

# The exact Phase 2 additions, verbatim from database/schema.sql, frozen here
# for the same reason PRE_PHASE2_SCHEMA is frozen in the Phase 2 test module:
# this must represent what a real pre-Phase-3 database looked like, not
# silently track whatever the schema file says today.
PRE_PHASE3_SCHEMA = PRE_PHASE2_SCHEMA + """
-- PHASE 2 ADDITIONS -- schema foundation for auth/RBAC, IoT device identity,
-- the supply-chain workflow, QR/product-unit identity, blockchain checkpoints,
-- audit logging and notifications (added on top of the schema above; nothing
-- above this line was changed).
--
-- Same rules as the rest of this file: every statement is idempotent
-- (CREATE TABLE IF NOT EXISTS), there is no DROP anywhere, and re-running this
-- file against an already-migrated or a pre-Phase-2 database is always safe.
--
-- Deliberate simplifications made in this pass (documented, not hidden):
--   * "roles/permissions" is a single CHECK-constrained `role` column on
--     `users`, matching the existing convention used for `hives.status` and
--     `health_evaluations.health_status`, rather than a separate
--     roles/permissions join-table system. A real permissions table is easy
--     to add later (Phase 4) if a role's access needs to be split more finely
--     than "one of nine fixed roles" -- nothing here blocks that.
--   * "QR identities" is not a separate table: `product_units` already *is*
--     the QR identity (external `unit_id` + `secret_hash`), so a second table
--     would just duplicate it.
--   * `honey_batches` is left completely untouched, including its existing
--     unconstrained `hive_id TEXT` column (see the Engineering Audit, finding
--     DB-02) -- fixing that FK is supply-chain business logic, not schema
--     foundation, and changing it here risked existing data/behaviour for no
--     Phase-2 benefit. `batch_harvest_links` below is the additive way a
--     later phase attaches real harvest provenance to a batch without ever
--     touching this column.
--
-- No route, authentication, or business logic reads or writes any table below
-- yet -- that is later phases. This migration only establishes the schema.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- organizations -- every actor in the supply chain belongs to one
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      TEXT    NOT NULL UNIQUE,        -- external id, e.g. 'ORG-0001'
    name        TEXT    NOT NULL,
    org_type    TEXT    NOT NULL
                CHECK (org_type IN ('BEEKEEPER', 'FPO_AGGREGATOR', 'PROCESSOR',
                                     'LAB', 'LOGISTICS', 'RETAILER', 'AUTHORITY',
                                     'ADMIN')),
    status      TEXT    NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DISABLED')),
    created_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    updated_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_orgs_org_id ON organizations (org_id);
CREATE INDEX IF NOT EXISTS idx_orgs_type   ON organizations (org_type);
CREATE INDEX IF NOT EXISTS idx_orgs_status ON organizations (status);

-- ---------------------------------------------------------------------------
-- users -- schema only; no auth logic reads/writes this table yet (Phase 3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL UNIQUE,     -- external id, e.g. 'USR-0001'
    organization_id INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    email           TEXT    NOT NULL UNIQUE,
    phone           TEXT    UNIQUE,
    password_hash   TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    role            TEXT    NOT NULL
                    CHECK (role IN ('ADMIN', 'BEEKEEPER', 'FPO_AGGREGATOR',
                                     'PROCESSOR', 'LAB', 'LOGISTICS', 'RETAILER',
                                     'AUTHORITY', 'CONSUMER')),
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DISABLED')),
    last_login_at   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    updated_at      TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_users_user_id ON users (user_id);
CREATE INDEX IF NOT EXISTS idx_users_org     ON users (organization_id);
CREATE INDEX IF NOT EXISTS idx_users_role    ON users (role);
CREATE INDEX IF NOT EXISTS idx_users_email   ON users (email);

-- ---------------------------------------------------------------------------
-- devices -- per-device identity, replacing the fleet-wide shared secret
-- (credential_hash is populated starting in the IoT-security phase; the
-- column exists now so that phase is additive, not another migration)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'DEV-0001'
    hive_id             INTEGER REFERENCES hives (id) ON DELETE SET NULL,
    organization_id     INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    credential_hash     TEXT,
    firmware_version    TEXT,
    status              TEXT    NOT NULL DEFAULT 'PROVISIONED'
                        CHECK (status IN ('PROVISIONED', 'ACTIVE', 'OFFLINE',
                                            'FAULTY', 'REVOKED')),
    last_seen_at        TEXT,
    battery_level       REAL,
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    updated_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_devices_device_id ON devices (device_id);
CREATE INDEX IF NOT EXISTS idx_devices_hive      ON devices (hive_id);
CREATE INDEX IF NOT EXISTS idx_devices_org       ON devices (organization_id);

-- ---------------------------------------------------------------------------
-- harvest_events -- a harvest is its own event, separate from the batch it
-- feeds into (a batch may blend multiple harvests -- see batch_harvest_links)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS harvest_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    harvest_id          TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'HRV-2026-0001'
    hive_id             INTEGER NOT NULL REFERENCES hives (id) ON DELETE RESTRICT,
    beekeeper_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    organization_id     INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    harvest_date        TEXT    NOT NULL,
    quantity_kg         REAL    NOT NULL,
    floral_source       TEXT,
    latitude            REAL,
    longitude           REAL,
    frame_count         INTEGER,
    notes               TEXT,
    status              TEXT    NOT NULL DEFAULT 'RECORDED'
                        CHECK (status IN ('RECORDED', 'LINKED_TO_BATCH', 'VOID')),
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_harvest_hive ON harvest_events (hive_id);
CREATE INDEX IF NOT EXISTS idx_harvest_org  ON harvest_events (organization_id);
CREATE INDEX IF NOT EXISTS idx_harvest_date ON harvest_events (harvest_date);

-- ---------------------------------------------------------------------------
-- batch_harvest_links -- many-to-many: a batch may be a blend of several
-- harvests. This is the additive link between harvest_events and the
-- existing, untouched honey_batches table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS batch_harvest_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE CASCADE,
    harvest_id  TEXT    NOT NULL REFERENCES harvest_events (harvest_id) ON DELETE RESTRICT,
    weight_kg   REAL,
    UNIQUE (batch_id, harvest_id)
);

CREATE INDEX IF NOT EXISTS idx_bhl_batch   ON batch_harvest_links (batch_id);
CREATE INDEX IF NOT EXISTS idx_bhl_harvest ON batch_harvest_links (harvest_id);

-- ---------------------------------------------------------------------------
-- processing_events -- filtering/heating/blending/settling/storage.
-- input_batch_id covers the simple one-input case; a BLENDING event with
-- multiple inputs uses batch_blend_inputs below instead.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processing_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    processing_id           TEXT    NOT NULL UNIQUE,  -- external id, e.g. 'PRC-0001'
    input_batch_id          TEXT    REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    output_batch_id         TEXT    REFERENCES honey_batches (batch_id) ON DELETE SET NULL,
    processor_org_id        INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    performed_by_user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    process_type            TEXT    NOT NULL
                            CHECK (process_type IN ('FILTERING', 'HEATING', 'BLENDING',
                                                      'SETTLING', 'STORAGE')),
    started_at              TEXT,
    completed_at            TEXT,
    temperature_c           REAL,
    equipment               TEXT,
    notes                   TEXT,
    created_at              TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_processing_input  ON processing_events (input_batch_id);
CREATE INDEX IF NOT EXISTS idx_processing_output ON processing_events (output_batch_id);

-- ---------------------------------------------------------------------------
-- batch_blend_inputs -- many-to-many: a BLENDING processing_event may combine
-- several input batches into one output batch.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS batch_blend_inputs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    processing_id   TEXT    NOT NULL REFERENCES processing_events (processing_id) ON DELETE CASCADE,
    input_batch_id  TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    weight_kg       REAL,
    UNIQUE (processing_id, input_batch_id)
);

CREATE INDEX IF NOT EXISTS idx_bbi_processing ON batch_blend_inputs (processing_id);

-- ---------------------------------------------------------------------------
-- lab_results -- real laboratory quality data, deliberately separate from
-- the telemetry-derived quality_score already on honey_batches (Audit R16:
-- hive temperature is not evidence of honey adulteration)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lab_results (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lab_result_id       TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'LAB-0001'
    batch_id            TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    lab_org_id          INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    technician_user_id  INTEGER REFERENCES users (id) ON DELETE SET NULL,
    test_date           TEXT,
    moisture_pct        REAL,
    hmf_mg_kg           REAL,
    c4_sugar_pct        REAL,
    pollen_analysis     TEXT,
    colour              TEXT,
    acidity             REAL,
    certificate_id      TEXT,
    certificate_url     TEXT,
    status              TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'PASSED', 'FAILED', 'REVIEW')),
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_lab_batch  ON lab_results (batch_id);
CREATE INDEX IF NOT EXISTS idx_lab_status ON lab_results (status);

-- ---------------------------------------------------------------------------
-- packaging_events + product_units -- product_units IS the QR identity: a
-- unique external unit_id plus a secret_hash for cryptographic per-unit QR
-- verification (populated starting in the QR-security phase).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS packaging_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    packaging_id            TEXT    NOT NULL UNIQUE,  -- external id, e.g. 'PKG-0001'
    batch_id                TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    packaged_by_user_id     INTEGER REFERENCES users (id) ON DELETE SET NULL,
    organization_id         INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    unit_size_g             INTEGER,
    unit_count              INTEGER NOT NULL DEFAULT 0,
    packed_at               TEXT,
    best_before             TEXT,
    created_at              TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_packaging_batch ON packaging_events (batch_id);

CREATE TABLE IF NOT EXISTS product_units (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id             TEXT    NOT NULL UNIQUE,   -- public-facing, e.g. 'UNIT-000001'
    packaging_id        TEXT    NOT NULL REFERENCES packaging_events (packaging_id) ON DELETE RESTRICT,
    batch_id            TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    secret_hash         TEXT,
    status              TEXT    NOT NULL DEFAULT 'ISSUED'
                        CHECK (status IN ('ISSUED', 'ACTIVATED', 'SOLD', 'RECALLED')),
    scan_count          INTEGER NOT NULL DEFAULT 0,
    first_scanned_at    TEXT,
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_units_packaging ON product_units (packaging_id);
CREATE INDEX IF NOT EXISTS idx_units_batch     ON product_units (batch_id);

-- ---------------------------------------------------------------------------
-- custody_transfers -- two-party acknowledgement (sender cannot accept on the
-- receiver's behalf; that rule is enforced in the API layer, not here)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS custody_transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id     TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'CUS-0001'
    batch_id        TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    from_org_id     INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    to_org_id       INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    quantity_kg     REAL,
    status          TEXT    NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED')),
    initiated_at    TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_custody_batch  ON custody_transfers (batch_id);
CREATE INDEX IF NOT EXISTS idx_custody_status ON custody_transfers (status);

-- ---------------------------------------------------------------------------
-- shipments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id             TEXT    NOT NULL UNIQUE,  -- external id, e.g. 'SHP-0001'
    batch_id                TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    custody_transfer_id     TEXT    REFERENCES custody_transfers (transfer_id) ON DELETE SET NULL,
    origin                  TEXT,
    destination             TEXT,
    vehicle                 TEXT,
    driver                  TEXT,
    departure_at            TEXT,
    arrival_at              TEXT,
    status                  TEXT    NOT NULL DEFAULT 'CREATED'
                            CHECK (status IN ('CREATED', 'IN_TRANSIT', 'DELIVERED', 'EXCEPTION')),
    max_temperature_c       REAL,
    min_temperature_c       REAL,
    created_at              TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_shipments_batch ON shipments (batch_id);

-- ---------------------------------------------------------------------------
-- retail_records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retail_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    retail_record_id    TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'RTL-0001'
    batch_id            TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    retailer_org_id     INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    status              TEXT    NOT NULL DEFAULT 'EXPECTED'
                        CHECK (status IN ('EXPECTED', 'RECEIVED', 'ON_SHELF', 'SOLD', 'RECALLED')),
    received_at         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_retail_batch ON retail_records (batch_id);

-- ---------------------------------------------------------------------------
-- recalls -- a consumer QR verification for a recalled batch must show this
-- immediately (business logic for that comes later; the table exists now)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recalls (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id           TEXT    NOT NULL UNIQUE,    -- external id, e.g. 'RCL-0001'
    batch_id            TEXT    NOT NULL REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    reason              TEXT    NOT NULL,
    issued_by_user_id   INTEGER REFERENCES users (id) ON DELETE SET NULL,
    status              TEXT    NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'RESOLVED', 'CANCELLED')),
    issued_at           TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_recalls_batch ON recalls (batch_id);

-- ---------------------------------------------------------------------------
-- blockchain_checkpoints -- generalised anchor record. honey_batches keeps
-- its own anchor_tx_hash column for backward compatibility; this table is
-- the forward-looking home for anchoring any entity type, not just batches.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS blockchain_checkpoints (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id       TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'CHK-0001'
    checkpoint_type     TEXT    NOT NULL,          -- e.g. 'BATCH_ANCHOR', 'CHAIN_CHECKPOINT'
    entity_type         TEXT    NOT NULL,          -- e.g. 'honey_batches'
    entity_id           TEXT    NOT NULL,          -- e.g. the batch_id
    tx_hash             TEXT,
    block_number        INTEGER,
    chain_id            INTEGER,
    status              TEXT    NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'SUBMITTED', 'CONFIRMED', 'FAILED')),
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    confirmed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_bc_entity ON blockchain_checkpoints (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_bc_status ON blockchain_checkpoints (status);

-- ---------------------------------------------------------------------------
-- audit_log -- immutable application audit trail (no UPDATE/DELETE path is
-- provided anywhere in the application by design)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id       INTEGER REFERENCES users (id) ON DELETE SET NULL,
    organization_id     INTEGER REFERENCES organizations (id) ON DELETE SET NULL,
    action              TEXT    NOT NULL,
    resource_type       TEXT,
    resource_id         TEXT,
    metadata            TEXT,               -- JSON blob
    ip_address          TEXT,
    result              TEXT    NOT NULL DEFAULT 'SUCCESS'
                        CHECK (result IN ('SUCCESS', 'FAILURE')),
    occurred_at         TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_user_id);
CREATE INDEX IF NOT EXISTS idx_audit_time  ON audit_log (occurred_at DESC);

-- ---------------------------------------------------------------------------
-- notifications
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id     TEXT    NOT NULL UNIQUE,   -- external id, e.g. 'NTF-0001'
    recipient_user_id   INTEGER REFERENCES users (id) ON DELETE CASCADE,
    notification_type   TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    body                TEXT,
    read_at             TEXT,
    created_at          TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications (recipient_user_id);

"""

PHASE3_NEW_TABLES = ("revoked_tokens",)


class TestPhase3SchemaMigratesSafely(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp_dir = Path(tempfile.mkdtemp(prefix="hc-migration-p3-"))
        cls.db_path = cls._tmp_dir / "pre_phase3.db"

        conn = _raw_connect(cls.db_path)
        conn.executescript(PRE_PHASE3_SCHEMA)
        conn.execute(
            "INSERT INTO hives (hive_id, name, location, status) "
            "VALUES ('HIVE-PRE3', 'Pre-Phase-3 hive', 'Somewhere', 'ACTIVE');"
        )
        conn.execute(
            "INSERT INTO organizations (org_id, name, org_type, status, created_at, updated_at) "
            "VALUES ('ORG-PRE3', 'Pre-existing Org', 'BEEKEEPER', 'ACTIVE', "
            " '2026-09-01 00:00:00', '2026-09-01 00:00:00');"
        )
        conn.commit()
        conn.close()

        conn = _raw_connect(cls.db_path)
        cls.pre_hive_row = dict(
            conn.execute("SELECT * FROM hives WHERE hive_id = 'HIVE-PRE3';").fetchone()
        )
        conn.close()

        # Confirm organization_id genuinely does not exist yet in the
        # pre-migration schema -- otherwise this test would prove nothing.
        assert "organization_id" not in cls.pre_hive_row, (
            "test setup bug: PRE_PHASE3_SCHEMA already has hives.organization_id"
        )

        cls._original_database_path = config.DATABASE_PATH
        config.DATABASE_PATH = cls.db_path
        db.close_connection()
        db.initialise_database()

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        config.DATABASE_PATH = cls._original_database_path
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def test_existing_hive_survives_with_organization_id_null(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            row = dict(
                conn.execute("SELECT * FROM hives WHERE hive_id = 'HIVE-PRE3';").fetchone()
            )
            for key, value in self.pre_hive_row.items():
                self.assertEqual(row.get(key), value, f"hives.{key} changed")
            self.assertIn("organization_id", row)
            self.assertIsNone(row["organization_id"])
        finally:
            conn.close()

    def test_revoked_tokens_table_was_created(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='revoked_tokens';"
            ).fetchone()
            self.assertIsNotNone(found)
        finally:
            conn.close()

    def test_organization_id_foreign_key_accepts_a_real_organization(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            org_pk = conn.execute(
                "SELECT id FROM organizations WHERE org_id = 'ORG-PRE3';"
            ).fetchone()["id"]
            conn.execute(
                "UPDATE hives SET organization_id = ? WHERE hive_id = 'HIVE-PRE3';", (org_pk,)
            )
            conn.commit()
            row = conn.execute(
                "SELECT organization_id FROM hives WHERE hive_id = 'HIVE-PRE3';"
            ).fetchone()
            self.assertEqual(row["organization_id"], org_pk)
        finally:
            conn.execute(
                "UPDATE hives SET organization_id = NULL WHERE hive_id = 'HIVE-PRE3';"
            )
            conn.commit()
            conn.close()

    def test_organization_id_foreign_key_rejects_a_bogus_organization(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE hives SET organization_id = 999999 WHERE hive_id = 'HIVE-PRE3';"
                )
                conn.commit()
        finally:
            conn.close()

    def test_revoked_tokens_jti_is_a_unique_primary_key(self) -> None:
        conn = _raw_connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO revoked_tokens (jti, revoked_at) VALUES ('jti-dup-test', "
                "'2026-09-01 00:00:00');"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO revoked_tokens (jti, revoked_at) VALUES ('jti-dup-test', "
                    "'2026-09-01 01:00:00');"
                )
                conn.commit()
        finally:
            conn.execute("DELETE FROM revoked_tokens WHERE jti = 'jti-dup-test';")
            conn.commit()
            conn.close()

    def test_migration_is_idempotent(self) -> None:
        conn = _raw_connect(self.db_path)
        before = dict(
            conn.execute("SELECT * FROM hives WHERE hive_id = 'HIVE-PRE3';").fetchone()
        )
        conn.close()

        db.close_connection()
        db.initialise_database()

        conn = _raw_connect(self.db_path)
        try:
            after = dict(
                conn.execute("SELECT * FROM hives WHERE hive_id = 'HIVE-PRE3';").fetchone()
            )
            self.assertEqual(after, before)
        finally:
            conn.close()

    def test_existing_api_still_works_against_the_phase3_migrated_database(self) -> None:
        from honeychain import auth as auth_module
        from honeychain.app import create_app

        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id="USR-P3-MIGRATION-ADMIN",
                organization_pk=None,
                email="p3-migration-admin@honeychain.test",
                password_hash=auth_module.hash_password("irrelevant"),
                name="Phase 3 Migration Admin",
                role="ADMIN",
            )
        admin_row = db.get_user_by_user_id("USR-P3-MIGRATION-ADMIN")
        token, _jti = auth_module.generate_token(admin_row)
        headers = {"Authorization": f"Bearer {token}"}

        app = create_app(initialise_db=False, load_model=False)
        app.config["TESTING"] = True
        client = app.test_client()

        self.assertEqual(client.get("/api/health").status_code, 200)
        response = client.get("/api/hives", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(h["hive_id"] == "HIVE-PRE3" for h in response.get_json()))


if __name__ == "__main__":
    unittest.main()
