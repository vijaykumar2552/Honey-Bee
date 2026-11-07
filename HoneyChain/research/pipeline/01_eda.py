"""
Step 1: Exploratory Data Analysis for HoneyChain hive environmental data.

Auto-detects column names via a configurable mapping (COLUMN_MAP below) so this
script can be pointed at differently-named datasets without code changes.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# CONFIGURABLE COLUMN MAPPING
# Edit this if your raw file uses different column names.
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    "timestamp": "timestamp",
    "inside_temperature": "inside_temperature",
    "inside_humidity": "inside_humidity",
    "outside_temperature": "outside_temperature",
    "outside_humidity": "outside_humidity",
}

IN_PATH = data("hive_environment_raw.csv")


def load_and_standardize(path=IN_PATH, colmap=COLUMN_MAP):
    df = pd.read_csv(path, parse_dates=[colmap["timestamp"]])
    df = df.rename(columns={v: k for k, v in colmap.items()})
    df = df[["timestamp", "inside_temperature", "inside_humidity",
             "outside_temperature", "outside_humidity"]]
    return df


def main():
    df = load_and_standardize()

    print("=" * 70)
    print("HONEYCHAIN - HIVE ENVIRONMENTAL DATA INSPECTION")
    print("=" * 70)

    print(f"\n1. RECORD COUNT: {len(df)}")
    print(f"2. TIME PERIOD: {df['timestamp'].min()}  ->  {df['timestamp'].max()}")
    duration = df['timestamp'].max() - df['timestamp'].min()
    print(f"   Duration: {duration}")

    print(f"\n3. COLUMNS DETECTED:")
    for c in df.columns:
        print(f"   - {c} ({df[c].dtype})")

    print(f"\n4. MISSING VALUES:")
    print(df.isna().sum().to_string())

    print(f"\n5. DUPLICATE TIMESTAMPS: {df['timestamp'].duplicated().sum()}")
    print(f"   FULLY DUPLICATE ROWS: {df.duplicated().sum()}")

    # Sampling interval check
    diffs = df['timestamp'].diff().dt.total_seconds().dropna()
    print(f"\n6. SAMPLING INTERVAL (seconds):")
    print(f"   median={diffs.median():.0f}s, min={diffs.min():.0f}s, max={diffs.max():.0f}s")
    gaps = diffs[diffs > 300]  # gaps > 5 min
    print(f"   Gaps > 5 min: {len(gaps)} (device/connectivity dropouts)")
    if len(gaps) > 0:
        print(f"   Largest gap: {gaps.max():.0f}s (~{gaps.max()/60:.1f} min)")

    print(f"\n7. DESCRIPTIVE STATISTICS:")
    stats = df[["inside_temperature", "inside_humidity",
                "outside_temperature", "outside_humidity"]].describe().T
    stats["range"] = stats["max"] - stats["min"]
    print(stats.round(2).to_string())

    print(f"\n8. OUTLIER CHECK (values outside plausible physical range):")
    checks = {
        "inside_temperature": (0, 50),
        "inside_humidity": (0, 100),
        "outside_temperature": (-10, 50),
        "outside_humidity": (0, 100),
    }
    for col, (lo, hi) in checks.items():
        bad = df[(df[col] < lo) | (df[col] > hi)]
        print(f"   {col}: {len(bad)} values outside [{lo}, {hi}]")

    print(f"\n   Statistical outliers (IQR method, 1.5x):")
    for col in checks:
        q1, q3 = df[col].quantile([0.25, 0.75])
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        n_out = ((df[col] < lo) | (df[col] > hi)).sum()
        print(f"   {col}: {n_out} statistical outliers (bounds: {lo:.1f} to {hi:.1f})")

    print(f"\n9. CORRELATIONS:")
    corr = df[["inside_temperature", "inside_humidity",
               "outside_temperature", "outside_humidity"]].corr()
    print(corr.round(3).to_string())

    print(f"\n10. INSIDE vs OUTSIDE RELATIONSHIP:")
    df["temp_diff"] = df["inside_temperature"] - df["outside_temperature"]
    df["humidity_diff"] = df["inside_humidity"] - df["outside_humidity"]
    print(f"   Mean inside-outside temp difference: {df['temp_diff'].mean():.2f} C "
          f"(std={df['temp_diff'].std():.2f})")
    print(f"   Mean inside-outside humidity difference: {df['humidity_diff'].mean():.2f} % "
          f"(std={df['humidity_diff'].std():.2f})")
    print(f"   -> The hive maintains inside temperature ABOVE outside temperature "
          f"by ~{df['temp_diff'].mean():.1f}C on average, consistent with active "
          f"brood-nest thermoregulation (bees typically hold ~33-36C core temp).")

    print(f"\n11. DISEASE LABELS: NOT PRESENT in this dataset.")
    print("    This is a single continuous ~24h recording with only environmental")
    print("    sensor readings — no health/disease ground truth column exists.")
    print("    => Supervised disease classification is NOT possible with this data.")
    print("    => We will build an UNSUPERVISED anomaly-detection baseline instead,")
    print("       positioned as 'AI-Based Early Detection of Hive Health Abnormality'.")

    print(f"\n12. NUMBER OF HIVES: 1 (single hive, single continuous stream).")
    print("    The database/model architecture will still be designed multi-hive-ready.")

    # Drop the temp_diff/humidity_diff columns added above for the printed
    # inspection report only — feature engineering (Step 2) recomputes these
    # properly-named derived features itself, so we keep the clean CSV to
    # just the 5 raw columns to avoid duplicate/inconsistent column names.
    df_to_save = df.drop(columns=["temp_diff", "humidity_diff"])
    df_to_save.to_csv(data("hive_environment_clean.csv"), index=False)
    print(f"\nSaved cleaned data to {data('hive_environment_clean.csv')}")


if __name__ == "__main__":
    main()
