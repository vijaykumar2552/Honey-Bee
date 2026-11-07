# Harvest Workflow (Phase 5)

`POST /api/harvest` — roles **ADMIN**, **BEEKEEPER** (own organization only).

```
actor (from bearer token, never the payload)
 -> hive exists, is ACTIVE, is owned by an ACTIVE organization the actor may act for
 -> validated harvest event (immutable; canonical SHA-256 event_hash)
 -> ledger block for the batch (existing hash-linked ledger)
 -> batch_harvest_links provenance row
    ...all in ONE database transaction
 -> audit: HARVEST_CREATED + BATCH_CREATED (HARVEST_DENIED on cross-org attempts)
```

| Field | Rules |
|---|---|
| `hive_id` | required; must exist, be ACTIVE, and belong to an organization |
| `total_weight` + `unit` | `> 0`, `<= 10 000 kg`; unit `kg` (default) or `g` |
| `floral_type` | required |
| `harvest_date` | `YYYY-MM-DD` or ISO-8601 datetime (kept as `harvested_at`); not in the future (1-day timezone tolerance), not older than 366 days; default today |
| `latitude`/`longitude` | optional, together, valid ranges |
| `frame_count` | optional integer 0-200 |
| `notes` | optional, <= 1000 chars, stored verbatim as data |
| `photo_sha256` | optional 64-hex SHA-256 of an evidence photo (a content reference, not a URL) |
| `harvest_id`, `batch_id` | optional client ids; duplicates -> **409** |
| `organization_id` | optional; if sent it must equal the hive's organization (else 400) |

Status codes: 201 created · 400 validation / unassigned hive · 401 unauthenticated ·
403 wrong role, other organization, or inactive organization · 404 unknown hive ·
409 duplicate id or non-ACTIVE hive.

Retrieval (all organization-scoped; ADMIN/AUTHORITY see all):

* `GET /api/harvest` — list
* `GET /api/harvest/<harvest_id>` — detail + `verification` (recomputed event hash, linked batch ledger checks)
* `GET /api/batches/<batch_id>/provenance` — batch -> harvest(s) -> hive, with verification
* `GET /api/batches` — now organization-scoped (previously returned every organization's batches)

Hashing: harvest events and ledger blocks share one serialiser (`ledger.canonical_json`).
The ledger block hash is unchanged from earlier phases; the harvest is committed to by its
own `event_hash`, and `batch_harvest_links` ties the two. Since Phase 6 the harvest
transaction also records a `BATCH_CREATED` chain event (payload: batch id, block index,
block hash, harvest id, harvest event hash, hive id -- no GPS, notes, names or quantities),
which reaches the blockchain inside the next Merkle checkpoint. See `docs/BLOCKCHAIN.md`.
