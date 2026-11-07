"""Configuration fallback and production validation tests."""

from __future__ import annotations

import os
import subprocess
import sys
from unittest import mock

from tests.base import HoneyChainTestCase, REPO_ROOT

from honeychain import config


class TestConfigurationProblems(HoneyChainTestCase):
    def test_fully_configured_test_environment_reports_no_problems(self) -> None:
        self.assertEqual(config.configuration_problems(), [])
        self.assertIs(self._health_field("configuration_ok"), True)

    def _health_field(self, key):
        body = self.client.get("/api/health").get_json()
        if key in body:
            return body[key]
        for value in body.values():
            if isinstance(value, dict) and key in value:
                return value[key]
        raise AssertionError(f"{key} not in /api/health")

    def test_missing_required_keys_are_named(self) -> None:
        with mock.patch.object(config, "AUTH_SECRET", None), \
             mock.patch.object(config, "DEVICE_MASTER_KEY", None):
            problems = " ".join(config.configuration_problems())
        self.assertIn("HONEYCHAIN_AUTH_SECRET", problems)
        self.assertIn("HONEYCHAIN_DEVICE_MASTER_KEY", problems)

    def test_anchoring_enabled_without_chain_settings_is_reported(self) -> None:
        with mock.patch.multiple(config, CHAIN_ANCHOR_ENABLED=True, RPC_URL=None,
                                 CONTRACT_ADDRESS=None, CHAIN_ID=None), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRIVATE_KEY", None)
            problems = " ".join(config.configuration_problems())
        for name in ("RPC_URL", "CONTRACT_ADDRESS", "CHAIN_ID", "PRIVATE_KEY"):
            self.assertIn(name, problems)
        self.assertIn("never as anchored", problems)

    def test_development_startup_uses_fallback_when_auth_secret_is_missing(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "HONEYCHAIN_AUTH_SECRET"}
        env["HONEYCHAIN_DB_PATH"] = "/tmp/hc-config-check.db"
        env["HONEYCHAIN_LOG_LEVEL"] = "INFO"   # the test harness silences logs below CRITICAL
        out = subprocess.run([sys.executable, "-c",
                              "from honeychain import config; "
                              "from honeychain.app import create_app; "
                              "create_app(load_model=False); "
                              "assert config.AUTH_SECRET"],
                             cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        self.assertNotIn("CONFIGURATION: HONEYCHAIN_AUTH_SECRET is not set", out.stderr + out.stdout)

    def test_env_example_documents_every_variable_the_code_reads(self) -> None:
        import re
        from pathlib import Path
        root = Path(REPO_ROOT)
        code = "".join(p.read_text() for p in (root / "honeychain").glob("*.py"))
        used = set(re.findall(r'"(HONEYCHAIN_[A-Z0-9_]+)"', code))
        used = {u for u in used if not u.endswith("_")}
        documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (root / ".env.example").read_text(), re.M))
        self.assertEqual(sorted(used - documented), [])
