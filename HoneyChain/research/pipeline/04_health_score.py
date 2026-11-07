"""
Step 4-8: Hive Health Score, Persistence Logic, and Explainable AI

DISCLAIMER (see also README "Scientific Limitation" section):
The "Hive Health Score" and "Risk Level" produced here are AI-DERIVED
ENVIRONMENTAL ANOMALY INDICATORS, not medical diagnoses. Temperature and
humidity cannot, by themselves, confirm a specific bee disease (e.g. Varroa,
Nosema, foulbrood). This module screens for abnormal ENVIRONMENTAL patterns
that may warrant a beekeeper's inspection — it does not diagnose disease.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np
import joblib

PERSISTENCE_WINDOW = 5  # number of consecutive abnormal readings required to escalate risk


def anomaly_score_to_health_score(iso_scores: np.ndarray) -> np.ndarray:
    """
    Maps Isolation Forest decision_function output (roughly -0.5..0.5, higher
    = more normal) onto a 0-100 Hive Health Score.

    We use a robust min-max scaling based on the TRAINING distribution's
    typical range (computed once and hardcoded here from the fitted model's
    training scores) so that scores are stable and comparable across batches,
    rather than being rescaled relative to whatever batch happens to be
    scored right now (which would make scores incomparable over time).
    """
    # Reference range learned from training data (see calibrate_reference()).
    calib = joblib.load(model_path(f"score_calibration.joblib"))
    lo, hi = calib["p01"], calib["p99"]
    scaled = (iso_scores - lo) / (hi - lo)
    scaled = np.clip(scaled, 0, 1)
    return (scaled * 100).round(1)


def calibrate_reference(train_scores: np.ndarray):
    """Compute and persist the 1st/99th percentile of TRAIN anomaly scores as
    the reference range for 0-100 scaling. Must be called once during
    training (see main() below) — never recomputed from live/production data,
    or the score's meaning would drift over time."""
    calib = {"p01": float(np.percentile(train_scores, 1)),
             "p99": float(np.percentile(train_scores, 99))}
    joblib.dump(calib, model_path(f"score_calibration.joblib"))
    return calib


def classify_status(health_score: float) -> tuple:
    """Returns (status, risk_level) from the 0-100 health score."""
    if health_score >= 80:
        return "HEALTHY", "LOW"
    elif health_score >= 60:
        return "NORMAL - MONITOR", "LOW-MEDIUM"
    elif health_score >= 40:
        return "WARNING", "MEDIUM"
    else:
        return "HIGH RISK", "HIGH"


def apply_persistence_logic(df: pd.DataFrame, window: int = PERSISTENCE_WINDOW) -> pd.DataFrame:
    """
    Prevents a single noisy/abnormal reading from triggering a high-risk
    alert. An observation is only treated as a CONFIRMED abnormality if it
    is abnormal (iso_forest_pred == -1) for `window` consecutive readings.

    Adds:
      - is_raw_abnormal: single-reading anomaly flag (noisy)
      - consecutive_abnormal_count: how many in a row up to this point
      - is_persistent_abnormal: True once the streak reaches `window`
      - final_status / final_risk_level: persistence-adjusted classification
    """
    df = df.copy()
    df["is_raw_abnormal"] = (df["iso_forest_pred"] == -1)

    # Count consecutive True's ending at each row
    streak = df["is_raw_abnormal"].groupby(
        (~df["is_raw_abnormal"]).cumsum()
    ).cumcount() + 1
    df["consecutive_abnormal_count"] = np.where(df["is_raw_abnormal"], streak, 0)
    df["is_persistent_abnormal"] = df["consecutive_abnormal_count"] >= window

    # Final status: only escalate to WARNING/HIGH RISK based on the raw health
    # score IF the abnormality has persisted; otherwise cap at "NORMAL - MONITOR"
    # even if the instantaneous score looks poor (reduces false alarms from
    # transient weather blips / sensor noise).
    final_status, final_risk = [], []
    for _, row in df.iterrows():
        base_status, base_risk = classify_status(row["health_score"])
        if base_status in ("WARNING", "HIGH RISK") and not row["is_persistent_abnormal"]:
            final_status.append("NORMAL - MONITOR (transient anomaly)")
            final_risk.append("LOW-MEDIUM")
        else:
            final_status.append(base_status)
            final_risk.append(base_risk)
    df["final_status"] = final_status
    df["final_risk_level"] = final_risk
    return df


def explain_row(row: pd.Series, train_stats: dict) -> list:
    """
    Rule-based, sensor-grounded explanation for why a row was flagged.
    Compares the row's values to the TRAINING distribution's mean/std
    (learned healthy baseline) rather than inventing biological causes.
    """
    reasons = []
    checks = [
        ("inside_humidity", "Inside humidity"),
        ("inside_temperature", "Inside temperature"),
        ("outside_temperature", "Outside temperature"),
        ("outside_humidity", "Outside humidity"),
        ("temperature_difference", "Inside/outside temperature difference"),
        ("humidity_difference", "Inside/outside humidity difference"),
    ]
    for col, label in checks:
        mean, std = train_stats[col]["mean"], train_stats[col]["std"]
        if std == 0:
            continue
        z = (row[col] - mean) / std
        if abs(z) >= 2:
            direction = "above" if z > 0 else "below"
            reasons.append(
                f"{label} is {abs(z):.1f} standard deviations {direction} "
                f"the learned healthy baseline ({row[col]:.1f} vs baseline mean {mean:.1f})."
            )
    if row.get("is_persistent_abnormal", False):
        reasons.append(
            f"This abnormal pattern has persisted for "
            f"{int(row['consecutive_abnormal_count'])} consecutive readings, "
            f"not just a single noisy data point."
        )
    if not reasons:
        reasons.append("Environmental readings are within the learned normal range.")
    return reasons


def main():
    iso_forest = joblib.load(model_path(f"isolation_forest.joblib"))
    scaler = joblib.load(model_path(f"scaler.joblib"))
    feature_cols = joblib.load(model_path(f"feature_columns.joblib"))

    train_df = pd.read_csv(data("scored_train.csv"), parse_dates=["timestamp"])

    # Calibrate the 0-100 score reference range from TRAIN scores only.
    calibrate_reference(train_df["iso_forest_score"].values)

    # Baseline stats for explanations (fit on train only)
    stat_cols = ["inside_humidity", "inside_temperature", "outside_temperature",
                 "outside_humidity", "temperature_difference", "humidity_difference"]
    train_stats = {c: {"mean": train_df[c].mean(), "std": train_df[c].std()} for c in stat_cols}
    joblib.dump(train_stats, model_path(f"train_baseline_stats.joblib"))

    all_scored = []
    for split in ["train", "val", "test"]:
        df = pd.read_csv(data(f"scored_{split}.csv"), parse_dates=["timestamp"])
        df["health_score"] = anomaly_score_to_health_score(df["iso_forest_score"].values)
        df = apply_persistence_logic(df)
        df["reasons"] = df.apply(lambda r: " | ".join(explain_row(r, train_stats)), axis=1)
        df["split"] = split
        all_scored.append(df)
        print(f"\n{split.upper()} split status counts:")
        print(df["final_status"].value_counts().to_string())

    full = pd.concat(all_scored, ignore_index=True)
    full.to_csv(data("hive_health_scored.csv"), index=False)
    print("\nSaved full scored dataset (with health score, status, reasons) to "
          + data("hive_health_scored.csv"))

    print("\nSample explanations for flagged rows:")
    flagged = full[full["final_status"].isin(["WARNING", "HIGH RISK"])].head(3)
    for _, row in flagged.iterrows():
        print(f"\n  [{row['timestamp']}] Score={row['health_score']}, Status={row['final_status']}")
        for r in row["reasons"].split(" | "):
            print(f"    - {r}")


if __name__ == "__main__":
    main()
