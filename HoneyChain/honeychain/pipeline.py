"""
honeychain.pipeline
===================
The single ingestion path every telemetry reading travels:

    validated reading
        -> rule engine          (deterministic, authoritative safety floor)
        -> swarm rate detector  (needs history, so it runs inside the txn)
        -> IsolationForest      (optional; escalates only)
        -> final health verdict
        -> SQLite (one atomic transaction: hive + telemetry + evaluation)
        -> SSE broadcast
        -> dashboard

Fusion policy
-------------
The rule engine is never overridden downwards. The ML model can *escalate* a
reading to CRITICAL but can never talk one out of CRITICAL or WARNING, because
the rules encode hard biological limits while the model encodes "unusual
compared to the training distribution". An unusual-but-safe reading should not
raise an alarm on its own either -- so an ML anomaly is reported as CRITICAL
only in combination with the rule verdict, and the reason string always says
which layer fired.

Persistence is atomic: the hive row, the telemetry row and the health row are
written in one transaction. The SSE broadcast happens *after* commit, so a
client can never be told about a reading that was subsequently rolled back.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import config, db, rules, sensor_faults, validation
from .events import broker
from .ml import ML_ANOMALOUS, ML_WATCH, predictor

logger = logging.getLogger(__name__)


def evaluate_health(
    reading: dict[str, Any],
    swarm_signal: dict[str, Any] | None = None,
    sensor_flags: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Combine rule engine, swarm detector and ML into one health evaluation.

    Fusion policy (Phase 7):

    * The deterministic rules are authoritative. Nothing ever softens them.
    * The ML model is a complementary *risk signal*, reported as a graded
      band (NORMAL / WATCH / ANOMALOUS) with its score. An ANOMALOUS band may
      raise the status to at most config.ML_MAX_ESCALATION (default WARNING:
      the model is trained on synthetic data and is not a validated
      diagnostic, so it cannot raise a CRITICAL alarm on its own).
    * ML is NOT applied to the verdict when any feature had to be imputed or
      the reading carries sensor data-quality flags: an unusual score there
      most likely reflects the sensor, not the colony, and must not be
      presented as a biological conclusion.
    """
    rule_result = rules.evaluate_rules(reading)
    rule_status = rule_result["status"]
    reasons: list[str] = list(rule_result["reasons"])
    sources: list[str] = ["RULE"]
    status = rule_status

    if swarm_signal:
        status = rules.combine(status, swarm_signal["status"])
        reasons.extend(swarm_signal["reasons"])
        sources.append("RATE")

    prediction = predictor.predict(reading)
    ml_applied = False
    if prediction.available:
        sources.append("ML")
        if prediction.imputed_fields or sensor_flags:
            why = []
            if prediction.imputed_fields:
                why.append("not measured: " + ", ".join(prediction.imputed_fields))
            if sensor_flags:
                why.append("sensor data-quality flags: " + ", ".join(sensor_flags))
            reasons.append(f"ML band {prediction.band} NOT applied to health ({'; '.join(why)})")
        elif prediction.band == ML_ANOMALOUS:
            ml_applied = True
            status = rules.combine(status, config.ML_MAX_ESCALATION)
            reasons.append(
                f"ML risk signal: reading is ANOMALOUS for model {prediction.model_version} "
                f"(decision score {prediction.anomaly_score:.4f} < 0). Model trained on "
                "SYNTHETIC data -- not a validated diagnosis."
            )
        elif prediction.band == ML_WATCH:
            reasons.append(
                f"ML watch: score {prediction.anomaly_score:.4f} is within the lowest 5% of "
                "normal training readings (no status change)"
            )
    elif prediction.error:
        logger.debug("ML unavailable for this reading: %s", prediction.error)

    return {
        "health_status": status,
        "rule_status": rule_status,
        "prediction": prediction.verdict,
        "anomaly_score": prediction.anomaly_score,
        "ml_band": prediction.band,
        "ml_applied": ml_applied,
        "model_version": prediction.model_version,
        "source": "+".join(sources),
        "reason": " | ".join(reasons),
        "ml_available": prediction.available,
    }


def _seconds_between(later: str, earlier: str | None) -> float | None:
    if not earlier:
        return None
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        return (datetime.strptime(later[:19], fmt) - datetime.strptime(earlier[:19], fmt)).total_seconds()
    except ValueError:
        return None


def ingest_reading(
    reading: dict[str, Any],
    *,
    device: dict[str, Any] | None = None,
    device_timestamp: str | None = None,
) -> dict[str, Any]:
    """Persist one validated reading with its health evaluation, then broadcast.

    Returns the joined record exactly as ``/api/telemetry/latest`` would return
    it, so the SSE payload and the polling payload are structurally identical
    and the dashboard needs only one renderer.

    Two callers:

    * the legacy shared-secret webhook (``device=None``): unchanged behaviour,
      including auto-creating an unknown hive -- which is exactly why that
      path is now opt-in only (config.LEGACY_WEBHOOK_ENABLED);
    * the per-device endpoint (``device`` given): the hive is the device's
      bound hive, never created on the fly; the device's own event time is
      preserved in ``device_timestamp``; sensor data-quality flags are
      computed and stored; the device's last-seen/status is updated in the
      same transaction as the reading.
    """
    hive_id = reading["hive_id"]
    timestamp = db.utc_now()
    flags: list[str] = []

    if device is None and device_timestamp is None and reading.get("device_timestamp"):
        # Legacy path: keep the device clock when it is parseable, never fail on it.
        parsed = validation.parse_device_timestamp(reading.get("device_timestamp"))
        device_timestamp = parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else None

    with db.transaction() as conn:
        if device is not None:
            hive_pk = int(device["hive_id"])
            previous = db.recent_readings_for_hive(
                conn, hive_pk,
                max(config.SENSOR_FLATLINE_COUNT - 1, config.SENSOR_ROBUST_MIN_HISTORY, 1),
            )
            flags = sensor_faults.detect_faults(
                reading,
                previous,
                seconds_since_previous=_seconds_between(
                    timestamp, previous[0]["timestamp"] if previous else None
                ),
            )
        else:
            hive_pk = db.get_or_create_hive(conn, hive_id)

        # The swarm detector needs the hive's recent weights, and must see them
        # from inside this transaction to stay consistent with what is written.
        swarm_signal = None
        weight = reading.get("weight")
        if weight is not None:
            history = db.recent_weights(
                conn, hive_pk, config.SWARM_WINDOW_SECONDS, timestamp
            )
            swarm_signal = rules.detect_sudden_weight_loss(
                float(weight), [*history, float(weight)], config.SWARM_DROP_KG
            )

        evaluation = evaluate_health(reading, swarm_signal, flags)

        telemetry_pk = db.insert_telemetry(
            conn,
            hive_pk,
            reading,
            timestamp,
            device_pk=int(device["id"]) if device is not None else None,
            device_timestamp=device_timestamp,
            sensor_flags=",".join(flags) if flags else None,
        )
        db.insert_health_evaluation(conn, hive_pk, telemetry_pk, timestamp, evaluation)
        if device is not None:
            db.mark_device_seen(
                conn, int(device["id"]), reading.get("battery_level")
            )

    record = db.telemetry_by_id(telemetry_pk)
    if record is None:  # pragma: no cover - would mean the commit vanished
        raise RuntimeError("Telemetry row disappeared immediately after commit")

    logger.info(
        "%s -> %s | %s | %s | %s",
        hive_id,
        f"{reading['temperature']:.1f}C" if reading.get("temperature") is not None else "--",
        f"{reading['humidity']:.0f}%" if reading.get("humidity") is not None else "--",
        f"{reading['weight']:.1f}kg" if reading.get("weight") is not None else "--",
        evaluation["health_status"],
    )

    broker.publish("telemetry", record)
    if flags:
        broker.publish("sensor_fault", {"hive_id": hive_id, "flags": flags,
                                        "telemetry_id": telemetry_pk})
    if evaluation["health_status"] == rules.CRITICAL:
        broker.publish(
            "alert",
            {
                "hive_id": hive_id,
                "timestamp": timestamp,
                "health_status": rules.CRITICAL,
                "reason": evaluation["reason"],
            },
        )
    return record


def ingest_batch(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ingest every reading from a multi-hive payload."""
    stored: list[dict[str, Any]] = []
    for reading in readings:
        stored.append(ingest_reading(reading))

    summary = db.dashboard_summary()
    broker.publish("summary", summary)
    return stored
