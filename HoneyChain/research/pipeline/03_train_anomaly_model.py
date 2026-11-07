"""
Step 3: LEVEL 1 - Unsupervised Anomaly Detection Model Training

WHY A CHRONOLOGICAL SPLIT (not random shuffling):
This is time-series sensor data. A hive's environmental state at 2:05pm is
highly correlated with its state at 2:04pm (autocorrelation, rolling/lag
features). If we randomly shuffled rows into train/val/test, near-identical
rows from the same few minutes could end up on both sides of the split. The
model would then be evaluated on data leaking information from training,
giving a falsely optimistic sense of how well it detects UNSEEN, FUTURE
abnormal behavior. In a real deployment, the model only ever sees the past to
judge the present, so we replicate that here: train on the earliest data,
validate on the middle segment, and test on the most recent segment.

MODEL CHOICE:
No disease labels exist in this dataset (confirmed in Step 1), so we cannot
train a supervised classifier. Instead we train unsupervised anomaly
detectors that learn the shape of "normal" hive environmental behavior from
the training window and flag deviations from it in later data:
  - Isolation Forest (primary): scales well, robust to feature correlation,
    fast inference (needed for real-time ESP32-triggered predictions)
  - Local Outlier Factor (comparison): density-based, good at local anomalies
  - One-Class SVM (comparison): boundary-based, sensitive to scaling

All scalers/models are fit ONLY on the training split and then applied
(never re-fit) to validation/test/production data, to avoid leakage.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np
import joblib
from sklearn.ensemble import IsolationForest
from sklearn.svm import OneClassSVM
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler

RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)

IN_PATH = data("hive_features.csv")

FEATURE_COLS_EXCLUDE = {"timestamp", "time_of_day"}


def chronological_split(df, train_frac=0.70, val_frac=0.15):
    n = len(df)
    train_end = int(n * train_frac)
    val_end = int(n * (train_frac + val_frac))
    train = df.iloc[:train_end].copy()
    val = df.iloc[train_end:val_end].copy()
    test = df.iloc[val_end:].copy()
    return train, val, test


def get_feature_columns(df):
    return [c for c in df.columns if c not in FEATURE_COLS_EXCLUDE]


def main():
    df = pd.read_csv(IN_PATH, parse_dates=["timestamp"])
    feature_cols = get_feature_columns(df)
    print(f"Using {len(feature_cols)} features for anomaly detection.")

    train_df, val_df, test_df = chronological_split(df)
    print(f"\nChronological split:")
    print(f"  Train: {len(train_df)} rows  ({train_df['timestamp'].min()} -> {train_df['timestamp'].max()})")
    print(f"  Val:   {len(val_df)} rows  ({val_df['timestamp'].min()} -> {val_df['timestamp'].max()})")
    print(f"  Test:  {len(test_df)} rows  ({test_df['timestamp'].min()} -> {test_df['timestamp'].max()})")

    X_train = train_df[feature_cols].values
    X_val = val_df[feature_cols].values
    X_test = test_df[feature_cols].values

    # Fit scaler ONLY on training data (no leakage)
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)
    X_test_scaled = scaler.transform(X_test)

    # -----------------------------------------------------------------
    # Candidate 1: Isolation Forest (primary choice)
    # -----------------------------------------------------------------
    iso_forest = IsolationForest(
        n_estimators=200,
        contamination=0.05,  # assume ~5% of readings may be borderline/abnormal
        random_state=RANDOM_SEED,
        n_jobs=-1,
    )
    iso_forest.fit(X_train_scaled)

    # -----------------------------------------------------------------
    # Candidate 2: One-Class SVM (comparison)
    # -----------------------------------------------------------------
    ocsvm = OneClassSVM(kernel="rbf", nu=0.05, gamma="scale")
    ocsvm.fit(X_train_scaled)

    # -----------------------------------------------------------------
    # Candidate 3: Local Outlier Factor (comparison, novelty mode)
    # -----------------------------------------------------------------
    lof = LocalOutlierFactor(n_neighbors=20, novelty=True, contamination=0.05)
    lof.fit(X_train_scaled)

    # -----------------------------------------------------------------
    # Evaluate all three on validation set using internal agreement +
    # anomaly rate stability, since there is no ground truth to score against.
    # A "good" unsupervised detector should flag a small, STABLE proportion
    # of points as anomalous, roughly consistent between val and test.
    # -----------------------------------------------------------------
    TARGET_CONTAMINATION = 0.05
    results = {}
    for name, model in [("IsolationForest", iso_forest),
                         ("OneClassSVM", ocsvm),
                         ("LocalOutlierFactor", lof)]:
        val_pred = model.predict(X_val_scaled)  # -1 = anomaly, 1 = normal
        test_pred = model.predict(X_test_scaled)
        val_rate = (val_pred == -1).mean()
        test_rate = (test_pred == -1).mean()
        # Score = how close BOTH splits are to the intended ~5% anomaly rate,
        # not merely how similar val and test are to each other. A model that
        # consistently flags 70% of readings as "abnormal" is stably wrong,
        # not trustworthy — being close to val==test alone doesn't make it good.
        deviation_from_target = abs(val_rate - TARGET_CONTAMINATION) + abs(test_rate - TARGET_CONTAMINATION)
        results[name] = {"val_anomaly_rate": val_rate, "test_anomaly_rate": test_rate,
                          "deviation_from_target": deviation_from_target}
        print(f"\n{name}:")
        print(f"  Val anomaly rate:  {val_rate:.3f}")
        print(f"  Test anomaly rate: {test_rate:.3f}")
        print(f"  Deviation from {TARGET_CONTAMINATION:.0%} target (lower=better): {deviation_from_target:.3f}")

    best_name = min(results, key=lambda k: results[k]["deviation_from_target"])
    print(f"\n>>> Selected model: {best_name} (closest to target anomaly rate on both val and test)")

    print(f"\n*** IMPORTANT DATA LIMITATION ***")
    print("This dataset covers only ~24 hours from a single hive — one full")
    print("day/night cycle. A chronological split therefore puts distinctly")
    print("different times-of-day (e.g. cool pre-dawn vs warm midday) into")
    print("different splits. Some of the swings seen above between val/test")
    print("anomaly rates reflect this natural diurnal shift, not necessarily")
    print("model instability. For a production-grade baseline, at least")
    print("1-2 weeks of multi-day data is recommended so the model learns")
    print("the full daily cycle as 'normal' rather than treating unfamiliar")
    print("times of day as anomalous.")

    best_model = {"IsolationForest": iso_forest, "OneClassSVM": ocsvm,
                  "LocalOutlierFactor": lof}[best_name]

    # -----------------------------------------------------------------
    # Persist artifacts
    # -----------------------------------------------------------------
    joblib.dump(scaler, model_path(f"scaler.joblib"))
    joblib.dump(iso_forest, model_path(f"isolation_forest.joblib"))
    joblib.dump(ocsvm, model_path(f"one_class_svm.joblib"))
    joblib.dump(lof, model_path(f"local_outlier_factor.joblib"))
    joblib.dump(feature_cols, model_path(f"feature_columns.joblib"))
    with open(model_path(f"best_model_name.txt"), "w") as f:
        f.write(best_name)

    print(f"\nSaved all models + scaler + feature list to {MODEL_DIR}/")

    # Save train/val/test splits with anomaly scores attached for downstream
    # health-score calculation and visualization.
    for split_name, split_df, X_scaled in [
        ("train", train_df, X_train_scaled),
        ("val", val_df, X_val_scaled),
        ("test", test_df, X_test_scaled),
    ]:
        split_df = split_df.copy()
        # decision_function: higher = more normal, lower/negative = more anomalous
        split_df["iso_forest_score"] = iso_forest.decision_function(X_scaled)
        split_df["iso_forest_pred"] = iso_forest.predict(X_scaled)  # -1/1
        split_df.to_csv(data(f"scored_{split_name}.csv"), index=False)

    print("Saved scored train/val/test CSVs to the shared data/ directory")


if __name__ == "__main__":
    main()
