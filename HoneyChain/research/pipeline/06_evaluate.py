"""
Step 14: Model Evaluation

IMPORTANT: This dataset has NO disease/health ground-truth labels (confirmed
in 01_eda.py). Therefore we CANNOT and DO NOT compute classification metrics
like accuracy, precision, recall, F1, or ROC-AUC for "disease detection" —
doing so would require inventing labels, which would be scientifically
dishonest.

Ground-truth disease diagnosis is not available, therefore the model detects
environmental abnormalities rather than confirmed disease. What we evaluate
instead are standard UNSUPERVISED anomaly-detection diagnostics:
  - Anomaly rate stability across chronological splits (train/val/test)
  - Score distribution shape
  - Sensitivity check: does the model react sensibly to injected synthetic
    extreme values (sanity check, not a real evaluation of disease detection)

If/when verified disease labels become available (see DATA_LABELING_STRATEGY.md),
re-run this script's "IF LABELS AVAILABLE" branch, which computes full
supervised metrics with emphasis on recall/false negatives.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import numpy as np
import joblib



def unsupervised_evaluation():
    df = pd.read_csv(data("hive_health_scored.csv"), parse_dates=["timestamp"])

    print("=" * 70)
    print("UNSUPERVISED ANOMALY DETECTION EVALUATION")
    print("(No disease ground truth available in this dataset)")
    print("=" * 70)

    for split in ["train", "val", "test"]:
        sub = df[df["split"] == split]
        rate = (sub["iso_forest_pred"] == -1).mean()
        print(f"\n{split.upper()}:")
        print(f"  N = {len(sub)}")
        print(f"  Raw anomaly rate: {rate:.1%}")
        print(f"  Mean health score: {sub['health_score'].mean():.1f}")
        print(f"  Persistent-abnormal rate (after noise filtering): "
              f"{sub['is_persistent_abnormal'].mean():.1%}")
        print(f"  Final status breakdown:")
        print(sub['final_status'].value_counts(normalize=True).round(3).to_string())

    print("\n" + "=" * 70)
    print("SANITY CHECK: injected synthetic extreme readings")
    print("(Verifies the model actually reacts to clearly abnormal input —")
    print(" this is a sanity check on model behavior, NOT a disease-detection")
    print(" accuracy metric.)")
    print("=" * 70)

    import sys
    sys.path.append(".")
    from predict import HivePredictor
    predictor = HivePredictor(hive_id="SanityCheck")

    # Warm up buffer with normal-looking readings
    base_time = pd.Timestamp("2026-09-05 08:00:00")
    for i in range(35):
        predictor.predict_new_reading(
            timestamp=base_time + pd.Timedelta(minutes=i),
            inside_temperature=34.5, inside_humidity=73,
            outside_temperature=30.0, outside_humidity=61,
        )

    # Now inject a clearly extreme reading (simulated overheating + humidity crash)
    extreme_result = predictor.predict_new_reading(
        timestamp=base_time + pd.Timedelta(minutes=36),
        inside_temperature=42.0,  # far above normal hive temp
        inside_humidity=30,        # far below normal
        outside_temperature=30.0,
        outside_humidity=61,
    )
    print(f"\nInjected extreme reading result:")
    print(f"  Health score: {extreme_result.get('health_score')}")
    print(f"  Status: {extreme_result.get('status')}")
    print(f"  Anomaly score: {extreme_result.get('anomaly_score')}")
    print(f"  Reasons: {extreme_result.get('reason')}")
    if extreme_result.get("health_score", 100) < 50:
        print("  PASS: model correctly flags a clearly extreme reading as low health score.")
    else:
        print("  WARNING: model did not flag an obviously extreme reading strongly — review calibration.")


def supervised_evaluation_if_labels_available(label_col="disease_label"):
    """
    Placeholder for when verified disease labels exist. NOT executed now
    because no labels are present in the current dataset.
    """
    from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                                  f1_score, confusion_matrix, roc_auc_score)
    print("This function requires a labelled dataset with a "
          f"'{label_col}' column populated from verified diagnoses. "
          "Skipping — no such data currently exists.")
    print("When available, this function should report Accuracy, Precision, "
          "Recall, F1, Confusion Matrix, and ROC-AUC, with special emphasis "
          "on RECALL and false negatives, because missing an unhealthy hive "
          "is worse than a false alarm.")


if __name__ == "__main__":
    unsupervised_evaluation()
    print()
    supervised_evaluation_if_labels_available()
