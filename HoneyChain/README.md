# HoneyChain

Honey traceability and smart beekeeping for **SIH 2026, Problem Statement 26021 (Team APISENSE)**.
It follows honey from the hive to the consumer: authenticated hive telemetry, a hive-health
assessment, harvest, processing and blending, laboratory testing, packaging into uniquely
identified jars, custody hand-offs, shipment, retail, recall, and public verification of each
jar's QR code. Records are tamper-evident and can be anchored to an Ethereum-compatible
blockchain.

This README describes only what is implemented. Limitations are listed at the end.

## Repository layout

```
HoneyChain/
├── app.py                  WSGI entry point (python app.py)
├── honeychain/             Flask JSON API + services (auth, IoT, ML, ledger, supply chain,
│                           blockchain client, consumer verification, authority views)
├── frontend/               React 18 + Vite + TypeScript console (served at /app/)
├── templates/, static/     Original operational dashboard (/) and QR fallback page (/q)
├── blockchain/             Hardhat project: HoneyChainRegistry.sol, deploy script, tests
├── database/schema.sql     SQLite schema (column migrations live in honeychain/db.py)
├── models/                 Trained anomaly model + metadata (provenance, hashes)
├── data/                   Datasets + DATASETS.json manifest (REAL vs SYNTHETIC)
├── train_model.py          Deterministic model training (synthetic data)
├── inference.py            Standalone offline scoring CLI
├── simulate_hardware.py    Device simulator (per-device signed requests)
├── scripts/                create_admin_user.py, seed_demo.py
├── tests/                  Backend, security, migration, QR, blockchain and E2E tests
├── research/               Offline ML study on the real single-hive capture (not runtime)
└── docs/                   Architecture, API, security, database, deployment, phase docs
```

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # set HONEYCHAIN_AUTH_SECRET and HONEYCHAIN_DEVICE_MASTER_KEY at least
export $(grep -v '^#' .env | xargs)
python scripts/seed_demo.py       # optional: full DEMO dataset through the real API
python app.py                     # http://localhost:5000
(cd frontend && npm install && npm run build)   # console at /app/, consumer page /app/verify
```

More detail: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## What is implemented

| Area | Summary | Docs |
|---|---|---|
| Auth and RBAC | Bearer tokens, 9 roles, organization isolation, logout revocation, throttling, audit log, ADMIN organization management | `docs/SECURITY.md` |
| IoT | Per-device credentials, signed requests, replay protection, hive binding, device time preserved, silence alerts, sensor data-quality flags | `docs/IOT_PROTOCOL.md` |
| Hive health | Deterministic rules (authoritative) plus an IsolationForest risk signal, capped at WARNING, with graded bands | `docs/ML.md` |
| Harvest | Atomic hive -> harvest -> batch provenance on the hash-linked ledger | `docs/HARVEST_WORKFLOW.md` |
| Supply chain | Processing, blending, lab requests and results, packaging, two-party custody, shipments, retail, recall | `docs/SUPPLY_CHAIN.md` |
| Blockchain | One hash-linked event log, Merkle checkpoints, one client; contract extended additively | `docs/BLOCKCHAIN.md` |
| Consumer | Per-unit cryptographic QR, public verification page (`/app/verify`), recall and suspicious-activity warnings | `docs/CONSUMER_VERIFICATION.md` |
| Oversight | AUTHORITY dashboard: production, compliance, hotspots, scan signals, recalls | `docs/AUTHORITY.md` |
| API | 75 routes with access levels | `docs/API.md` |

## Data honesty

* The anomaly model is trained **only on synthetic data**. Its figures are synthetic
  evaluations, **not real-world accuracy**. No labelled real hive data exists yet.
* `data/raw_data.txt` is a real ESP32 capture from **one** hive (temperature and humidity
  only). Everything else in `data/` is either derived from it or synthetic; see
  `data/DATASETS.json`.
* Simulated devices are registered as `SIMULATED`, and their readings are labelled that way.
  Demo data is named `DEMO ...`, and the demo lab certificate is
  `DEMO-NOT-A-REAL-CERTIFICATE`.
* Blockchain anchoring is verified against a **local Hardhat chain** in the test suite, not
  a public network.

## Testing

```bash
python tests/run_tests.py                              # backend (includes live local chain)
(cd frontend && npm run typecheck && npm test && npm run build)
(cd blockchain && npx hardhat test)
```

## Limitations

* The React console covers sign-in, the authority view and consumer verification. Other
  operational screens are in the original dashboard (`/`), which refreshes by
  authenticated polling.
* SQLite only (no PostgreSQL); single-process rate limits and SSE; no Docker or CI.
* ML real-world performance is unknown; devices without audio always run rules-only.
* Custody moves whole batches; a recall cannot yet be lifted; regions come from the
  free-text hive location.
* Physical-hardware operation is not verified by the automated tests. They exercise the
  device API path with signed software requests.

## Configuration reference

All configuration comes from environment variables (copy `.env.example` to `.env`; never commit
`.env`). Problems with required keys are logged at startup as `CONFIGURATION:` errors and reported
as `configuration_ok` in `GET /api/health`.

| Variable | Required | Used in | Purpose |
|---|---|---|---|
| `HONEYCHAIN_AUTH_SECRET` | **yes** | `honeychain/auth.py` | Signs login bearer tokens. Unset -> every authenticated request is 401. |
| `HONEYCHAIN_DEVICE_MASTER_KEY` | **yes** (for devices) | `honeychain/device_auth.py` | Derives per-device secrets. Unset -> device registration 503, telemetry 401. |
| `HONEYCHAIN_PUBLIC_BASE_URL` | **yes** in production | `honeychain/unit_qr.py`, `config.py` | Base URL embedded in printed product QR codes (`<base>/q#...`). |
| `HONEYCHAIN_DB_PATH` | no | `honeychain/db.py` | SQLite file (default `instance/honeychain.db`). |
| `HONEYCHAIN_AUTH_TOKEN_TTL` | no | `honeychain/auth.py` | Token lifetime in seconds. |
| `HONEYCHAIN_CHAIN_ANCHOR` | no | `honeychain/chain_anchor.py` | `true` enables on-chain checkpoint anchoring. |
| `RPC_URL`, `CHAIN_ID`, `CONTRACT_ADDRESS`, `PRIVATE_KEY` | only if anchoring | `honeychain/chain_anchor.py` | Blockchain endpoint, network id (checked), registry address, signer key. |
| `HONEYCHAIN_CHECKPOINT_MAX_ATTEMPTS`, `HONEYCHAIN_CHAIN_TX_TIMEOUT` | no | `honeychain/chain_events.py`, `chain_anchor.py` | Anchoring retry budget / receipt timeout. |
| `HONEYCHAIN_LEGACY_WEBHOOK_ENABLED`, `HONEYCHAIN_WEBHOOK_SECRET` | no (off) | `honeychain/app.py`, `security.py` | Legacy shared-secret device webhook (disabled by default). |
| `HONEYCHAIN_DEVICE_*` (skew, backfill, interval, silence) | no | `honeychain/device_auth.py`, `devices.py` | Replay window, device clock limits, silence alerting. |
| `HONEYCHAIN_RULE_*` | no | `honeychain/rules.py` (validated in `config.py`) | Safety-rule threshold overrides; invalid values stop startup. |
| `HONEYCHAIN_ML_MAX_ESCALATION` | no | `honeychain/pipeline.py` | Highest status ML may raise (`WARNING` default). |
| `HONEYCHAIN_SENSOR_*` | no | `honeychain/sensor_faults.py` | Data-quality heuristics. |
| `HONEYCHAIN_SIGNAL_*` | no | `honeychain/authority.py`, `consumer.py` | Suspicious-scan thresholds. |
| `HONEYCHAIN_PUBLIC_VERIFY_PER_MINUTE`, `HONEYCHAIN_LOGIN_PER_MINUTE_PER_ADDRESS` | no | `honeychain/ratelimit.py` users | Per-address rate limits. |
| `FIREBASE_*` | no | `honeychain/firebase_bridge.py` | Optional Firebase bridge; skipped when unset. |
| `HONEYCHAIN_DEBUG`, `HONEYCHAIN_LOG_LEVEL`, `HONEYCHAIN_SWARM_WINDOW_SECONDS` | no | `config.py`, `rules.py` | Debug/logging; swarm-detection window. |

Blockchain deployment (Hardhat, `blockchain/.env.example`): `SEPOLIA_RPC_URL`, `PRIVATE_KEY`,
`ETHERSCAN_API_KEY`, `REPORT_GAS` -- used only by `blockchain/hardhat.config.js` when deploying.

**CORS:** none is configured, by design. The React app is served same-origin by Flask at `/app/`
(build with `cd frontend && npm install && npm run build`), and `npm run dev` proxies `/api` to Flask.
