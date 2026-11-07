"""
Shared test harness.

Every test module imports :class:`HoneyChainTestCase`, which gives each test
class its own temporary database and QR directory. Nothing here touches the
developer's real ``instance/honeychain.db``.

The environment must be configured *before* ``honeychain.config`` is imported,
because config reads os.environ at import time -- hence the assignments at the
top of this module, which runs first via ``tests/__init__.py``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# --- configure the environment before honeychain.config is imported ---------
TEST_SECRET = "unit-test-secret-do-not-use-in-production"
TEST_AUTH_SECRET = "unit-test-auth-secret-do-not-use-in-production"
_TEST_DB_DIR = tempfile.mkdtemp(prefix="honeychain-tests-")

os.environ["HONEYCHAIN_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["HONEYCHAIN_AUTH_SECRET"] = TEST_AUTH_SECRET
# Phase 4: per-device credentials are derived from this master key.
TEST_DEVICE_MASTER_KEY = "unit-test-device-master-key-do-not-use-in-production"
os.environ["HONEYCHAIN_DEVICE_MASTER_KEY"] = TEST_DEVICE_MASTER_KEY
# The pre-Phase-4 shared-secret webhook is disabled by default in production
# (config.LEGACY_WEBHOOK_ENABLED). The original test modules exercise the
# validation / normalisation / pipeline code through that endpoint, so the
# suite opts in explicitly here; tests/test_phase4_iot_security.py verifies
# the production default (disabled -> 410) separately.
os.environ["HONEYCHAIN_LEGACY_WEBHOOK_ENABLED"] = "true"
os.environ["HONEYCHAIN_PUBLIC_BASE_URL"] = "http://testserver:5000"
os.environ["HONEYCHAIN_DB_PATH"] = str(Path(_TEST_DB_DIR) / "test.db")
os.environ["HONEYCHAIN_LOG_LEVEL"] = "CRITICAL"
os.environ["HONEYCHAIN_SSE_KEEPALIVE"] = "0.2"

from honeychain import auth, config, db  # noqa: E402
from honeychain.app import create_app  # noqa: E402


class HoneyChainTestCase(unittest.TestCase):
    """Base case: a fresh database and Flask test client per test class."""

    load_model = True

    @classmethod
    def setUpClass(cls) -> None:
        cls._db_dir = Path(tempfile.mkdtemp(prefix="hc-case-"))
        config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cls.app = create_app(initialise_db=True, load_model=cls.load_model)
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        db.close_connection()
        shutil.rmtree(cls._db_dir, ignore_errors=True)

    def setUp(self) -> None:
        from honeychain import ratelimit
        ratelimit.reset_all()   # limits are per test, not shared across the suite
        # Truncate between tests so each starts from a known state. DELETE FROM
        # (not DROP) keeps the schema and exercises the real foreign keys.
        # Deletion order matters here: children before parents, since foreign
        # keys are enforced (ON foreign_keys=ON) and would otherwise reject a
        # parent delete while a child row still references it.
        with db.transaction() as conn:
            for table in (
                "revoked_tokens",
                "audit_log",
                "notifications",
                "device_nonces",
                "recalls",               # Phase 8 supply chain: children first
                "retail_records",
                "shipments",
                "custody_transfers",
                "consumer_scans",        # Phase 10: child of product_units -> first
                "product_units",
                "packaging_events",
                "lab_results",
                "batch_blend_inputs",
                "processing_events",
                "batch_state",
                "chain_events",          # Phase 6 (no FKs; cleared for isolation)
                "blockchain_checkpoints",
                "batch_harvest_links",   # Phase 5: children of honey_batches /
                "harvest_events",        # hives / users (FK RESTRICT) -> first
                "health_evaluations",
                "telemetry_readings",
                "devices",
                "honey_batches",
                "hives",
                "users",
                "organizations",
            ):
                conn.execute(f"DELETE FROM {table};")
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('hives','telemetry_readings','health_evaluations','honey_batches',"
                " 'users','organizations','audit_log','devices','notifications');"
            )

    # -- auth helpers --------------------------------------------------
    def create_org(
        self, org_id: str = "ORG-TEST-001", name: str = "Test Org", org_type: str = "BEEKEEPER"
    ) -> int:
        with db.transaction() as conn:
            return db.create_organization(conn, org_id=org_id, name=name, org_type=org_type)

    def own_hive(self, hive_id: str, org_id: str = "ORG-TEST-001") -> int:
        """Give a (webhook-seeded) hive an owning organization. Phase 5
        requires one for any harvest, because provenance must name who owns
        the hive. Idempotent; a no-op if the hive does not exist."""
        org = db.get_organization_by_org_id(org_id)
        org_pk = org["id"] if org else self.create_org(org_id=org_id)
        db.assign_hive_organization(hive_id, org_pk)
        return org_pk

    def create_user(
        self,
        *,
        user_id: str,
        email: str,
        role: str,
        organization_pk: int | None = None,
        password: str = "TestPass123!",
        name: str | None = None,
        status: str = "ACTIVE",
    ) -> dict:
        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id=user_id,
                organization_pk=organization_pk,
                email=email,
                password_hash=auth.hash_password(password),
                name=name or user_id,
                role=role,
            )
        if status != "ACTIVE":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET status = ? WHERE user_id = ?;", (status, user_id)
                )
        return db.get_user_by_user_id(user_id)

    def auth_headers_for(self, user_row: dict) -> dict:
        token, _jti = auth.generate_token(user_row)
        return {"Authorization": f"Bearer {token}"}

    def admin_headers(self) -> dict:
        """One ADMIN user + a valid token. Idempotent within a test: the
        first call creates the user, every subsequent call in the same test
        (each test starting from a truncated ``users`` table) reuses the same
        row rather than re-inserting it -- a test that calls a helper using
        this more than once (e.g. posting two harvests) would otherwise hit
        the UNIQUE constraint on email/user_id on the second call."""
        existing = db.get_user_by_user_id("USR-ADMIN-TEST")
        admin = existing or self.create_user(
            user_id="USR-ADMIN-TEST", email="admin-test@honeychain.test", role="ADMIN"
        )
        return self.auth_headers_for(admin)

    # -- helpers -----------------------------------------------------------
    def post_telemetry(self, payload, *, mode: str = "header", secret: str | None = None):
        """POST to the webhook with valid authentication by default."""
        import hashlib
        import hmac
        import json as json_module

        body = json_module.dumps(payload).encode("utf-8")
        secret = TEST_SECRET if secret is None else secret

        if mode == "header":
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return self.client.post(
                "/api/firebase/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-HoneyChain-Signature": signature,
                },
            )
        if mode == "query":
            return self.client.post(
                f"/api/firebase/webhook?auth={secret}",
                data=body,
                headers={"Content-Type": "application/json"},
            )
        return self.client.post(
            "/api/firebase/webhook", data=body, headers={"Content-Type": "application/json"}
        )

    def seed_hive(self, hive_id: str = "HIVE-001", count: int = 12) -> None:
        """Insert healthy telemetry so quality scoring has real data to use."""
        for index in range(count):
            response = self.post_telemetry(
                {
                    "hive_id": hive_id,
                    "temperature": 34.4 + (index % 3) * 0.2,
                    "humidity": 60.0 + (index % 4),
                    "weight": 42.0 + (index % 3) * 0.3,
                    "audio_frequency": 255.0 + (index % 5),
                    "battery_level": 95.0,
                }
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
