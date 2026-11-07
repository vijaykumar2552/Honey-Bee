"""
honeychain.ml
=============
IsolationForest-backed anomaly detection for hive telemetry.

The predictor is loaded once at Flask start-up and held in memory for the
lifetime of the process. ``IsolationForest.decision_function`` is read-only and
therefore safe to call concurrently, but the *load / reload* path mutates shared
state, so it is guarded by a lock and readers take a cheap snapshot reference.

Degradation policy
------------------
If the model files are missing, corrupt, built from an incompatible scikit-learn
version, or trained on a different feature schema, the application **must keep
running**. Every failure path sets ``available = False`` and the caller silently
falls back to the deterministic rule engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import warnings
from dataclasses import dataclass
from typing import Any

from . import config
from .rules import CRITICAL, HEALTHY

ML_NORMAL, ML_WATCH, ML_ANOMALOUS = "NORMAL", "WATCH", "ANOMALOUS"
DATA_SOURCES = ("SYNTHETIC", "REAL", "MIXED")
ML_NOTICE = ("Telemetry anomaly detector. Trained on the data_source shown; any evaluation "
             "figures are measured on that same kind of data. It is not a disease diagnosis "
             "and not a honey-quality test. Deterministic rules remain authoritative.")


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_version(dist: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return None

logger = logging.getLogger(__name__)

#: Feature schema the production model is trained on, in order.
DEFAULT_FEATURE_COLUMNS: list[str] = [
    "temperature",
    "humidity",
    "weight",
    "audio_frequency",
]

#: Neutral values used only to fill a *missing* feature so a partial payload can
#: still be scored. Chosen as the centre of the healthy band, i.e. the least
#: informative value possible -- this cannot manufacture an anomaly on its own.
NEUTRAL_FEATURE_VALUES: dict[str, float] = {
    "temperature": 34.5,
    "humidity": 62.0,
    "weight": 42.0,
    "audio_frequency": 265.0,
}


@dataclass(frozen=True)
class Prediction:
    """Outcome of one ML scoring call."""

    available: bool
    verdict: str | None = None       # HEALTHY / CRITICAL (legacy binary view)
    anomaly_score: float | None = None  # decision_function: higher = more normal
    is_anomaly: bool = False
    imputed_fields: tuple[str, ...] = ()
    error: str | None = None
    band: str | None = None          # NORMAL / WATCH / ANOMALOUS (graded, explainable)
    model_version: str | None = None


class HiveHealthPredictor:
    """Loads and applies the trained IsolationForest. Never raises to callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: Any = None
        self._feature_columns: list[str] = list(DEFAULT_FEATURE_COLUMNS)
        self._metadata: dict[str, Any] = {}
        self._available = False
        self._load_error: str | None = None
        self._compatibility: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------
    def load(self) -> bool:
        """(Re)load artefacts. Returns True only for a verified, compatible model.

        Order matters -- everything that can be checked WITHOUT unpickling is
        checked first, because unpickling executes code from the file:

          1. metadata present, schema_version >= 2, provenance declared
          2. runtime scikit-learn == the version that wrote the pickle
          3. SHA-256 of both artefacts == the hashes recorded at training
          4. unpickle with InconsistentVersionWarning escalated to an error
          5. object is a detector whose feature count matches the schema

        Any failure leaves the model unavailable with a specific reason, and
        the deterministic rule engine carries on alone (safe degradation).
        """
        with self._lock:
            self._model, self._available, self._load_error = None, False, None
            self._metadata, self._compatibility = {}, {}

            def refuse(reason: str) -> bool:
                self._load_error = reason
                logger.warning("ML model unavailable (%s) -- rule engine only", reason)
                return False

            if not config.MODEL_PATH.is_file():
                return refuse(f"model artefact not found: {config.MODEL_PATH.name} "
                              "(run `python train_model.py`)")
            try:
                metadata = json.loads(config.MODEL_METADATA_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return refuse("model metadata missing or unreadable; provenance unknown")
            if not isinstance(metadata, dict) or int(metadata.get("schema_version", 0)) < 2:
                return refuse("model metadata predates provenance tracking (schema_version < 2); "
                              "retrain with `python train_model.py`")
            data_source = (metadata.get("provenance") or {}).get("data_source")
            if data_source not in DATA_SOURCES:
                return refuse("model metadata does not declare a valid training data_source")
            self._metadata = metadata

            trained = (metadata.get("environment") or {}).get("scikit_learn")
            runtime = _installed_version("scikit-learn")
            self._compatibility = {"trained_with_scikit_learn": trained,
                                   "runtime_scikit_learn": runtime,
                                   "compatible": bool(trained) and trained == runtime}
            if not self._compatibility["compatible"]:
                return refuse(f"model was pickled with scikit-learn {trained}, runtime is "
                              f"{runtime}; refusing to unpickle across versions "
                              "(install the pinned version or retrain)")

            artifact = metadata.get("artifact") or {}
            for path, key in ((config.MODEL_PATH, "model_sha256"),
                              (config.FEATURE_COLUMNS_PATH, "feature_columns_sha256")):
                expected = artifact.get(key)
                try:
                    actual = _sha256(path) if path.is_file() else None
                except Exception as exc:  # unreadable file must degrade, never raise
                    return refuse(f"could not read artefact {getattr(path, 'name', path)}: "
                                  f"{type(exc).__name__}")
                if not expected or actual != expected:
                    return refuse(f"artefact integrity check failed for {path.name}; "
                                  "file does not match the hash recorded at training")

            try:
                import joblib  # lazy: the app must start without scikit-learn
                from sklearn.exceptions import InconsistentVersionWarning
            except ImportError as exc:
                return refuse(f"ML dependencies unavailable: {type(exc).__name__}")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", InconsistentVersionWarning)
                    model = joblib.load(config.MODEL_PATH)
                    loaded_columns = joblib.load(config.FEATURE_COLUMNS_PATH)
            except Exception as exc:
                return refuse(f"could not load model: {type(exc).__name__}")

            if not hasattr(model, "decision_function") or not hasattr(model, "predict"):
                return refuse("loaded object is not a scikit-learn detector")
            if not isinstance(loaded_columns, (list, tuple)) or not loaded_columns:
                return refuse("feature schema artefact is empty or malformed")
            feature_columns = [str(c) for c in loaded_columns]
            if feature_columns != list(metadata.get("feature_columns") or []):
                return refuse("feature schema does not match model metadata")
            expected_n = getattr(model, "n_features_in_", None)
            if expected_n is not None and int(expected_n) != len(feature_columns):
                return refuse(f"feature schema mismatch: model expects {expected_n}, "
                              f"schema lists {len(feature_columns)}")

            self._model, self._feature_columns, self._available = model, feature_columns, True
            logger.info("IsolationForest %s loaded (trained on %s data)",
                        metadata.get("model_version"), data_source)
            return True

    # -- introspection -----------------------------------------------------
    @property
    def available(self) -> bool:
        return self._available

    @property
    def feature_columns(self) -> list[str]:
        return list(self._feature_columns)

    def status(self) -> dict[str, Any]:
        """Model state for /api/ml/status and the dashboard. Provenance first:
        every metric shown is labelled with the data it was measured on."""
        md = self._metadata or {}
        provenance = md.get("provenance") or {}
        return {
            "available": self._available,
            "error": self._load_error,
            "model_version": md.get("model_version"),
            "model_type": type(self._model).__name__ if self._model is not None else None,
            "purpose": md.get("purpose"),
            "feature_columns": list(self._feature_columns),
            "contamination": getattr(self._model, "contamination", None),
            "random_state": getattr(self._model, "random_state", None),
            "n_estimators": getattr(self._model, "n_estimators", None),
            "training_data_source": provenance.get("data_source"),
            "real_world_data_used": provenance.get("real_world_data_used"),
            "compatibility": dict(self._compatibility),
            "bands": md.get("bands"),
            "synthetic_evaluation": md.get("synthetic_evaluation"),
            "real_world_evaluation": md.get("real_world_evaluation"),
            "notice": ML_NOTICE,
            "metadata": md,
        }

    # -- inference ---------------------------------------------------------
    def _vectorise(self, reading: dict[str, Any]) -> tuple[list[float], tuple[str, ...]]:
        """Align a reading onto the trained feature order.

        Returns the feature vector plus the names that had to be imputed, so the
        caller can report honestly that part of the input was not measured.
        """
        vector: list[float] = []
        imputed: list[str] = []
        for name in self._feature_columns:
            value = reading.get(name)
            if value is None:
                vector.append(NEUTRAL_FEATURE_VALUES.get(name, 0.0))
                imputed.append(name)
            else:
                vector.append(float(value))
        return vector, tuple(imputed)

    def predict(self, reading: dict[str, Any]) -> Prediction:
        """Score one validated reading. Returns an unavailable Prediction on any error."""
        model = self._model  # single atomic read; never mutated in place
        columns = self._feature_columns
        if not self._available or model is None:
            return Prediction(available=False, error=self._load_error or "model not loaded")

        try:
            vector, imputed = self._vectorise(reading)
            if len(imputed) == len(columns):
                # Nothing real to score.
                return Prediction(available=False, error="no measured features present")

            import numpy as np

            matrix = np.asarray([vector], dtype=float)
            score = float(model.decision_function(matrix)[0])
            label = int(model.predict(matrix)[0])  # -1 anomaly, 1 normal
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ML inference failed (%s); using rule engine", type(exc).__name__)
            return Prediction(available=False, error=f"inference error: {type(exc).__name__}")

        is_anomaly = label == -1
        bands = self._metadata.get("bands") or {}
        watch_below = float(bands.get("watch_below", 0.0))
        band = ML_ANOMALOUS if score < 0.0 else (ML_WATCH if score < watch_below else ML_NORMAL)
        return Prediction(
            available=True,
            verdict=CRITICAL if is_anomaly else HEALTHY,
            anomaly_score=round(score, 6),
            is_anomaly=is_anomaly,
            imputed_fields=imputed,
            band=band,
            model_version=self._metadata.get("model_version"),
        )


#: Process-wide singleton, loaded during application start-up.
predictor = HiveHealthPredictor()
