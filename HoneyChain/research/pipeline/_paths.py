"""
Path resolution for the offline research pipeline.

The original scripts used ``../data`` and ``../models``, which only worked when
run from inside the old ``src/`` directory. These constants resolve against the
repository root instead, so the scripts run correctly from anywhere.

IMPORTANT -- ``MODEL_DIR`` points at ``research/models/``, deliberately NOT the
top-level ``models/``. The research models are trained on ~20 inside/outside
environmental features, whereas the production detector uses the four features
the live ESP32 nodes report. Writing research artefacts into the production
directory would leave the API with a model whose feature schema does not match
its input (honeychain.ml would detect the mismatch and fall back to the rule
engine, so the service would stay up, but the detector would be silently off).
Keeping the two sets apart avoids that entirely.
"""

from __future__ import annotations

from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
RESEARCH_DIR = PIPELINE_DIR.parent
REPO_ROOT = RESEARCH_DIR.parent

DATA_DIR = REPO_ROOT / "data"                 # shared datasets (read + write)
MODEL_DIR = RESEARCH_DIR / "models"           # research artefacts ONLY
OUTPUT_DIR = RESEARCH_DIR / "outputs" / "plots"

MODEL_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def data(name: str) -> str:
    """Absolute path to a file in the shared data directory."""
    return str(DATA_DIR / name)


def model_path(name: str) -> str:
    """Absolute path to a research model artefact.

    Named ``model_path`` rather than ``model`` because several pipeline scripts
    use ``model`` as a loop variable for the sklearn estimator itself.
    """
    return str(MODEL_DIR / name)
