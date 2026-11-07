"""
honeychain.ledger
=================
``HoneyBlockLedger`` -- an append-only, SHA-256 hash-linked chain of honey
batches, persisted in the same SQLite database as the telemetry it is derived
from.

Why a local chain
-----------------
The project already contains an optional Ethereum/Sepolia anchor
(:mod:`honeychain.chain_anchor`). That anchor is valuable but needs funded keys
and network access, so it cannot be the *only* integrity mechanism. The local
ledger gives the system a self-contained, offline-verifiable chain of custody;
the Ethereum anchor, when enabled, publishes each block hash externally so the
local chain itself cannot be silently rewritten wholesale.

Canonical hashing
-----------------
Blocks are hashed over a canonical JSON serialisation: fixed key order,
no insignificant whitespace, UTF-8, and floats rounded to a fixed precision so
that ``42.5`` and ``42.500000001`` cannot produce different hashes for what a
human would call the same batch. The block's own ``block_hash`` and derived
fields (``qr_path``, ``anchor_tx_hash``) are excluded from the digest --
otherwise the hash could never be computed or the QR recorded.

Verification recomputes each block's hash from its stored fields and re-walks
the ``previous_hash`` links. Editing any earlier row invalidates that block and
every block after it, which is exactly the property a "does the row exist?"
check fails to provide.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from . import db

logger = logging.getLogger(__name__)

#: Hash of the block preceding the genesis batch.
GENESIS_PREVIOUS_HASH = "0" * 64

#: Fields covered by the block digest, in canonical order.
HASHED_FIELDS: tuple[str, ...] = (
    "block_index",
    "batch_id",
    "hive_id",
    "harvest_date",
    "total_weight",
    "floral_type",
    "quality_score",
    "timestamp",
    "previous_hash",
)

#: Decimal places applied to every float before hashing.
FLOAT_PRECISION = 4


class LedgerError(Exception):
    """Raised for ledger-level problems such as a duplicate batch id."""


class DuplicateBatchError(LedgerError):
    """The batch id already exists (an API conflict, HTTP 409)."""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, FLOAT_PRECISION)
    if isinstance(value, int):
        return value
    if value is None:
        return None
    return str(value)


def canonical_json(payload: dict[str, Any]) -> str:
    """THE canonical serialiser for every hashed record in HoneyChain: sorted
    keys, no whitespace, floats rounded to FLOAT_PRECISION, non-scalars
    stringified. Ledger blocks and harvest events (honeychain.harvests) both
    hash through this one function -- there is no second hashing scheme for
    off-chain records."""
    return json.dumps(
        {key: _canonical_value(value) for key, value in payload.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_payload(block: dict[str, Any]) -> str:
    """Deterministic serialisation of the hashed subset of a block."""
    return canonical_json({field: block.get(field) for field in HASHED_FIELDS})


def compute_block_hash(block: dict[str, Any]) -> str:
    """SHA-256 of a block's canonical payload, as lowercase hex."""
    return sha256_hex(canonical_payload(block))


class HoneyBlockLedger:
    """Append-only ledger of honey batches backed by the ``honey_batches`` table."""

    # -- writing -----------------------------------------------------------
    def add_batch(
        self,
        batch_id: str,
        hive_id: str,
        harvest_date: str,
        total_weight: float,
        floral_type: str,
        quality_score: float,
        quality_detail: dict[str, Any] | None = None,
        in_transaction: Callable[[Any, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Append a batch as a new block and return it.

        ``in_transaction(conn, block)``, if given, runs inside the SAME
        transaction right after the block row is written, so records that
        must exist together with the block (e.g. the harvest event and its
        batch link) commit or roll back atomically with it. Any exception it
        raises aborts the block too.

        The duplicate check, tip lookup, hashing and insert all happen inside a
        single ``BEGIN IMMEDIATE`` transaction, so two concurrent harvests
        cannot both claim the same ``block_index`` or the same ``previous_hash``.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM honey_batches WHERE batch_id = ?;", (batch_id,)
            ).fetchone()
            if existing is not None:
                raise DuplicateBatchError(f"Batch id '{batch_id}' already exists in the ledger")

            index = db.next_block_index(conn)
            tip = conn.execute(
                "SELECT block_hash FROM honey_batches WHERE block_index = ?;", (index - 1,)
            ).fetchone()
            previous_hash = tip["block_hash"] if tip is not None else GENESIS_PREVIOUS_HASH

            block: dict[str, Any] = {
                "block_index": index,
                "batch_id": batch_id,
                "hive_id": hive_id,
                "harvest_date": harvest_date,
                "total_weight": float(total_weight),
                "floral_type": floral_type,
                "quality_score": float(quality_score),
                "timestamp": timestamp,
                "previous_hash": previous_hash,
            }
            block["block_hash"] = compute_block_hash(block)
            block["quality_detail"] = quality_detail or {}
            db.insert_batch(conn, block)
            if in_transaction is not None:
                in_transaction(conn, block)

        logger.info(
            "Ledger block %d created for batch %s (hash %s...)",
            index,
            batch_id,
            block["block_hash"][:12],
        )
        return block

    # -- reading -----------------------------------------------------------
    @staticmethod
    def get_batch(batch_id: str) -> dict[str, Any] | None:
        return db.get_batch(batch_id)

    @staticmethod
    def all_batches(ascending: bool = True) -> list[dict[str, Any]]:
        return db.all_batches(ascending=ascending)

    @staticmethod
    def height() -> int:
        return len(db.all_batches())

    # -- verification ------------------------------------------------------
    def verify_batch_authenticity(self, batch_id: str) -> dict[str, Any]:
        """Full cryptographic verification of one batch.

        Steps
        -----
        1. Locate the batch.
        2. Recompute its SHA-256 from the stored field values.
        3. Compare against the stored ``block_hash``.
        4. Confirm ``previous_hash`` matches the actual preceding block's hash.
        5. Re-verify every ancestor, so tampering *anywhere* earlier in the
           chain surfaces here rather than passing silently.
        6. Report a structured result.
        """
        record = db.get_batch(batch_id)
        if record is None:
            return {
                "found": False,
                "status": "NOT_FOUND",
                "verified": False,
                "batch_id": batch_id,
                "message": "No such batch in the ledger",
                "checks": [],
            }

        checks: list[dict[str, Any]] = []

        recomputed = compute_block_hash(record)
        stored = str(record["block_hash"])
        hash_ok = recomputed == stored
        checks.append(
            {
                "name": "block_hash_integrity",
                "label": "Block contents match their recorded fingerprint",
                "passed": hash_ok,
                "expected": stored,
                "actual": recomputed,
            }
        )

        index = int(record["block_index"])
        if index == 0:
            link_ok = str(record["previous_hash"]) == GENESIS_PREVIOUS_HASH
            previous_expected = GENESIS_PREVIOUS_HASH
        else:
            previous = db.get_batch_by_index(index - 1)
            if previous is None:
                link_ok = False
                previous_expected = None
            else:
                previous_expected = str(previous["block_hash"])
                link_ok = str(record["previous_hash"]) == previous_expected
        checks.append(
            {
                "name": "chain_linkage",
                "label": "Block is correctly linked to the block before it",
                "passed": link_ok,
                "expected": previous_expected,
                "actual": str(record["previous_hash"]),
            }
        )

        ancestors_ok, broken_at = self._verify_ancestors(index)
        checks.append(
            {
                "name": "ancestor_chain",
                "label": "Every earlier block in the chain is still intact",
                "passed": ancestors_ok,
                "broken_at_block": broken_at,
            }
        )

        verified = hash_ok and link_ok and ancestors_ok
        status = "VERIFIED" if verified else "TAMPERED"
        logger.info("Batch %s verification result: %s", batch_id, status)

        return {
            "found": True,
            "status": status,
            "verified": verified,
            "batch_id": batch_id,
            "block_index": index,
            "block_hash": stored,
            "recomputed_hash": recomputed,
            "previous_hash": str(record["previous_hash"]),
            "timestamp": record["timestamp"],
            "chain_height": self.height(),
            "checks": checks,
            "message": (
                "Cryptographic verification passed: this batch's record is unaltered "
                "and correctly chained."
                if verified
                else "Verification FAILED: the stored record does not match its "
                "cryptographic fingerprint or its position in the chain."
            ),
        }

    @staticmethod
    def _verify_ancestors(index: int) -> tuple[bool, int | None]:
        """Recompute and re-link every block from genesis up to ``index - 1``."""
        previous_hash = GENESIS_PREVIOUS_HASH
        for position in range(index):
            block = db.get_batch_by_index(position)
            if block is None:
                return False, position
            if compute_block_hash(block) != str(block["block_hash"]):
                return False, position
            if str(block["previous_hash"]) != previous_hash:
                return False, position
            previous_hash = str(block["block_hash"])
        return True, None

    def verify_chain(self) -> dict[str, Any]:
        """Verify the entire ledger end to end."""
        blocks = db.all_batches(ascending=True)
        previous_hash = GENESIS_PREVIOUS_HASH
        broken: list[dict[str, Any]] = []

        for expected_index, block in enumerate(blocks):
            problems: list[str] = []
            if int(block["block_index"]) != expected_index:
                problems.append("block_index is out of sequence")
            if compute_block_hash(block) != str(block["block_hash"]):
                problems.append("recomputed hash does not match the stored hash")
            if str(block["previous_hash"]) != previous_hash:
                problems.append("previous_hash does not match the preceding block")
            if problems:
                broken.append(
                    {
                        "block_index": block["block_index"],
                        "batch_id": block["batch_id"],
                        "problems": problems,
                    }
                )
            previous_hash = str(block["block_hash"])

        return {
            "valid": not broken,
            "height": len(blocks),
            "broken_blocks": broken,
            "head_hash": previous_hash if blocks else GENESIS_PREVIOUS_HASH,
        }


#: Process-wide ledger instance (stateless -- all state lives in SQLite).
ledger = HoneyBlockLedger()
