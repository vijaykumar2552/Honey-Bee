"""
honeychain.db
=============
SQLite data-access layer.

Design notes
------------
* One connection **per thread** (``threading.local``). SQLite connection
  objects are not safe to share across threads, and both the Flask request
  workers and the SSE generator threads touch the database.
* WAL journalling so a long-running reader (the dashboard) never blocks the
  webhook writer.
* All writes go through :func:`transaction`, which commits on success and rolls
  back on any exception -- telemetry and its health evaluation are therefore
  written atomically or not at all.
* Every statement is parameterised. There is no string interpolation of user
  input into SQL anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from . import config

logger = logging.getLogger(__name__)

_local = threading.local()

#: Serialises write transactions. SQLite handles concurrent writers by raising
#: "database is locked"; taking this lock in-process turns that into a short
#: wait instead of an error under simulator load.
_write_lock = threading.RLock()


def utc_now() -> str:
    """Canonical timestamp format used throughout the system (UTC, second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    """Return this thread's connection, creating and configuring it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    config.ensure_runtime_directories()
    conn = sqlite3.connect(
        str(config.DATABASE_PATH),
        timeout=30.0,
        isolation_level=None,  # explicit transaction control via `transaction()`
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    _local.conn = conn
    return conn


def close_connection(_exception: BaseException | None = None) -> None:
    """Close the calling thread's connection if it has one (Flask teardown hook)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - best effort on shutdown
            logger.debug("Error closing SQLite connection", exc_info=True)
        _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a block inside a single SQLite transaction.

    Commits on clean exit, rolls back on any exception, and always releases the
    process-level write lock.
    """
    conn = get_connection()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        conn.execute("COMMIT;")


def query_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return get_connection().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return get_connection().execute(sql, tuple(params)).fetchone()


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,)
    ).fetchone()
    return row is not None


#: Columns added after the first release. Each entry is applied with
#: ALTER TABLE ADD COLUMN only when the column is genuinely absent, so an
#: existing populated database is upgraded in place without losing a row.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("health_evaluations", "rule_status", "TEXT"),
    ("health_evaluations", "source", "TEXT NOT NULL DEFAULT 'RULE'"),
    ("health_evaluations", "reason", "TEXT"),
    ("honey_batches", "quality_detail", "TEXT"),
    ("honey_batches", "qr_path", "TEXT"),
    ("honey_batches", "anchor_tx_hash", "TEXT"),
    ("telemetry_readings", "battery_level", "REAL"),
    ("telemetry_readings", "received_at", "TEXT"),
    # Phase 3: organization ownership for existing hives. Nullable -- a hive
    # created before this migration (or via the device webhook, which still
    # has no concept of an organization) has organization_id = NULL, meaning
    # "unassigned". Authorization treats an unassigned hive as visible/
    # actionable by ADMIN/AUTHORITY only, never by a specific organization's
    # own role, until an admin explicitly assigns it.
    ("hives", "organization_id", "INTEGER REFERENCES organizations (id)"),
    # Phase 4: device identity + preserved device event time + data-quality
    # flags. All nullable/defaulted, so every pre-Phase-4 row stays valid:
    # legacy readings simply have no device, no device timestamp, no flags.
    ("telemetry_readings", "device_timestamp", "TEXT"),
    ("telemetry_readings", "device_id", "INTEGER REFERENCES devices (id)"),
    ("telemetry_readings", "sensor_flags", "TEXT"),
    ("devices", "credential_version", "INTEGER NOT NULL DEFAULT 1"),
    ("devices", "expected_interval_seconds", "INTEGER"),
    # Phase 5: full harvest timestamp (when supplied), content hash of an
    # evidence photo, and the harvest event's canonical SHA-256.
    ("harvest_events", "harvested_at", "TEXT"),
    ("harvest_events", "photo_sha256", "TEXT"),
    ("harvest_events", "event_hash", "TEXT"),
    # Phase 6: Merkle checkpoint bookkeeping on the Phase 2 table.
    ("blockchain_checkpoints", "from_seq", "INTEGER"),
    ("blockchain_checkpoints", "to_seq", "INTEGER"),
    ("blockchain_checkpoints", "merkle_root", "TEXT"),
    ("blockchain_checkpoints", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("blockchain_checkpoints", "last_error", "TEXT"),
    ("blockchain_checkpoints", "last_attempt_at", "TEXT"),
    ("blockchain_checkpoints", "contract_address", "TEXT"),
    # Phase 7: graded ML band + the exact model that produced it; and whether a
    # device is a physical sensor or a simulator (demo data must be labelled).
    ("health_evaluations", "ml_band", "TEXT"),
    ("health_evaluations", "model_version", "TEXT"),
    ("devices", "data_origin", "TEXT NOT NULL DEFAULT 'PHYSICAL' "
     "CHECK (data_origin IN ('PHYSICAL', 'SIMULATED'))"),
    # Phase 8: actors + provenance links on the Phase 2 supply-chain tables.
    ("processing_events", "input_kg", "REAL"),
    ("processing_events", "output_kg", "REAL"),
    ("lab_results", "requested_by_user_id", "INTEGER REFERENCES users (id)"),
    ("lab_results", "requested_by_org_id", "INTEGER REFERENCES organizations (id)"),
    ("lab_results", "completed_at", "TEXT"),
    ("lab_results", "result_hash", "TEXT"),
    ("packaging_events", "units_digest", "TEXT"),
    ("custody_transfers", "initiated_by_user_id", "INTEGER REFERENCES users (id)"),
    ("custody_transfers", "resolved_by_user_id", "INTEGER REFERENCES users (id)"),
    ("shipments", "from_org_id", "INTEGER REFERENCES organizations (id)"),
    ("shipments", "to_org_id", "INTEGER REFERENCES organizations (id)"),
    ("shipments", "created_by_user_id", "INTEGER REFERENCES users (id)"),
    ("shipments", "updated_at", "TEXT"),
    ("retail_records", "shipment_id", "TEXT REFERENCES shipments (shipment_id)"),
    ("retail_records", "custody_transfer_id", "TEXT REFERENCES custody_transfers (transfer_id)"),
    ("retail_records", "shelved_at", "TEXT"),
    # Phase 9: per-unit QR -- SHA-256 of the HMAC tag (never the secret),
    # the QR construction version, and label revocation.
    ("product_units", "qr_verifier", "TEXT"),
    ("product_units", "qr_version", "INTEGER"),
    ("product_units", "qr_revoked_at", "TEXT"),
    ("product_units", "last_scanned_at", "TEXT"),
    # Phase 10: an EXCEPTION must say why (was recorded without any reason).
    ("shipments", "exception_reason", "TEXT"),
)

#: Indexes on columns that may have been added by _MIGRATIONS above. These
#: cannot live in database/schema.sql, because that script runs BEFORE the
#: column migrations and would fail on a pre-migration database.
_POST_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry_readings (device_id);",
    "CREATE INDEX IF NOT EXISTS idx_hives_org ON hives (organization_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoint_range "
    "ON blockchain_checkpoints (from_seq, to_seq) WHERE from_seq IS NOT NULL;",
    # Phase 8: at most ONE pending transfer per batch (no double hand-off),
    # one open lab request per (batch, lab), one shipment per transfer, one
    # retail record per shipment.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_transfer "
    "ON custody_transfers (batch_id) WHERE status = 'PENDING';",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_lab_request "
    "ON lab_results (batch_id, lab_org_id) WHERE status = 'PENDING';",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_shipment_per_transfer "
    "ON shipments (custody_transfer_id) WHERE custody_transfer_id IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_retail_per_shipment "
    "ON retail_records (shipment_id) WHERE shipment_id IS NOT NULL;",
)


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        if not _table_exists(conn, table):
            continue
        if column in _existing_columns(conn, table):
            continue
        # SQLite cannot add a NOT NULL column without a default; every entry in
        # _MIGRATIONS therefore either allows NULL or supplies a DEFAULT.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
        logger.info("Schema migration: added %s.%s", table, column)
    for statement in _POST_MIGRATION_INDEXES:
        conn.execute(statement)


def initialise_database() -> None:
    """Create/upgrade the schema. Idempotent and non-destructive."""
    config.ensure_runtime_directories()
    conn = get_connection()

    if not config.SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Schema file not found: {config.SCHEMA_PATH}")

    schema_sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
    with _write_lock:
        conn.executescript(schema_sql)
        _apply_column_migrations(conn)

    logger.info("Database ready at %s (WAL, foreign keys ON)", config.DATABASE_PATH)


def pragma_report() -> dict[str, Any]:
    """Expose the live PRAGMA state so /api/health can prove WAL/FK are on."""
    conn = get_connection()
    return {
        "journal_mode": conn.execute("PRAGMA journal_mode;").fetchone()[0],
        "foreign_keys": bool(conn.execute("PRAGMA foreign_keys;").fetchone()[0]),
        "synchronous": conn.execute("PRAGMA synchronous;").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# Hive helpers
# ---------------------------------------------------------------------------

def get_or_create_hive(conn: sqlite3.Connection, hive_id: str) -> int:
    """Return the integer primary key for ``hive_id``, inserting the hive if new.

    Must be called inside an open transaction so that hive creation, telemetry
    insertion and health insertion share one atomic unit.
    """
    row = conn.execute("SELECT id FROM hives WHERE hive_id = ?;", (hive_id,)).fetchone()
    if row is not None:
        return int(row["id"])

    conn.execute(
        "INSERT INTO hives (hive_id, name, location, status, created_at) "
        "VALUES (?, ?, ?, 'ACTIVE', ?);",
        (hive_id, hive_id, None, utc_now()),
    )
    row = conn.execute("SELECT id FROM hives WHERE hive_id = ?;", (hive_id,)).fetchone()
    logger.info("Registered new hive %s", hive_id)
    return int(row["id"])


def resolve_hive_pk(hive_id: str) -> int | None:
    row = query_one("SELECT id FROM hives WHERE hive_id = ?;", (hive_id,))
    return int(row["id"]) if row else None


def list_hives(*, restrict_to_org_id: int | None = None) -> list[dict[str, Any]]:
    """All hives, or only those owned by one organization.

    ``restrict_to_org_id=None`` means no restriction (used for ADMIN/AUTHORITY,
    who may see every hive). Passing a specific organization id naturally
    excludes hives with organization_id IS NULL too, since `= ?` never matches
    NULL in SQL -- an unassigned hive is invisible to every specific org until
    an admin assigns it, which is the intended default.
    """
    if restrict_to_org_id is None:
        return [dict(r) for r in query_all("SELECT * FROM hives ORDER BY hive_id;")]
    return [
        dict(r)
        for r in query_all(
            "SELECT * FROM hives WHERE organization_id = ? ORDER BY hive_id;",
            (restrict_to_org_id,),
        )
    ]


def get_hive_by_external_id(hive_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM hives WHERE hive_id = ?;", (hive_id,))
    return dict(row) if row else None


def get_hive_by_pk(pk: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM hives WHERE id = ?;", (pk,))
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Telemetry + health persistence
# ---------------------------------------------------------------------------

def insert_telemetry(
    conn: sqlite3.Connection,
    hive_pk: int,
    reading: dict[str, Any],
    timestamp: str,
    *,
    device_pk: int | None = None,
    device_timestamp: str | None = None,
    sensor_flags: str | None = None,
) -> int:
    """``timestamp`` keeps its original meaning (server receipt time, used by
    every existing query and the swarm window). ``device_timestamp`` is the
    device's own event time, stored alongside rather than overwritten."""
    cursor = conn.execute(
        "INSERT INTO telemetry_readings "
        "(hive_id, timestamp, temperature, humidity, weight, audio_frequency, "
        " battery_level, received_at, device_id, device_timestamp, sensor_flags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            hive_pk,
            timestamp,
            reading.get("temperature"),
            reading.get("humidity"),
            reading.get("weight"),
            reading.get("audio_frequency"),
            reading.get("battery_level"),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            device_pk,
            device_timestamp,
            sensor_flags,
        ),
    )
    return int(cursor.lastrowid)


def insert_health_evaluation(
    conn: sqlite3.Connection,
    hive_pk: int,
    telemetry_pk: int,
    timestamp: str,
    evaluation: dict[str, Any],
) -> int:
    cursor = conn.execute(
        "INSERT INTO health_evaluations "
        "(hive_id, telemetry_id, timestamp, health_status, anomaly_score, "
        " prediction, rule_status, source, reason, ml_band, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            hive_pk,
            telemetry_pk,
            timestamp,
            evaluation["health_status"],
            evaluation.get("anomaly_score"),
            evaluation.get("prediction"),
            evaluation.get("rule_status"),
            evaluation.get("source", "RULE"),
            evaluation.get("reason"),
            evaluation.get("ml_band"),
            evaluation.get("model_version"),
        ),
    )
    return int(cursor.lastrowid)


#: Joined projection reused by /api/telemetry/latest, the SSE bootstrap and the
#: per-record push, so every surface shows byte-identical field names.
_LATEST_SQL = """
SELECT  t.id              AS telemetry_id,
        h.hive_id         AS hive_id,
        h.name            AS hive_name,
        h.location        AS location,
        t.timestamp       AS timestamp,
        t.temperature     AS temperature,
        t.humidity        AS humidity,
        t.weight          AS weight,
        t.audio_frequency AS audio_frequency,
        t.battery_level   AS battery_level,
        e.health_status   AS health_status,
        e.anomaly_score   AS anomaly_score,
        e.prediction      AS prediction,
        e.rule_status     AS rule_status,
        e.source          AS source,
        e.reason          AS reason,
        t.device_timestamp AS device_timestamp,
        t.sensor_flags    AS sensor_flags,
        d.device_id       AS device_id,
        CASE WHEN d.id IS NULL THEN 'UNATTRIBUTED' ELSE d.data_origin END AS data_origin,
        e.ml_band         AS ml_band,
        e.model_version   AS model_version
FROM        telemetry_readings t
JOIN        hives              h ON h.id = t.hive_id
LEFT JOIN   health_evaluations e ON e.telemetry_id = t.id
LEFT JOIN   devices            d ON d.id = t.device_id
"""


def latest_telemetry(
    limit: int | None = None, *, restrict_to_org_id: int | None = None
) -> list[dict[str, Any]]:
    """Most recent joined telemetry + health rows, newest first.

    ``restrict_to_org_id`` filters to hives owned by one organization when
    given (None means no restriction, i.e. ADMIN/AUTHORITY).
    """
    limit = limit or config.LATEST_LIMIT
    if restrict_to_org_id is None:
        rows = query_all(
            _LATEST_SQL + " ORDER BY t.timestamp DESC, t.id DESC LIMIT ?;", (int(limit),)
        )
    else:
        rows = query_all(
            _LATEST_SQL
            + " WHERE h.organization_id = ? ORDER BY t.timestamp DESC, t.id DESC LIMIT ?;",
            (restrict_to_org_id, int(limit)),
        )
    return [dict(row) for row in rows]


def telemetry_by_id(telemetry_pk: int) -> dict[str, Any] | None:
    row = query_one(_LATEST_SQL + " WHERE t.id = ?;", (telemetry_pk,))
    return dict(row) if row else None


def recent_weights(
    conn: sqlite3.Connection, hive_pk: int, window_seconds: int, before: str
) -> list[float]:
    """Weights recorded for a hive inside the trailing window, newest first.

    Used by the sudden-weight-loss (swarming) detector. ``before`` bounds the
    query so a replayed/backdated reading cannot be compared against readings
    that came after it.
    """
    rows = conn.execute(
        "SELECT weight FROM telemetry_readings "
        "WHERE hive_id = ? AND weight IS NOT NULL "
        "  AND timestamp <= ? "
        "  AND timestamp >= DATETIME(?, ?) "
        "ORDER BY timestamp DESC, id DESC LIMIT 50;",
        (hive_pk, before, before, f"-{int(window_seconds)} seconds"),
    ).fetchall()
    return [float(r["weight"]) for r in rows]


def hive_history(hive_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Historical telemetry+health for one hive (external id), newest first."""
    rows = query_all(
        _LATEST_SQL + " WHERE h.hive_id = ? ORDER BY t.timestamp DESC, t.id DESC LIMIT ?;",
        (hive_id, int(limit)),
    )
    return [dict(r) for r in rows]


def dashboard_summary(*, restrict_to_org_id: int | None = None) -> dict[str, Any]:
    """Fleet-wide metrics computed from the database, never from the browser.

    "Active hives" counts hives that have actually reported telemetry, not rows
    in the hives table, so a stale registration cannot inflate the number.

    ``restrict_to_org_id`` scopes every figure to one organization's hives
    when given (None means no restriction, i.e. ADMIN/AUTHORITY) -- fleet-wide
    numbers are exactly the kind of "private operational data" organization
    isolation is meant to protect.
    """
    if restrict_to_org_id is None:
        totals = query_one(
            """
            SELECT COUNT(DISTINCT t.hive_id) AS active_hives,
                   AVG(t.temperature)        AS avg_temperature,
                   AVG(t.weight)             AS avg_weight,
                   COUNT(*)                  AS reading_count
            FROM telemetry_readings t;
            """
        )
        statuses = query_all(
            """
            SELECT e.health_status AS status, COUNT(*) AS n
            FROM health_evaluations e
            GROUP BY e.health_status;
            """
        )
    else:
        totals = query_one(
            """
            SELECT COUNT(DISTINCT t.hive_id) AS active_hives,
                   AVG(t.temperature)        AS avg_temperature,
                   AVG(t.weight)             AS avg_weight,
                   COUNT(*)                  AS reading_count
            FROM telemetry_readings t
            JOIN hives h ON h.id = t.hive_id
            WHERE h.organization_id = ?;
            """,
            (restrict_to_org_id,),
        )
        statuses = query_all(
            """
            SELECT e.health_status AS status, COUNT(*) AS n
            FROM health_evaluations e
            JOIN hives h ON h.id = e.hive_id
            WHERE h.organization_id = ?
            GROUP BY e.health_status;
            """,
            (restrict_to_org_id,),
        )
    counts = {row["status"]: int(row["n"]) for row in statuses}
    total_evaluated = sum(counts.values())
    healthy = counts.get("HEALTHY", 0)
    overall = round(healthy / total_evaluated * 100.0, 1) if total_evaluated else None

    return {
        "active_hives": int(totals["active_hives"] or 0),
        "avg_temperature": round(totals["avg_temperature"], 2)
        if totals["avg_temperature"] is not None
        else None,
        "avg_weight": round(totals["avg_weight"], 2)
        if totals["avg_weight"] is not None
        else None,
        "overall_health_percent": overall,
        "reading_count": int(totals["reading_count"] or 0),
        "status_counts": {
            "HEALTHY": counts.get("HEALTHY", 0),
            "WARNING": counts.get("WARNING", 0),
            "CRITICAL": counts.get("CRITICAL", 0),
        },
    }


# ---------------------------------------------------------------------------
# Ledger persistence (used by honeychain.ledger)
# ---------------------------------------------------------------------------

def batch_exists(batch_id: str) -> bool:
    return query_one("SELECT 1 FROM honey_batches WHERE batch_id = ?;", (batch_id,)) is not None


def get_batch(batch_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM honey_batches WHERE batch_id = ?;", (batch_id,))
    return dict(row) if row else None


def get_batch_by_index(block_index: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM honey_batches WHERE block_index = ?;", (block_index,))
    return dict(row) if row else None


def all_batches(ascending: bool = True) -> list[dict[str, Any]]:
    order = "ASC" if ascending else "DESC"
    return [
        dict(r)
        for r in query_all(f"SELECT * FROM honey_batches ORDER BY block_index {order};")
    ]


def next_block_index(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(block_index) AS m FROM honey_batches;").fetchone()
    return 0 if row["m"] is None else int(row["m"]) + 1


def insert_batch(conn: sqlite3.Connection, block: dict[str, Any]) -> int:
    cursor = conn.execute(
        "INSERT INTO honey_batches "
        "(block_index, batch_id, hive_id, harvest_date, total_weight, floral_type, "
        " quality_score, quality_detail, timestamp, previous_hash, block_hash, qr_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            block["block_index"],
            block["batch_id"],
            block["hive_id"],
            block["harvest_date"],
            block["total_weight"],
            block["floral_type"],
            block["quality_score"],
            json.dumps(block.get("quality_detail") or {}, sort_keys=True),
            block["timestamp"],
            block["previous_hash"],
            block["block_hash"],
            block.get("qr_path"),
        ),
    )
    return int(cursor.lastrowid)


def set_batch_qr_path(batch_id: str, qr_path: str) -> None:
    """Store the generated QR filename.

    Deliberately *not* part of the hashed payload: the QR image is derived from
    the batch id, so recording it must not change the block hash.
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE honey_batches SET qr_path = ? WHERE batch_id = ?;", (qr_path, batch_id)
        )


def set_batch_anchor(batch_id: str, tx_hash: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE honey_batches SET anchor_tx_hash = ? WHERE batch_id = ?;",
            (tx_hash, batch_id),
        )


# ---------------------------------------------------------------------------
# Authentication: organizations, users, token revocation, audit log
# (Phase 3 -- see honeychain.auth for the logic that calls these)
# ---------------------------------------------------------------------------

def get_organization_by_org_id(org_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM organizations WHERE org_id = ?;", (org_id,))
    return dict(row) if row else None


def get_organization_by_pk(pk: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM organizations WHERE id = ?;", (pk,))
    return dict(row) if row else None


def create_organization(
    conn: sqlite3.Connection, *, org_id: str, name: str, org_type: str
) -> int:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO organizations (org_id, name, org_type, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
        (org_id, name, org_type, now, now),
    )
    return int(cursor.lastrowid)


def get_user_by_email(email: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM users WHERE email = ?;", (email.strip().lower(),))
    return dict(row) if row else None


def get_user_by_user_id(user_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM users WHERE user_id = ?;", (user_id,))
    return dict(row) if row else None


def create_user(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    organization_pk: int | None,
    email: str,
    password_hash: str,
    name: str,
    role: str,
    phone: str | None = None,
) -> int:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO users "
        "(user_id, organization_id, email, phone, password_hash, name, role, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?);",
        (
            user_id,
            organization_pk,
            email.strip().lower(),
            phone,
            password_hash,
            name,
            role,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def update_last_login(user_id: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?;",
            (utc_now(), utc_now(), user_id),
        )


def is_token_revoked(jti: str) -> bool:
    return query_one("SELECT 1 FROM revoked_tokens WHERE jti = ?;", (jti,)) is not None


def revoke_token(jti: str, expires_at: str | None = None) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO revoked_tokens (jti, revoked_at, expires_at) "
            "VALUES (?, ?, ?);",
            (jti, utc_now(), expires_at),
        )


def insert_audit_event(
    *,
    actor_user_pk: int | None,
    organization_pk: int | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: str | None = None,
    ip_address: str | None = None,
    result: str = "SUCCESS",
) -> int:
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO audit_log "
            "(actor_user_id, organization_id, action, resource_type, resource_id, "
            " metadata, ip_address, result, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                actor_user_pk,
                organization_pk,
                action,
                resource_type,
                resource_id,
                metadata,
                ip_address,
                result,
                utc_now(),
            ),
        )
        return int(cursor.lastrowid)


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in query_all(
            "SELECT a.*, u.user_id AS actor_external_id, u.name AS actor_name "
            "FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id "
            "ORDER BY a.occurred_at DESC, a.id DESC LIMIT ?;",
            (int(limit),),
        )
    ]


def recent_login_failures(email: str, window_seconds: int) -> int:
    """Count of LOGIN_FAILURE audit events for this email in the trailing window.

    Used for the login-attempt throttle. Matched on the metadata field (which
    stores the attempted email) rather than actor_user_id, since a failed
    login for an unknown email has no user row to link to.
    """
    row = query_one(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE action = 'LOGIN_FAILURE' AND metadata = ? "
        "  AND occurred_at >= DATETIME('now', ?);",
        (email.strip().lower(), f"-{int(window_seconds)} seconds"),
    )
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Hive provisioning (Phase 4 -- device binding needs hives that exist BEFORE
# any telemetry, instead of being auto-created by whatever id a request uses)
# ---------------------------------------------------------------------------

def create_hive(
    conn: sqlite3.Connection,
    *,
    hive_id: str,
    organization_pk: int | None,
    name: str | None = None,
    location: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO hives (hive_id, name, location, status, created_at, organization_id) "
        "VALUES (?, ?, ?, 'ACTIVE', ?, ?);",
        (hive_id, name or hive_id, location, utc_now(), organization_pk),
    )
    return int(cursor.lastrowid)


def assign_hive_organization(hive_id: str, organization_pk: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE hives SET organization_id = ? WHERE hive_id = ?;",
            (organization_pk, hive_id),
        )


# ---------------------------------------------------------------------------
# Devices (Phase 4)
# ---------------------------------------------------------------------------

def get_device_by_device_id(device_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM devices WHERE device_id = ?;", (device_id,))
    return dict(row) if row else None


def create_device(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    hive_pk: int,
    organization_pk: int,
    credential_hash: str,
    firmware_version: str | None,
    expected_interval_seconds: int | None,
    data_origin: str = "PHYSICAL",
) -> int:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO devices (device_id, hive_id, organization_id, credential_hash, "
        " credential_version, firmware_version, expected_interval_seconds, status, "
        " created_at, updated_at, data_origin) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, 'PROVISIONED', ?, ?, ?);",
        (device_id, hive_pk, organization_pk, credential_hash, firmware_version,
         expected_interval_seconds, now, now, data_origin),
    )
    return int(cursor.lastrowid)


def list_devices(*, restrict_to_org_id: int | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT d.*, h.hive_id AS hive_external_id, o.org_id AS org_external_id "
        "FROM devices d LEFT JOIN hives h ON h.id = d.hive_id "
        "LEFT JOIN organizations o ON o.id = d.organization_id "
    )
    if restrict_to_org_id is None:
        return [dict(r) for r in query_all(sql + "ORDER BY d.device_id;")]
    return [
        dict(r)
        for r in query_all(sql + "WHERE d.organization_id = ? ORDER BY d.device_id;",
                           (restrict_to_org_id,))
    ]


def get_device_detail(device_id: str) -> dict[str, Any] | None:
    row = query_one(
        "SELECT d.*, h.hive_id AS hive_external_id, o.org_id AS org_external_id "
        "FROM devices d LEFT JOIN hives h ON h.id = d.hive_id "
        "LEFT JOIN organizations o ON o.id = d.organization_id "
        "WHERE d.device_id = ?;",
        (device_id,),
    )
    return dict(row) if row else None


def update_device_credentials(device_id: str, version: int, credential_hash: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE devices SET credential_version = ?, credential_hash = ?, updated_at = ? "
            "WHERE device_id = ?;",
            (version, credential_hash, utc_now(), device_id),
        )


def set_device_status(device_id: str, status: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE devices SET status = ?, updated_at = ? WHERE device_id = ?;",
            (status, utc_now(), device_id),
        )


def mark_device_seen(
    conn: sqlite3.Connection, device_pk: int, battery_level: float | None
) -> str:
    """Record a successful authenticated report. Returns the PREVIOUS status
    so the caller can detect an OFFLINE -> ACTIVE recovery. A REVOKED or
    FAULTY status is never overwritten here."""
    row = conn.execute("SELECT status FROM devices WHERE id = ?;", (device_pk,)).fetchone()
    previous = row["status"] if row else "PROVISIONED"
    new_status = previous if previous in ("REVOKED", "FAULTY") else "ACTIVE"
    now = utc_now()
    conn.execute(
        "UPDATE devices SET last_seen_at = ?, status = ?, updated_at = ?, "
        " battery_level = COALESCE(?, battery_level) WHERE id = ?;",
        (now, new_status, now, battery_level, device_pk),
    )
    return previous


def record_device_nonce(device_pk: int, nonce: str) -> None:
    """Raises sqlite3.IntegrityError if (device, nonce) was already used."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO device_nonces (device_id, nonce, seen_at) VALUES (?, ?, ?);",
            (device_pk, nonce, utc_now()),
        )


def purge_device_nonces(older_than_seconds: int) -> None:
    """Nonces older than twice the freshness window can never be replayed
    successfully anyway (the signed timestamp would be rejected first), so
    they are safe to forget."""
    with transaction() as conn:
        conn.execute(
            "DELETE FROM device_nonces WHERE seen_at < DATETIME('now', ?);",
            (f"-{int(older_than_seconds)} seconds",),
        )


def recent_readings_for_hive(
    conn: sqlite3.Connection, hive_pk: int, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT temperature, humidity, weight, timestamp FROM telemetry_readings "
        "WHERE hive_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?;",
        (hive_pk, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def find_silent_devices(default_interval: int, multiplier: float) -> list[dict[str, Any]]:
    """ACTIVE devices whose last report is older than interval x multiplier.

    Only ACTIVE devices are candidates: PROVISIONED devices have never
    reported (nothing went silent), and OFFLINE ones were already alerted --
    which is what keeps this from alerting repeatedly for one condition.
    """
    rows = query_all(
        "SELECT d.*, h.hive_id AS hive_external_id FROM devices d "
        "LEFT JOIN hives h ON h.id = d.hive_id "
        "WHERE d.status = 'ACTIVE' AND d.last_seen_at IS NOT NULL "
        "  AND d.last_seen_at < DATETIME('now', "
        "      '-' || CAST(COALESCE(d.expected_interval_seconds, ?) * ? AS INTEGER) "
        "      || ' seconds');",
        (int(default_interval), float(multiplier)),
    )
    return [dict(r) for r in rows]


def active_users_in_organization(organization_pk: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in query_all(
            "SELECT * FROM users WHERE organization_id = ? AND status = 'ACTIVE';",
            (organization_pk,),
        )
    ]


def create_notification(
    *, notification_id: str, recipient_user_pk: int, notification_type: str,
    title: str, body: str | None = None,
) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO notifications (notification_id, recipient_user_id, "
            " notification_type, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?);",
            (notification_id, recipient_user_pk, notification_type, title, body, utc_now()),
        )


def list_notifications_for_user(user_pk: int, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in query_all(
            "SELECT notification_id, notification_type, title, body, read_at, created_at "
            "FROM notifications WHERE recipient_user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?;",
            (user_pk, int(limit)),
        )
    ]


# ---------------------------------------------------------------------------
# Harvest events (Phase 5)
# ---------------------------------------------------------------------------

_HARVEST_SELECT = (
    "SELECT e.*, h.hive_id AS hive_external_id, o.org_id AS org_external_id, "
    "       u.user_id AS harvested_by_user_id, u.name AS harvested_by_name "
    "FROM harvest_events e "
    "JOIN hives h ON h.id = e.hive_id "
    "LEFT JOIN organizations o ON o.id = e.organization_id "
    "LEFT JOIN users u ON u.id = e.beekeeper_user_id "
)


def insert_harvest_event(conn: sqlite3.Connection, event: dict[str, Any]) -> int:
    cursor = conn.execute(
        "INSERT INTO harvest_events (harvest_id, hive_id, beekeeper_user_id, organization_id, "
        " harvest_date, harvested_at, quantity_kg, floral_source, latitude, longitude, "
        " frame_count, notes, photo_sha256, status, event_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (event["harvest_id"], event["hive_pk"], event["beekeeper_user_pk"],
         event["organization_pk"], event["harvest_date"], event.get("harvested_at"),
         event["quantity_kg"], event.get("floral_source"), event.get("latitude"),
         event.get("longitude"), event.get("frame_count"), event.get("notes"),
         event.get("photo_sha256"), event["status"], event["event_hash"],
         event["created_at"]),
    )
    return int(cursor.lastrowid)


def link_batch_harvest(conn: sqlite3.Connection, batch_id: str, harvest_id: str,
                       weight_kg: float) -> None:
    conn.execute(
        "INSERT INTO batch_harvest_links (batch_id, harvest_id, weight_kg) VALUES (?, ?, ?);",
        (batch_id, harvest_id, weight_kg),
    )


def harvest_exists(harvest_id: str) -> bool:
    return query_one("SELECT 1 FROM harvest_events WHERE harvest_id = ?;", (harvest_id,)) is not None


def get_harvest(harvest_id: str) -> dict[str, Any] | None:
    row = query_one(_HARVEST_SELECT + "WHERE e.harvest_id = ?;", (harvest_id,))
    return dict(row) if row else None


def list_harvests(*, restrict_to_org_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if restrict_to_org_id is None:
        rows = query_all(_HARVEST_SELECT + "ORDER BY e.id DESC LIMIT ?;", (int(limit),))
    else:
        rows = query_all(_HARVEST_SELECT + "WHERE e.organization_id = ? ORDER BY e.id DESC LIMIT ?;",
                         (restrict_to_org_id, int(limit)))
    return [dict(r) for r in rows]


def batches_for_harvest(harvest_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in query_all(
        "SELECT batch_id, weight_kg FROM batch_harvest_links WHERE harvest_id = ? ORDER BY id;",
        (harvest_id,))]


def harvests_for_batch(batch_id: str) -> list[str]:
    return [r["harvest_id"] for r in query_all(
        "SELECT harvest_id FROM batch_harvest_links WHERE batch_id = ? ORDER BY id;", (batch_id,))]


def batch_organization_id(batch_id: str) -> int | None:
    """Owning organization of a batch, via its hive (honey_batches.hive_id is
    the hive's external id -- Audit DB-02 -- so this is a text join)."""
    row = query_one(
        "SELECT h.organization_id FROM honey_batches b JOIN hives h ON h.hive_id = b.hive_id "
        "WHERE b.batch_id = ?;", (batch_id,))
    return row["organization_id"] if row else None


def batch_ids_for_org(organization_pk: int) -> set[str]:
    """Batches an organization legitimately participates in: its own hives'
    batches (Phase 5) plus, since Phase 8, batches it originated, holds, sent
    or received in custody, was asked to test, or received at retail."""
    ids = {r["batch_id"] for r in query_all(
        "SELECT b.batch_id FROM honey_batches b JOIN hives h ON h.hive_id = b.hive_id "
        "WHERE h.organization_id = ?;", (organization_pk,))}
    for sql in ("SELECT batch_id FROM batch_state WHERE origin_org_id = ? OR holder_org_id = ?;",
                "SELECT batch_id FROM custody_transfers WHERE from_org_id = ? OR to_org_id = ?;",
                "SELECT batch_id FROM lab_results WHERE lab_org_id = ? OR requested_by_org_id = ?;",
                "SELECT batch_id FROM retail_records WHERE retailer_org_id = ? OR retailer_org_id = ?;"):
        ids.update(r["batch_id"] for r in query_all(sql, (organization_pk, organization_pk)))
    return ids
