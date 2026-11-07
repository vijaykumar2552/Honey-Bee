# HoneyChain API reference

All JSON. Authenticated endpoints take `Authorization: Bearer <token>` from
`POST /api/auth/login`. "roles: ..." means the authenticated user must have one of those
roles; organization isolation (IDOR protection) is then enforced on top of the role check.

This table is **generated from the route decorators in the source**. It was cross-checked
by calling every non-public route without a token: all returned 401, including when the
auth secret is unset (fail closed).

| Method | Path | Access |
|---|---|---|
| `GET` | `/` | PUBLIC |
| `GET` | `/api/admin/organizations` | roles: ADMIN, AUTHORITY |
| `POST` | `/api/admin/organizations` | roles: ADMIN |
| `POST` | `/api/admin/organizations/<org_id>/status` | roles: ADMIN |
| `GET` | `/api/audit-logs` | roles: ADMIN, AUTHORITY |
| `POST` | `/api/auth/login` | PUBLIC (rate-limited per address + per email) |
| `POST` | `/api/auth/logout` | any authenticated user |
| `GET` | `/api/auth/me` | any authenticated user |
| `POST` | `/api/auth/register` | roles: ADMIN |
| `GET` | `/api/authority/hotspots` | roles: AUTHORITY, ADMIN |
| `GET` | `/api/authority/overview` | roles: AUTHORITY, ADMIN |
| `GET` | `/api/authority/recalls` | roles: AUTHORITY, ADMIN |
| `GET` | `/api/authority/signals` | roles: AUTHORITY, ADMIN |
| `GET` | `/api/batches` | any authenticated user |
| `POST` | `/api/batches/<batch_id>/lab-requests` | roles: *sc.HOLDER_ROLES |
| `GET` | `/api/batches/<batch_id>/lab-results` | any authenticated user |
| `POST` | `/api/batches/<batch_id>/packaging` | roles: PROCESSOR |
| `POST` | `/api/batches/<batch_id>/processing` | roles: PROCESSOR |
| `GET` | `/api/batches/<batch_id>/provenance` | any authenticated user |
| `GET` | `/api/batches/<batch_id>/trace` | any authenticated user |
| `GET` | `/api/batches/<batch_id>/units` | any authenticated user |
| `POST` | `/api/blends` | roles: PROCESSOR |
| `GET` | `/api/chain` | PUBLIC |
| `GET` | `/api/chain/checkpoints` | roles: ADMIN, AUTHORITY |
| `POST` | `/api/chain/checkpoints` | roles: ADMIN |
| `GET` | `/api/chain/checkpoints/<checkpoint_id>/verify` | PUBLIC |
| `POST` | `/api/chain/checkpoints/submit` | roles: ADMIN |
| `GET` | `/api/chain/events` | any authenticated user |
| `GET` | `/api/chain/events/<event_id>` | any authenticated user |
| `GET` | `/api/chain/log/verify` | roles: ADMIN, AUTHORITY |
| `GET` | `/api/custody/transfers` | any authenticated user |
| `POST` | `/api/custody/transfers` | roles: *sc.HOLDER_ROLES |
| `POST` | `/api/custody/transfers/<transfer_id>/<action>` | roles: *sc.HOLDER_ROLES |
| `GET` | `/api/devices` | any authenticated user |
| `POST` | `/api/devices` | roles: ADMIN, BEEKEEPER |
| `GET` | `/api/devices/<device_id>` | any authenticated user |
| `POST` | `/api/devices/<device_id>/revoke` | roles: ADMIN, BEEKEEPER |
| `POST` | `/api/devices/<device_id>/rotate-secret` | roles: ADMIN, BEEKEEPER |
| `POST` | `/api/devices/silence-check` | roles: ADMIN |
| `POST` | `/api/devices/telemetry` | device HMAC (per-device secret, signed timestamp + nonce) |
| `POST` | `/api/firebase/webhook` | shared HMAC -- legacy, **disabled by default** (410) |
| `GET` | `/api/harvest` | any authenticated user |
| `POST` | `/api/harvest` | roles: ADMIN, BEEKEEPER |
| `GET` | `/api/harvest/<harvest_id>` | any authenticated user |
| `GET` | `/api/health` | PUBLIC |
| `GET` | `/api/hives` | any authenticated user |
| `POST` | `/api/hives` | roles: ADMIN, BEEKEEPER |
| `POST` | `/api/hives/<hive_id>/assign` | roles: ADMIN |
| `GET` | `/api/hives/<hive_id>/history` | any authenticated user |
| `GET` | `/api/lab/requests` | roles: LAB |
| `POST` | `/api/lab/results/<lab_result_id>` | roles: LAB |
| `GET` | `/api/ml/status` | any authenticated user |
| `GET` | `/api/notifications` | any authenticated user |
| `POST` | `/api/public/verify` | PUBLIC (rate-limited per address) |
| `GET` | `/api/recalls` | any authenticated user |
| `POST` | `/api/recalls` | roles: AUTHORITY, ADMIN |
| `GET` | `/api/retail` | any authenticated user |
| `POST` | `/api/retail/<retail_record_id>/receive` | roles: RETAILER |
| `POST` | `/api/retail/<retail_record_id>/shelve` | roles: RETAILER |
| `POST` | `/api/retail/units/<unit_id>/sell` | roles: RETAILER |
| `GET` | `/api/shipments` | any authenticated user |
| `POST` | `/api/shipments` | roles: *sc.HOLDER_ROLES |
| `GET` | `/api/shipments/<shipment_id>` | any authenticated user |
| `POST` | `/api/shipments/<shipment_id>/status` | roles: *sc.HOLDER_ROLES |
| `GET` | `/api/summary` | any authenticated user |
| `GET` | `/api/telemetry/latest` | any authenticated user |
| `GET` | `/api/telemetry/stream` | any authenticated user |
| `POST` | `/api/units/<unit_id>/qr` | roles: PROCESSOR, ADMIN |
| `POST` | `/api/units/<unit_id>/revoke-qr` | roles: PROCESSOR, ADMIN |
| `GET` | `/api/units/<unit_id>/trace` | any authenticated user |
| `GET` | `/api/verify/<batch_id>` | PUBLIC |
| `GET` | `/app/` | PUBLIC |
| `GET` | `/app/<path:filename>` | PUBLIC |
| `GET` | `/q` | PUBLIC |
| `GET` | `/verify/<batch_id>` | PUBLIC |

Status codes used consistently: 200/201 success, 400 validation, 401 unauthenticated,
403 wrong role or other organization, 404 unknown resource, 409 conflict or invalid state
transition, 410 retired endpoint, 429 rate-limited.

Workflow details: `docs/HARVEST_WORKFLOW.md`, `docs/SUPPLY_CHAIN.md`,
`docs/CONSUMER_VERIFICATION.md`, `docs/AUTHORITY.md`, `docs/IOT_PROTOCOL.md`,
`docs/BLOCKCHAIN.md`.
