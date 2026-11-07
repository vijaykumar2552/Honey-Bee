"""
honeychain.config
=================
Single source of truth for every runtime setting.

Secrets are read from the process environment (optionally seeded from a local
``.env`` file). Development mode creates stable, randomly generated fallback
secrets in the git-ignored ``instance/.dev-secrets.json`` file. Production
mode remains fail-closed and requires explicit secrets.
"""

from __future__ import annotations

import logging
import os
import json
import secrets
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Filesystem layout (everything is resolved relative to the repository root so
# the app behaves identically no matter which directory it is launched from)
# --------------------------------------------------------------------------
PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
BASE_DIR: Final[Path] = PACKAGE_DIR.parent

TEMPLATES_DIR: Final[Path] = BASE_DIR / "templates"
STATIC_DIR: Final[Path] = BASE_DIR / "static"
QRCODE_DIR: Final[Path] = STATIC_DIR / "qrcodes"
MODELS_DIR: Final[Path] = BASE_DIR / "models"
DATA_DIR: Final[Path] = BASE_DIR / "data"
INSTANCE_DIR: Final[Path] = BASE_DIR / "instance"
SCHEMA_PATH: Final[Path] = BASE_DIR / "database" / "schema.sql"

MODEL_PATH: Final[Path] = MODELS_DIR / "isolation_forest.joblib"
FEATURE_COLUMNS_PATH: Final[Path] = MODELS_DIR / "feature_columns.joblib"
MODEL_METADATA_PATH: Final[Path] = MODELS_DIR / "model_metadata.json"


def _load_dotenv() -> None:
    """Seed os.environ from a local .env file if python-dotenv is installed.

    Values already present in the real environment always win, so container /
    systemd / CI configuration is never overridden by a stale checked-out file.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return
    for candidate in (BASE_DIR / ".env", BASE_DIR / "instance" / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


_load_dotenv()


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s; using default %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s; using default %s", name, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


RUNTIME_ENV: Final[str] = _env_str("HONEYCHAIN_ENV", "development").lower()
IS_DEVELOPMENT: Final[bool] = RUNTIME_ENV in {"development", "dev", "demo", "test"}
_DEV_SECRETS_PATH: Final[Path] = INSTANCE_DIR / ".dev-secrets.json"


def _development_secret(name: str) -> str:
    """Return a stable, randomly generated secret for local development.

    The file is ignored by git and keeps device credentials valid across
    application restarts. Production never calls this function.
    """
    if _DEV_SECRETS_PATH.is_file():
        try:
            values = json.loads(_DEV_SECRETS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Unable to read development secrets from {_DEV_SECRETS_PATH}"
            ) from exc
        value = values.get(name)
        if isinstance(value, str) and value:
            return value
        raise RuntimeError(f"Development secret {name!r} is missing")

    values = {
        "HONEYCHAIN_AUTH_SECRET": secrets.token_urlsafe(48),
        "HONEYCHAIN_DEVICE_MASTER_KEY": secrets.token_urlsafe(48),
        "HONEYCHAIN_WEBHOOK_SECRET": secrets.token_urlsafe(48),
    }
    try:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        _DEV_SECRETS_PATH.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"Unable to persist development secrets to {_DEV_SECRETS_PATH}"
        ) from exc
    return values[name]


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    return _development_secret(name) if IS_DEVELOPMENT else None


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------
#: Shared secret used for HMAC-SHA256 signing of the Firebase/ESP32 webhook.
#: Development mode supplies a persisted random fallback; production remains
#: fail-closed when this value is absent.
WEBHOOK_SECRET: Final[str | None] = _secret("HONEYCHAIN_WEBHOOK_SECRET")

#: Secret used to sign user-authentication bearer tokens (honeychain.auth).
#: Deliberately separate from WEBHOOK_SECRET -- they protect different things
#: and must be independently rotatable. Development mode supplies a persisted
#: random fallback; production remains fail-closed when this value is absent.
AUTH_SECRET: Final[str | None] = _secret("HONEYCHAIN_AUTH_SECRET")

#: How long a login token remains valid. Short-lived by design (1 hour
#: default) since this phase does not implement refresh-token rotation --
#: an expired session simply requires logging in again. See
#: docs/API.md for that documented limitation.
AUTH_TOKEN_TTL_SECONDS: Final[int] = _env_int("HONEYCHAIN_AUTH_TOKEN_TTL", 3600)

#: Header the signature is expected in.
SIGNATURE_HEADER: Final[str] = "X-HoneyChain-Signature"

#: Query-string parameter carrying the raw shared secret. Required because
#: Firebase Realtime Database webhooks / Cloud Functions triggers cannot always
#: attach custom headers.
AUTH_QUERY_PARAM: Final[str] = "auth"

# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
DATABASE_PATH: Final[Path] = Path(
    _env_str("HONEYCHAIN_DB_PATH", str(INSTANCE_DIR / "honeychain.db"))
).expanduser()

PUBLIC_BASE_URL: Final[str] = _env_str(
    "HONEYCHAIN_PUBLIC_BASE_URL", "http://localhost:5000"
).rstrip("/")

HOST: Final[str] = _env_str("HONEYCHAIN_HOST", "0.0.0.0")
PORT: Final[int] = _env_int("HONEYCHAIN_PORT", 5000)
DEBUG: Final[bool] = _env_bool("HONEYCHAIN_DEBUG", False)
LOG_LEVEL: Final[str] = _env_str("HONEYCHAIN_LOG_LEVEL", "INFO").upper()

#: Maximum request body accepted on the webhook (bytes). Guards against a
#: hostile client streaming an unbounded JSON document into memory.
MAX_CONTENT_LENGTH: Final[int] = _env_int("HONEYCHAIN_MAX_CONTENT_LENGTH", 1_048_576)

#: How many joined telemetry+health rows /api/telemetry/latest returns.
LATEST_LIMIT: Final[int] = _env_int("HONEYCHAIN_LATEST_LIMIT", 20)

# --------------------------------------------------------------------------
# Server-Sent Events
# --------------------------------------------------------------------------
SSE_KEEPALIVE_SECONDS: Final[float] = _env_float("HONEYCHAIN_SSE_KEEPALIVE", 15.0)
SSE_CLIENT_QUEUE_SIZE: Final[int] = _env_int("HONEYCHAIN_SSE_QUEUE_SIZE", 100)
SSE_MAX_CLIENTS: Final[int] = _env_int("HONEYCHAIN_SSE_MAX_CLIENTS", 200)

# --------------------------------------------------------------------------
# Health evaluation
# --------------------------------------------------------------------------
#: Sudden-weight-loss (swarming) detector: a drop of at least this many kg
#: within SWARM_WINDOW_SECONDS is escalated to CRITICAL. This runs *in addition
#: to* the absolute-threshold rule engine, which is left exactly as specified.
SWARM_DROP_KG: Final[float] = _env_float("HONEYCHAIN_SWARM_DROP_KG", 2.0)
SWARM_WINDOW_SECONDS: Final[int] = _env_int("HONEYCHAIN_SWARM_WINDOW_SECONDS", 60)

#: Per-client-address limits (honeychain.ratelimit; in-process, per worker).
PUBLIC_VERIFY_PER_MINUTE: Final[int] = _env_int("HONEYCHAIN_PUBLIC_VERIFY_PER_MINUTE", 30)
LOGIN_PER_MINUTE_PER_ADDRESS: Final[int] = _env_int("HONEYCHAIN_LOGIN_PER_MINUTE_PER_ADDRESS", 20)

#: Built React frontend (``cd frontend && npm run build``), served at /app/.
FRONTEND_DIST: Final[Path] = BASE_DIR / "frontend" / "dist"

# --------------------------------------------------------------------------
# Oversight signals (Phase 10)
# --------------------------------------------------------------------------
#: Wrong/forged-tag scans naming ONE real unit before it is flagged.
SIGNAL_FORGERY_ATTEMPTS: Final[int] = _env_int("HONEYCHAIN_SIGNAL_FORGERY_ATTEMPTS", 3)
#: Authentic scans of ONE unit within 24 h before it is flagged (copied label).
SIGNAL_SCAN_VELOCITY_24H: Final[int] = _env_int("HONEYCHAIN_SIGNAL_SCAN_VELOCITY_24H", 10)

# --------------------------------------------------------------------------
# Intelligence (Phase 7)
# --------------------------------------------------------------------------
def _rule_threshold_overrides() -> dict[str, float]:
    """HONEYCHAIN_RULE_<NAME> overrides for the deterministic safety rules.

    Fails CLOSED: an unparseable, non-finite, out-of-range or inverted
    threshold raises at startup instead of silently running the safety
    rules with a bad value or quietly falling back to a default.
    """
    names = ("TEMP_CRITICAL_LOW", "TEMP_CRITICAL_HIGH", "WEIGHT_CRITICAL_LOW",
             "HUMIDITY_WARNING_LOW", "HUMIDITY_WARNING_HIGH")
    out: dict[str, float] = {}
    for name in names:
        raw = os.environ.get(f"HONEYCHAIN_RULE_{name}")
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"HONEYCHAIN_RULE_{name} must be a number, got {raw!r}") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"HONEYCHAIN_RULE_{name} must be finite")
        out[name] = value
    bounds = {"TEMP": (-10.0, 60.0), "WEIGHT": (0.0, 200.0), "HUMIDITY": (0.0, 100.0)}
    for name, value in out.items():
        low, high = bounds[name.split("_")[0]]
        if not low <= value <= high:
            raise ValueError(f"HONEYCHAIN_RULE_{name}={value} is outside {low}..{high}")
    from .rules import DEFAULT_THRESHOLDS  # noqa: WPS433 (avoid import cycle at module top)
    merged = {**DEFAULT_THRESHOLDS, **out}
    for low, high in (("TEMP_CRITICAL_LOW", "TEMP_CRITICAL_HIGH"),
                      ("HUMIDITY_WARNING_LOW", "HUMIDITY_WARNING_HIGH")):
        if merged[low] >= merged[high]:
            raise ValueError(f"{low} ({merged[low]}) must be below {high} ({merged[high]})")
    return out


RULE_THRESHOLDS: Final[dict[str, float]] = _rule_threshold_overrides()

#: Highest status an ML anomaly may raise a reading to. The current model is
#: trained on SYNTHETIC data only, so by default it can raise a WARNING
#: ("risk signal") but never a CRITICAL alarm on its own -- CRITICAL stays
#: with the deterministic rules. Only WARNING or CRITICAL are accepted.
_ml_cap = os.environ.get("HONEYCHAIN_ML_MAX_ESCALATION", "WARNING").strip().upper()
if _ml_cap not in ("WARNING", "CRITICAL"):
    raise ValueError("HONEYCHAIN_ML_MAX_ESCALATION must be WARNING or CRITICAL")
ML_MAX_ESCALATION: Final[str] = _ml_cap

#: Robust (median/MAD) outlier detection on device telemetry. 3.5 is the
#: Iglewicz & Hoaglin (1993) recommended cut-off for the modified z-score.
SENSOR_ROBUST_Z_THRESHOLD: Final[float] = _env_float("HONEYCHAIN_SENSOR_ROBUST_Z", 3.5)
SENSOR_ROBUST_MIN_HISTORY: Final[int] = _env_int("HONEYCHAIN_SENSOR_ROBUST_MIN_HISTORY", 20)

# --------------------------------------------------------------------------
# Per-device IoT authentication (Phase 4) -- see honeychain/device_auth.py
# and docs/IOT_PROTOCOL.md.
# --------------------------------------------------------------------------
#: Master key from which every device's individual secret is derived
#: (HMAC-SHA256(master_key, "honeychain-device-v1|<device_id>|<version>")).
#: The server never stores a raw device secret. Development mode supplies a
#: persisted random fallback; production remains fail-closed when absent.
DEVICE_MASTER_KEY: Final[str | None] = _secret("HONEYCHAIN_DEVICE_MASTER_KEY")

#: Maximum allowed difference between a device request's signed timestamp and
#: server time. Outside this window the request is rejected as stale/future,
#: which (together with the nonce store) is what makes replay impossible.
DEVICE_REQUEST_MAX_SKEW_SECONDS: Final[int] = _env_int("HONEYCHAIN_DEVICE_MAX_SKEW", 300)

#: Per-reading device timestamps: how far in the future a reading may claim
#: to be (clock drift tolerance), and how far in the past (buffered backfill).
DEVICE_FUTURE_TOLERANCE_SECONDS: Final[int] = _env_int("HONEYCHAIN_DEVICE_FUTURE_TOLERANCE", 300)
DEVICE_MAX_BACKFILL_SECONDS: Final[int] = _env_int("HONEYCHAIN_DEVICE_MAX_BACKFILL", 7 * 24 * 3600)

#: Device-silence detection: a device is marked OFFLINE once it has been
#: silent for longer than (its expected interval x this multiplier).
DEVICE_DEFAULT_EXPECTED_INTERVAL_SECONDS: Final[int] = _env_int(
    "HONEYCHAIN_DEVICE_EXPECTED_INTERVAL", 300
)
DEVICE_SILENCE_MULTIPLIER: Final[float] = _env_float("HONEYCHAIN_DEVICE_SILENCE_MULTIPLIER", 3.0)

#: Sensor-fault heuristics (data-quality flags, never health verdicts).
SENSOR_FLATLINE_COUNT: Final[int] = _env_int("HONEYCHAIN_SENSOR_FLATLINE_COUNT", 10)
SENSOR_MAX_TEMP_JUMP_C: Final[float] = _env_float("HONEYCHAIN_SENSOR_MAX_TEMP_JUMP_C", 8.0)
SENSOR_MAX_HUMIDITY_JUMP_PCT: Final[float] = _env_float("HONEYCHAIN_SENSOR_MAX_HUMIDITY_JUMP", 30.0)
SENSOR_JUMP_WINDOW_SECONDS: Final[int] = _env_int("HONEYCHAIN_SENSOR_JUMP_WINDOW", 120)

#: The pre-Phase-4 fleet-wide shared-secret webhook (/api/firebase/webhook).
#: Disabled by default: one leaked device would expose the whole fleet, any
#: device could impersonate any hive, and it auto-creates hives on first
#: sight (Engineering Audit SEC-04 / SEC-12). Kept, opt-in, for migration of
#: existing firmware only.
LEGACY_WEBHOOK_ENABLED: Final[bool] = _env_bool(
    "HONEYCHAIN_LEGACY_WEBHOOK_ENABLED", IS_DEVELOPMENT
)

# --------------------------------------------------------------------------
# Optional Ethereum (Sepolia) anchoring -- entirely opt-in.
# The core system is fully functional with these unset.
# --------------------------------------------------------------------------
CHAIN_ANCHOR_ENABLED: Final[bool] = _env_bool("HONEYCHAIN_CHAIN_ANCHOR", False)
#: The chain the contract lives on (e.g. 31337 local Hardhat, 80002 Polygon
#: Amoy, 11155111 Sepolia). Required when anchoring is enabled, and checked
#: against the RPC endpoint's actual chain id before any transaction is sent,
#: so a mis-pointed RPC_URL can never anchor on the wrong network. Unset or
#: non-numeric -> anchoring reports unavailable (fails closed).
_chain_id_raw = os.environ.get("CHAIN_ID", "").strip()
CHAIN_ID: Final[int | None] = int(_chain_id_raw) if _chain_id_raw.isdigit() else None
#: Retry budget for a checkpoint whose anchoring transaction failed.
CHECKPOINT_MAX_ATTEMPTS: Final[int] = _env_int("HONEYCHAIN_CHECKPOINT_MAX_ATTEMPTS", 5)
#: Seconds to wait for a transaction receipt before recording a timeout.
CHAIN_TX_TIMEOUT_SECONDS: Final[int] = _env_int("HONEYCHAIN_CHAIN_TX_TIMEOUT", 120)
RPC_URL: Final[str | None] = os.environ.get("RPC_URL") or None
CONTRACT_ADDRESS: Final[str | None] = os.environ.get("CONTRACT_ADDRESS") or None
# PRIVATE_KEY is deliberately *not* bound to a module-level constant; it is read
# on demand inside honeychain.chain_anchor so it lives in memory as briefly as
# possible and can never be printed by an accidental `import config; vars()`.

# --------------------------------------------------------------------------
# Optional Firebase Admin bridge -- also entirely opt-in.
# --------------------------------------------------------------------------
FIREBASE_ENABLED: Final[bool] = _env_bool("HONEYCHAIN_FIREBASE", False)
FIREBASE_DATABASE_URL: Final[str | None] = os.environ.get("FIREBASE_DATABASE_URL") or None
FIREBASE_CREDENTIALS_PATH: Final[str | None] = (
    os.environ.get("FIREBASE_CREDENTIALS_PATH") or None
)


def ensure_runtime_directories() -> None:
    """Create every directory the application writes to. Safe to call repeatedly."""
    for directory in (QRCODE_DIR, MODELS_DIR, INSTANCE_DIR, DATABASE_PATH.parent):
        directory.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Install a consistent log format across the app, simulator and CLI tools."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Werkzeug's per-request access log is very noisy once a 5-node simulator is
    # posting every 2 seconds; keep warnings and above.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def public_verify_url(batch_id: str) -> str:
    """Absolute consumer-facing verification URL embedded into a batch QR code."""
    return f"{PUBLIC_BASE_URL}/verify/{batch_id}"


def configuration_problems() -> list[str]:
    """Missing/invalid configuration, as clear human-readable messages.

    Required for core operation -> reported as errors at startup (and in
    /api/health) instead of failing silently. Optional integrations are only
    checked when they are switched on.
    """
    problems: list[str] = []
    if not AUTH_SECRET:
        problems.append("HONEYCHAIN_AUTH_SECRET is not set: every login and authenticated API "
                        "request will be rejected (401).")
    if not DEVICE_MASTER_KEY:
        problems.append("HONEYCHAIN_DEVICE_MASTER_KEY is not set: device registration returns 503 "
                        "and all device telemetry is rejected (401).")
    if LEGACY_WEBHOOK_ENABLED and not WEBHOOK_SECRET:
        problems.append("HONEYCHAIN_LEGACY_WEBHOOK_ENABLED=true but HONEYCHAIN_WEBHOOK_SECRET is not set.")
    if CHAIN_ANCHOR_ENABLED:
        missing = [name for name, value in (("RPC_URL", RPC_URL), ("CONTRACT_ADDRESS", CONTRACT_ADDRESS),
                                            ("CHAIN_ID", CHAIN_ID),
                                            ("PRIVATE_KEY", os.environ.get("PRIVATE_KEY")))
                   if not value]
        if missing:
            problems.append("HONEYCHAIN_CHAIN_ANCHOR=true but " + ", ".join(missing)
                            + " not set: checkpoints will be recorded as FAILED, never as anchored.")
    if not PUBLIC_BASE_URL.startswith(("http://", "https://")):
        problems.append("HONEYCHAIN_PUBLIC_BASE_URL must start with http:// or https:// (it is "
                        "embedded in every printed product QR code).")
    return problems
