"""Release audit: database failure during a request is a clean 500 with no
internal detail (no SQL, exception type or traceback leaked)."""

from __future__ import annotations

import sqlite3
from unittest import mock

from tests.base import HoneyChainTestCase

from honeychain import db


class TestDatabaseFailure(HoneyChainTestCase):
    def test_database_error_returns_generic_500_without_leaking_internals(self) -> None:
        headers = self.admin_headers()
        boom = sqlite3.OperationalError("disk I/O error: SELECT * FROM hives WHERE secret_internal")
        with mock.patch.object(db, "list_hives", side_effect=boom):
            r = self.client.get("/api/hives", headers=headers)
        self.assertEqual(r.status_code, 500)
        text = r.get_data(as_text=True)
        self.assertTrue(r.is_json)
        for leaked in ("OperationalError", "SELECT", "secret_internal", "Traceback", "disk I/O"):
            self.assertNotIn(leaked, text)

    def test_public_verification_survives_a_database_error_without_leaking(self) -> None:
        with mock.patch.object(db, "query_one", side_effect=sqlite3.OperationalError("locked")):
            r = self.client.post("/api/public/verify", json={"qr": "HC1.UNIT-ABCDEFGHIJKLMNOP." + "a" * 43})
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("locked", r.get_data(as_text=True))
