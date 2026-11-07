#!/usr/bin/env python3
"""
simulate_hardware.py
====================
Simulates a fleet of ESP32 hive nodes posting signed telemetry to the
HoneyChain webhook. Used for demos, end-to-end verification and light load
testing.

Basic run (header HMAC, the default)::

    export HONEYCHAIN_WEBHOOK_SECRET='your-secret'
    python simulate_hardware.py

Query-parameter authentication (Firebase Cloud Function compatibility mode)::

    python simulate_hardware.py --auth-mode query

Trigger the swarming scenario on HIVE-003 twenty seconds in::

    python simulate_hardware.py --anomaly HIVE-003:swarm@20

Design notes
------------
* One thread per hive, bounded by ``--hives``; there is no unbounded thread
  creation and every thread is a daemon so Ctrl-C exits promptly.
* Every HTTP call has an explicit timeout. Connection errors are logged and the
  node keeps running -- a node must survive the API restarting.
* Healthy telemetry is drawn from the same distributions ``train_model.py``
  uses for its normal class, so a healthy simulated node is genuinely healthy
  as far as the detector is concerned rather than being flagged by construction.
* The secret is read from the environment or ``--secret``; nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from honeychain import device_auth  # noqa: E402  (shared signing function)

logger = logging.getLogger("simulator")

DEFAULT_ENDPOINT = "http://localhost:5000/api/firebase/webhook"
DEFAULT_HIVES = ["HIVE-001", "HIVE-002", "HIVE-003", "HIVE-004", "HIVE-005"]
DEFAULT_INTERVAL = 2.0
REQUEST_TIMEOUT = 8.0

_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Anomaly scenarios
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """A time-boxed deviation applied to one node's readings."""

    name: str
    start_after: float          # seconds after start-up
    duration: float             # seconds the transition takes
    persist: bool = True        # keep the end state after the transition


SCENARIO_LIBRARY: dict[str, dict[str, Any]] = {
    # The specified HIVE-003 case: 5 kg lost inside a 10-second window.
    "swarm": {
        "duration": 10.0,
        "weight_delta": -5.0,
        "audio_delta": +170.0,
        "temperature_delta": +0.6,
        "humidity_delta": -4.0,
        "description": "swarm departure: 5 kg lost in 10 s with acoustic excitation",
    },
    "heat_loss": {
        "duration": 60.0,
        "weight_delta": -6.0,
        "audio_delta": -80.0,
        "temperature_delta": -9.0,
        "humidity_delta": +12.0,
        "description": "overwintering heat loss: brood temperature collapses",
    },
    "varroa": {
        "duration": 45.0,
        "weight_delta": -3.0,
        "audio_delta": +135.0,
        "temperature_delta": -1.4,
        "humidity_delta": +4.0,
        "description": "varroa infestation: acoustic signature shifts",
    },
    "chill": {
        "duration": 30.0,
        "weight_delta": 0.0,
        "audio_delta": -60.0,
        "temperature_delta": -6.0,
        "humidity_delta": +8.0,
        "description": "brood chill: temperature drops below the 30 C rule floor",
    },
    "theft": {
        "duration": 15.0,
        "weight_delta": -38.0,
        "audio_delta": -120.0,
        "temperature_delta": -3.0,
        "humidity_delta": 0.0,
        "description": "hive theft: weight falls below the 5 kg absolute floor",
    },
}


def parse_anomaly_spec(spec: str) -> tuple[str, Scenario]:
    """Parse ``HIVE-003:swarm@20`` into ``("HIVE-003", Scenario(...))``."""
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"Invalid anomaly '{spec}'. Expected HIVE-ID:scenario[@delay_seconds]"
        )
    hive_id, remainder = spec.split(":", 1)
    if "@" in remainder:
        name, delay_text = remainder.split("@", 1)
        try:
            delay = float(delay_text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid delay in '{spec}': '{delay_text}' is not a number"
            ) from None
    else:
        name, delay = remainder, 20.0

    name = name.strip().lower()
    if name not in SCENARIO_LIBRARY:
        raise argparse.ArgumentTypeError(
            f"Unknown scenario '{name}'. Choose from: {', '.join(SCENARIO_LIBRARY)}"
        )
    return hive_id.strip(), Scenario(
        name=name, start_after=delay, duration=SCENARIO_LIBRARY[name]["duration"]
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class HiveNode:
    """One simulated ESP32 node with its own baseline and RNG stream."""

    hive_id: str
    endpoint: str
    secret: str
    auth_mode: str
    interval: float
    device_id: str | None = None  # set => per-device signing (Phase 4)
    scenario: Scenario | None = None
    seed: int | None = None

    session: requests.Session = field(default_factory=requests.Session, repr=False)
    _rng: random.Random = field(init=False, repr=False)
    _started_at: float = field(init=False, default=0.0)
    _sent: int = field(init=False, default=0)
    _failed: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed if self.seed is not None else hash(self.hive_id))
        # Per-node baseline drawn from the healthy training distribution.
        self.base_temperature = 34.6 + self._rng.uniform(-0.25, 0.25)
        self.base_humidity = 61.0 + self._rng.uniform(-3.0, 3.0)
        self.base_weight = 41.5 + self._rng.uniform(-1.0, 3.5)
        self.base_audio = 256.0 + self._rng.uniform(-8.0, 8.0)
        self.battery = 100.0 - self._rng.uniform(0.0, 8.0)

    # -- telemetry ---------------------------------------------------------
    def _scenario_progress(self, elapsed: float) -> float:
        """0.0 before the scenario starts, ramping to 1.0 across its duration."""
        if self.scenario is None:
            return 0.0
        offset = elapsed - self.scenario.start_after
        if offset <= 0:
            return 0.0
        if offset >= self.scenario.duration:
            return 1.0 if self.scenario.persist else 0.0
        return offset / self.scenario.duration

    def build_reading(self, elapsed: float) -> dict[str, Any]:
        """Gaussian-noised telemetry with any active scenario applied."""
        phase = (elapsed / 120.0) * 2 * 3.14159265
        day = __import__("math").sin(phase)  # slow synthetic day/night cycle

        temperature = self.base_temperature + 0.45 * day + self._rng.gauss(0, 0.35)
        humidity = self.base_humidity - 6.0 * day + self._rng.gauss(0, 2.8)
        weight = self.base_weight + 0.5 * day + self._rng.gauss(0, 0.35)
        audio = self.base_audio + 18.0 * max(day, 0.0) + self._rng.gauss(0, 9.0)

        progress = self._scenario_progress(elapsed)
        if progress > 0 and self.scenario is not None:
            deltas = SCENARIO_LIBRARY[self.scenario.name]
            temperature += deltas["temperature_delta"] * progress
            humidity += deltas["humidity_delta"] * progress
            weight += deltas["weight_delta"] * progress
            audio += deltas["audio_delta"] * progress

        # Battery drains slowly and monotonically, as a real node's would.
        self.battery = max(5.0, self.battery - self._rng.uniform(0.0, 0.02))

        return {
            "hive_id": self.hive_id,
            "temperature": round(_clamp(temperature, -20, 60), 2),
            "humidity": round(_clamp(humidity, 0, 100), 2),
            "weight": round(_clamp(weight, 0, 200), 2),
            "audio_frequency": round(_clamp(audio, 20, 2000), 1),
            "battery_level": round(self.battery, 1),
        }

    # -- transport ---------------------------------------------------------
    def _post(self, reading: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(reading, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        url = self.endpoint

        if self.auth_mode == "device":
            # Phase 4 per-device scheme -- the exact signing function the
            # server verifies with (honeychain.device_auth.sign_request).
            ts = str(int(time.time()))
            nonce = secrets.token_hex(16)
            headers[device_auth.DEVICE_ID_HEADER] = self.device_id
            headers[device_auth.TIMESTAMP_HEADER] = ts
            headers[device_auth.NONCE_HEADER] = nonce
            headers[device_auth.SIGNATURE_HEADER] = device_auth.sign_request(
                self.device_id, self.secret, ts, nonce, body
            )
        elif self.auth_mode == "header":
            headers["X-HoneyChain-Signature"] = hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        else:  # query mode -- Firebase webhook compatibility
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}auth={requests.utils.quote(self.secret, safe='')}"

        try:
            response = self.session.post(
                url, data=body, headers=headers, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.RequestException as exc:
            self._failed += 1
            logger.warning("%s transport error: %s", self.hive_id, type(exc).__name__)
            return None

        if response.status_code != 200:
            self._failed += 1
            detail = response.text[:160].replace("\n", " ")
            logger.error("%s -> HTTP %s %s", self.hive_id, response.status_code, detail)
            return None

        self._sent += 1
        try:
            return response.json()
        except ValueError:
            return None

    # -- loop --------------------------------------------------------------
    def run(self) -> None:
        self._started_at = time.monotonic()
        # Stagger node start-ups so five nodes do not fire simultaneously.
        time.sleep(self._rng.uniform(0, self.interval))

        while not _shutdown.is_set():
            elapsed = time.monotonic() - self._started_at
            reading = self.build_reading(elapsed)
            result = self._post(reading)

            status = "?"
            if result and result.get("records"):
                status = result["records"][0].get("health_status") or "?"
            logger.info(
                "%s -> %.1f\u00b0C | %.0f%% | %.1fkg | %.0fHz | %.0f%% batt | %s",
                self.hive_id,
                reading["temperature"],
                reading["humidity"],
                reading["weight"],
                reading["audio_frequency"],
                reading["battery_level"],
                status,
            )
            _shutdown.wait(self.interval)

    def stats(self) -> dict[str, Any]:
        return {"hive_id": self.hive_id, "sent": self._sent, "failed": self._failed}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate ESP32 hive nodes posting signed telemetry to HoneyChain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available anomaly scenarios:\n"
        + "\n".join(
            f"  {name:<10} {spec['description']}" for name, spec in SCENARIO_LIBRARY.items()
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("HONEYCHAIN_WEBHOOK_URL", DEFAULT_ENDPOINT),
        help=f"webhook URL (default {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("HONEYCHAIN_WEBHOOK_SECRET"),
        help="shared secret; defaults to $HONEYCHAIN_WEBHOOK_SECRET",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("header", "query"),
        default=os.environ.get("HONEYCHAIN_AUTH_MODE", "header"),
        help="header = X-HoneyChain-Signature HMAC (default); query = ?auth=SECRET",
    )
    parser.add_argument(
        "--device",
        action="append",
        default=[],
        metavar="HIVE_ID:DEVICE_ID:DEVICE_SECRET",
        help="per-device mode (Phase 4, recommended): one per registered device; "
        "posts to /api/devices/telemetry with per-device signatures. "
        "Overrides --hives/--secret/--auth-mode.",
    )
    parser.add_argument(
        "--hives",
        nargs="*",
        default=DEFAULT_HIVES,
        help=f"hive ids to simulate (default: {' '.join(DEFAULT_HIVES)})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"seconds between readings per node (default {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--anomaly",
        action="append",
        default=[],
        metavar="HIVE:SCENARIO[@DELAY]",
        help="schedule an anomaly, e.g. HIVE-003:swarm@20 (repeatable)",
    )
    parser.add_argument(
        "--default-swarm",
        action="store_true",
        help="shorthand for --anomaly HIVE-003:swarm@20",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="stop after N seconds (0 = run until Ctrl-C)",
    )
    parser.add_argument("--seed", type=int, default=None, help="seed for reproducible runs")
    parser.add_argument("--quiet", action="store_true", help="only log warnings and errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    device_specs: list[tuple[str, str, str]] = []
    for spec in args.device:
        parts = spec.split(":", 2)
        if len(parts) != 3 or not all(parts):
            logger.error("--device must be HIVE_ID:DEVICE_ID:DEVICE_SECRET (got %r)", spec[:40])
            return 2
        device_specs.append((parts[0], parts[1], parts[2]))
    if device_specs:
        base = args.endpoint.split("/api/")[0].rstrip("/")
        args.endpoint = f"{base}/api/devices/telemetry"
        args.hives = [hive for hive, _, _ in device_specs]
        args.auth_mode = "device"
        args.secret = "per-device"  # placeholder; real secrets are per node below

    if not args.secret:
        logger.error(
            "No webhook secret. Set HONEYCHAIN_WEBHOOK_SECRET or pass --secret. "
            "Every request would otherwise be rejected with 401."
        )
        return 2

    if len(args.hives) > 100:
        logger.error("Refusing to start more than 100 nodes (bounded concurrency).")
        return 2

    scenarios: dict[str, Scenario] = {}
    specs = list(args.anomaly)
    if args.default_swarm:
        specs.append("HIVE-003:swarm@20")
    for spec in specs:
        hive_id, scenario = parse_anomaly_spec(spec)
        scenarios[hive_id] = scenario
        logger.info(
            "Scheduled '%s' on %s at t+%.0fs (%s)",
            scenario.name,
            hive_id,
            scenario.start_after,
            SCENARIO_LIBRARY[scenario.name]["description"],
        )

    nodes = [
        HiveNode(
            hive_id=hive_id,
            endpoint=args.endpoint,
            secret=device_specs[index][2] if device_specs else args.secret,
            auth_mode=args.auth_mode,
            device_id=device_specs[index][1] if device_specs else None,
            interval=args.interval,
            scenario=scenarios.get(hive_id),
            seed=None if args.seed is None else args.seed + index,
        )
        for index, hive_id in enumerate(args.hives)
    ]

    def handle_signal(_signum, _frame):
        logger.info("Shutting down simulator...")
        _shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info(
        "Starting %d node(s) -> %s (auth: %s, interval: %.1fs)",
        len(nodes),
        args.endpoint,
        args.auth_mode,
        args.interval,
    )

    threads = [
        threading.Thread(target=node.run, name=f"node-{node.hive_id}", daemon=True)
        for node in nodes
    ]
    for thread in threads:
        thread.start()

    try:
        if args.duration > 0:
            _shutdown.wait(args.duration)
            _shutdown.set()
        else:
            while any(thread.is_alive() for thread in threads):
                time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown.set()

    for thread in threads:
        thread.join(timeout=REQUEST_TIMEOUT + 1)

    total_sent = sum(node.stats()["sent"] for node in nodes)
    total_failed = sum(node.stats()["failed"] for node in nodes)
    logger.info("Simulator stopped. Accepted: %d, failed: %d", total_sent, total_failed)
    return 0 if total_sent else 1


if __name__ == "__main__":
    sys.exit(main())
