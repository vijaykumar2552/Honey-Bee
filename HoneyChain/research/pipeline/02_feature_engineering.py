"""
Step 2: Feature Engineering

Transforms raw 4-sensor readings into a rich feature set so the anomaly model
can learn not just "is this value normal in isolation" but "is this hive
behaving normally compared to its own recent history and the outside world".

All rolling/lag features use only PAST data (no look-ahead), which is required
because this will later be split chronologically for training/validation/test.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np

IN_PATH = data("hive_environment_clean.csv")
OUT_PATH = data("hive_features.csv")

# Rolling window sizes, in NUMBER OF SAMPLES (not fixed minutes), since the
# sampling interval is ~1 reading/minute but has occasional gaps.
SHORT_WINDOW = 5     # ~5 minutes  -> short-term noise smoothing
LONG_WINDOW = 30      # ~30 minutes -> medium-term drift/trend
LAG_STEPS = [1, 5, 15]  # minutes-ago lag features


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # ---- 1. Derived cross-sensor features -----------------------------
    df["temperature_difference"] = df["inside_temperature"] - df["outside_temperature"]
    df["humidity_difference"] = df["inside_humidity"] - df["outside_humidity"]
    df["absolute_temperature_difference"] = df["temperature_difference"].abs()
    df["absolute_humidity_difference"] = df["humidity_difference"].abs()

    # ---- 2. Temporal features -------------------------------------------
    df["hour"] = df["timestamp"].dt.hour
    df["day"] = df["timestamp"].dt.day
    df["day_of_week"] = df["timestamp"].dt.dayofweek
    df["month"] = df["timestamp"].dt.month

    def time_of_day(h):
        if 5 <= h < 12:
            return "morning"
        elif 12 <= h < 17:
            return "afternoon"
        elif 17 <= h < 21:
            return "evening"
        else:
            return "night"
    df["time_of_day"] = df["hour"].apply(time_of_day)
    # one-hot encode for model use
    tod_dummies = pd.get_dummies(df["time_of_day"], prefix="tod")
    df = pd.concat([df, tod_dummies], axis=1)

    base_cols = ["inside_temperature", "inside_humidity",
                 "outside_temperature", "outside_humidity"]

    # ---- 3. Rolling statistics (past-only, causal) -----------------------
    for col in base_cols:
        for win, label in [(SHORT_WINDOW, "short"), (LONG_WINDOW, "long")]:
            df[f"{col}_roll_mean_{label}"] = (
                df[col].rolling(window=win, min_periods=1).mean()
            )
            df[f"{col}_roll_std_{label}"] = (
                df[col].rolling(window=win, min_periods=1).std().fillna(0)
            )
            df[f"{col}_roll_min_{label}"] = (
                df[col].rolling(window=win, min_periods=1).min()
            )
            df[f"{col}_roll_max_{label}"] = (
                df[col].rolling(window=win, min_periods=1).max()
            )

    # ---- 4. Rate of change / trend ---------------------------------------
    for col in base_cols:
        df[f"{col}_rate_of_change"] = df[col].diff().fillna(0)
        # trend = slope of short rolling window (simple diff of rolling mean)
        df[f"{col}_trend"] = (
            df[f"{col}_roll_mean_{'short'}"].diff().fillna(0)
        )

    # ---- 5. Lag features --------------------------------------------------
    for col in base_cols:
        for lag in LAG_STEPS:
            df[f"{col}_lag_{lag}"] = df[col].shift(lag)

    # Drop rows with NaNs introduced by the largest lag (start of series only)
    df = df.dropna().reset_index(drop=True)

    return df


def main():
    df = pd.read_csv(IN_PATH)
    feat_df = engineer_features(df)
    feat_df.to_csv(OUT_PATH, index=False)
    print(f"Engineered feature set: {feat_df.shape[0]} rows x {feat_df.shape[1]} columns")
    print(f"Saved to {OUT_PATH}")
    print("\nFeature columns:")
    for c in feat_df.columns:
        print(f"  - {c}")


if __name__ == "__main__":
    main()
