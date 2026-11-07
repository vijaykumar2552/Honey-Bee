#!/usr/bin/env python3
"""
train_model.py
==============
Trains the production anomaly detector used by the HoneyChain API.

    python train_model.py

Outputs
-------
``models/isolation_forest.joblib``   fitted IsolationForest
``models/feature_columns.joblib``    ["temperature", "humidity", "weight", "audio_frequency"]
``models/model_metadata.json``       training provenance + measured detection rates
``data/synthetic_hive_telemetry.csv``  the generated training set (inspectable)

Why synthetic data
------------------
The repository's real dataset (``data/hive_environment_*.csv``) is ~24 hours of
inside/outside temperature and humidity from a single hive. It contains no
weight and no acoustic channel, so it cannot train a detector over the four
features the live ESP32 nodes actually report. The generator below therefore
produces a physically grounded synthetic baseline over all four channels, with
the failure modes a beekeeper genuinely cares about. The real dataset is kept
and is still used by the offline research pipeline in ``research/``.

Anomaly classes generated
-------------------------
* **Swarming** -- rapid weight loss plus an acoustic rise (departing colony).
* **Overwintering heat loss** -- brood temperature falling away from 34.5 C
  while weight slowly declines as stores are consumed.
* **Varroa infestation** -- shifted and broadened acoustic signature with a
  mildly depressed brood temperature.

Anomalies make up 5 % of the data, matching ``contamination=0.05``. Everything
is driven by a fixed seed, so the same model is reproducible on any machine.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from honeychain import config  # noqa: E402  (path set up above)

logger = logging.getLogger("train_model")

RANDOM_SEED = 42
FEATURE_COLUMNS = ["temperature", "humidity", "weight", "audio_frequency"]
CONTAMINATION = 0.05
N_ESTIMATORS = 200

DEFAULT_NORMAL_SAMPLES = 8000
CSV_PATH = config.DATA_DIR / "synthetic_hive_telemetry.csv"


# ---------------------------------------------------------------------------
# Synthetic generators
# ---------------------------------------------------------------------------

def generate_normal(rng: np.random.Generator, n: int) -> np.ndarray:
    """Healthy colony behaviour over repeated day/night cycles.

    Bees thermoregulate the brood nest to ~34.5 C almost independently of
    ambient conditions, so temperature varies only slightly with the diurnal
    phase. Humidity swings more. Weight rises gradually through a nectar flow
    with a small daily forage cycle. Acoustic output sits near 250 Hz and lifts
    a little during daytime activity.
    """
    phase = rng.uniform(0, 2 * np.pi, n)
    day = np.sin(phase)

    temperature = 34.6 + 0.45 * day + rng.normal(0, 0.42, n)
    humidity = 61.0 + 6.0 * (-day) + rng.normal(0, 3.4, n)
    # Slow seasonal accumulation across the sample window plus a forage cycle.
    trend = np.linspace(0, 6.0, n)
    rng.shuffle(trend)
    weight = 40.0 + trend + 0.5 * day + rng.normal(0, 0.45, n)
    audio_frequency = 252.0 + 18.0 * np.clip(day, 0, None) + rng.normal(0, 11.0, n)

    return np.column_stack([temperature, humidity, weight, audio_frequency])


def generate_swarming(rng: np.random.Generator, n: int) -> np.ndarray:
    """Swarm preparation and departure: sharp weight loss, acoustic excitation."""
    temperature = 35.4 + rng.normal(0, 0.7, n)
    humidity = 58.0 + rng.normal(0, 5.0, n)
    # A departing prime swarm removes roughly 40-60 % of the colony's bees.
    weight = 33.5 - rng.uniform(0, 4.5, n) + rng.normal(0, 0.6, n)
    audio_frequency = 470.0 + rng.normal(0, 45.0, n)
    return np.column_stack([temperature, humidity, weight, audio_frequency])


def generate_overwintering_heat_loss(rng: np.random.Generator, n: int) -> np.ndarray:
    """Cluster failing to hold brood temperature in winter, stores draining."""
    temperature = 26.5 - rng.uniform(0, 6.0, n) + rng.normal(0, 1.0, n)
    humidity = 74.0 + rng.normal(0, 7.0, n)
    weight = 30.0 - rng.uniform(0, 8.0, n) + rng.normal(0, 0.8, n)
    audio_frequency = 175.0 + rng.normal(0, 22.0, n)
    return np.column_stack([temperature, humidity, weight, audio_frequency])


def generate_varroa(rng: np.random.Generator, n: int) -> np.ndarray:
    """Varroa destructor infestation: shifted, broadened acoustic signature."""
    temperature = 33.1 + rng.normal(0, 1.0, n)
    humidity = 66.0 + rng.normal(0, 6.0, n)
    weight = 38.0 - rng.uniform(0, 4.0, n) + rng.normal(0, 0.9, n)
    # Both a downward shift and a much wider spread relative to healthy 250 Hz.
    audio_frequency = 380.0 + rng.normal(0, 60.0, n)
    audio_frequency = np.where(
        rng.random(n) < 0.35, 150.0 + rng.normal(0, 25.0, n), audio_frequency
    )
    return np.column_stack([temperature, humidity, weight, audio_frequency])


ANOMALY_GENERATORS = {
    "swarming": generate_swarming,
    "overwintering_heat_loss": generate_overwintering_heat_loss,
    "varroa": generate_varroa,
}


def _clip_to_sensor_ranges(matrix: np.ndarray) -> np.ndarray:
    """Keep every generated value inside the API's accepted sensor envelope."""
    from honeychain.validation import SENSOR_RANGES

    clipped = matrix.copy()
    for index, name in enumerate(FEATURE_COLUMNS):
        low, high = SENSOR_RANGES[name]
        clipped[:, index] = np.clip(clipped[:, index], low, high)
    return clipped


def build_dataset(
    normal_samples: int = DEFAULT_NORMAL_SAMPLES, seed: int = RANDOM_SEED
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(features, labels)`` where label 0 = normal, 1 = anomaly.

    Anomalies are sized so they form exactly ``CONTAMINATION`` of the dataset.
    """
    rng = np.random.default_rng(seed)

    total = int(round(normal_samples / (1.0 - CONTAMINATION)))
    anomaly_total = total - normal_samples
    per_class = anomaly_total // len(ANOMALY_GENERATORS)
    remainder = anomaly_total - per_class * len(ANOMALY_GENERATORS)

    blocks = [generate_normal(rng, normal_samples)]
    labels = [np.zeros(normal_samples, dtype=int)]

    for position, (name, generator) in enumerate(ANOMALY_GENERATORS.items()):
        count = per_class + (1 if position < remainder else 0)
        blocks.append(generator(rng, count))
        labels.append(np.full(count, 1, dtype=int))
        logger.info("Generated %d %s samples", count, name)

    features = _clip_to_sensor_ranges(np.vstack(blocks))
    label_vector = np.concatenate(labels)

    order = rng.permutation(len(features))
    return features[order], label_vector[order]


def write_dataset_csv(features: np.ndarray, labels: np.ndarray, path: Path) -> None:
    """Persist the training set so the model's inputs are auditable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Fixed, obviously artificial epoch: synthetic rows must never look like
    # they were recorded at a real time, and the file must be reproducible.
    start = SYNTHETIC_EPOCH
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("timestamp," + ",".join(FEATURE_COLUMNS) + ",is_anomaly\n")
        for index, (row, label) in enumerate(zip(features, labels)):
            stamp = (start + timedelta(seconds=2 * index)).strftime("%Y-%m-%d %H:%M:%S")
            values = ",".join(f"{value:.4f}" for value in row)
            handle.write(f"{stamp},{values},{int(label)}\n")
    logger.info("Wrote training dataset to %s (%d rows)", path, len(features))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

WATCH_QUANTILE = 0.05
SYNTHETIC_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def train(normal_samples: int = DEFAULT_NORMAL_SAMPLES, seed: int = RANDOM_SEED) -> dict:
    import joblib
    from sklearn.ensemble import IsolationForest

    features, labels = build_dataset(normal_samples, seed)
    logger.info(
        "Dataset: %d rows (%d normal, %d anomalous, %.1f%% contamination)",
        len(features),
        int((labels == 0).sum()),
        int((labels == 1).sum()),
        100.0 * labels.mean(),
    )

    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        contamination=CONTAMINATION,
        random_state=seed,
        n_jobs=1,  # single-threaded: bit-for-bit reproducible artefact
    )
    model.fit(features)

    predictions = model.predict(features)  # -1 anomaly, 1 normal
    flagged = predictions == -1
    truth = labels == 1

    true_positive = int((flagged & truth).sum())
    false_positive = int((flagged & ~truth).sum())
    false_negative = int((~flagged & truth).sum())

    recall = true_positive / max(int(truth.sum()), 1)
    precision = true_positive / max(true_positive + false_positive, 1)
    false_alarm_rate = false_positive / max(int((~truth).sum()), 1)

    per_class: dict[str, float] = {}
    rng = np.random.default_rng(seed + 1)
    for name, generator in ANOMALY_GENERATORS.items():
        probe = _clip_to_sensor_ranges(generator(rng, 400))
        per_class[name] = float((model.predict(probe) == -1).mean())

    healthy_probe = _clip_to_sensor_ranges(generate_normal(rng, 2000))
    healthy_false_alarm = float((model.predict(healthy_probe) == -1).mean())

    # WATCH band, derived from data rather than picked by hand: the score
    # below which the lowest 5% of NORMAL training readings fall. Readings
    # between this and the model's own anomaly boundary (0) are "unusual but
    # within the normal range" -- a watch signal, never an alarm.
    normal_scores = model.decision_function(features[labels == 0])
    watch_below = float(np.quantile(normal_scores, WATCH_QUANTILE))

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, config.MODEL_PATH)
    joblib.dump(FEATURE_COLUMNS, config.FEATURE_COLUMNS_PATH)
    write_dataset_csv(features, labels, CSV_PATH)

    import platform
    import sklearn
    model_sha = _sha256(config.MODEL_PATH)
    metadata = {
        "schema_version": 2,
        "model_version": f"iforest-synthetic-{model_sha[:12]}",
        "model": "sklearn.ensemble.IsolationForest",
        "purpose": "telemetry anomaly detection (NOT honey quality, NOT a disease diagnosis)",
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "feature_columns": FEATURE_COLUMNS,
        "hyperparameters": {"contamination": CONTAMINATION, "n_estimators": N_ESTIMATORS,
                            "random_state": seed, "n_jobs": 1},
        # A pickled scikit-learn estimator is only valid for the exact
        # version that wrote it; honeychain.ml refuses to unpickle otherwise.
        "environment": {"python": platform.python_version(), "scikit_learn": sklearn.__version__,
                        "numpy": np.__version__, "joblib": joblib.__version__},
        # Checked BEFORE unpickling (unpickling executes code).
        "artifact": {"model_sha256": model_sha,
                     "feature_columns_sha256": _sha256(config.FEATURE_COLUMNS_PATH)},
        "provenance": {
            "data_source": "SYNTHETIC",
            "real_world_data_used": False,
            "generator": "train_model.py build_dataset(): seeded parametric distributions",
            "seed": seed,
            "dataset_csv": str(CSV_PATH.relative_to(config.BASE_DIR)),
            "dataset_sha256": _sha256(CSV_PATH),
            "rows": int(len(features)),
            "normal": int((labels == 0).sum()),
            "anomalous": int((labels == 1).sum()),
            "synthetic_anomaly_classes": list(ANOMALY_GENERATORS.keys()),
        },
        "bands": {
            "anomalous_below": 0.0,
            "watch_below": round(watch_below, 6),
            "derivation": f"watch_below = {WATCH_QUANTILE:.0%} quantile of decision_function "
                          "over NORMAL synthetic training readings; anomalous_below = the "
                          "model's contamination boundary (decision_function < 0)",
        },
        "synthetic_evaluation": {
            "data_source": "SYNTHETIC",
            "disclaimer": "Measured on data drawn from the SAME synthetic generator used for "
                          "training. These are NOT real-world accuracy figures and must not "
                          "be presented as such.",
            "training_fit": {"recall": round(recall, 4), "precision": round(precision, 4),
                             "false_alarm_rate_on_normal": round(false_alarm_rate, 4),
                             "false_negatives": false_negative},
            "held_out_detection_rate": {k: round(v, 4) for k, v in per_class.items()},
            "held_out_false_alarm_rate": round(healthy_false_alarm, 4),
        },
        # No labelled real-world hive data exists yet; nothing is claimed.
        "real_world_evaluation": None,
    }
    config.MODEL_METADATA_PATH.write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    logger.info("Saved model to %s", config.MODEL_PATH)
    logger.info("Saved feature schema to %s", config.FEATURE_COLUMNS_PATH)
    logger.info(
        "Detection on held-out anomalies: %s",
        ", ".join(f"{k}={v:.1%}" for k, v in per_class.items()),
    )
    logger.info("False-alarm rate on held-out healthy data: %.2f%%", healthy_false_alarm * 100)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the HoneyChain anomaly detector")
    parser.add_argument(
        "--normal-samples",
        type=int,
        default=DEFAULT_NORMAL_SAMPLES,
        help=f"number of healthy samples to generate (default {DEFAULT_NORMAL_SAMPLES})",
    )
    parser.add_argument(
        "--seed", type=int, default=RANDOM_SEED, help="random seed (default 42)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)-7s] %(message)s"
    )
    metadata = train(args.normal_samples, args.seed)
    print(json.dumps({"model_version": metadata["model_version"],
                      "synthetic_evaluation (NOT real-world accuracy)":
                          metadata["synthetic_evaluation"]["held_out_detection_rate"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
