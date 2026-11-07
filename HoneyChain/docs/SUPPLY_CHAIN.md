# Honey Supply Chain (Phase 8)

```
Hive -> Harvest -> Batch -> Processing / Blending -> Lab -> Packaging -> Product Units
     -> Custody transfer (sender initiates, receiver accepts/rejects) -> Shipment
     -> Retail receipt -> On shelf -> Sold          (Recall can stop any point)
```

## Custody model

`honey_batches` is the append-only ledger and is never updated. `batch_state` holds what
legitimately changes: current **holder**, **origin** organization, **remaining_kg**, and
status `ACTIVE | CONSUMED | RECALLED`. Batches from before Phase 8 get a row lazily on
first use (origin = holder = the hive's owner, remaining = ledger weight). Every state
change is also an immutable chain event.

**Only the current holder may act on a batch** (process, blend, request a lab test,
package, transfer, ship). Reads are allowed to organizations that participate in that
batch's chain (origin, current/previous holders, the requested lab, the receiving
retailer). ADMIN/AUTHORITY see everything. Everyone else gets 403.

**No operations during a hand-off.** Once the holder offers a batch in a custody
transfer, it may not process, blend, package or re-offer that batch (409) until the
transfer is accepted, rejected or cancelled. Otherwise the receiver would accept a batch
whose contents no longer match what was offered. Requesting a lab test (sampling) stays
allowed.

## Endpoints

| Step | Endpoint | Actor |
|---|---|---|
| Processing | `POST /api/batches/<id>/processing` (FILTERING, HEATING, SETTLING, STORAGE; `output_kg <= input_kg`) | PROCESSOR holder |
| Blending | `POST /api/blends` - new batch; every input and its kg kept in `batch_blend_inputs` | PROCESSOR holding every input |
| Lab request | `POST /api/batches/<id>/lab-requests` names ONE lab organization | holder |
| Lab result | `POST /api/lab/results/<id>` - only that lab, once; immutable afterwards | LAB |
| Lab status | `GET /api/batches/<id>/lab-results` - `NOT_TESTED / PENDING / PASSED / FAILED / REVIEW` | participants |
| Packaging | `POST /api/batches/<id>/packaging` - blocked for FAILED lab or RECALLED batch | PROCESSOR holder |
| Custody | `POST /api/custody/transfers`, `/<id>/accept`, `/<id>/reject`, `/<id>/cancel` | sender / receiver |
| Shipment | `POST /api/shipments`, `POST /api/shipments/<id>/status` (`CREATED -> IN_TRANSIT -> DELIVERED`, `EXCEPTION`) | sender |
| Retail | `POST /api/retail/<id>/receive`, `/shelve`, `POST /api/retail/units/<unit>/sell` | RETAILER |
| Recall | `POST /api/recalls` - propagates to blends made from the batch, its units and retail records | AUTHORITY, ADMIN |
| Trace | `GET /api/units/<unit>/trace`, `GET /api/batches/<id>/trace` | participants |

A transfer that has a shipment can only be accepted once the shipment is `DELIVERED`.
The database allows one pending transfer per batch, one shipment per transfer and one
retail record per shipment (partial unique indexes).

## Integrity

* **Lab results.** Nothing is fabricated: a batch with no result reads `NOT_TESTED`, never
  "passed". Results carry a `result_hash` and are anchored as `LAB_RESULT_RECORDED`.
  Editing a stored result is detected by `GET /api/chain/log/verify`. A result may not
  be dated before its sample was requested (no backdating).
* **Product units.** Each unit gets an ID with 80 random bits (not sequential) and a
  192-bit secret. The secret is **returned once** in the packaging response for label
  printing, and only its SHA-256 is stored (Phase 9 builds QR verification on it). The set
  of units is committed by `units_digest` in the `PACKAGED` event. Deleting or injecting a
  unit is detected.
* **Events.** Events are recorded through the single Phase 6 service **inside the same
  transaction** as the action: `PROCESSING_RECORDED`, `BATCH_CREATED` (for blends),
  `LAB_RESULT_RECORDED`, `PACKAGED`, `CUSTODY_TRANSFERRED`, `SHIPPED`, `RECEIVED`,
  `RETAIL_REGISTERED`, `RECALLED`. If the event write fails, the action rolls back. These
  are *local* events; on-chain anchoring happens only through confirmed Merkle checkpoints.

## Known limitations

* Custody moves whole batches; units cannot be split across retailers.
* A blend's ledger `hive_id` lists its source hives (`HIVE-A+HIVE-B`; beyond 250 characters it
  becomes `<N>-SOURCE-HIVES`, with the full list always in `batch_blend_inputs`), and its
  `quality_score` is the kg-weighted mean of the inputs' hive-husbandry scores (not a lab
  measurement). The full provenance lives in `batch_blend_inputs`.
* Unit sales are audited but not individually anchored (volume); retail registration is.
* Lab thresholds are the lab's own verdict; the system records rather than judges them.
