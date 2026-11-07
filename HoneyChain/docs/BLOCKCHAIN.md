# HoneyChain Blockchain Architecture (Phase 6)

## What is on-chain, and what is not

| Data | Where |
|---|---|
| Harvest details, GPS, notes, quantities, names, telemetry | **Off-chain only** (SQLite) |
| Every traceability event (ids + hashes) | Off-chain hash-linked log `chain_events` |
| Merkle **root** over a range of event hashes | **On-chain** (`HoneyChainRegistry.anchorCheckpoint`) |

One transaction anchors any number of events. No personal data ever reaches the chain,
and a root reveals nothing about the events under it.

## Components

* `honeychain/chain_events.py` — **the** chain-event service. Supply-chain modules call
  `record_event()` inside their own database transaction; nothing else writes events or
  talks to the chain. Event types: `BATCH_CREATED`, `PROCESSING_RECORDED`,
  `LAB_RESULT_RECORDED`, `PACKAGED`, `CUSTODY_TRANSFERRED`, `SHIPPED`, `RECEIVED`,
  `RETAIL_REGISTERED`, `RECALLED` (Phase 5 emits `BATCH_CREATED`; Phase 8 emits the rest).
* `honeychain/merkle.py` — tree + inclusion proofs.
* `honeychain/chain_anchor.py` — **the single blockchain client**.
* `blockchain/contracts/HoneyChainRegistry.sol` — pre-existing contract, extended
  additively with `anchorCheckpoint`, `getCheckpoint`, `verifyCheckpoint`,
  `checkpointExists`, `checkpointCount`. Every pre-existing function is byte-identical.
  `createBatch` remains for compatibility but the backend no longer calls it.

## Event hashing

```
payload_hash = sha256(canonical_json(payload))            # ids and hashes only
event_hash   = sha256(canonical_json({seq, event_id, event_type, resource_type,
                 resource_id, organization_id, actor_id, occurred_at,
                 payload_hash, previous_hash}))
```

`canonical_json` is the same serialiser the ledger uses: one hashing scheme. `seq` is
contiguous from 1; `previous_hash` links each event to the one before it. Recording the
same `(event_type, resource_type, resource_id)` twice returns the original event.

## Merkle construction (re-implementable verifier)

```
leaf = SHA-256(0x00 || event_hash_bytes)
node = SHA-256(0x01 || left || right)
odd level -> last node is promoted unchanged (never duplicated)
```

Domain separation follows RFC 6962. Promotion avoids the duplicate-leaf ambiguity of
CVE-2012-2459. A known-answer test pins the construction: changing it would make roots
already anchored on-chain unverifiable.

## Operating it

```
POST /api/chain/checkpoints          (ADMIN)  freeze un-checkpointed events -> PENDING
POST /api/chain/checkpoints/submit   (ADMIN)  anchor PENDING/retryable FAILED, oldest first
GET  /api/chain/checkpoints          (ADMIN, AUTHORITY)
GET  /api/chain/log/verify           (ADMIN, AUTHORITY) full log + domain cross-check
GET  /api/chain/events[/<id>]        (organization-scoped; detail includes Merkle proof)
GET  /api/chain/checkpoints/<id>/verify   PUBLIC: hashes + tx reference only
```

Checkpoint states: `PENDING` -> `SUBMITTED` -> `CONFIRMED`, or `FAILED` (with
`last_error`, retried up to `HONEYCHAIN_CHECKPOINT_MAX_ATTEMPTS`). A checkpoint becomes
`CONFIRMED` **only** after a mined receipt with status 1, or after reading back an
identical root already on chain. An on-chain root that differs from the local one is
flagged, never overwritten. Checkpoints anchor strictly in order; submission stops at
the first failure.

Configuration: `HONEYCHAIN_CHAIN_ANCHOR=true`, `RPC_URL`, `CHAIN_ID` (checked against
the RPC before anything is sent), `CONTRACT_ADDRESS` (must contain code), `PRIVATE_KEY`
(environment only; never logged or returned). The signer must be `authorized` on the
contract.

## Verification status in this repository

* Contract compiled with the official solc **0.8.24** build (`commit e11b9ed9`). It is
  obtained from the `solc` npm package when `binaries.soliditylang.org` is unreachable;
  see the resolver in `blockchain/hardhat.config.js`.
* Hardhat: 19 pre-existing + 7 checkpoint tests.
* `tests/test_phase6_live_chain.py` starts a **local Hardhat node (chain 31337)**, deploys
  the contract and drives the production client. Every success is re-checked on-chain
  (receipt status, `verifyCheckpoint`). It proves the integration code end-to-end on a
  **local development chain only**. It says nothing about public-network availability,
  gas costs or funding, which remain untested here.

## Known limitations

* Attribution is authenticated (`actor_id` comes from the verified session and is
  covered by the event hash). Per-organization asymmetric signatures are **not**
  implemented; they require per-organization key custody.
* Checkpoint creation/submission is triggered via the admin API (for example from cron),
  not by an in-process scheduler.
* Batches created before Phase 6 have no chain events and are not back-filled.
* Contract ownership is a single key (no multisig/timelock, no pause).
