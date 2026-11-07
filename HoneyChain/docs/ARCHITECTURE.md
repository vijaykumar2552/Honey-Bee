# Architecture

```
ESP32 / simulator --(per-device HMAC)--> Flask JSON API (honeychain/) --> SQLite (WAL)
                                            |   |                           |
React console (frontend/, /app/) ---------- +   +-- chain_events (hash-linked log)
Original dashboard (templates/, static/, /)          |
Public QR page (/app/verify, /q) -- POST /api/public/verify
                                                     +-- Merkle checkpoint --> chain_anchor.py
                                                             --> HoneyChainRegistry (blockchain/)
```

| Layer | Location | Notes |
|---|---|---|
| API / services | `honeychain/` (Python package) | One Flask app (`create_app`), blueprints for the supply chain and authority views |
| Entry point | `app.py` | WSGI; `python app.py` for local dev |
| Auth / RBAC | `honeychain/auth.py` | Bearer tokens (itsdangerous), 9 roles, organization isolation, revocation on logout |
| IoT | `device_auth.py`, `devices.py`, `sensor_faults.py` | Per-device derived secrets, replay protection, silence detection, data-quality flags |
| Intelligence | `rules.py` (authoritative), `ml.py`, `pipeline.py` | IsolationForest trained on synthetic data; capped at WARNING; verified before unpickling |
| Ledger | `ledger.py` | Hash-linked batch blocks; `canonical_json` is the single hashing serializer |
| Supply chain | `harvests.py`, `supply_chain.py`, `supply_routes.py` | Harvest, processing, blending, lab, packaging, custody, shipment, retail, recall |
| Blockchain | `chain_events.py`, `merkle.py`, `chain_anchor.py` | One event service, one client; only Merkle roots go on-chain |
| Consumer | `unit_qr.py`, `consumer.py` | Per-unit HMAC QR; public allow-listed provenance |
| Oversight | `authority.py` | Read-only AUTHORITY/ADMIN views and scan signals; serves `/app/` |
| Frontend | `frontend/` | React 18 + Vite + TypeScript (sign-in, authority dashboard, consumer verification) |
| Contract | `blockchain/` | Hardhat project, `HoneyChainRegistry.sol` |
| Schema | `database/schema.sql` + `_MIGRATIONS` in `honeychain/db.py` | Idempotent; upgrades existing databases in place |
| ML training | `train_model.py`, `inference.py`, `models/`, `data/` | Deterministic, reproducible artefact; dataset manifest `data/DATASETS.json` |
| Research | `research/` | Offline study on the real single-hive capture; not used at runtime |

Why the layout is what it is: the `honeychain` package is imported by name from the
service, scripts and tests, so it is kept intact rather than split into
`backend/routes|services|models` folders. Its modules already separate those concerns.
Tests share fixtures by importing each other (`tests.test_phase8_supply_chain` and others),
so they stay in one flat `tests/` package. Their file names group them by area
(`test_phase4_iot_security`, `test_phase9_consumer`, `test_*_schema_migration`, and so on).
