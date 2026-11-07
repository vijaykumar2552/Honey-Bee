# research — auxiliary offline analysis

**Not part of the production service.** The live system is the Flask app at the
repository root. This directory preserves the original data-science work on the
real hive dataset.

## Contents

| Path | Role |
|---|---|
| `pipeline/01_eda.py` … `06_evaluate.py` | The original staged analysis |
| `pipeline/predict.py` | Batch inference against the research model |
| `pipeline/parse_raw.py` | Raw log → CSV |
| `pipeline/_paths.py` | Repository-root-relative path resolution (added) |
| `streamlit_dashboard.py` | Streamlit view over `data/hive_health_scored.csv` |
| `models/` | Research artefacts — **isolated from production `models/`** |
| `outputs/plots/` | Generated figures |
| `inspect_hive_data.py` | Read-only Firebase inspection helper |

## Two changes made during consolidation

1. **Paths.** The scripts used `../data` and `../models`, which only worked when
   run from inside the old `src/` directory. They now resolve against the
   repository root via `_paths.py`, so they run from anywhere.

2. **Model isolation.** Research artefacts write to `research/models/`, **not**
   the top-level `models/`. The research model is trained on ~20 inside/outside
   environmental features; production uses the four features the live ESP32
   nodes report. Writing research artefacts into the production directory would
   leave the API holding a model whose feature schema does not match its input.
   (`honeychain/ml.py` would detect the mismatch and fall back to the rule
   engine, so the service would stay up — but the detector would be silently
   off. Keeping them apart avoids the situation entirely.)

`_paths.model_path()` is named that way rather than `model` because several
scripts use `model` as a loop variable for the estimator itself.

## Running it

```bash
pip install pandas matplotlib seaborn plotly streamlit scikit-learn joblib
cd research/pipeline
python 01_eda.py
python 02_feature_engineering.py
python 03_train_anomaly_model.py
python 04_health_score.py
python 05_visualize.py
python 06_evaluate.py
```

Then, from the repository root:

```bash
streamlit run research/streamlit_dashboard.py
```

## Scientific limitation (carried over from the original work)

Temperature and humidity are environmental indicators and cannot independently
confirm a specific honeybee disease. This pipeline screens for abnormal
environmental patterns and flags disease *risk*; confirmatory diagnosis requires
colony inspection. The dataset is also only ~24 hours from a single hive — one
day/night cycle — which is why the production detector uses a separately
generated four-channel dataset instead. See README §10.
