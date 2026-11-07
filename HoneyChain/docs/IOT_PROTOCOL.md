# HoneyChain IoT Device Protocol (Phase 4)

Every sensor node authenticates **individually**. The old fleet-wide shared
secret (`/api/firebase/webhook`) is disabled by default.

## 1. Provisioning

1. An ADMIN or the owning BEEKEEPER creates the hive: `POST /api/hives {"hive_id": "HIVE-001"}`.
2. Register a device against it: `POST /api/devices {"hive_id": "HIVE-001", "firmware_version": "1.4.2", "expected_interval_seconds": 300}`.
   A simulator or demo feed MUST be registered with `"data_origin": "SIMULATED"`; its readings are then
   labelled `SIMULATED` in every telemetry record instead of passing as real sensor data (default `PHYSICAL`).
3. The `201` response contains `credentials.device_id` and `credentials.device_secret`.
   **The secret is shown exactly once.** Flash it into device storage (ideally a secure element).
   If lost, rotate: `POST /api/devices/<device_id>/rotate-secret` (the old secret stops working immediately).

A device is permanently bound to one hive and that hive's organization. It can
never report for another hive, and it can never create a hive.

## 2. Sending telemetry

`POST /api/devices/telemetry` with a JSON body (same reading formats as before) and four headers:

| Header | Value |
|---|---|
| `X-Device-Id` | e.g. `DEV-195E64288A8A` |
| `X-Device-Timestamp` | current unix time, integer seconds |
| `X-Device-Nonce` | 16-128 chars `[A-Za-z0-9_-]`, **never reused** by this device |
| `X-Device-Signature` | lowercase hex HMAC-SHA256 (below) |

```
string_to_sign = device_id + "\n" + timestamp + "\n" + nonce + "\n" + hex(sha256(raw_body))
signature      = hex(HMAC_SHA256(key = device_secret, message = string_to_sign))
```

Sign the **exact bytes** you send. Reference implementation:
`honeychain.device_auth.sign_request` (used by both the server and `simulate_hardware.py --device`).

## 3. Replay protection

* `X-Device-Timestamp` must be within `HONEYCHAIN_DEVICE_MAX_SKEW` seconds (default 300) of server time -> sync the RTC via NTP.
* Each `(device, nonce)` pair is accepted once. An exact replay returns **409**.

## 4. Device event time

Each reading may carry its own `timestamp` (unix seconds/ms or ISO-8601). It is stored as
`device_timestamp`, never overwritten by server time. Buffered readings may be up to
`HONEYCHAIN_DEVICE_MAX_BACKFILL` old (default 7 days); a reading more than
`HONEYCHAIN_DEVICE_FUTURE_TOLERANCE` seconds in the future is rejected with 400.
With no per-reading timestamp, the signed request timestamp is used.

## 5. Responses

| Status | Meaning |
|---|---|
| 200 | accepted |
| 400 | invalid payload / out-of-range value / bad device timestamp |
| 401 | authentication failed (generic by design: bad signature, unknown or revoked device, stale timestamp, malformed header) |
| 403 | authenticated, but reporting for a hive it is not bound to |
| 409 | replay (nonce already used) |

## 6. Monitoring

* `POST /api/devices/silence-check` (ADMIN; run from cron) marks ACTIVE devices OFFLINE once silent for
  expected-interval x `HONEYCHAIN_DEVICE_SILENCE_MULTIPLIER`, writes one audit event and notifies the
  organization's users -- once per transition, not repeatedly. The next valid report returns it to ACTIVE.
* Readings carry data-quality `sensor_flags` (`MISSING_*`, `IMPLAUSIBLE_JUMP_*`, `FLATLINE_*`). These are
  heuristics about the *sensor*, not diagnoses of the *hive*, and never change the health verdict.

## 7. Known limitations

* The master key protects every derived secret: store it in a secrets manager, not in the repo.
* A device secret extracted from a stolen node compromises that one device only; revoke it.
  Physical tamper-resistance (secure element) is a hardware concern outside this software.
* `telemetry_readings.timestamp` remains server receipt time for all existing queries/charts;
  device event time is stored alongside in `device_timestamp`.
