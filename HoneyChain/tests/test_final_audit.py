"""
Final-audit fixes: organization management API, rate limiting, original
dashboard authentication, demo seed, and README honesty.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.base import HoneyChainTestCase, REPO_ROOT

from honeychain import config, db
from honeychain.ratelimit import RateLimiter

ROOT = Path(REPO_ROOT)


class TestOrganizationApi(HoneyChainTestCase):
    def test_admin_creates_lists_and_suspends_organizations(self) -> None:
        admin = self.admin_headers()
        r = self.client.post("/api/admin/organizations", headers=admin,
                             json={"org_id": "ORG-NEW-1", "name": "New Apiary", "org_type": "beekeeper"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {**r.get_json(), "org_id": "ORG-NEW-1", "org_type": "BEEKEEPER",
                                        "status": "ACTIVE"})
        self.assertNotIn("id", r.get_json())                       # no internal key
        auto = self.client.post("/api/admin/organizations", headers=admin,
                                json={"name": "Auto Id Lab", "org_type": "LAB"}).get_json()
        self.assertRegex(auto["org_id"], r"^ORG-[0-9A-F]{8}$")
        ids = [o["org_id"] for o in self.client.get("/api/admin/organizations", headers=admin).get_json()]
        self.assertIn("ORG-NEW-1", ids)
        s = self.client.post("/api/admin/organizations/ORG-NEW-1/status", headers=admin,
                             json={"status": "SUSPENDED"})
        self.assertEqual(s.get_json()["status"], "SUSPENDED")
        actions = [e["action"] for e in db.list_audit_events(50)]
        self.assertIn("ORGANIZATION_CREATED", actions)
        self.assertIn("ORGANIZATION_STATUS_CHANGED", actions)

    def test_validation_conflicts_and_not_found(self) -> None:
        admin = self.admin_headers()
        post = lambda body: self.client.post("/api/admin/organizations", headers=admin, json=body)  # noqa: E731
        self.assertEqual(post({"org_id": "ORG-DUP", "name": "A", "org_type": "LAB"}).status_code, 400)
        self.assertEqual(post({"org_id": "ORG-DUP", "name": "Dup Org", "org_type": "LAB"}).status_code, 201)
        self.assertEqual(post({"org_id": "ORG-DUP", "name": "Dup Org", "org_type": "LAB"}).status_code, 409)
        self.assertEqual(post({"name": "Bad Type", "org_type": "PIRATE"}).status_code, 400)
        self.assertEqual(post({"org_id": "bad id!", "name": "Bad Id", "org_type": "LAB"}).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/organizations/ORG-NOPE/status", headers=admin,
                                          json={"status": "SUSPENDED"}).status_code, 404)
        self.assertEqual(self.client.post("/api/admin/organizations/ORG-DUP/status", headers=admin,
                                          json={"status": "DELETED"}).status_code, 400)

    def test_only_admin_may_write_authority_may_read(self) -> None:
        org = self.create_org(org_id="ORG-X", name="X")
        for role in ("AUTHORITY", "BEEKEEPER", "PROCESSOR", "CONSUMER"):
            h = self.auth_headers_for(self.create_user(user_id=f"USR-{role}", email=f"{role}@a.test",
                                                       role=role, organization_pk=org))
            self.assertEqual(self.client.post("/api/admin/organizations", headers=h,
                                              json={"name": "Nope Org", "org_type": "LAB"}).status_code, 403)
            self.assertEqual(self.client.post("/api/admin/organizations/ORG-X/status", headers=h,
                                              json={"status": "SUSPENDED"}).status_code, 403)
            expect = 200 if role == "AUTHORITY" else 403
            self.assertEqual(self.client.get("/api/admin/organizations", headers=h).status_code, expect)
        self.assertEqual(self.client.get("/api/admin/organizations").status_code, 401)


class TestRateLimiting(HoneyChainTestCase):
    def test_limiter_window_and_bounded_keys(self) -> None:
        lim = RateLimiter(2, 10)
        self.assertEqual([lim.hit("a", now=0)[0], lim.hit("a", now=1)[0], lim.hit("a", now=2)[0]],
                         [True, True, False])
        self.assertGreaterEqual(lim.hit("a", now=3)[1], 1)
        self.assertTrue(lim.hit("a", now=10.5)[0])                 # oldest hit expired
        with mock.patch.object(RateLimiter, "MAX_KEYS", 3):
            small = RateLimiter(1, 60)
            for i in range(10):
                small.hit(f"k{i}", now=0)
            self.assertLessEqual(len(small._hits), 3)

    def test_public_verify_is_limited_before_anything_is_logged(self) -> None:
        for _ in range(config.PUBLIC_VERIFY_PER_MINUTE):
            self.assertEqual(self.client.post("/api/public/verify", json={"qr": "x"}).status_code, 200)
        logged = db.query_one("SELECT COUNT(*) AS n FROM consumer_scans;")["n"]
        r = self.client.post("/api/public/verify", json={"qr": "x"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()["result"], "RATE_LIMITED")
        self.assertGreaterEqual(int(r.headers["Retry-After"]), 1)
        self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM consumer_scans;")["n"], logged)

    def test_login_is_limited_per_address_across_different_emails(self) -> None:
        for i in range(config.LOGIN_PER_MINUTE_PER_ADDRESS):
            r = self.client.post("/api/auth/login", json={"email": f"spray{i}@x.test", "password": "nope"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/auth/login", json={"email": "another@x.test", "password": "nope"})
        self.assertEqual(r.status_code, 429)


class TestOriginalDashboardAuth(unittest.TestCase):
    def test_dashboard_sends_the_token_and_no_longer_uses_unauthenticatable_sse(self) -> None:
        js = (ROOT / "static/js/dashboard.js").read_text(encoding="utf-8")
        self.assertIn("headers.Authorization = 'Bearer ' + token", js)
        self.assertIn("var TOKEN_KEY = 'honeychain.token';", js)
        self.assertNotIn("new EventSource", js)
        self.assertIn("honeychain.token", (ROOT / "frontend/src/services/api.ts").read_text())


class TestReadmeHonesty(unittest.TestCase):
    def test_readme_labels_synthetic_metrics_and_current_auth(self) -> None:
        readme = (ROOT / "README.md").read_text()
        api = (ROOT / "docs/API.md").read_text()
        self.assertIn("trained **only on synthetic data**", readme)
        self.assertIn("**not real-world accuracy**", readme)
        self.assertIn("local Hardhat chain", readme)
        self.assertIn("Physical-hardware operation is not verified", readme)
        self.assertIn("| `GET` | `/api/telemetry/latest` | any authenticated user |", api)
        self.assertIn("legacy, **disabled by default**", api)
        self.assertTrue((ROOT / "scripts/seed_demo.py").is_file())     # referenced by README
        for doc in ("ARCHITECTURE", "API", "SECURITY", "DATABASE", "DEPLOYMENT"):
            self.assertTrue((ROOT / "docs" / f"{doc}.md").is_file(), doc)
        # Negative guards (restored after the README rewrite): no unlabelled
        # accuracy/detection figures anywhere in the README.
        import re
        self.assertNotIn("Measured on held-out samples from the last run:", readme)
        self.assertIsNone(re.search(r"\d+(\.\d+)?\s*%", readme),
                          "README must not quote percentage figures (synthetic metrics live in docs/ML.md)")
        for claim in ("varroa detection", "disease detection", "production-ready", "production ready"):
            self.assertNotIn(claim, readme.lower(), claim)


class TestDemoSeed(unittest.TestCase):
    def test_seed_runs_the_real_flow_and_refuses_to_run_twice(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="hc-seed-"))
        env = {**os.environ, "HONEYCHAIN_DB_PATH": str(tmp / "demo.db"),
               "HONEYCHAIN_AUTH_SECRET": "seed-auth", "HONEYCHAIN_DEVICE_MASTER_KEY": "seed-master",
               "HONEYCHAIN_LOG_LEVEL": "ERROR"}
        env.pop("HONEYCHAIN_LEGACY_WEBHOOK_ENABLED", None)
        run = lambda: subprocess.run([sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env,  # noqa: E731
                                     capture_output=True, text=True, timeout=240)
        first = run()
        self.assertEqual(first.returncode, 0, first.stderr[-800:])
        self.assertIn("Consumer verification: AUTHENTIC", first.stdout)
        self.assertIn("is ON_SHELF", first.stdout)
        self.assertNotIn("password_hash", first.stdout)
        second = run()
        self.assertEqual(second.returncode, 1)
        self.assertIn("refusing to seed twice", second.stderr)

    def test_seed_refuses_without_secrets(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("HONEYCHAIN_AUTH_SECRET", "HONEYCHAIN_DEVICE_MASTER_KEY")}
        env["HONEYCHAIN_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "x.db")
        env["HONEYCHAIN_ENV"] = "production"
        env["HONEYCHAIN_AUTH_SECRET"] = ""
        env["HONEYCHAIN_DEVICE_MASTER_KEY"] = ""
        out = subprocess.run([sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
