# Database

SQLite in WAL mode with foreign keys on. The schema lives in `database/schema.sql`
(idempotent `CREATE ... IF NOT EXISTS`, never `DROP`). Columns added to existing tables
are applied by `_MIGRATIONS` in `honeychain/db.py`; indexes on those columns are created
after the column migrations. `create_app()` runs both at start-up, so a new database and
an old one reach the same schema with no manual step.

Main tables: `organizations`, `users`, `revoked_tokens`, `hives`, `devices`,
`device_nonces`, `telemetry_readings`, `health_evaluations`, `harvest_events`,
`honey_batches` (ledger blocks), `batch_harvest_links`, `batch_state`,
`processing_events`, `batch_blend_inputs`, `lab_results`, `packaging_events`,
`product_units`, `custody_transfers`, `shipments`, `retail_records`, `recalls`,
`chain_events`, `blockchain_checkpoints`, `consumer_scans`, `audit_log`, `notifications`.

Invariants enforced by the database itself: foreign keys, CHECK-constrained statuses,
at most one pending custody transfer per batch, one retail record per shipment, one active
recall per batch, a unique (device, nonce) pair, and unique checkpoint ranges.

Migration safety: every phase from 2 to 10 has a `tests/test_phase*_schema_migration.py`.
Each builds a genuine *older* database with real rows, migrates it in place, and proves
that existing values survive unchanged, new constraints hold, and re-running is a no-op.

PostgreSQL is **not** implemented; the SQL is SQLite-specific in places.
