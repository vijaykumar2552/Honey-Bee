"""Rule engine, ML integration, atomic persistence and the anomaly flow."""

from __future__ import annotations

import hashlib
import hmac
import unittest
from unittest import mock

from tests.base import TEST_SECRET, HoneyChainTestCase

from honeychain import db, rules
from honeychain.ml import HiveHealthPredictor, Prediction, predictor
from honeychain.pipeline import evaluate_health


class TestRuleEngine(unittest.TestCase):
    """The specified rule engine, exactly as written. Pure function, no I/O."""

    def test_healthy_when_all_values_safe(self):
        result = rules.evaluate_rules(
            {"temperature": 34.5, "humidity": 62.0, "weight": 42.0}
        )
        self.assertEqual(result["status"], rules.HEALTHY)

    def test_critical_when_temperature_below_30(self):
        result = rules.evaluate_rules({"temperature": 29.9, "humidity": 60.0, "weight": 40.0})
        self.assertEqual(result["status"], rules.CRITICAL)

    def test_critical_when_temperature_above_38(self):
        result = rules.evaluate_rules({"temperature": 38.1, "humidity": 60.0, "weight": 40.0})
        self.assertEqual(result["status"], rules.CRITICAL)

    def test_critical_when_weight_below_5(self):
        result = rules.evaluate_rules({"temperature": 34.5, "humidity": 60.0, "weight": 4.9})
        self.assertEqual(result["status"], rules.CRITICAL)

    def test_temperature_boundaries_are_not_critical(self):
        """30 and 38 exactly are inside the band: the rules use < and >."""
        for temperature in (30.0, 38.0):
            self.assertEqual(
                rules.evaluate_rules({"temperature": temperature, "humidity": 60.0})["status"],
                rules.HEALTHY,
            )

    def test_weight_boundary_is_not_critical(self):
        self.assertEqual(
            rules.evaluate_rules({"temperature": 34.0, "humidity": 60.0, "weight": 5.0})["status"],
            rules.HEALTHY,
        )

    def test_warning_when_humidity_above_85(self):
        result = rules.evaluate_rules({"temperature": 34.5, "humidity": 85.1, "weight": 40.0})
        self.assertEqual(result["status"], rules.WARNING)

    def test_warning_when_humidity_below_40(self):
        result = rules.evaluate_rules({"temperature": 34.5, "humidity": 39.9, "weight": 40.0})
        self.assertEqual(result["status"], rules.WARNING)

    def test_critical_takes_priority_over_warning(self):
        result = rules.evaluate_rules({"temperature": 20.0, "humidity": 95.0, "weight": 40.0})
        self.assertEqual(result["status"], rules.CRITICAL)

    def test_missing_sensors_never_trigger_an_alarm(self):
        """Absence of evidence is not evidence of a problem."""
        self.assertEqual(rules.evaluate_rules({"humidity": 60.0})["status"], rules.HEALTHY)
        self.assertEqual(rules.evaluate_rules({"temperature": 34.5})["status"], rules.HEALTHY)

    def test_reasons_are_always_populated(self):
        for reading in (
            {"temperature": 34.5, "humidity": 60.0},
            {"temperature": 20.0},
            {"humidity": 95.0},
        ):
            self.assertTrue(rules.evaluate_rules(reading)["reasons"])


class TestSwarmDetector(unittest.TestCase):

    def test_five_kg_drop_is_flagged(self):
        signal = rules.detect_sudden_weight_loss(37.1, [42.1, 41.0, 39.5, 37.1], 2.0)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["status"], rules.CRITICAL)
        self.assertAlmostEqual(signal["drop_kg"], 5.0, places=1)

    def test_gentle_variation_is_not_flagged(self):
        self.assertIsNone(rules.detect_sudden_weight_loss(42.0, [42.4, 42.2, 42.0], 2.0))

    def test_single_reading_cannot_trigger(self):
        self.assertIsNone(rules.detect_sudden_weight_loss(37.0, [37.0], 2.0))

    def test_missing_weight_is_ignored(self):
        self.assertIsNone(rules.detect_sudden_weight_loss(None, [42.0, 37.0], 2.0))

    def test_combine_returns_most_severe(self):
        self.assertEqual(rules.combine("HEALTHY", "CRITICAL"), "CRITICAL")
        self.assertEqual(rules.combine("HEALTHY", "WARNING"), "WARNING")
        self.assertEqual(rules.combine("HEALTHY", "HEALTHY"), "HEALTHY")


class TestMlFusion(unittest.TestCase):
    """The model may escalate a verdict; it may never soften one."""

    def test_ml_escalates_healthy_but_only_up_to_the_configured_cap(self):
        # Phase 7 (intentional change): the model is trained on SYNTHETIC data
        # only, so by default an ML anomaly raises a WARNING ("risk signal"),
        # not a CRITICAL alarm. It still escalates and still never softens.
        from honeychain import config
        anomalous = Prediction(
            available=True, verdict="CRITICAL", anomaly_score=-0.05, is_anomaly=True,
            band="ANOMALOUS", model_version="test",
        )
        reading = {"temperature": 34.5, "humidity": 60.0, "weight": 42.0, "audio_frequency": 250.0}
        with mock.patch.object(predictor, "predict", return_value=anomalous):
            result = evaluate_health(reading)
        self.assertEqual(result["health_status"], "WARNING")
        self.assertEqual(result["rule_status"], "HEALTHY")
        self.assertIn("ML", result["source"])
        self.assertTrue(result["ml_applied"])
        with mock.patch.object(config, "ML_MAX_ESCALATION", "CRITICAL"), \
             mock.patch.object(predictor, "predict", return_value=anomalous):
            self.assertEqual(evaluate_health(reading)["health_status"], "CRITICAL")

    def test_ml_cannot_downgrade_a_rule_critical(self):
        normal = Prediction(
            available=True, verdict="HEALTHY", anomaly_score=0.2, is_anomaly=False
        )
        with mock.patch.object(predictor, "predict", return_value=normal):
            result = evaluate_health({"temperature": 20.0, "humidity": 60.0, "weight": 42.0})
        self.assertEqual(result["health_status"], "CRITICAL")

    def test_ml_cannot_downgrade_a_rule_warning(self):
        normal = Prediction(
            available=True, verdict="HEALTHY", anomaly_score=0.2, is_anomaly=False
        )
        with mock.patch.object(predictor, "predict", return_value=normal):
            result = evaluate_health({"temperature": 34.5, "humidity": 95.0, "weight": 42.0})
        self.assertEqual(result["health_status"], "WARNING")

    def test_falls_back_to_rules_when_ml_unavailable(self):
        unavailable = Prediction(available=False, error="model not loaded")
        with mock.patch.object(predictor, "predict", return_value=unavailable):
            result = evaluate_health({"temperature": 34.5, "humidity": 60.0, "weight": 42.0})
        self.assertEqual(result["health_status"], "HEALTHY")
        self.assertEqual(result["source"], "RULE")
        self.assertFalse(result["ml_available"])
        self.assertIsNone(result["anomaly_score"])


class TestPredictorResilience(unittest.TestCase):
    """Every artefact failure mode must degrade, never raise."""

    def test_missing_model_file(self):
        instance = HiveHealthPredictor()
        with mock.patch("honeychain.config.MODEL_PATH") as path:
            path.is_file.return_value = False
            path.name = "isolation_forest.joblib"
            self.assertFalse(instance.load())
        self.assertFalse(instance.available)
        self.assertFalse(instance.predict({"temperature": 34.0}).available)

    # Phase 7: the loader verifies metadata, scikit-learn version and artefact
    # hashes BEFORE unpickling, so these build REAL artefacts whose recorded
    # hashes legitimately match -- the failure must then be caught at load.
    def _artefacts(self, model_obj=None, raw_model: bytes | None = None,
                   columns=("temperature", "humidity", "weight", "audio_frequency")):
        import hashlib, importlib.metadata, json, tempfile, joblib
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="hc-ml-"))
        model_path, cols_path, meta_path = tmp / "m.joblib", tmp / "c.joblib", tmp / "meta.json"
        if raw_model is not None:
            model_path.write_bytes(raw_model)
        else:
            joblib.dump(model_obj, model_path)
        joblib.dump(list(columns), cols_path)
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
        meta_path.write_text(json.dumps({
            "schema_version": 2, "model_version": "test", "feature_columns": list(columns),
            "provenance": {"data_source": "SYNTHETIC"},
            "environment": {"scikit_learn": importlib.metadata.version("scikit-learn")},
            "artifact": {"model_sha256": sha(model_path), "feature_columns_sha256": sha(cols_path)},
        }))
        return mock.patch.multiple("honeychain.config", MODEL_PATH=model_path,
                                   FEATURE_COLUMNS_PATH=cols_path, MODEL_METADATA_PATH=meta_path)

    def test_corrupt_model_file(self):
        instance = HiveHealthPredictor()
        with self._artefacts(raw_model=b"\x80\x04truncated-pickle"):
            self.assertFalse(instance.load())
        self.assertIn("could not load model", instance.status()["error"])

    def test_non_model_object_rejected(self):
        instance = HiveHealthPredictor()
        with self._artefacts(model_obj={"not": "a model"}):
            self.assertFalse(instance.load())
        self.assertIn("not a scikit-learn detector", instance.status()["error"])

    def test_feature_schema_mismatch_rejected(self):
        """A model trained on a different feature count must not be used."""
        import numpy as np
        from sklearn.ensemble import IsolationForest
        seven = IsolationForest(n_estimators=5, random_state=0).fit(np.zeros((20, 7)))
        instance = HiveHealthPredictor()
        with self._artefacts(model_obj=seven):
            self.assertFalse(instance.load())
        self.assertIn("mismatch", instance.status()["error"])

    def test_real_model_loads_and_scores(self):
        instance = HiveHealthPredictor()
        if not instance.load():
            self.skipTest("model artefacts not present; run train_model.py first")
        prediction = instance.predict(
            {"temperature": 34.5, "humidity": 62.0, "weight": 42.0, "audio_frequency": 258.0}
        )
        self.assertTrue(prediction.available)
        self.assertFalse(prediction.is_anomaly)
        self.assertIsInstance(prediction.anomaly_score, float)

    def test_real_model_flags_a_swarm_signature(self):
        instance = HiveHealthPredictor()
        if not instance.load():
            self.skipTest("model artefacts not present; run train_model.py first")
        prediction = instance.predict(
            {"temperature": 34.8, "humidity": 67.0, "weight": 37.1, "audio_frequency": 420.0}
        )
        self.assertTrue(prediction.available)
        self.assertTrue(prediction.is_anomaly, "post-swarm signature should be anomalous")


class TestIngestionPipeline(HoneyChainTestCase):

    def test_reading_and_evaluation_are_both_written(self):
        self.post_telemetry(
            {"hive_id": "HIVE-001", "temperature": 34.2, "humidity": 65.0, "weight": 42.5}
        )
        rows = db.latest_telemetry()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["health_status"])
        self.assertIsNotNone(rows[0]["reason"])

    def test_hive_is_auto_registered(self):
        self.post_telemetry({"hive_id": "HIVE-NEW", "temperature": 34.0, "humidity": 60.0})
        self.assertIn("HIVE-NEW", [h["hive_id"] for h in db.list_hives()])

    def test_rollback_leaves_no_partial_row(self):
        """If the health insert fails, the telemetry row must not survive."""
        with mock.patch(
            "honeychain.db.insert_health_evaluation", side_effect=RuntimeError("boom")
        ):
            response = self.post_telemetry(
                {"hive_id": "HIVE-ATOMIC", "temperature": 34.0, "humidity": 60.0}
            )
        self.assertEqual(response.status_code, 500)

        self.assertEqual(db.latest_telemetry(), [])
        count = db.query_one("SELECT COUNT(*) AS n FROM telemetry_readings;")["n"]
        self.assertEqual(count, 0)

    def test_500_response_hides_internal_detail(self):
        with mock.patch(
            "honeychain.db.insert_health_evaluation", side_effect=RuntimeError("secret internals")
        ):
            response = self.post_telemetry(
                {"hive_id": "HIVE-X", "temperature": 34.0, "humidity": 60.0}
            )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret internals", body)
        self.assertNotIn("Traceback", body)

    def test_multi_hive_payload_writes_every_hive(self):
        response = self.post_telemetry(
            {
                "HIVE-001": {"temperature": 34.2, "humidity": 65, "weight": 42.5},
                "HIVE-002": {"temperature": 35.1, "humidity": 71, "weight": 38.1},
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ingested"], 2)
        self.assertEqual({h["hive_id"] for h in db.list_hives()}, {"HIVE-001", "HIVE-002"})

    def test_sudden_weight_loss_escalates_to_critical(self):
        """The HIVE-003 scenario: 5 kg lost while still above the 5 kg floor."""
        for weight in (42.1, 41.8, 42.0):
            self.post_telemetry(
                {
                    "hive_id": "HIVE-003",
                    "temperature": 34.5,
                    "humidity": 65.0,
                    "weight": weight,
                    "audio_frequency": 260.0,
                }
            )
        response = self.post_telemetry(
            {
                "hive_id": "HIVE-003",
                "temperature": 34.8,
                "humidity": 67.0,
                "weight": 37.1,
                "audio_frequency": 420.0,
            }
        )
        record = response.get_json()["records"][0]
        self.assertEqual(record["health_status"], "CRITICAL")
        # The absolute rules alone see nothing wrong -- this is the point of
        # having a rate-of-change layer at all.
        self.assertEqual(record["rule_status"], "HEALTHY")
        self.assertIn("weight loss", record["reason"].lower())

    def test_dashboard_summary_matches_stored_rows(self):
        self.seed_hive("HIVE-001", count=4)
        self.seed_hive("HIVE-002", count=4)
        summary = db.dashboard_summary()
        self.assertEqual(summary["active_hives"], 2)
        self.assertEqual(summary["reading_count"], 8)
        self.assertEqual(sum(summary["status_counts"].values()), 8)
        self.assertIsNotNone(summary["avg_temperature"])

    def test_summary_is_empty_safe(self):
        summary = db.dashboard_summary()
        self.assertEqual(summary["active_hives"], 0)
        self.assertIsNone(summary["avg_temperature"])
        self.assertIsNone(summary["overall_health_percent"])


if __name__ == "__main__":
    unittest.main()
