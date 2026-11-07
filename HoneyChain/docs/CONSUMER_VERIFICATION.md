# Consumer Verification (Phase 9)

## The QR code

Each product unit gets a 192-bit secret at packaging (Phase 8). Only its SHA-256 is stored.
The label carries a tag derived from the secret, never the secret itself:

```
tag     = base64url( HMAC-SHA256(key = unit_secret, msg = "HC1|" + unit_id) )
payload = "HC1." + unit_id + "." + tag
QR      = <PUBLIC_BASE_URL>/q#<payload>          # payload in the URL fragment
```

* The server stores only `qr_verifier = SHA-256(tag)`. It is set at packaging, while the
  secret is available. Verification is a constant-time comparison. Neither the raw secret
  nor the tag is ever stored.
* HMAC binds the tag to the version and the unit ID: a tag copied onto another unit ID, or
  used with another version, fails.
* Browsers never send the fragment, so the payload does not reach server access logs. The
  landing page `GET /q` posts it to the API and renders the answer with `textContent` only.
* Re-issue (for example, reprinting a label): `POST /api/units/<unit_id>/qr {"unit_secret": ...}`.
  Only the packaging organization (or ADMIN) can do this, and it must re-present the secret
  from the packaging response. A wrong secret returns 403 and is audited. The PNG is
  rendered in memory; nothing is written to disk.
* Revocation (for example, stolen label stock): `POST /api/units/<unit_id>/revoke-qr`.

## Public verification: `POST /api/public/verify {"qr": "<scanned text>"}`

No login. `Cache-Control: no-store`, so a recall is never served stale.

| result | meaning |
|---|---|
| `AUTHENTIC` | Issued by HoneyChain for this exact unit and not revoked. **Not a statement of safety or quality.** Includes provenance, the lab picture, any recall, and warnings. |
| `REVOKED` | Genuine but revoked label. No provenance, but an active **recall is still shown**. |
| `INVALID` | `MALFORMED`, `UNSUPPORTED_VERSION`, or `NOT_RECOGNISED`. An unknown unit and a wrong or modified tag get the identical response, so the endpoint does not reveal which unit IDs exist. |

Provenance is built from database rows through explicit allow-lists. It shows harvest
(hive, date, producer name), processing, blend sources recursively with weights, lab
status plus completed results (verdict, date, laboratory name, measured values) and pending
count, accepted custody hand-offs, shipments, retail, and the chain events with their real
anchoring state (`tx_hash` only for a CONFIRMED checkpoint). It never contains internal IDs,
people's names or emails, GPS, notes, vehicles or drivers, secrets, verifiers, or audit data.
Recall of any source batch is shown on units of every blend made from it.

## Known limitations

* A photographed label is a working copy. Authentic scans are counted (`scan_count`,
  `first_scanned_at`, `last_scanned_at`), but clone detection from those signals is Phase 10.
* The public endpoint has no rate limiting, so scan counts can be inflated by flooding.
* Anchoring lookups scan the whole event log per request (O(n)); fine at pilot scale.
* The pre-existing batch-level page `GET /verify/<batch_id>` still verifies batch *record
  integrity*. It is unchanged and does not authenticate individual units.
