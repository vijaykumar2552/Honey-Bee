"""
honeychain.validation
=====================
Two responsibilities, deliberately kept separate:

1. **Normalisation** -- turn any of the payload shapes real Firebase/ESP32
   deployments emit into a flat list of ``{hive_id, temperature, ...}`` dicts.
2. **Validation** -- reject anything outside physically plausible sensor
   ranges, plus NaN/Infinity, wrong types and malformed identifiers.

Normalisation never guesses a value. If a field is absent it stays absent; it is
not defaulted to zero, because a fabricated 0 kg reading would trip the
``weight < 5`` CRITICAL rule and raise a false alarm.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Physically plausible operating envelope for each sensor. Readings outside
#: these bounds indicate a faulty sensor or a spoofed payload, not a real hive.
SENSOR_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (-20.0, 60.0),      # degrees Celsius
    "humidity": (0.0, 100.0),          # percent
    "weight": (0.0, 200.0),            # kilograms
    "audio_frequency": (20.0, 2000.0), # hertz
    "battery_level": (0.0, 100.0),     # percent
}

#: Sensor fields recognised on input, in canonical order.
SENSOR_FIELDS: tuple[str, ...] = tuple(SENSOR_RANGES.keys())

#: At least one of these must be present for a payload to be worth storing.
REQUIRED_ANY: tuple[str, ...] = ("temperature", "humidity", "weight", "audio_frequency")

#: Hive identifiers are used in URLs and filenames; keep them to a safe alphabet.
HIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: Aliases seen across ESP32 sketches, Firebase trees and older HoneyChain code.
FIELD_ALIASES: dict[str, str] = {
    "temp": "temperature",
    "temperature_c": "temperature",
    "temperaturec": "temperature",
    "inside_temperature": "temperature",
    "hive_temperature": "temperature",
    "hum": "humidity",
    "humidity_percent": "humidity",
    "inside_humidity": "humidity",
    "hive_humidity": "humidity",
    "wt": "weight",
    "weight_kg": "weight",
    "hive_weight": "weight",
    "mass": "weight",
    "audio": "audio_frequency",
    "audiofrequency": "audio_frequency",
    "audio_freq": "audio_frequency",
    "frequency": "audio_frequency",
    "sound_frequency": "audio_frequency",
    "battery": "battery_level",
    "batterylevel": "battery_level",
    "battery_percent": "battery_level",
    "bat": "battery_level",
}

#: Keys that carry the hive identifier in the various payload shapes.
HIVE_ID_KEYS: tuple[str, ...] = ("hive_id", "hiveId", "hiveID", "hive", "id", "device_id", "deviceId")

#: Keys that may carry a device-supplied timestamp.
TIMESTAMP_KEYS: tuple[str, ...] = ("timestamp", "ts", "time", "recorded_at", "recordedAt")


class ValidationError(ValueError):
    """Raised for any payload the API must answer with HTTP 400."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        return {"error": "Bad Request", "message": self.message, "details": self.errors}


@dataclass
class NormalisationResult:
    readings: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _canonical_key(key: str) -> str:
    lowered = str(key).strip().lower()
    if lowered in SENSOR_RANGES:
        return lowered
    return FIELD_ALIASES.get(lowered, lowered)


def _looks_like_sensor_object(value: Any) -> bool:
    """True when a dict contains at least one recognised sensor field."""
    if not isinstance(value, dict):
        return False
    return any(_canonical_key(k) in SENSOR_RANGES for k in value.keys())


def _extract_hive_id(obj: dict[str, Any]) -> str | None:
    for key in HIVE_ID_KEYS:
        if key in obj and obj[key] not in (None, ""):
            return str(obj[key]).strip()
    # Case-insensitive second pass for sketches that emit HIVE_ID / Hive_Id.
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in ("hive_id", "hiveid", "hive", "device_id", "deviceid"):
        if key in lowered and lowered[key] not in (None, ""):
            return str(lowered[key]).strip()
    return None


def _flatten_sensor_object(obj: dict[str, Any], hive_id: str | None) -> dict[str, Any]:
    """Project one sensor dict onto the canonical field names."""
    reading: dict[str, Any] = {}
    resolved = hive_id or _extract_hive_id(obj)
    if resolved is not None:
        reading["hive_id"] = resolved

    for raw_key, raw_value in obj.items():
        canonical = _canonical_key(raw_key)
        if canonical in SENSOR_RANGES:
            reading[canonical] = raw_value
        elif str(raw_key) in TIMESTAMP_KEYS or canonical in ("timestamp", "ts", "time"):
            reading["device_timestamp"] = raw_value
    return reading


def normalise_payload(payload: Any, default_hive_id: str | None = None) -> list[dict[str, Any]]:
    """Flatten any supported payload shape into a list of candidate readings.

    Supported shapes
    ----------------
    * flat object            ``{"hive_id": "HIVE-001", "temperature": 34.2, ...}``
    * nested Firebase tree   ``{"HIVE-001": {...}, "HIVE-002": {...}}``
    * array of objects       ``[{...}, {...}]``
    * wrapped envelopes      ``{"data": <any of the above>}`` /
                             ``{"readings": [...]}`` / ``{"hives": {...}}``
    * Firebase event wrapper ``{"path": "/HIVE-001", "data": {...}}``

    Raises
    ------
    ValidationError
        If the payload is not an object/array, or contains no recognisable
        sensor data at all.
    """
    if payload is None:
        raise ValidationError("Request body is empty")

    # -- Firebase RTDB event wrapper: {"path": "/HIVE-001", "data": {...}} ---
    if isinstance(payload, dict) and "data" in payload and "path" in payload:
        path_hive = str(payload.get("path") or "").strip("/").split("/")[0] or None
        return normalise_payload(payload["data"], default_hive_id or path_hive or None)

    # -- generic envelopes --------------------------------------------------
    if isinstance(payload, dict):
        for envelope in ("readings", "telemetry", "hives", "records", "data"):
            inner = payload.get(envelope)
            if isinstance(inner, (dict, list)) and not _looks_like_sensor_object(payload):
                return normalise_payload(inner, default_hive_id)

    # -- array of readings --------------------------------------------------
    if isinstance(payload, list):
        readings: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                readings.extend(normalise_payload(item, default_hive_id))
        if not readings:
            raise ValidationError("Array payload contained no readable telemetry objects")
        return readings

    if not isinstance(payload, dict):
        raise ValidationError("Payload must be a JSON object or array of objects")

    # -- flat single reading ------------------------------------------------
    if _looks_like_sensor_object(payload):
        return [_flatten_sensor_object(payload, default_hive_id)]

    # -- nested Firebase tree: {"HIVE-001": {...}, "HIVE-002": {...}} -------
    readings = []
    for key, value in payload.items():
        if isinstance(value, dict) and _looks_like_sensor_object(value):
            readings.append(_flatten_sensor_object(value, str(key).strip()))
        elif isinstance(value, dict):
            # One more level: {"hives": {"HIVE-001": {"sensors": {...}}}}
            for sub_value in value.values():
                if isinstance(sub_value, dict) and _looks_like_sensor_object(sub_value):
                    readings.append(_flatten_sensor_object(sub_value, str(key).strip()))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _looks_like_sensor_object(item):
                    readings.append(_flatten_sensor_object(item, str(key).strip()))

    if not readings:
        raise ValidationError(
            "No recognisable telemetry found. Expected at least one of: "
            + ", ".join(REQUIRED_ANY)
        )
    return readings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _coerce_number(field_name: str, value: Any) -> float:
    """Convert to float, rejecting bools, NaN, Infinity and non-numeric text."""
    if isinstance(value, bool):
        raise ValidationError(
            f"Field '{field_name}' must be a number, received a boolean",
            [{"field": field_name, "issue": "type", "expected": "number"}],
        )
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(
                f"Field '{field_name}' is empty",
                [{"field": field_name, "issue": "empty"}],
            )
        # Explicitly block the textual spellings float() would happily accept.
        if text.lower().lstrip("+-") in {"nan", "inf", "infinity"}:
            raise ValidationError(
                f"Field '{field_name}' must be a finite number",
                [{"field": field_name, "issue": "non_finite"}],
            )
        try:
            number = float(text)
        except ValueError:
            raise ValidationError(
                f"Field '{field_name}' must be a number, received '{text[:32]}'",
                [{"field": field_name, "issue": "type", "expected": "number"}],
            ) from None
    else:
        raise ValidationError(
            f"Field '{field_name}' must be a number",
            [{"field": field_name, "issue": "type", "expected": "number"}],
        )

    if math.isnan(number) or math.isinf(number):
        raise ValidationError(
            f"Field '{field_name}' must be a finite number (NaN/Infinity rejected)",
            [{"field": field_name, "issue": "non_finite"}],
        )
    return number


def validate_hive_id(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(
            "Missing hive identifier",
            [{"field": "hive_id", "issue": "missing"}],
        )
    hive_id = str(value).strip()
    if not HIVE_ID_PATTERN.match(hive_id):
        raise ValidationError(
            "Invalid hive identifier: use 1-64 characters from A-Z, a-z, 0-9, '-' and '_'",
            [{"field": "hive_id", "issue": "format", "value": hive_id[:64]}],
        )
    return hive_id


def validate_reading(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate one normalised reading. Raises ValidationError on any problem."""
    if not isinstance(candidate, dict):
        raise ValidationError("Each telemetry reading must be a JSON object")

    reading: dict[str, Any] = {"hive_id": validate_hive_id(candidate.get("hive_id"))}

    errors: list[dict[str, Any]] = []
    for name in SENSOR_FIELDS:
        if name not in candidate or candidate[name] is None:
            continue
        try:
            number = _coerce_number(name, candidate[name])
        except ValidationError as exc:
            errors.extend(exc.errors or [{"field": name, "issue": "invalid"}])
            continue

        low, high = SENSOR_RANGES[name]
        if not (low <= number <= high):
            errors.append(
                {
                    "field": name,
                    "issue": "out_of_range",
                    "value": number,
                    "min": low,
                    "max": high,
                }
            )
            continue
        reading[name] = number

    if errors:
        raise ValidationError("One or more sensor values failed validation", errors)

    if not any(name in reading for name in REQUIRED_ANY):
        raise ValidationError(
            "Reading contains no usable sensor values",
            [{"field": "sensors", "issue": "missing", "expected_any": list(REQUIRED_ANY)}],
        )

    if "device_timestamp" in candidate and candidate["device_timestamp"] is not None:
        reading["device_timestamp"] = str(candidate["device_timestamp"])[:64]

    return reading


def validate_batch(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate every reading in a multi-hive payload; all-or-nothing.

    A partially accepted Firebase tree would leave the operator unsure which
    hives actually landed, so one bad node rejects the whole request.
    """
    validated: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        try:
            validated.append(validate_reading(candidate))
        except ValidationError as exc:
            for item in exc.errors or [{"issue": "invalid"}]:
                enriched = dict(item)
                enriched["index"] = index
                enriched.setdefault("message", exc.message)
                errors.append(enriched)

    if errors:
        raise ValidationError("Telemetry payload failed validation", errors)
    return validated


# ---------------------------------------------------------------------------
# Device timestamps (Phase 4)
# ---------------------------------------------------------------------------

def parse_device_timestamp(value: Any) -> datetime | None:
    """Parse a device-supplied timestamp into an aware UTC datetime.

    Accepts unix epoch seconds or milliseconds (int/float/numeric string) and
    ISO-8601 strings (``Z`` suffix or explicit offset; a naive string is taken
    as UTC). Returns ``None`` when the value cannot be interpreted -- the
    caller decides whether that is an error.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if number != number or number in (float("inf"), float("-inf")):
            return None
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_device_timestamp(
    value: Any,
    *,
    now: datetime,
    future_tolerance_seconds: int,
    max_backfill_seconds: int,
) -> str:
    """Validate a per-reading device timestamp against server time.

    Returns the canonical ``YYYY-MM-DD HH:MM:SS`` UTC string, or raises
    ValidationError for an unparseable value, a clock too far in the future,
    or a reading older than the allowed backfill window.
    """
    parsed = parse_device_timestamp(value)
    if parsed is None:
        raise ValidationError(
            "Unrecognised device timestamp",
            [{"field": "timestamp", "issue": "format"}],
        )
    delta = (parsed - now).total_seconds()
    if delta > future_tolerance_seconds:
        raise ValidationError(
            "Device timestamp is too far in the future (check the device clock)",
            [{"field": "timestamp", "issue": "future", "drift_seconds": int(delta)}],
        )
    if -delta > max_backfill_seconds:
        raise ValidationError(
            "Device timestamp is older than the allowed backfill window",
            [{"field": "timestamp", "issue": "too_old", "age_seconds": int(-delta)}],
        )
    return parsed.strftime("%Y-%m-%d %H:%M:%S")
