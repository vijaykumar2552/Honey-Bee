"""
honeychain.rules
================
Deterministic, biologically grounded health rules.

:func:`evaluate_rules` implements the specified rule engine **exactly** and is
the single shared implementation used by the webhook, the ML fallback path and
the offline tests. It is a pure function -- no database, no I/O -- so it stays
trivially testable and reusable.

:func:`detect_sudden_weight_loss` is a *separate*, additive layer. Absolute
thresholds cannot see a swarming event: a colony that sheds 5 kg in ten seconds
is still well above the 5 kg absolute floor. Rate of change is what identifies
it, so it is evaluated alongside the rule engine rather than folded into it.
"""

from __future__ import annotations

from typing import Any, Iterable

HEALTHY = "HEALTHY"
WARNING = "WARNING"
CRITICAL = "CRITICAL"

#: Severity ordering, used when combining verdicts from several layers.
SEVERITY: dict[str, int] = {HEALTHY: 0, WARNING: 1, CRITICAL: 2}

# Brood-nest thermoregulation band. A colony holds ~34-35 degrees C; sustained
# excursions below 30 or above 38 indicate brood chill or overheating.
TEMP_CRITICAL_LOW = 30.0
TEMP_CRITICAL_HIGH = 38.0

# Below 5 kg the box is effectively empty -- absconded colony or theft.
WEIGHT_CRITICAL_LOW = 5.0

# Outside 40-85 % RH nectar curing and brood viability both suffer.
HUMIDITY_WARNING_LOW = 40.0
HUMIDITY_WARNING_HIGH = 85.0

#: The documented defaults above, by name. Operators may override them per
#: deployment (config.RULE_THRESHOLDS, from HONEYCHAIN_RULE_* variables); the
#: defaults themselves are pinned by tests/test_phase7_intelligence.py so a
#: silent change to a biological safety limit fails the build.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "TEMP_CRITICAL_LOW": TEMP_CRITICAL_LOW,
    "TEMP_CRITICAL_HIGH": TEMP_CRITICAL_HIGH,
    "WEIGHT_CRITICAL_LOW": WEIGHT_CRITICAL_LOW,
    "HUMIDITY_WARNING_LOW": HUMIDITY_WARNING_LOW,
    "HUMIDITY_WARNING_HIGH": HUMIDITY_WARNING_HIGH,
}


def active_thresholds() -> dict[str, float]:
    """The thresholds in force right now (validated overrides, else defaults)."""
    from . import config  # local import: rules stays importable without config side effects

    return {**DEFAULT_THRESHOLDS, **config.RULE_THRESHOLDS}


def evaluate_rules(reading: dict[str, Any]) -> dict[str, Any]:
    """Classify a single validated reading.

    CRITICAL when ``temperature < 30`` or ``temperature > 38`` or ``weight < 5``.
    Otherwise WARNING when ``humidity > 85`` or ``humidity < 40``.
    Otherwise HEALTHY.

    A missing sensor never triggers a rule: absence of evidence is not an alarm.
    """
    t = active_thresholds()
    TEMP_CRITICAL_LOW, TEMP_CRITICAL_HIGH = t["TEMP_CRITICAL_LOW"], t["TEMP_CRITICAL_HIGH"]
    WEIGHT_CRITICAL_LOW = t["WEIGHT_CRITICAL_LOW"]
    HUMIDITY_WARNING_LOW, HUMIDITY_WARNING_HIGH = t["HUMIDITY_WARNING_LOW"], t["HUMIDITY_WARNING_HIGH"]
    reasons: list[str] = []

    temperature = reading.get("temperature")
    weight = reading.get("weight")
    humidity = reading.get("humidity")

    critical = False
    if temperature is not None:
        if temperature < TEMP_CRITICAL_LOW:
            critical = True
            reasons.append(
                f"Temperature {temperature:.1f} C is below the {TEMP_CRITICAL_LOW:.0f} C "
                "brood-viability floor"
            )
        elif temperature > TEMP_CRITICAL_HIGH:
            critical = True
            reasons.append(
                f"Temperature {temperature:.1f} C is above the {TEMP_CRITICAL_HIGH:.0f} C "
                "overheating ceiling"
            )
    if weight is not None and weight < WEIGHT_CRITICAL_LOW:
        critical = True
        reasons.append(
            f"Hive weight {weight:.1f} kg is below the {WEIGHT_CRITICAL_LOW:.0f} kg "
            "minimum viable mass"
        )

    if critical:
        return {"status": CRITICAL, "reasons": reasons}

    if humidity is not None:
        if humidity > HUMIDITY_WARNING_HIGH:
            reasons.append(
                f"Humidity {humidity:.1f} % exceeds {HUMIDITY_WARNING_HIGH:.0f} % "
                "(fermentation / mould risk)"
            )
            return {"status": WARNING, "reasons": reasons}
        if humidity < HUMIDITY_WARNING_LOW:
            reasons.append(
                f"Humidity {humidity:.1f} % is below {HUMIDITY_WARNING_LOW:.0f} % "
                "(desiccation risk)"
            )
            return {"status": WARNING, "reasons": reasons}

    reasons.append("All monitored sensor values are inside the safe operating band")
    return {"status": HEALTHY, "reasons": reasons}


def detect_sudden_weight_loss(
    current_weight: float | None,
    recent_weights: Iterable[float],
    drop_threshold_kg: float,
) -> dict[str, Any] | None:
    """Flag a swarming / theft event from rate of change.

    ``recent_weights`` are the hive's weights inside the trailing detection
    window (newest first, including the current reading). If the window maximum
    exceeds the current weight by at least ``drop_threshold_kg`` the event is
    escalated to CRITICAL.

    Returns ``None`` when there is nothing to report, so callers can treat a
    falsy result as "no additional signal".
    """
    if current_weight is None:
        return None
    window = [w for w in recent_weights if w is not None]
    if len(window) < 2:
        return None

    peak = max(window)
    drop = peak - current_weight
    if drop < drop_threshold_kg:
        return None

    return {
        "status": CRITICAL,
        "drop_kg": round(drop, 2),
        "peak_kg": round(peak, 2),
        "reasons": [
            f"Sudden weight loss of {drop:.1f} kg detected "
            f"(peak {peak:.1f} kg -> {current_weight:.1f} kg) -- possible swarming, "
            "absconding or hive theft"
        ],
    }


def combine(*statuses: str) -> str:
    """Return the most severe of the supplied statuses."""
    valid = [s for s in statuses if s in SEVERITY]
    if not valid:
        return HEALTHY
    return max(valid, key=lambda s: SEVERITY[s])
