"""
Step 10: Real-Time Prediction Function

This module is what the CLOUD/SERVER side calls whenever a new reading
arrives from an ESP32 (or any IoT device). The ESP32 itself only collects and
transmits raw sensor values — all AI inference happens here.

Usage:
    from predict import HivePredictor
    predictor = HivePredictor(hive_id="Hive_001")
    result = predictor.predict_new_reading(
        timestamp="2026-09-05 10:15:00",
        inside_temperature=34.6,
        inside_humidity=71,
        outside_temperature=30.1,
        outside_humidity=60,
    )
    print(result)

NOTE ON STATE: because rolling/lag features depend on recent history, this
predictor keeps a short in-memory buffer of the hive's last N readings per
hive_id. In production, replace this buffer with a query against the
Hive_ID-partitioned rows in the cloud database (see database/schema.sql),
so the buffer survives server restarts and works correctly across multiple
hives/devices.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np
import joblib
from collections import defaultdict, deque

BUFFER_SIZE = 35  # must be >= LONG_WINDOW used in feature engineering + max lag


class HivePredictor:
    def __init__(self, hive_id="Hive_001", model_dir=MODEL_DIR, persistence_window=5):
        self.hive_id = hive_id
        self.scaler = joblib.load(f"{model_dir}/scaler.joblib")
        self.model = joblib.load(f"{model_dir}/isolation_forest.joblib")
        self.feature_cols = joblib.load(f"{model_dir}/feature_columns.joblib")
        self.calib = joblib.load(f"{model_dir}/score_calibration.joblib")
        self.train_stats = joblib.load(f"{model_dir}/train_baseline_stats.joblib")
        self.persistence_window = persistence_window

        # Per-hive rolling buffer of raw readings (deque of dicts)
        self._buffers = defaultdict(lambda: deque(maxlen=BUFFER_SIZE))
        self._abnormal_streak = defaultdict(int)

    def _engineer_features_for_latest(self, hive_id):
        """Recomputes rolling/lag/temporal features for the MOST RECENT
        reading using this hive's buffer, mirroring 02_feature_engineering.py."""
        buf = list(self._buffers[hive_id])
        df = pd.DataFrame(buf)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        df["temperature_difference"] = df["inside_temperature"] - df["outside_temperature"]
        df["humidity_difference"] = df["inside_humidity"] - df["outside_humidity"]
        df["absolute_temperature_difference"] = df["temperature_difference"].abs()
        df["absolute_humidity_difference"] = df["humidity_difference"].abs()

        df["hour"] = df["timestamp"].dt.hour
        df["day"] = df["timestamp"].dt.day
        df["day_of_week"] = df["timestamp"].dt.dayofweek
        df["month"] = df["timestamp"].dt.month

        def time_of_day(h):
            if 5 <= h < 12: return "morning"
            elif 12 <= h < 17: return "afternoon"
            elif 17 <= h < 21: return "evening"
            else: return "night"
        tod = df["hour"].apply(time_of_day)
        for cat in ["afternoon", "evening", "morning", "night"]:
            df[f"tod_{cat}"] = (tod == cat).astype(int)

        base_cols = ["inside_temperature", "inside_humidity",
                     "outside_temperature", "outside_humidity"]
        for col in base_cols:
            for win, label in [(5, "short"), (30, "long")]:
                df[f"{col}_roll_mean_{label}"] = df[col].rolling(win, min_periods=1).mean()
                df[f"{col}_roll_std_{label}"] = df[col].rolling(win, min_periods=1).std().fillna(0)
                df[f"{col}_roll_min_{label}"] = df[col].rolling(win, min_periods=1).min()
                df[f"{col}_roll_max_{label}"] = df[col].rolling(win, min_periods=1).max()
        for col in base_cols:
            df[f"{col}_rate_of_change"] = df[col].diff().fillna(0)
            df[f"{col}_trend"] = df[f"{col}_roll_mean_short"].diff().fillna(0)
        for col in base_cols:
            for lag in [1, 5, 15]:
                df[f"{col}_lag_{lag}"] = df[col].shift(lag)

        latest = df.iloc[[-1]].copy()
        # Fill any remaining NaNs (e.g. insufficient history for a lag) with
        # the current value as a safe fallback, rather than dropping the row
        # (a live prediction must always return a result).
        latest = latest.ffill(axis=0).fillna(0)
        return latest

    def predict_new_reading(self, timestamp, inside_temperature, inside_humidity,
                             outside_temperature, outside_humidity, hive_id=None):
        hive_id = hive_id or self.hive_id

        # --- Basic input validation -----------------------------------
        for name, val, lo, hi in [
            ("inside_temperature", inside_temperature, 0, 55),
            ("inside_humidity", inside_humidity, 0, 100),
            ("outside_temperature", outside_temperature, -20, 55),
            ("outside_humidity", outside_humidity, 0, 100),
        ]:
            if val is None or not (lo <= val <= hi):
                return {"error": f"Invalid or out-of-range value for {name}: {val}"}

        reading = {
            "timestamp": timestamp,
            "inside_temperature": float(inside_temperature),
            "inside_humidity": float(inside_humidity),
            "outside_temperature": float(outside_temperature),
            "outside_humidity": float(outside_humidity),
        }
        self._buffers[hive_id].append(reading)

        try:
            feat_row = self._engineer_features_for_latest(hive_id)
            X = feat_row[self.feature_cols].values
            X_scaled = self.scaler.transform(X)

            iso_score = float(self.model.decision_function(X_scaled)[0])
            iso_pred = int(self.model.predict(X_scaled)[0])  # -1 or 1

            # Health score via calibrated range from training
            lo, hi = self.calib["p01"], self.calib["p99"]
            scaled = np.clip((iso_score - lo) / (hi - lo), 0, 1)
            health_score = round(float(scaled * 100), 1)

            # Persistence tracking
            is_abnormal = iso_pred == -1
            if is_abnormal:
                self._abnormal_streak[hive_id] += 1
            else:
                self._abnormal_streak[hive_id] = 0
            streak = self._abnormal_streak[hive_id]
            is_persistent = streak >= self.persistence_window

            # Base classification from score
            if health_score >= 80:
                status, risk = "HEALTHY", "LOW"
            elif health_score >= 60:
                status, risk = "NORMAL - MONITOR", "LOW-MEDIUM"
            elif health_score >= 40:
                status, risk = "WARNING", "MEDIUM"
            else:
                status, risk = "HIGH RISK", "HIGH"

            # Persistence override: downgrade a transient bad score
            if status in ("WARNING", "HIGH RISK") and not is_persistent:
                status = "NORMAL - MONITOR (transient anomaly)"
                risk = "LOW-MEDIUM"

            # Explanation
            reasons = []
            row = feat_row.iloc[0]
            for col, label in [
                ("inside_humidity", "Inside humidity"),
                ("inside_temperature", "Inside temperature"),
                ("outside_temperature", "Outside temperature"),
                ("outside_humidity", "Outside humidity"),
                ("temperature_difference", "Inside/outside temperature difference"),
                ("humidity_difference", "Inside/outside humidity difference"),
            ]:
                mean, std = self.train_stats[col]["mean"], self.train_stats[col]["std"]
                if std == 0:
                    continue
                z = (row[col] - mean) / std
                if abs(z) >= 2:
                    direction = "above" if z > 0 else "below"
                    reasons.append(
                        f"{label} is {abs(z):.1f} SD {direction} the learned "
                        f"healthy baseline ({row[col]:.1f} vs baseline mean {mean:.1f})."
                    )
            if is_persistent:
                reasons.append(f"Abnormal pattern has persisted for {streak} consecutive readings.")
            if not reasons:
                reasons.append("Environmental readings are within the learned normal range.")

            return {
                "hive_id": hive_id,
                "timestamp": str(timestamp),
                "health_score": health_score,
                "status": status,
                "risk_level": risk,
                "anomaly_score": round(iso_score, 4),
                "is_persistent_abnormality": bool(is_persistent),
                "consecutive_abnormal_readings": int(streak),
                "reason": reasons,
                "disclaimer": (
                    "This is an AI-derived environmental health/risk score, not a "
                    "medical disease diagnosis. Confirmatory diagnosis requires "
                    "physical hive inspection."
                ),
            }
        except Exception as e:
            return {"error": f"Prediction failed: {str(e)}"}


if __name__ == "__main__":
    # Quick smoke test using a few readings replayed from the real dataset
    predictor = HivePredictor(hive_id="Hive_001")
    test_df = pd.read_csv(data("hive_environment_clean.csv")).tail(40)
    for _, r in test_df.iterrows():
        result = predictor.predict_new_reading(
            timestamp=r["timestamp"],
            inside_temperature=r["inside_temperature"],
            inside_humidity=r["inside_humidity"],
            outside_temperature=r["outside_temperature"],
            outside_humidity=r["outside_humidity"],
        )
    print("Final reading result:")
    import json
    print(json.dumps(result, indent=2))
