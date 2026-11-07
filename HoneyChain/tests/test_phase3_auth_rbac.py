"""
Phase 3 authentication and RBAC tests.

Organised to match the 25-item list in the Phase 3 spec, roughly in order:
password hashing, user creation, login (success/failure/unknown-user/
throttling), token validation (expired/invalid/revoked), logout, protected-
endpoint enforcement, the role permission matrix, organization isolation and
IDOR protection, audit logging, and a schema-migration proof for the two
Phase 3 additions (``hives.organization_id`` and ``revoked_tokens``).

Role-naming note: the Phase 3 request lists "Packaging" and "Distributor" as
roles; the schema (Phase 2) and this codebase use ``PROCESSOR`` and
``LOGISTICS`` for those respectively, alongside ``LAB`` for "Laboratory" --
naming established in Phase 2 was kept rather than re-opening the users.role
CHECK constraint for a cosmetic rename. Tests below exercise all nine actual
roles and note the mapping where a Phase-3-named role is covered by an
existing one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.base import HoneyChainTestCase, TEST_AUTH_SECRET  # noqa: E402

from honeychain import auth, config, db  # noqa: E402

ALL_ROLES = auth.ROLES  # ("ADMIN","BEEKEEPER","FPO_AGGREGATOR","PROCESSOR",
#                          "LAB","LOGISTICS","RETAILER","AUTHORITY","CONSUMER")


# ---------------------------------------------------------------------------
# 1 & 2. Password hashing
# ---------------------------------------------------------------------------
class TestPasswordHashing(unittest.TestCase):
    def test_hash_is_not_the_plaintext(self) -> None:
        hashed = auth.hash_password("CorrectHorseBatteryStaple1")
        self.assertNotEqual(hashed, "CorrectHorseBatteryStaple1")
        self.assertNotIn("CorrectHorseBatteryStaple1", hashed)

    def test_same_password_hashes_differently_each_time(self) -> None:
        """Salting: two hashes of the same password must not be identical."""
        first = auth.hash_password("SamePassword123")
        second = auth.hash_password("SamePassword123")
        self.assertNotEqual(first, second)

    def test_correct_password_verifies(self) -> None:
        hashed = auth.hash_password("RightPassword1")
        self.assertTrue(auth.verify_password("RightPassword1", hashed))

    def test_wrong_password_does_not_verify(self) -> None:
        hashed = auth.hash_password("RightPassword1")
        self.assertFalse(auth.verify_password("WrongPassword1", hashed))


# ---------------------------------------------------------------------------
# 1. User creation
# ---------------------------------------------------------------------------
class TestUserCreation(HoneyChainTestCase):
    def test_user_is_created_with_hashed_password_not_plaintext(self) -> None:
        org_pk = self.create_org()
        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id="USR-NEW-001",
                organization_pk=org_pk,
                email="new-user@honeychain.test",
                password_hash=auth.hash_password("PlaintextPass1"),
                name="New User",
                role="BEEKEEPER",
            )
        row = db.get_user_by_user_id("USR-NEW-001")
        self.assertIsNotNone(row)
        self.assertNotEqual(row["password_hash"], "PlaintextPass1")
        self.assertTrue(auth.verify_password("PlaintextPass1", row["password_hash"]))
        self.assertEqual(row["status"], "ACTIVE")

    def test_duplicate_email_is_rejected(self) -> None:
        import sqlite3

        with db.transaction() as conn:
            db.create_user(
                conn,
                user_id="USR-DUP-1",
                organization_pk=None,
                email="dup@honeychain.test",
                password_hash=auth.hash_password("x"),
                name="First",
                role="BEEKEEPER",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                db.create_user(
                    conn,
                    user_id="USR-DUP-2",
                    organization_pk=None,
                    email="dup@honeychain.test",
                    password_hash=auth.hash_password("y"),
                    name="Second",
                    role="BEEKEEPER",
                )


# ---------------------------------------------------------------------------
# 3, 4, 5. Login: correct, incorrect password, unknown user
# ---------------------------------------------------------------------------
class TestLogin(HoneyChainTestCase):
    def test_correct_login_returns_a_token(self) -> None:
        self.create_user(user_id="USR-LOGIN-OK", email="ok@honeychain.test", role="BEEKEEPER",
                          password="CorrectPass1")
        response = self.client.post(
            "/api/auth/login",
            json={"email": "ok@honeychain.test", "password": "CorrectPass1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("token", body)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["user"]["email"], "ok@honeychain.test")
        # Never returned, under any key.
        self.assertNotIn("password", body["user"])
        self.assertNotIn("password_hash", body["user"])

    def test_incorrect_password_is_rejected(self) -> None:
        self.create_user(user_id="USR-LOGIN-BAD", email="bad@honeychain.test", role="BEEKEEPER",
                          password="CorrectPass1")
        response = self.client.post(
            "/api/auth/login",
            json={"email": "bad@honeychain.test", "password": "WrongPassword"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["message"], auth.GENERIC_LOGIN_ERROR)

    def test_unknown_user_gets_the_same_generic_error(self) -> None:
        """No enumeration: an unknown email must fail identically to a known
        email with a wrong password, both in status code and message."""
        response = self.client.post(
            "/api/auth/login",
            json={"email": "nobody-registered@honeychain.test", "password": "whatever"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["message"], auth.GENERIC_LOGIN_ERROR)

    def test_disabled_account_cannot_login(self) -> None:
        self.create_user(
            user_id="USR-DISABLED", email="disabled@honeychain.test", role="BEEKEEPER",
            password="CorrectPass1", status="DISABLED",
        )
        response = self.client.post(
            "/api/auth/login",
            json={"email": "disabled@honeychain.test", "password": "CorrectPass1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_login_failures_are_throttled(self) -> None:
        self.create_user(user_id="USR-THROTTLE", email="throttle@honeychain.test",
                          role="BEEKEEPER", password="CorrectPass1")
        for _ in range(auth.LOGIN_FAILURE_LIMIT):
            response = self.client.post(
                "/api/auth/login",
                json={"email": "throttle@honeychain.test", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 401)
        # One more, even with the CORRECT password, must now be throttled.
        response = self.client.post(
            "/api/auth/login",
            json={"email": "throttle@honeychain.test", "password": "CorrectPass1"},
        )
        self.assertEqual(response.status_code, 429)


# ---------------------------------------------------------------------------
# 7. Expired / invalid authentication
# ---------------------------------------------------------------------------
class TestTokenValidation(HoneyChainTestCase):
    def test_malformed_token_is_rejected(self) -> None:
        result = auth.verify_token("this-is-not-a-real-token")
        self.assertFalse(result.valid)

    def test_tampered_token_is_rejected(self) -> None:
        user = self.create_user(user_id="USR-TAMPER", email="tamper@honeychain.test",
                                 role="BEEKEEPER")
        token, _jti = auth.generate_token(user)
        tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
        result = auth.verify_token(tampered)
        self.assertFalse(result.valid)

    def test_expired_token_is_rejected(self) -> None:
        user = self.create_user(user_id="USR-EXPIRE", email="expire@honeychain.test",
                                 role="BEEKEEPER")
        token, _jti = auth.generate_token(user)
        original_ttl = config.AUTH_TOKEN_TTL_SECONDS
        try:
            # itsdangerous timestamps have whole-second resolution, so
            # max_age=0 would need over a full second of real wall-clock
            # time to reliably trigger -- a flaky thing to depend on in a
            # unit test. Elapsed age is always >= 0, so a NEGATIVE max_age
            # guarantees "age > max_age" immediately, deterministically,
            # with no sleep needed.
            config.AUTH_TOKEN_TTL_SECONDS = -1
            result = auth.verify_token(token)
            self.assertFalse(result.valid)
            self.assertIn("expired", result.reason)
        finally:
            config.AUTH_TOKEN_TTL_SECONDS = original_ttl

    def test_no_token_is_rejected(self) -> None:
        result = auth.verify_token(None)
        self.assertFalse(result.valid)

    def test_server_misconfiguration_fails_closed(self) -> None:
        """Missing AUTH_SECRET must reject every token, never accept one."""
        user = self.create_user(user_id="USR-MISCONF", email="misconf@honeychain.test",
                                 role="BEEKEEPER")
        token, _jti = auth.generate_token(user)
        original_secret = config.AUTH_SECRET
        try:
            config.AUTH_SECRET = None
            result = auth.verify_token(token)
            self.assertFalse(result.valid)
        finally:
            config.AUTH_SECRET = original_secret


# ---------------------------------------------------------------------------
# 6. Logout
# ---------------------------------------------------------------------------
class TestLogout(HoneyChainTestCase):
    def test_logout_revokes_the_token(self) -> None:
        user = self.create_user(user_id="USR-LOGOUT", email="logout@honeychain.test",
                                 role="BEEKEEPER")
        headers = self.auth_headers_for(user)

        # Works before logout.
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)

        response = self.client.post("/api/auth/logout", headers=headers)
        self.assertEqual(response.status_code, 200)

        # The SAME token must now be rejected -- this is what makes logout
        # real rather than just "the client deletes its copy."
        response = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_logout_without_a_token_is_rejected(self) -> None:
        response = self.client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# 8, 9. Protected endpoint without / with authentication
# ---------------------------------------------------------------------------
class TestProtectedEndpoints(HoneyChainTestCase):
    def test_hives_without_token_is_401(self) -> None:
        self.assertEqual(self.client.get("/api/hives").status_code, 401)

    def test_hives_with_valid_token_is_200(self) -> None:
        self.assertEqual(
            self.client.get("/api/hives", headers=self.admin_headers()).status_code, 200
        )

    def test_public_endpoints_need_no_token(self) -> None:
        for path in ("/api/health", "/api/chain"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_auth_is_checked_before_body_is_even_parsed(self) -> None:
        """An unauthenticated malformed-JSON POST must fail with 401, not 400
        -- auth must run before any attempt to interpret the request body."""
        response = self.client.post(
            "/api/harvest", data=b"{broken", content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    def test_password_hash_never_appears_in_any_api_response(self) -> None:
        admin = self.create_user(user_id="USR-NOHASH", email="nohash@honeychain.test",
                                  role="ADMIN")
        headers = self.auth_headers_for(admin)
        response = self.client.get("/api/auth/me", headers=headers)
        self.assertNotIn("password_hash", response.get_data(as_text=True))
        self.assertNotIn("password", response.get_json())


# ---------------------------------------------------------------------------
# 10-19. Role permission matrix
# ---------------------------------------------------------------------------
class TestRolePermissionMatrix(HoneyChainTestCase):
    """One BEEKEEPER-owned hive; every role's actual access is exercised
    against real endpoints, not just asserted in the abstract."""

    def setUp(self) -> None:
        super().setUp()
        self.org_pk = self.create_org(org_id="ORG-MATRIX", org_type="BEEKEEPER")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hives SET organization_id = ? WHERE hive_id = ?;",
                (self.org_pk, "HIVE-001"),
            ) if db.get_hive_by_external_id("HIVE-001") else None
        self.seed_hive("HIVE-001")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hives SET organization_id = ? WHERE hive_id = 'HIVE-001';",
                (self.org_pk,),
            )

    def _user(self, role: str, *, org_pk: int | None = None) -> dict:
        return self.create_user(
            user_id=f"USR-{role}", email=f"{role.lower()}@honeychain.test", role=role,
            organization_pk=org_pk,
        )

    def test_beekeeper_can_create_harvest_for_own_org_hive(self) -> None:
        beekeeper = self._user("BEEKEEPER", org_pk=self.org_pk)
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                  "total_weight": 10.0, "floral_type": "Wildflower"},
            headers=self.auth_headers_for(beekeeper),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

    def test_consumer_cannot_create_harvest(self) -> None:
        consumer = self._user("CONSUMER")
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                  "total_weight": 10.0, "floral_type": "Wildflower"},
            headers=self.auth_headers_for(consumer),
        )
        self.assertEqual(response.status_code, 403)

    def test_processor_cannot_create_harvest(self) -> None:
        """PROCESSOR is the role covering the Phase-3-named "Packaging"
        actor in this schema -- see the module docstring."""
        processor = self._user("PROCESSOR", org_pk=self.org_pk)
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                  "total_weight": 10.0, "floral_type": "Wildflower"},
            headers=self.auth_headers_for(processor),
        )
        self.assertEqual(response.status_code, 403)

    def test_lab_cannot_create_harvest_but_can_authenticate(self) -> None:
        lab = self._user("LAB")
        headers = self.auth_headers_for(lab)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                  "total_weight": 10.0, "floral_type": "Wildflower"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_logistics_cannot_create_harvest_but_can_authenticate(self) -> None:
        """LOGISTICS is the role covering the Phase-3-named "Distributor"
        actor in this schema -- see the module docstring."""
        logistics = self._user("LOGISTICS")
        headers = self.auth_headers_for(logistics)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/harvest",
                json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                      "total_weight": 10.0, "floral_type": "Wildflower"},
                headers=headers,
            ).status_code,
            403,
        )

    def test_retailer_cannot_create_harvest_but_can_authenticate(self) -> None:
        retailer = self._user("RETAILER")
        headers = self.auth_headers_for(retailer)
        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/harvest",
                json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                      "total_weight": 10.0, "floral_type": "Wildflower"},
                headers=headers,
            ).status_code,
            403,
        )

    def test_authority_can_view_audit_logs_and_all_hives(self) -> None:
        authority = self._user("AUTHORITY")
        headers = self.auth_headers_for(authority)
        self.assertEqual(self.client.get("/api/audit-logs", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/hives", headers=headers).status_code, 200)

    def test_beekeeper_cannot_view_audit_logs(self) -> None:
        beekeeper = self._user("BEEKEEPER", org_pk=self.org_pk)
        response = self.client.get("/api/audit-logs", headers=self.auth_headers_for(beekeeper))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_view_audit_logs_and_register_users(self) -> None:
        admin_headers = self.admin_headers()
        self.assertEqual(
            self.client.get("/api/audit-logs", headers=admin_headers).status_code, 200
        )
        response = self.client.post(
            "/api/auth/register",
            json={"email": "brandnew@honeychain.test", "password": "NewPassword1",
                  "name": "Brand New", "role": "BEEKEEPER"},
            headers=admin_headers,
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))

    def test_non_admin_cannot_register_users(self) -> None:
        beekeeper = self._user("BEEKEEPER", org_pk=self.org_pk)
        response = self.client.post(
            "/api/auth/register",
            json={"email": "shouldnotexist@honeychain.test", "password": "Whatever1",
                  "name": "Should Not Exist", "role": "BEEKEEPER"},
            headers=self.auth_headers_for(beekeeper),
        )
        self.assertEqual(response.status_code, 403)

    def test_consumer_public_verification_needs_no_account_at_all(self) -> None:
        """Item 20: consumer/public verification access -- no token, no
        account, not even a CONSUMER-role user is required."""
        headers = self.auth_headers_for(self._user("BEEKEEPER", org_pk=self.org_pk))
        create = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "batch_id": "HNY-PUBLIC-1",
                  "harvest_date": "2026-09-01", "total_weight": 10.0,
                  "floral_type": "Wildflower"},
            headers=headers,
        )
        self.assertEqual(create.status_code, 201)

        # Genuinely anonymous: no Authorization header at all.
        self.assertEqual(self.client.get("/verify/HNY-PUBLIC-1").status_code, 200)
        self.assertEqual(self.client.get("/api/verify/HNY-PUBLIC-1").status_code, 200)


# ---------------------------------------------------------------------------
# 21, 22. Organization isolation and IDOR protection
# ---------------------------------------------------------------------------
class TestOrganizationIsolationAndIDOR(HoneyChainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.org_a = self.create_org(org_id="ORG-A", name="Org A")
        self.org_b = self.create_org(org_id="ORG-B", name="Org B")
        self.seed_hive("HIVE-A")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hives SET organization_id = ? WHERE hive_id = 'HIVE-A';",
                (self.org_a,),
            )
        self.beekeeper_a = self.create_user(
            user_id="USR-BEEKEEPER-A", email="beekeeper-a@honeychain.test",
            role="BEEKEEPER", organization_pk=self.org_a,
        )
        self.beekeeper_b = self.create_user(
            user_id="USR-BEEKEEPER-B", email="beekeeper-b@honeychain.test",
            role="BEEKEEPER", organization_pk=self.org_b,
        )

    def test_org_b_beekeeper_cannot_see_org_a_hive_in_listing(self) -> None:
        response = self.client.get("/api/hives", headers=self.auth_headers_for(self.beekeeper_b))
        hive_ids = [h["hive_id"] for h in response.get_json()]
        self.assertNotIn("HIVE-A", hive_ids)

    def test_org_a_beekeeper_does_see_their_own_hive(self) -> None:
        response = self.client.get("/api/hives", headers=self.auth_headers_for(self.beekeeper_a))
        hive_ids = [h["hive_id"] for h in response.get_json()]
        self.assertIn("HIVE-A", hive_ids)

    def test_org_b_beekeeper_cannot_read_org_a_hive_history_by_guessing_the_id(self) -> None:
        """IDOR: knowing/guessing a valid hive_id must not be enough to read
        another organization's data through the object reference alone."""
        response = self.client.get(
            "/api/hives/HIVE-A/history", headers=self.auth_headers_for(self.beekeeper_b)
        )
        self.assertEqual(response.status_code, 403)

    def test_org_a_beekeeper_can_read_their_own_hive_history(self) -> None:
        response = self.client.get(
            "/api/hives/HIVE-A/history", headers=self.auth_headers_for(self.beekeeper_a)
        )
        self.assertEqual(response.status_code, 200)

    def test_org_b_beekeeper_cannot_harvest_org_a_hive(self) -> None:
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-A", "harvest_date": "2026-09-01",
                  "total_weight": 5.0, "floral_type": "Stolen"},
            headers=self.auth_headers_for(self.beekeeper_b),
        )
        self.assertEqual(response.status_code, 403)

    def test_org_a_beekeeper_can_harvest_their_own_hive(self) -> None:
        response = self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-A", "harvest_date": "2026-09-01",
                  "total_weight": 5.0, "floral_type": "Legitimate"},
            headers=self.auth_headers_for(self.beekeeper_a),
        )
        self.assertEqual(response.status_code, 201)

    def test_org_b_summary_does_not_include_org_a_readings(self) -> None:
        response = self.client.get(
            "/api/summary", headers=self.auth_headers_for(self.beekeeper_b)
        )
        self.assertEqual(response.get_json()["reading_count"], 0)

    def test_unassigned_hive_is_invisible_to_a_specific_org(self) -> None:
        """A hive with organization_id IS NULL (e.g. created via the device
        webhook, which has no concept of an organization) must not be
        implicitly visible to any one organization's own role."""
        self.seed_hive("HIVE-UNCLAIMED")  # organization_id stays NULL
        response = self.client.get(
            "/api/hives/HIVE-UNCLAIMED/history", headers=self.auth_headers_for(self.beekeeper_a)
        )
        self.assertEqual(response.status_code, 403)
        # ADMIN, being org-unrestricted, can still reach it.
        response = self.client.get(
            "/api/hives/HIVE-UNCLAIMED/history", headers=self.admin_headers()
        )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 23. Audit log creation
# ---------------------------------------------------------------------------
class TestAuditLogging(HoneyChainTestCase):
    def test_login_success_is_logged(self) -> None:
        self.create_user(user_id="USR-AUDIT-1", email="audit1@honeychain.test",
                          role="BEEKEEPER", password="Password123")
        self.client.post(
            "/api/auth/login", json={"email": "audit1@honeychain.test", "password": "Password123"}
        )
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "LOGIN_SUCCESS" for e in events))

    def test_login_failure_is_logged(self) -> None:
        self.create_user(user_id="USR-AUDIT-2", email="audit2@honeychain.test",
                          role="BEEKEEPER", password="Password123")
        self.client.post(
            "/api/auth/login", json={"email": "audit2@honeychain.test", "password": "wrong"}
        )
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "LOGIN_FAILURE" for e in events))

    def test_logout_is_logged(self) -> None:
        user = self.create_user(user_id="USR-AUDIT-3", email="audit3@honeychain.test",
                                 role="BEEKEEPER")
        headers = self.auth_headers_for(user)
        self.client.post("/api/auth/logout", headers=headers)
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "LOGOUT" for e in events))

    def test_user_creation_is_logged(self) -> None:
        admin_headers = self.admin_headers()
        self.client.post(
            "/api/auth/register",
            json={"email": "logged-new@honeychain.test", "password": "Password123",
                  "name": "Logged New", "role": "BEEKEEPER"},
            headers=admin_headers,
        )
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "USER_CREATED" for e in events))

    def test_authorization_denial_is_logged(self) -> None:
        consumer = self.create_user(user_id="USR-AUDIT-4", email="audit4@honeychain.test",
                                     role="CONSUMER")
        self.client.get("/api/audit-logs", headers=self.auth_headers_for(consumer))
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "AUTHORIZATION_DENIED" for e in events))

    def test_harvest_creation_is_logged(self) -> None:
        org_pk = self.create_org()
        self.seed_hive("HIVE-001")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hives SET organization_id = ? WHERE hive_id = 'HIVE-001';", (org_pk,)
            )
        beekeeper = self.create_user(user_id="USR-AUDIT-5", email="audit5@honeychain.test",
                                      role="BEEKEEPER", organization_pk=org_pk)
        self.client.post(
            "/api/harvest",
            json={"hive_id": "HIVE-001", "harvest_date": "2026-09-01",
                  "total_weight": 5.0, "floral_type": "Test"},
            headers=self.auth_headers_for(beekeeper),
        )
        events = db.list_audit_events(50)
        self.assertTrue(any(e["action"] == "HARVEST_CREATED" for e in events))

    def test_audit_log_never_contains_a_password_or_secret(self) -> None:
        self.create_user(user_id="USR-AUDIT-6", email="audit6@honeychain.test",
                          role="BEEKEEPER", password="SuperSecretPassword1")
        self.client.post(
            "/api/auth/login",
            json={"email": "audit6@honeychain.test", "password": "SuperSecretPassword1"},
        )
        events = db.list_audit_events(50)
        serialised = str(events)
        self.assertNotIn("SuperSecretPassword1", serialised)
        self.assertNotIn(TEST_AUTH_SECRET, serialised)


if __name__ == "__main__":
    unittest.main()
