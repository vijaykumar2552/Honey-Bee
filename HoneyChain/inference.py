#!/usr/bin/env python3
"""
inference.py
============
Standalone inference entry point: raw input in, structured prediction out.

The Flask service scores telemetry inline via `honeychain.pipeline`, which is
the right design for a live API -- spawning a subprocess per reading would be
far slower than an in-process call against an already-loaded model. This module
exists for the cases where a *separate* entry point is genuinely useful:

* batch scoring a CSV or JSONL file offline
* a smoke test that the saved artefact still loads and behaves
* being called from another language or a job scheduler
* CI checks on a newly trained model before it is promoted

It shares `honeychain.pipeline.evaluate_health` with the API, so a reading
scored here gets byte-identical output to the same reading posted to the
webhook. Duplicating the scoring logic in a second script is exactly how the
two silently drift apart.

Usage
-----
    # single reading
    python inference.py --json '{"hive_id":"HIVE-001","temperature":34.5,
                                 "humidity":62,"weight":42.0,"audio_frequency":258}'

    # explicit fields
    python inference.py --hive-id HIVE-001 --temperature 34.5 --humidity 62 \
                        --weight 42.0 --audio-frequency 258

    # batch: a JSONL file, or a CSV with a header row
    python inference.py --input readings.jsonl --output scored.jsonl
    python inference.py --input readings.csv --format csv

    # stdin
    echo '{"hive_id":"H1","temperature":25.0}' | python inference.py --stdin

Exit codes: 0 success, 1 input/validation error, 2 model unavailable
(with `--require-model`).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from honeychain import validation  # noqa: E402
from honeychain.ml import predictor  # noqa: E402
from honeychain.pipeline import evaluate_health  # noqa: E402

logger = logging.getLogger("inference")


def predict(raw: dict[str, Any]) -> dict[str, Any]:
    """Score one raw reading and return a structured result.

    This is the function to import if you are embedding HoneyChain scoring in
    another Python program::

        from inference import predict
        result = predict({"hive_id": "HIVE-001", "temperature": 34.5, ...})

    Parameters
    ----------
    raw
        A reading in any shape `honeychain.validation` accepts: canonical field
        names, or aliases such as ``temp`` / ``hum`` / ``wt`` / ``audio``.

    Returns
    -------
    dict
        ``{"ok": True, "hive_id": ..., "input": {...}, "prediction": {...}}`` on
        success, or ``{"ok": False, "error": ..., "details": [...]}`` when the
        input fails validation. Never raises for bad input -- callers get a
        result object either way, which is what a batch loop needs.

    Notes
    -----
    Stateless with respect to history: the sudden-weight-loss detector needs a
    hive's recent readings from the database and cannot run here, so a swarm
    that is only visible as a *rate* of change will not be flagged by this
    path. Absolute rule breaches and model anomalies are both detected. Post to
    `/api/firebase/webhook` if you need the rate detector too.
    """
    try:
        candidates = validation.normalise_payload(raw)
        reading = validation.validate_reading(candidates[0])
    except validation.ValidationError as exc:
        return {"ok": False, "error": exc.message, "details": exc.errors}
    except (IndexError, TypeError) as exc:
        return {"ok": False, "error": f"Unreadable input: {type(exc).__name__}", "details": []}

    evaluation = evaluate_health(reading, swarm_signal=None)

    return {
        "ok": True,
        "hive_id": reading["hive_id"],
        "input": {k: v for k, v in reading.items() if k != "hive_id"},
        "prediction": {
            "health_status": evaluation["health_status"],
            "rule_status": evaluation["rule_status"],
            "ml_verdict": evaluation["prediction"],
            "anomaly_score": evaluation["anomaly_score"],
            "source": evaluation["source"],
            "ml_available": evaluation["ml_available"],
            "reason": evaluation["reason"],
        },
    }


def predict_many(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score a list of readings. One bad row does not abort the batch."""
    return [predict(reading) for reading in readings]


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _read_jsonl(handle) -> Iterator[dict[str, Any]]:
    for line_number, line in enumerate(handle, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping line %d: malformed JSON (%s)", line_number, exc.msg)


def _read_csv(handle) -> Iterator[dict[str, Any]]:
    reader = csv.DictReader(handle)
    if reader.fieldnames is None:
        logger.error("CSV has no header row")
        return
    for row in reader:
        # Blank cells become None rather than "", so validation treats them as
        # absent instead of rejecting them as an empty value.
        yield {key: (value if value not in ("", None) else None) for key, value in row.items()}


def _load_input(path: Path, fmt: str) -> list[dict[str, Any]]:
    if fmt == "auto":
        fmt = "csv" if path.suffix.lower() == ".csv" else "jsonl"
    with path.open("r", encoding="utf-8", newline="") as handle:
        if fmt == "csv":
            return list(_read_csv(handle))
        first = handle.readline()
        handle.seek(0)
        # A file starting with '[' is a JSON array, not JSON Lines.
        if first.lstrip().startswith("["):
            payload = json.load(handle)
            return payload if isinstance(payload, list) else [payload]
        return list(_read_jsonl(handle))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HoneyChain standalone inference",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="a single reading as a JSON string")
    source.add_argument("--input", type=Path, help="JSONL, JSON array or CSV file")
    source.add_argument("--stdin", action="store_true", help="read JSON/JSONL from stdin")
    source.add_argument("--hive-id", help="build a reading from individual flags")

    parser.add_argument("--temperature", type=float)
    parser.add_argument("--humidity", type=float)
    parser.add_argument("--weight", type=float)
    parser.add_argument("--audio-frequency", type=float)
    parser.add_argument("--battery-level", type=float)

    parser.add_argument("--format", choices=("auto", "jsonl", "csv"), default="auto")
    parser.add_argument("--output", type=Path, help="write JSONL results here (default: stdout)")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    parser.add_argument(
        "--require-model",
        action="store_true",
        help="exit 2 if the ML artefact is unavailable instead of using rules only",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    model_loaded = predictor.load()
    if not model_loaded:
        message = predictor.status().get("error") or "model unavailable"
        if args.require_model:
            logger.error("Model required but unavailable (%s). Run train_model.py.", message)
            return 2
        logger.warning("Model unavailable (%s); scoring with the rule engine only.", message)

    # ---- gather input ----
    try:
        if args.json:
            readings = [json.loads(args.json)]
        elif args.stdin:
            text = sys.stdin.read().strip()
            if not text:
                logger.error("No input received on stdin")
                return 1
            readings = (
                json.loads(text)
                if text.lstrip().startswith("[")
                else [json.loads(line) for line in text.splitlines() if line.strip()]
            )
            if isinstance(readings, dict):
                readings = [readings]
        elif args.input:
            if not args.input.is_file():
                logger.error("Input file not found: %s", args.input)
                return 1
            readings = _load_input(args.input, args.format)
        else:
            readings = [{
                "hive_id": args.hive_id,
                "temperature": args.temperature,
                "humidity": args.humidity,
                "weight": args.weight,
                "audio_frequency": args.audio_frequency,
                "battery_level": args.battery_level,
            }]
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON input: %s", exc.msg)
        return 1
    except OSError as exc:
        logger.error("Could not read input: %s", exc)
        return 1

    if not readings:
        logger.error("No readings to score")
        return 1

    results = predict_many(readings)

    # ---- emit ----
    if args.output:
        try:
            with args.output.open("w", encoding="utf-8") as handle:
                for result in results:
                    handle.write(json.dumps(result) + "\n")
        except OSError as exc:
            logger.error("Could not write output: %s", exc)
            return 1
        logger.info("Wrote %d result(s) to %s", len(results), args.output)
    else:
        indent = 2 if args.pretty else None
        payload = results[0] if len(results) == 1 else results
        print(json.dumps(payload, indent=indent))

    failed = sum(1 for r in results if not r["ok"])
    if failed:
        logger.warning("%d of %d reading(s) failed validation", failed, len(results))

    if not args.quiet and len(results) > 1:
        counts: dict[str, int] = {}
        for result in results:
            if result["ok"]:
                status = result["prediction"]["health_status"]
                counts[status] = counts.get(status, 0) + 1
        logger.info("Scored %d reading(s): %s", len(results) - failed, counts or "none")

    return 1 if failed == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
