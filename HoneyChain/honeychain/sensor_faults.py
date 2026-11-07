"""
honeychain.sensor_faults
========================
Data-quality flags for device telemetry (Phase 4).

These are deliberately simple, explainable heuristics -- NOT machine learning
and NOT a statement about hive health. A flag says "this sensor reading looks
untrustworthy", never "this hive is sick". Flags are stored alongside the
reading (``telemetry_readings.sensor_flags``); the reading itself is still
kept, because silently discarding data would hide the fault instead of
surfacing it.

What is (and is not) flagged:

* ``MISSING_<FIELD>`` -- a core channel (temperature/humidity/weight) was not
  reported at all.
* ``IMPLAUSIBLE_JUMP_TEMPERATURE`` / ``IMPLAUSIBLE_JUMP_HUMIDITY`` -- the value
  moved further than physically plausible inside a short window. Weight is
  intentionally NOT checked for jumps: a beekeeper removing a full super is a
  legitimate multi-kilogram step change, and sudden weight *loss* is already
  handled by the swarm detector in rules.py.
* ``FLATLINE_TEMPERATURE`` / ``FLATLINE_HUMIDITY`` -- the exact same value for
  N consecutive readings. Real environmental sensors always carry some noise;
  a perfectly constant value over many readings usually means a stuck or
  disconnected sensor. Weight is excluded for the same resolution reason.

Out-of-range, NaN/Infinity and malformed values never reach this module:
honeychain.validation rejects them with HTTP 400 before ingestion.
"""

from __future__ import annotations

from typing import Any

from . import config

CORE_FIELDS: tuple[str, ...] = ("temperature", "humidity", "weight")
JUMP_LIMITS: dict[str, str] = {
    "temperature": "SENSOR_MAX_TEMP_JUMP_C",
    "humidity": "SENSOR_MAX_HUMIDITY_JUMP_PCT",
}
FLATLINE_FIELDS: tuple[str, ...] = ("temperature", "humidity")


def detect_faults(
    reading: dict[str, Any],
    previous: list[dict[str, Any]],
    *,
    seconds_since_previous: float | None = None,
) -> list[str]:
    """Return the data-quality flags for ``reading``.

    ``previous`` is this hive's most recent readings, newest first, BEFORE the
    new one. ``seconds_since_previous`` is the gap to ``previous[0]``; jump
    checks only apply when the gap is inside SENSOR_JUMP_WINDOW_SECONDS (a big
    change across a six-hour gap is not implausible).
    """
    flags: list[str] = []

    for field in CORE_FIELDS:
        if reading.get(field) is None:
            flags.append(f"MISSING_{field.upper()}")

    if previous and seconds_since_previous is not None and (
        0 <= seconds_since_previous <= config.SENSOR_JUMP_WINDOW_SECONDS
    ):
        last = previous[0]
        for field, limit_name in JUMP_LIMITS.items():
            new, old = reading.get(field), last.get(field)
            if new is None or old is None:
                continue
            if abs(float(new) - float(old)) > float(getattr(config, limit_name)):
                flags.append(f"IMPLAUSIBLE_JUMP_{field.upper()}")

    needed = config.SENSOR_FLATLINE_COUNT - 1  # previous readings required
    if needed > 0 and len(previous) >= needed:
        window = previous[:needed]
        for field in FLATLINE_FIELDS:
            value = reading.get(field)
            if value is None:
                continue
            if all(p.get(field) is not None and float(p[field]) == float(value) for p in window):
                flags.append(f"FLATLINE_{field.upper()}")

    flags.extend(robust_outlier_flags(reading, previous))
    return flags


ROBUST_FIELDS: tuple[str, ...] = ("temperature", "humidity")


def modified_z(value: float, history: list[float]) -> float | None:
    """Iglewicz & Hoaglin modified z-score of ``value`` against ``history``:
    0.6745 * (x - median) / MAD. Robust to the outliers it is looking for
    (unlike mean/stddev). None when MAD is 0 (a constant history -- that case
    is the FLATLINE check's job, not this one's)."""
    ordered = sorted(history)
    n = len(ordered)
    median = (ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2)
    deviations = sorted(abs(v - median) for v in ordered)
    mad = (deviations[n // 2] if n % 2 else (deviations[n // 2 - 1] + deviations[n // 2]) / 2)
    if mad == 0:
        return None
    return 0.6745 * (value - median) / mad


def robust_outlier_flags(reading: dict[str, Any], previous: list[dict[str, Any]]) -> list[str]:
    """``STAT_OUTLIER_<FIELD>`` when a value is a statistical outlier against
    this hive's own recent history (|modified z| > SENSOR_ROBUST_Z_THRESHOLD,
    needing at least SENSOR_ROBUST_MIN_HISTORY prior values).

    Deliberately neutral naming: an outlier may be a failing sensor OR a
    genuine colony event, and statistics alone cannot tell which. It never
    changes the rule verdict (hard biological limits still apply); it only
    stops the ML layer from turning the reading into a health conclusion.
    """
    flags: list[str] = []
    for field in ROBUST_FIELDS:
        value = reading.get(field)
        if value is None:
            continue
        history = [float(p[field]) for p in previous if p.get(field) is not None]
        if len(history) < config.SENSOR_ROBUST_MIN_HISTORY:
            continue
        z = modified_z(float(value), history)
        if z is not None and abs(z) > config.SENSOR_ROBUST_Z_THRESHOLD:
            flags.append(f"STAT_OUTLIER_{field.upper()}")
    return flags
