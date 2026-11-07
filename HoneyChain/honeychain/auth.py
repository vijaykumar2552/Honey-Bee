"""
honeychain.auth
================
User authentication and role-based access control.

Design deliberately mirrors ``honeychain.security`` (the device-webhook auth
module already in this codebase): fail closed when misconfigured, one
generic error message for every login failure mode (never reveals whether an
email exists), constant-time password comparison (delegated to werkzeug,
which is timing-safe internally), and nothing here ever logs or returns a
password, a password hash, or a raw token.

Tokens are signed with ``itsdangerous.URLSafeTimedSerializer`` -- the same
primitive Flask itself uses for signed session cookies -- rather than a raw
JWT library. That keeps the dependency footprint at zero (itsdangerous ships
with Flask already) while giving the same guarantee: a tamper-evident,
time-limited bearer credential. A token carries only ``{user_id, jti}``; the
user's current role/status/organization is always re-read from the database
on every single request, never trusted from the token payload, so an admin
suspending an account or changing a role takes effect on the very next
request -- not just after the token eventually expires.

Known, documented limitation: tokens are short-lived (1 hour by default) and
there is no refresh-token rotation in this phase. An expired session means
logging in again. This is a deliberate scope boundary, not an oversight --
see the Phase 3 report.
"""

from __future__ import annotations

import logging
import secrets as _secrets
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from flask import Response, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import config, db

logger = logging.getLogger(__name__)

#: Single generic message for every login failure mode. Distinguishing
#: "no such user" from "wrong password" would let an attacker enumerate
#: which emails are registered.
GENERIC_LOGIN_ERROR = "Invalid email or password"

#: Single generic message for every authentication failure on a protected
#: route (missing token, expired token, malformed token, revoked token,
#: deleted user, suspended user) -- same reasoning as GENERIC_LOGIN_ERROR.
GENERIC_AUTH_ERROR = "Unauthorized: valid authentication required"

FORBIDDEN_ERROR = "Forbidden: your role does not permit this action"

ROLES: tuple[str, ...] = (
    "ADMIN",
    "BEEKEEPER",
    "FPO_AGGREGATOR",
    "PROCESSOR",
    "LAB",
    "LOGISTICS",
    "RETAILER",
    "AUTHORITY",
    "CONSUMER",
)

#: Roles allowed to act on behalf of any organization, bypassing the
#: organization-isolation checks applied to every other role.
ORG_UNRESTRICTED_ROLES: tuple[str, ...] = ("ADMIN", "AUTHORITY")

_TOKEN_SALT = "honeychain-auth-token-v1"

#: Login-attempt throttle: after this many failures for the same email
#: within the window below, further attempts are rejected with 429 without
#: even checking the password, until the window rolls past.
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Werkzeug's default method (scrypt) -- salted, slow-by-design, no extra
    dependency beyond what Flask already requires."""
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Whether an auth secret is present (never reveals its value)."""
    return bool(config.AUTH_SECRET)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.AUTH_SECRET, salt=_TOKEN_SALT)


def generate_token(user_row: dict[str, Any]) -> tuple[str, str]:
    """Issue a bearer token for an already-authenticated user.

    Returns ``(token, jti)``. The caller has no other use for ``jti`` today
    beyond tests, but it is what :func:`revoke_token`-style logout matches
    against, so it is handed back rather than buried.
    """
    jti = _secrets.token_hex(16)
    token = _serializer().dumps({"user_id": user_row["user_id"], "jti": jti})
    return token, jti


@dataclass(frozen=True)
class TokenResult:
    valid: bool
    user: dict[str, Any] | None = None
    jti: str | None = None
    reason: str = ""


def verify_token(token: str | None) -> TokenResult:
    """Validate a bearer token. Never raises.

    Checks, in order: server is configured, token is present, signature and
    expiry are valid, the token has not been explicitly revoked (logout), the
    user it names still exists, and that user's account is still ACTIVE.
    """
    if not config.AUTH_SECRET:
        logger.error("HONEYCHAIN_AUTH_SECRET is not configured; rejecting all tokens")
        return TokenResult(False, reason="server secret not configured")

    if not token:
        return TokenResult(False, reason="no token presented")

    try:
        payload = _serializer().loads(token, max_age=config.AUTH_TOKEN_TTL_SECONDS)
    except SignatureExpired:
        return TokenResult(False, reason="token expired")
    except BadSignature:
        return TokenResult(False, reason="token invalid")

    jti = payload.get("jti")
    if jti and db.is_token_revoked(jti):
        return TokenResult(False, reason="token revoked")

    user = db.get_user_by_user_id(payload.get("user_id"))
    if user is None:
        return TokenResult(False, reason="user no longer exists")
    if user["status"] != "ACTIVE":
        return TokenResult(False, reason="account not active")

    return TokenResult(True, user=user, jti=jti)


def _extract_token() -> str | None:
    """Bearer token from the Authorization header, or a ``?token=`` query
    parameter as a fallback -- needed for the SSE telemetry stream, whose
    browser EventSource API cannot attach custom headers. This mirrors the
    existing ``?auth=`` fallback already used by the device webhook for
    exactly the same class of client limitation.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        candidate = header[7:].strip()
        if candidate:
            return candidate
    query_token = request.args.get("token")
    return query_token.strip() if query_token else None


def _auth_error() -> Response:
    response = jsonify({"error": "unauthorized", "message": GENERIC_AUTH_ERROR})
    response.status_code = 401
    return response


def _forbidden_error() -> Response:
    response = jsonify({"error": "forbidden", "message": FORBIDDEN_ERROR})
    response.status_code = 403
    return response


def current_user() -> dict[str, Any] | None:
    """The authenticated user for this request, if ``require_auth``/
    ``require_role`` already ran. Returns ``None`` outside a protected view."""
    return getattr(g, "current_user", None)


def current_token_jti() -> str | None:
    """The jti of the token used for this request, for logout to revoke."""
    return getattr(g, "current_token_jti", None)


def require_auth(view: Callable) -> Callable:
    """Any authenticated, active user -- no specific role required."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = verify_token(_extract_token())
        if not result.valid:
            return _auth_error()
        g.current_user = result.user
        g.current_token_jti = result.jti
        return view(*args, **kwargs)

    return wrapped


def require_role(*roles: str) -> Callable[[Callable], Callable]:
    """Authenticated AND the user's role is one of ``roles``.

    Centralised here on purpose -- see the Phase 3 report -- rather than each
    route hand-rolling its own ``if user.role != "X"`` check, which is exactly
    how authorization bugs get introduced one route at a time.
    """

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = verify_token(_extract_token())
            if not result.valid:
                return _auth_error()
            if result.user["role"] not in roles:
                db.insert_audit_event(
                    actor_user_pk=_pk_of(result.user),
                    organization_pk=result.user.get("organization_id"),
                    action="AUTHORIZATION_DENIED",
                    resource_type="route",
                    resource_id=request.path,
                    metadata=f"role={result.user['role']} required={','.join(roles)}",
                    ip_address=request.remote_addr,
                    result="FAILURE",
                )
                return _forbidden_error()
            g.current_user = result.user
            g.current_token_jti = result.jti
            return view(*args, **kwargs)

        return wrapped

    return decorator


def can_access_organization(user: dict[str, Any], organization_pk: int | None) -> bool:
    """Organization-isolation check used inline by routes that load a
    specific resource and must confirm the caller may see/act on it.

    ADMIN and AUTHORITY bypass the check entirely (see ORG_UNRESTRICTED_ROLES).
    Everyone else must belong to exactly the organization that owns the
    resource. A resource with no organization (``organization_pk is None`` --
    e.g. a hive nobody has claimed yet) is accessible to nobody but an
    unrestricted role, which must explicitly assign ownership first.
    """
    if user["role"] in ORG_UNRESTRICTED_ROLES:
        return True
    if organization_pk is None:
        return False
    return user.get("organization_id") == organization_pk


def _pk_of(user: dict[str, Any]) -> int | None:
    """Internal integer primary key for a user dict, for audit_log FK columns."""
    return user.get("id")
