"""
honeychain.quality
==================
Derives a batch's Hive Quality Score from that hive's **recorded telemetry**.

The score is a weighted sum of six components, each normalised to 0-1 and then
scaled by its weight. The function is deterministic: the same rows always
produce the same score, with no randomness, no clock dependence and no
model inference involved.

If a hive has no telemetry, :func:`compute_quality_score` raises
:class:`InsufficientTelemetry` rather than inventing a number. A quality claim
printed on a jar has to be backed by measurements that actually exist.

Component weights (total 100)
-----------------------------
==========================  ======  ====================================================
Component                   Weight  What it measures
==========================  ======  ====================================================
Thermal accuracy               25   Mean brood temperature vs the 34.5 C optimum
Thermal stability              20   Standard deviation of temperature
Humidity compliance            15   Share of readings inside the 50-70 % curing band
Weight stability               15   Relative variation in hive mass over the window
Health record                  15   Share of evaluations classified HEALTHY
Anomaly-free record            10   Share of evaluations *not* flagged by the detector
==========================  ======  ====================================================
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: Number of most-recent readings considered for a harvest.
DEFAULT_WINDOW = 500

#: Optimal brood-nest temperature; deviation is penalised linearly.
OPTIMAL_TEMPERATURE = 34.5
#: Deviation (C) at which the thermal-accuracy component reaches zero.
TEMPERATURE_TOLERANCE = 5.0
#: Temperature standard deviation (C) at which stability reaches zero.
TEMPERATURE_STABILITY_LIMIT = 3.0
#: Ideal relative-humidity band for nectar curing.
HUMIDITY_IDEAL_LOW, HUMIDITY_IDEAL_HIGH = 50.0, 70.0
#: Weight coefficient of variation at which weight stability reaches zero.
WEIGHT_CV_LIMIT = 0.20

WEIGHTS: dict[str, float] = {
    "thermal_accuracy": 25.0,
    "thermal_stability": 20.0,
    "humidity_compliance": 15.0,
    "weight_stability": 15.0,
    "health_record": 15.0,
    "anomaly_free_record": 10.0,
}


class InsufficientTelemetry(Exception):
    """Raised when a hive has no readings to base a quality score on."""


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _thermal_accuracy(temperatures: list[float]) -> float | None:
    if not temperatures:
        return None
    deviation = abs(statistics.fmean(temperatures) - OPTIMAL_TEMPERATURE)
    return _clamp01(1.0 - deviation / TEMPERATURE_TOLERANCE)


def _thermal_stability(temperatures: list[float]) -> float | None:
    if len(temperatures) < 2:
        return None
    spread = statistics.pstdev(temperatures)
    return _clamp01(1.0 - spread / TEMPERATURE_STABILITY_LIMIT)


def _humidity_compliance(humidities: list[float]) -> float | None:
    if not humidities:
        return None
    inside = sum(1 for h in humidities if HUMIDITY_IDEAL_LOW <= h <= HUMIDITY_IDEAL_HIGH)
    return inside / len(humidities)


def _weight_stability(weights: list[float]) -> float | None:
    if len(weights) < 2:
        return None
    mean = statistics.fmean(weights)
    if mean <= 0:
        return 0.0
    coefficient_of_variation = statistics.pstdev(weights) / mean
    return _clamp01(1.0 - coefficient_of_variation / WEIGHT_CV_LIMIT)


def _health_record(statuses: list[str]) -> float | None:
    if not statuses:
        return None
    return sum(1 for s in statuses if s == "HEALTHY") / len(statuses)


def _anomaly_free_record(predictions: list[str | None]) -> float | None:
    scored = [p for p in predictions if p is not None]
    if not scored:
        return None
    return sum(1 for p in scored if p != "CRITICAL") / len(scored)


def compute_quality_score(
    hive_id: str, window: int = DEFAULT_WINDOW
) -> tuple[float, dict[str, Any]]:
    """Return ``(score_0_to_100, breakdown)`` for a hive.

    Components with no supporting data are excluded and the remaining weights
    are renormalised, so a hive that never reported humidity is not silently
    penalised for a sensor it does not have. The breakdown records exactly which
    components contributed and how many readings backed them.
    """
    history = db.hive_history(hive_id, limit=window)
    if not history:
        raise InsufficientTelemetry(
            f"Hive '{hive_id}' has no recorded telemetry; a quality score cannot be "
            "derived without measurements"
        )

    temperatures = [float(r["temperature"]) for r in history if r["temperature"] is not None]
    humidities = [float(r["humidity"]) for r in history if r["humidity"] is not None]
    weights = [float(r["weight"]) for r in history if r["weight"] is not None]
    statuses = [str(r["health_status"]) for r in history if r["health_status"]]
    predictions = [r["prediction"] for r in history]

    components: dict[str, float | None] = {
        "thermal_accuracy": _thermal_accuracy(temperatures),
        "thermal_stability": _thermal_stability(temperatures),
        "humidity_compliance": _humidity_compliance(humidities),
        "weight_stability": _weight_stability(weights),
        "health_record": _health_record(statuses),
        "anomaly_free_record": _anomaly_free_record(predictions),
    }

    contributing = {k: v for k, v in components.items() if v is not None}
    if not contributing:
        raise InsufficientTelemetry(
            f"Hive '{hive_id}' has telemetry rows but none usable for quality scoring"
        )

    total_weight = sum(WEIGHTS[k] for k in contributing)
    weighted = sum(WEIGHTS[k] * v for k, v in contributing.items())
    score = round(weighted / total_weight * 100.0, 2)

    breakdown: dict[str, Any] = {
        "score": score,
        "readings_analysed": len(history),
        "window": window,
        "components": {
            name: {
                "normalised": round(value, 4) if value is not None else None,
                "weight": WEIGHTS[name],
                "contribution": round(WEIGHTS[name] * value / total_weight * 100.0, 2)
                if value is not None
                else None,
                "available": value is not None,
            }
            for name, value in components.items()
        },
        "observed": {
            "avg_temperature": round(statistics.fmean(temperatures), 2) if temperatures else None,
            "temperature_stddev": round(statistics.pstdev(temperatures), 3)
            if len(temperatures) > 1
            else None,
            "avg_humidity": round(statistics.fmean(humidities), 2) if humidities else None,
            "avg_weight": round(statistics.fmean(weights), 2) if weights else None,
            "weight_stddev": round(statistics.pstdev(weights), 3) if len(weights) > 1 else None,
            "healthy_share": round(_health_record(statuses), 4)
            if statuses
            else None,
            "status_counts": {
                status: sum(1 for s in statuses if s == status)
                for status in ("HEALTHY", "WARNING", "CRITICAL")
            },
        },
    }
    logger.info("Quality score for %s: %.2f (%d readings)", hive_id, score, len(history))
    return score, breakdown


def production_health_audit(hive_id: str, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """Consumer-facing metrics for the verification page.

    Every field is either a real aggregate or ``None``; the template renders
    ``--`` for ``None`` rather than substituting a plausible-looking number.
    """
    try:
        score, breakdown = compute_quality_score(hive_id, window)
    except InsufficientTelemetry:
        return {
            "available": False,
            "avg_temperature": None,
            "weight_stability": None,
            "production_health": None,
            "quality_score": None,
            "readings_analysed": 0,
        }

    observed = breakdown["observed"]
    stability = breakdown["components"]["weight_stability"]["normalised"]
    health = breakdown["components"]["health_record"]["normalised"]
    return {
        "available": True,
        "avg_temperature": observed["avg_temperature"],
        "weight_stability": round(stability * 100.0, 1) if stability is not None else None,
        "production_health": round(health * 100.0, 1) if health is not None else None,
        "quality_score": score,
        "readings_analysed": breakdown["readings_analysed"],
        "status_counts": observed["status_counts"],
    }
