# Authority Oversight (Phase 10)

Read-only oversight for the **AUTHORITY** role (ADMIN may also read). Every figure is
computed from the database at request time. AUTHORITY is not an administrator: user,
organization and device management remain ADMIN-only. Issuing a recall (Phase 8) is the one
authority write action.

| Endpoint | Content |
|---|---|
| `GET /api/authority/overview[?region=]` | Production (active beekeepers and hives, harvests, kg, by region), supply-chain volumes, compliance (lab results by status, failed-lab batches, % of batches with a passed test, % of units traceable to a harvest, active recalls, shipment exceptions with their reasons) |
| `GET /api/authority/hotspots[?region=]` | Per region: latest hive-health status counts and offline devices; harvest geography in 0.1-degree cells |
| `GET /api/authority/signals` | Suspicious-scan **risk signals** with evidence (see below) |
| `GET /api/authority/recalls` | Each recall with affected batches (including blend descendants), units by status, retailers, and consumer scans since the recall |

**What the data does and does not support**

* "Region" is the hive's `location`; hives without one are grouped as `Unspecified`.
  Organizations have no region, so processing, shipment and retail volumes are national totals.
* Precise harvest coordinates are never returned; they are aggregated to ~11 km cells.
* Signals are not accusations. Each one is `classification: "SIGNAL"`.

| Signal | Severity | Trigger |
|---|---|---|
| `FORGED_TAG_ATTEMPTS` | HIGH | >= `HONEYCHAIN_SIGNAL_FORGERY_ATTEMPTS` (3) wrong or modified tags naming one real unit |
| `SCANNED_AFTER_RECALL` | HIGH | a recalled unit is authentically scanned |
| `REVOKED_LABEL_SCANNED` | MEDIUM | a revoked label is scanned |
| `HIGH_SCAN_VELOCITY` | MEDIUM | >= `HONEYCHAIN_SIGNAL_SCAN_VELOCITY_24H` (10) authentic scans of one unit in 24 h |
| `SCANNED_BEFORE_RETAIL` | LOW | authentic scan while the unit was still `ISSUED` (may be staff checks) |

Signals come from `consumer_scans`, an append-only log of every public verification
attempt. It stores **no IP address or client data**, only the unit (if it exists), the
result, the reason and the time. The public response is unchanged: an unknown unit and a
wrong tag still look identical to the caller.

Shipment `EXCEPTION`s now require an `exception_reason` (5-300 characters). Before this
phase an exception could be recorded with no cause at all.

## Frontend

`frontend/` is a React 18 + Vite 5 + TypeScript (strict) app. It uses the live API only;
there is no mock data.

```
cd frontend && npm install && npm run build     # typecheck + production build -> frontend/dist
# Flask then serves it at http://localhost:5000/app/
npm run dev                                       # development server, proxies /api to :5000
```

It provides sign-in (bearer token in `sessionStorage`, cleared on tab close) and the
authority dashboard (region filter, production, supply chain, compliance, hotspots,
signals, recalls), each with loading, empty and error states. Other roles see an explicit
note pointing to the existing dashboard at `/`. `/app/` is served with a strict CSP
(`default-src 'self'`, no inline scripts, `frame-ancestors 'none'`), `nosniff` and
`no-referrer`. File serving rejects path traversal, and unknown client routes fall back to
the app shell.
