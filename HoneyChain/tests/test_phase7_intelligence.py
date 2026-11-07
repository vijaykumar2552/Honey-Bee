"""
Phase 7 -- intelligence / ML hardening.

Several tests here are deliberate TRIPWIRES. They fail if anyone later:
  * reports synthetic evaluation as real-world accuracy (metadata, API, UI),
  * bypasses safe degradation (unpickling without the version/hash gates),
  * changes a biological safety threshold's default,
  * lets the requirements pin drift from the version that wrote the model,
  * changes training so the shipped artefact is no longer reproducible.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests.base import HoneyChainTestCase, REPO_ROOT
from tests.test_phase4_iot_security import DeviceTestCase

from honeychain import config, db, rules, sensor_faults
from honeychain.ml import HiveHealthPredictor, ML_ANOMALOUS, ML_NORMAL, ML_WATCH, Prediction, predictor
from honeychain.pipeline import evaluate_health

ROOT = Path(REPO_ROOT)
METADATA = json.loads((ROOT / "models/model_metadata.json").read_text())
NORMAL = {"temperature": 34.6, "humidity": 61.0, "weight": 42.0, "audio_frequency": 256.0}
SWARM = {"temperature": 35.4, "humidity": 60.0, "weight": 30.0, "audio_frequency": 480.0}
METRIC_WORDS = re.compile(r"(accuracy|precision|recall|detection_rate|false_alarm|f1)", re.I)


def _run(code: str, **env) -> subprocess.CompletedProcess:
    full = {**os.environ, **env}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
class TestProvenanceTripwires(unittest.TestCase):
    def test_metadata_declares_synthetic_training_and_no_real_world_claim(self) -> None:
        self.assertEqual(METADATA["schema_version"], 2)
        self.assertEqual(METADATA["provenance"]["data_source"], "SYNTHETIC")
        self.assertIs(METADATA["provenance"]["real_world_data_used"], False)
        self.assertIsNone(METADATA["real_world_evaluation"])
        self.assertIn("NOT real-world", METADATA["synthetic_evaluation"]["disclaimer"])

    def test_every_metric_lives_under_synthetic_evaluation(self) -> None:
        """TRIPWIRE: a metric anywhere else could be read as real accuracy."""
        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    here = f"{path}.{key}" if path else key
                    if METRIC_WORDS.search(key):
                        self.assertTrue(here.startswith("synthetic_evaluation."),
                                        f"metric '{here}' outside synthetic_evaluation")
                    walk(value, here)
        walk(METADATA, "")

    def test_api_reports_provenance_policy_and_thresholds(self) -> None:
        status = predictor.status()
        self.assertTrue(status["available"], status["error"])
        self.assertEqual(status["training_data_source"], "SYNTHETIC")
        self.assertIsNone(status["real_world_evaluation"])
        self.assertTrue(status["model_version"].startswith("iforest-synthetic-"))
        for key in status:
            if METRIC_WORDS.search(key):
                self.fail(f"top-level status key '{key}' looks like an accuracy claim")

    def test_dashboard_never_shows_synthetic_metrics_unlabelled(self) -> None:
        """TRIPWIRE for the old UI (still shipped until React parity)."""
        html = (ROOT / "templates/dashboard.html").read_text()
        js = (ROOT / "static/js/dashboard.js").read_text()
        self.assertIn("not real-world accuracy", html)
        self.assertIn("synthetic_evaluation", js)
        self.assertNotIn("(status.metadata || {}).held_out", js)
        for label in re.findall(r"\['([^']*(?:detection|false alarms)[^']*)'", js, re.I):
            self.assertIn("not real-world", label, label)

    def test_dataset_manifest_is_complete_hashed_and_consistent(self) -> None:
        manifest = json.loads((ROOT / "data/DATASETS.json").read_text())
        files = sorted(p.name for p in (ROOT / "data").iterdir()
                       if p.is_file() and p.name != "DATASETS.json")
        self.assertEqual(sorted(manifest["datasets"]), files, "undeclared or missing dataset")
        for name, entry in manifest["datasets"].items():
            self.assertIn(entry["data_source"], ("REAL", "SYNTHETIC"), name)
            self.assertIs(entry["has_ground_truth_health_labels"], False, name)
            self.assertEqual(hashlib.sha256((ROOT / "data" / name).read_bytes()).hexdigest(),
                             entry["sha256"], f"{name} changed without updating the manifest")
        training = manifest["production_model_training_data"]
        self.assertEqual(manifest["datasets"][training]["data_source"], "SYNTHETIC")
        self.assertTrue(METADATA["provenance"]["dataset_csv"].endswith(training))
        self.assertEqual(manifest["datasets"][training]["sha256"],
                         METADATA["provenance"]["dataset_sha256"])

    def test_synthetic_rows_use_an_artificial_epoch_not_real_time(self) -> None:
        with open(ROOT / "data/synthetic_hive_telemetry.csv") as fh:
            fh.readline()
            self.assertTrue(fh.readline().startswith("2000-01-01 "))


# ---------------------------------------------------------------------------
class TestVersionCompatibilityTripwires(unittest.TestCase):
    def test_requirements_pin_equals_the_version_that_wrote_the_model(self) -> None:
        pin = re.search(r"^scikit-learn==([\w.]+)\s*$",
                        (ROOT / "requirements.txt").read_text(), re.M)
        self.assertIsNotNone(pin, "scikit-learn must be pinned exactly")
        self.assertEqual(pin.group(1), METADATA["environment"]["scikit_learn"])
        self.assertEqual(importlib.metadata.version("scikit-learn"), pin.group(1))

    def test_model_loads_with_every_warning_escalated_to_an_error(self) -> None:
        out = _run("import warnings; warnings.simplefilter('error');"
                   "from honeychain.ml import HiveHealthPredictor as P; p=P();"
                   "print(p.load(), p.status()['error'])")
        self.assertEqual(out.stdout.strip(), "True None", out.stderr[-500:])

    def test_shipped_artefact_is_reproducible_from_source(self) -> None:
        """TRIPWIRE: retraining with the committed code + seed in a CLEAN
        interpreter (the real production path, `python train_model.py`) must
        yield a byte-identical model and dataset. A clean process matters:
        after another pickle has been loaded in-process, pickle framing can
        differ even for an identical model (verified: identical trees and
        decision_function), so bytes are compared only for a fresh process,
        and behaviour is compared as well."""
        tmp = Path(tempfile.mkdtemp(prefix="hc-retrain-"))
        csv = ROOT / "data" / "_retrain_check.csv"
        script = f"""
import json, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, {str(ROOT)!r})
from honeychain import config
import train_model
t = Path({str(tmp)!r})
with mock.patch.multiple(config, MODELS_DIR=t, MODEL_PATH=t / "m.joblib",
                         FEATURE_COLUMNS_PATH=t / "c.joblib", MODEL_METADATA_PATH=t / "meta.json"), \
     mock.patch.object(train_model, "CSV_PATH", Path({str(csv)!r})):
    meta = train_model.train()
print(json.dumps([meta["artifact"]["model_sha256"], meta["provenance"]["dataset_sha256"]]))
"""
        try:
            out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True,
                                 text=True, timeout=600)
            self.assertEqual(out.returncode, 0, out.stderr[-800:])
            model_sha, data_sha = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(model_sha, METADATA["artifact"]["model_sha256"])
            self.assertEqual(data_sha, METADATA["provenance"]["dataset_sha256"])

            import joblib
            import numpy as np
            shipped, rebuilt = (joblib.load(ROOT / "models/isolation_forest.joblib"),
                                joblib.load(tmp / "m.joblib"))
            X = np.random.default_rng(7).normal([34.6, 61, 42, 256], [1, 5, 3, 30], (3000, 4))
            self.assertTrue(np.array_equal(shipped.decision_function(X), rebuilt.decision_function(X)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            csv.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
class TestSafeLoadingGates(unittest.TestCase):
    """Every gate must refuse BEFORE unpickling and degrade, never raise."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-gates-"))
        for name in ("isolation_forest.joblib", "feature_columns.joblib", "model_metadata.json"):
            shutil.copy(ROOT / "models" / name, self.tmp / name)
        self.patch = mock.patch.multiple(
            config, MODEL_PATH=self.tmp / "isolation_forest.joblib",
            FEATURE_COLUMNS_PATH=self.tmp / "feature_columns.joblib",
            MODEL_METADATA_PATH=self.tmp / "model_metadata.json")
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _meta(self, mutate) -> None:
        meta = json.loads((self.tmp / "model_metadata.json").read_text())
        mutate(meta)
        (self.tmp / "model_metadata.json").write_text(json.dumps(meta))

    def _refused_without_unpickling(self, fragment: str) -> None:
        p = HiveHealthPredictor()
        with mock.patch("joblib.load", side_effect=AssertionError("must not unpickle")):
            self.assertFalse(p.load())
        self.assertIn(fragment, p.status()["error"])
        self.assertFalse(p.predict(NORMAL).available)

    def test_copied_artefacts_load(self) -> None:
        self.assertTrue(HiveHealthPredictor().load())

    def test_runtime_version_mismatch_is_refused(self) -> None:
        with mock.patch("honeychain.ml._installed_version", return_value="0.0.1"):
            self._refused_without_unpickling("refusing to unpickle across versions")

    def test_tampered_model_file_is_refused(self) -> None:
        path = self.tmp / "isolation_forest.joblib"
        data = bytearray(path.read_bytes()); data[-10] ^= 0xFF
        path.write_bytes(bytes(data))
        self._refused_without_unpickling("integrity check failed")

    def test_pre_provenance_metadata_is_refused(self) -> None:
        self._meta(lambda m: m.update(schema_version=1))
        self._refused_without_unpickling("predates provenance")

    def test_undeclared_training_data_is_refused(self) -> None:
        self._meta(lambda m: m["provenance"].pop("data_source"))
        self._refused_without_unpickling("data_source")

    def test_missing_metadata_is_refused(self) -> None:
        (self.tmp / "model_metadata.json").unlink()
        self._refused_without_unpickling("metadata missing")

    def test_inconsistent_version_warning_during_load_is_fatal(self) -> None:
        import joblib
        from sklearn.exceptions import InconsistentVersionWarning
        real = joblib.load

        def noisy(path):
            warnings.warn(InconsistentVersionWarning(estimator_name="X", current_sklearn_version="a",
                                                     original_sklearn_version="b"))
            return real(path)
        p = HiveHealthPredictor()
        with mock.patch("joblib.load", side_effect=noisy):
            self.assertFalse(p.load())
        self.assertIn("could not load model", p.status()["error"])

    def test_unreadable_artefact_degrades_instead_of_raising(self) -> None:
        with mock.patch("honeychain.ml._sha256", side_effect=PermissionError("denied")):
            self._refused_without_unpickling("could not read artefact")


# ---------------------------------------------------------------------------
class TestGradedInference(unittest.TestCase):
    def test_real_model_bands(self) -> None:
        self.assertEqual(predictor.predict(NORMAL).band, ML_NORMAL)
        swarm = predictor.predict(SWARM)
        self.assertEqual(swarm.band, ML_ANOMALOUS)
        self.assertEqual(swarm.model_version, METADATA["model_version"])

    def test_band_boundaries_follow_metadata(self) -> None:
        watch = METADATA["bands"]["watch_below"]
        self.assertGreater(watch, 0)
        p = HiveHealthPredictor()
        self.assertTrue(p.load())
        for score, band in ((-0.001, ML_ANOMALOUS), (0.0, ML_WATCH), (watch - 1e-6, ML_WATCH),
                            (watch, ML_NORMAL), (0.5, ML_NORMAL)):
            fake = mock.MagicMock()
            fake.decision_function.return_value = [score]
            fake.predict.return_value = [-1 if score < 0 else 1]
            p._model = fake
            self.assertEqual(p.predict(NORMAL).band, band, score)

    def test_watch_band_derivation_is_documented(self) -> None:
        self.assertIn("quantile", METADATA["bands"]["derivation"])


# ---------------------------------------------------------------------------
class TestFusionPolicy(unittest.TestCase):
    ANOM = Prediction(available=True, verdict="CRITICAL", anomaly_score=-0.1, is_anomaly=True,
                      band=ML_ANOMALOUS, model_version="t")

    def fuse(self, reading, prediction=None, flags=()):
        with mock.patch.object(predictor, "predict", return_value=prediction or self.ANOM):
            return evaluate_health(reading, None, flags)

    def test_anomaly_is_capped_at_warning_by_default(self) -> None:
        r = self.fuse(NORMAL)
        self.assertEqual((r["health_status"], r["ml_applied"], r["ml_band"]),
                         ("WARNING", True, ML_ANOMALOUS))
        self.assertIn("SYNTHETIC", r["reason"])

    def test_imputed_features_suppress_ml(self) -> None:
        imputed = Prediction(available=True, verdict="CRITICAL", anomaly_score=-0.1, is_anomaly=True,
                             band=ML_ANOMALOUS, imputed_fields=("audio_frequency",))
        r = self.fuse({"temperature": 34.5, "humidity": 60, "weight": 42}, imputed)
        self.assertEqual((r["health_status"], r["ml_applied"]), ("HEALTHY", False))
        self.assertIn("NOT applied", r["reason"])

    def test_sensor_flags_suppress_ml(self) -> None:
        r = self.fuse(NORMAL, flags=("FLATLINE_HUMIDITY",))
        self.assertEqual((r["health_status"], r["ml_applied"]), ("HEALTHY", False))
        self.assertIn("FLATLINE_HUMIDITY", r["reason"])

    def test_watch_never_changes_status(self) -> None:
        watch = Prediction(available=True, verdict="HEALTHY", anomaly_score=0.01, band=ML_WATCH)
        self.assertEqual(self.fuse(NORMAL, watch)["health_status"], "HEALTHY")

    def test_ml_never_softens_rules(self) -> None:
        normal = Prediction(available=True, verdict="HEALTHY", anomaly_score=0.3, band=ML_NORMAL)
        self.assertEqual(self.fuse({**NORMAL, "temperature": 20.0}, normal)["health_status"], "CRITICAL")

    def test_rules_alone_when_ml_unavailable(self) -> None:
        r = self.fuse(NORMAL, Prediction(available=False, error="x"))
        self.assertEqual((r["health_status"], r["source"], r["ml_band"]), ("HEALTHY", "RULE", None))

    def test_invalid_escalation_setting_refuses_to_start(self) -> None:
        out = _run("import honeychain.config", HONEYCHAIN_ML_MAX_ESCALATION="BOGUS")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("HONEYCHAIN_ML_MAX_ESCALATION", out.stderr)


# ---------------------------------------------------------------------------
class TestThresholdTripwires(unittest.TestCase):
    def test_default_safety_thresholds_are_pinned(self) -> None:
        """TRIPWIRE: changing a biological safety limit must be deliberate."""
        self.assertEqual(rules.DEFAULT_THRESHOLDS, {
            "TEMP_CRITICAL_LOW": 30.0, "TEMP_CRITICAL_HIGH": 38.0, "WEIGHT_CRITICAL_LOW": 5.0,
            "HUMIDITY_WARNING_LOW": 40.0, "HUMIDITY_WARNING_HIGH": 85.0})
        self.assertEqual(rules.active_thresholds(), rules.DEFAULT_THRESHOLDS)

    def test_override_changes_the_rule_verdict(self) -> None:
        with mock.patch.object(config, "RULE_THRESHOLDS", {"TEMP_CRITICAL_LOW": 33.0}):
            self.assertEqual(rules.evaluate_rules({"temperature": 32.0})["status"], "CRITICAL")
        self.assertEqual(rules.evaluate_rules({"temperature": 32.0})["status"], "HEALTHY")

    def test_invalid_overrides_refuse_to_start(self) -> None:
        for env in ({"HONEYCHAIN_RULE_TEMP_CRITICAL_LOW": "warm"},
                    {"HONEYCHAIN_RULE_TEMP_CRITICAL_LOW": "nan"},
                    {"HONEYCHAIN_RULE_HUMIDITY_WARNING_HIGH": "150"},
                    {"HONEYCHAIN_RULE_TEMP_CRITICAL_LOW": "39"}):   # inverted vs HIGH=38
            out = _run("import honeychain.config", **env)
            self.assertNotEqual(out.returncode, 0, env)

    def test_valid_override_is_accepted_and_reported(self) -> None:
        out = _run("from honeychain import rules; print(rules.active_thresholds()['TEMP_CRITICAL_LOW'])",
                   HONEYCHAIN_RULE_TEMP_CRITICAL_LOW="31.5")
        self.assertEqual(out.stdout.strip(), "31.5", out.stderr[-300:])


# ---------------------------------------------------------------------------
class TestRobustOutliers(unittest.TestCase):
    HIST = [{"temperature": 34.5 + (i % 5) * 0.05, "humidity": 60.0 + (i % 3) * 0.5} for i in range(25)]

    def test_modified_z_known_value(self) -> None:
        self.assertAlmostEqual(sensor_faults.modified_z(4.0, [1.0, 2.0, 3.0, 4.0, 5.0]),
                               0.6745 * 1.0 / 1.0)
        self.assertIsNone(sensor_faults.modified_z(3.0, [2.0, 2.0, 2.0]))

    def test_outlier_flagged_and_normal_not(self) -> None:
        self.assertIn("STAT_OUTLIER_TEMPERATURE",
                      sensor_faults.robust_outlier_flags({"temperature": 36.5}, self.HIST))
        self.assertEqual(sensor_faults.robust_outlier_flags({"temperature": 34.6, "humidity": 60.5},
                                                            self.HIST), [])

    def test_needs_minimum_history(self) -> None:
        self.assertEqual(sensor_faults.robust_outlier_flags({"temperature": 36.5}, self.HIST[:10]), [])


class TestDeviceIntelligenceEndToEnd(DeviceTestCase):
    def test_outlier_is_data_quality_not_a_health_conclusion(self) -> None:
        for i in range(config.SENSOR_ROBUST_MIN_HISTORY + 2):
            self.device_post(self.good_reading(temperature=34.5 + (i % 5) * 0.05,
                                               humidity=60 + (i % 3) * 0.5, audio_frequency=256))
        record = self.device_post(self.good_reading(temperature=36.5, humidity=60.5,
                                                    audio_frequency=256)).get_json()["records"][0]
        self.assertIn("STAT_OUTLIER_TEMPERATURE", record["sensor_flags"])
        self.assertEqual(record["health_status"], "HEALTHY")       # rules: 36.5 C is in range
        self.assertIn("NOT applied", record["reason"])

    def test_ml_band_and_model_version_are_persisted(self) -> None:
        record = self.device_post(self.good_reading(audio_frequency=256)).get_json()["records"][0]
        self.assertEqual(record["ml_band"], ML_NORMAL)
        self.assertEqual(record["model_version"], METADATA["model_version"])

    def test_safe_degradation_keeps_ingestion_working(self) -> None:
        with mock.patch.object(predictor, "_available", False):
            r = self.device_post(self.good_reading(audio_frequency=256))
        self.assertEqual(r.status_code, 200)
        record = r.get_json()["records"][0]
        self.assertEqual((record["health_status"], record["ml_band"]), ("HEALTHY", None))

    def test_simulated_device_readings_are_labelled(self) -> None:
        r = self.client.post("/api/devices", headers=self.h_a,
                             json={"hive_id": "HIVE-A", "data_origin": "SIMULATED"})
        creds = r.get_json()["credentials"]
        self.assertEqual(r.get_json()["device"]["data_origin"], "SIMULATED")
        rec = self.device_post(self.good_reading(), device_id=creds["device_id"],
                               secret=creds["device_secret"]).get_json()["records"][0]
        self.assertEqual(rec["data_origin"], "SIMULATED")
        physical = self.device_post(self.good_reading()).get_json()["records"][0]
        self.assertEqual(physical["data_origin"], "PHYSICAL")
        self.assertEqual(self.client.post("/api/devices", headers=self.h_a,
                                          json={"hive_id": "HIVE-A", "data_origin": "FAKE"}).status_code, 400)


class TestMlStatusApi(HoneyChainTestCase):
    def test_status_endpoint_exposes_policy_and_threshold_sources(self) -> None:
        body = self.client.get("/api/ml/status", headers=self.admin_headers()).get_json()
        self.assertEqual(body["fusion_policy"]["ml_max_escalation"], "WARNING")
        self.assertTrue(body["fusion_policy"]["rules_authoritative"])
        self.assertEqual(body["rule_thresholds"]["TEMP_CRITICAL_LOW"],
                         {"value": 30.0, "source": "default"})
        self.assertTrue(body["compatibility"]["compatible"])
        self.assertEqual(self.client.get("/api/ml/status").status_code, 401)

    def test_legacy_webhook_readings_are_unattributed(self) -> None:
        self.seed_hive("HIVE-LEGACY", count=1)
        row = self.client.get("/api/telemetry/latest", headers=self.admin_headers()).get_json()[0]
        self.assertEqual(row["data_origin"], "UNATTRIBUTED")


if __name__ == "__main__":
    unittest.main()
