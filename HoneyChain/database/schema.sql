-- ============================================================================
-- HoneyChain -- SQLite schema (production)
-- SIH 2026 Problem Statement 26021
--
-- Applied automatically at Flask start-up by honeychain.db.initialise_database().
-- Every statement is idempotent (CREATE ... IF NOT EXISTS), so re-running this
-- file against a populated database never destroys data. There is deliberately
-- no DROP TABLE anywhere in this file.
--
-- The historical PostgreSQL research schema is preserved unchanged at
-- the original PostgreSQL research schema (since removed); it modelled the offline
-- inside/outside environmental study and is not used by the live service.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- hives -- one row per physical hive node
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id     TEXT    NOT NULL UNIQUE,          -- external id, e.g. 'HIVE-001'
    name        TEXT,                             -- human label
    location    TEXT,                             -- apiary / GPS description
    status      TEXT    NOT NULL DEFAULT 'ACTIVE' -- ACTIVE / INACTIVE / RETIRED
                CHECK (status IN ('ACTIVE', 'INACTIVE', 'RETIRED')),
    created_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW'))
);

CREATE INDEX IF NOT EXISTS idx_hives_hive_id ON hives (hive_id);
CREATE INDEX IF NOT EXISTS idx_hives_status  ON hives (status);

-- ---------------------------------------------------------------------------
-- telemetry_readings -- raw sensor payloads received from ESP32 / Firebase
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id         INTEGER NOT NULL REFERENCES hives (id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    temperature     REAL,     -- degrees Celsius
    humidity        REAL,     -- percent relative humidity
    weight          REAL,     -- kilograms (gross hive weight)
    audio_frequency REAL,     -- hertz (dominant colony acoustic frequency)
    battery_level   REAL,     -- percent
    received_at     TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW'))
);

-- The dashboard, the SSE bootstrap query and the quality scorer all read
-- "most recent rows for a hive", which this composite index serves directly.
CREATE INDEX IF NOT EXISTS idx_telemetry_hive_time
    ON telemetry_readings (hive_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_time
    ON telemetry_readings (timestamp DESC);

-- ---------------------------------------------------------------------------
-- health_evaluations -- one row per telemetry reading, written in the same
-- transaction as the reading so the two can never diverge
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_evaluations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hive_id       INTEGER NOT NULL REFERENCES hives (id) ON DELETE CASCADE,
    telemetry_id  INTEGER NOT NULL UNIQUE
                          REFERENCES telemetry_readings (id) ON DELETE CASCADE,
    timestamp     TEXT    NOT NULL,
    health_status TEXT    NOT NULL
                  CHECK (health_status IN ('HEALTHY', 'WARNING', 'CRITICAL')),
    anomaly_score REAL,     -- IsolationForest decision_function; NULL when ML is unavailable
    prediction    TEXT,     -- raw ML verdict: HEALTHY / CRITICAL / NULL
    rule_status   TEXT,     -- verdict of the deterministic rule engine alone
    source        TEXT    NOT NULL DEFAULT 'RULE',  -- RULE / ML / RULE+ML
    reason        TEXT      -- human-readable justification (pipe separated)
);

CREATE INDEX IF NOT EXISTS idx_health_hive_time
    ON health_evaluations (hive_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_health_status
    ON health_evaluations (health_status);
CREATE INDEX IF NOT EXISTS idx_health_telemetry
    ON health_evaluations (telemetry_id);

-- ---------------------------------------------------------------------------
-- honey_batches -- the HoneyBlockLedger. Each row IS a block: it stores its own
-- SHA-256 hash plus the hash of the block before it, forming a tamper-evident
-- chain. `block_index` is the position in the chain (0 = genesis batch).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS honey_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_index   INTEGER NOT NULL UNIQUE,
    batch_id      TEXT    NOT NULL UNIQUE,
    hive_id       TEXT    NOT NULL,      -- external hive id, kept as text so the
                                         -- block payload is self-describing
    harvest_date  TEXT    NOT NULL,      -- ISO-8601 date
    total_weight  REAL    NOT NULL,      -- kilograms
    floral_type   TEXT    NOT NULL,
    quality_score REAL    NOT NULL,      -- 0-100, derived from real telemetry
    quality_detail TEXT,                 -- JSON breakdown of the score inputs
    timestamp     TEXT    NOT NULL,      -- block creation time (UTC, ISO-8601)
    previous_hash TEXT    NOT NULL,
    block_hash    TEXT    NOT NULL,
    qr_path       TEXT,                  -- static-relative path of the PNG
    anchor_tx_hash TEXT                  -- optional Ethereum anchor transaction
);

CREATE INDEX IF NOT EXISTS idx_batches_batch_id ON honey_batches (batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_index    ON honey_batches (block_index);
CREATE INDEX IF NOT EXISTS idx_batches_hive     ON honey_batches (hive_id);

-- ============================================================================
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

-- ============================================================================
-- PHASE 3 ADDITIONS -- authentication token revocation.
--
-- `users`, `organizations`, and every role/status CHECK constraint already
-- exist from Phase 2 -- nothing about the user model needed to change to
-- support real authentication. The one new thing login/logout genuinely
-- needs is somewhere to record "this specific token was explicitly logged
-- out before its natural expiry", since bearer tokens are stateless
-- (itsdangerous-signed) and otherwise remain valid until they expire on
-- their own.
-- ============================================================================
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti         TEXT    PRIMARY KEY,   -- the token's unique id, not the token itself
    revoked_at  TEXT    NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%S', 'NOW')),
    expires_at  TEXT                   -- when the row itself can be garbage-collected
);

CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expires ON revoked_tokens (expires_at);

-- ============================================================================
-- PHASE 4 ADDITIONS -- per-device IoT authentication.
--
-- device_nonces records every (device, nonce) pair accepted inside the
-- freshness window; the PRIMARY KEY is what makes a replayed request fail
-- even under concurrent submission. Rows older than twice the window are
-- purged (see honeychain.db.purge_device_nonces) because the signed
-- timestamp check already rejects anything that old.
--
-- New COLUMNS on existing tables (telemetry_readings.device_id,
-- .device_timestamp, .sensor_flags; devices.credential_version,
-- .expected_interval_seconds) are added by the _MIGRATIONS mechanism in
-- honeychain/db.py, not here, so pre-Phase-4 databases upgrade in place.
-- ============================================================================
CREATE TABLE IF NOT EXISTS device_nonces (
    device_id   INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    nonce       TEXT    NOT NULL,
    seen_at     TEXT    NOT NULL,
    PRIMARY KEY (device_id, nonce)
);

CREATE INDEX IF NOT EXISTS idx_device_nonces_seen ON device_nonces (seen_at);

-- ============================================================================
-- PHASE 6 ADDITIONS -- the canonical chain-event log.
--
-- ONE append-only, hash-linked log of every meaningful traceability event
-- (BATCH_CREATED now; PROCESSING_RECORDED, LAB_RESULT_RECORDED, PACKAGED,
-- CUSTODY_TRANSFERRED, SHIPPED, RECEIVED, RETAIL_REGISTERED, RECALLED as the
-- later phases emit them). Every event goes through honeychain.chain_events;
-- no module talks to the blockchain on its own.
--
-- * seq is contiguous from 1 and assigned inside the caller's transaction.
-- * payload holds identifiers and hashes only (never personal data); its
--   canonical SHA-256 is payload_hash.
-- * event_hash covers seq, ids, occurred_at, payload_hash and previous_hash,
--   so editing, deleting or reordering any event breaks the chain.
-- * (event_type, resource_type, resource_id) is UNIQUE: emitting the same
--   event twice returns the original instead of creating a duplicate.
--
-- Only Merkle ROOTS over ranges of event_hash values go on-chain
-- (blockchain_checkpoints -> HoneyChainRegistry.anchorCheckpoint).
-- ============================================================================
CREATE TABLE IF NOT EXISTS chain_events (
    seq             INTEGER PRIMARY KEY,
    event_id        TEXT    NOT NULL UNIQUE,
    event_type      TEXT    NOT NULL
                    CHECK (event_type IN ('BATCH_CREATED', 'PROCESSING_RECORDED',
                        'LAB_RESULT_RECORDED', 'PACKAGED', 'CUSTODY_TRANSFERRED',
                        'SHIPPED', 'RECEIVED', 'RETAIL_REGISTERED', 'RECALLED')),
    resource_type   TEXT    NOT NULL,
    resource_id     TEXT    NOT NULL,
    organization_id TEXT,              -- external org id (e.g. ORG-KOLAR)
    actor_id        TEXT,              -- external user/device id, never a name
    occurred_at     TEXT    NOT NULL,
    payload         TEXT    NOT NULL,  -- canonical JSON: ids and hashes only
    payload_hash    TEXT    NOT NULL,
    previous_hash   TEXT    NOT NULL,
    event_hash      TEXT    NOT NULL UNIQUE,
    UNIQUE (event_type, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_chain_events_resource ON chain_events (resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_chain_events_org ON chain_events (organization_id);

-- ============================================================================
-- PHASE 8 ADDITIONS -- mutable supply-chain state for a batch.
--
-- honey_batches is the append-only ledger and is never updated for custody.
-- batch_state holds what legitimately changes: who currently holds the batch,
-- how much honey remains (processing loss, blending, packaging consume it),
-- and whether it is ACTIVE, fully CONSUMED (e.g. blended away) or RECALLED.
-- A batch created before Phase 8 has no row yet; honeychain.supply_chain
-- creates it on first use from the hive owner and the ledger weight.
-- ============================================================================
CREATE TABLE IF NOT EXISTS batch_state (
    batch_id        TEXT    PRIMARY KEY REFERENCES honey_batches (batch_id) ON DELETE RESTRICT,
    origin_org_id   INTEGER REFERENCES organizations (id) ON DELETE RESTRICT,
    holder_org_id   INTEGER NOT NULL REFERENCES organizations (id) ON DELETE RESTRICT,
    remaining_kg    REAL    NOT NULL CHECK (remaining_kg >= 0),
    status          TEXT    NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'CONSUMED', 'RECALLED')),
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_state_holder ON batch_state (holder_org_id);

-- ============================================================================
-- PHASE 10 ADDITIONS -- oversight.
--
-- consumer_scans: one row per public verification attempt (append-only).
-- Stores NO IP address or client data. unit_id is set only when the scanned
-- code names a unit that exists (also for a wrong/forged tag, which is the
-- point: repeated forged attempts against a real unit are a signal). It is
-- never returned by the public API.
-- ============================================================================
CREATE TABLE IF NOT EXISTS consumer_scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id     TEXT    REFERENCES product_units (unit_id) ON DELETE RESTRICT,
    result      TEXT    NOT NULL CHECK (result IN ('AUTHENTIC', 'REVOKED', 'INVALID')),
    reason      TEXT,
    scanned_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consumer_scans_unit ON consumer_scans (unit_id, scanned_at);
CREATE INDEX IF NOT EXISTS idx_consumer_scans_time ON consumer_scans (scanned_at);
