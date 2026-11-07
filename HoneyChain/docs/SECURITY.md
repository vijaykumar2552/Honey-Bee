# Security model

* **Secrets** come only from the environment (`.env.example` lists every variable with
  placeholders): `HONEYCHAIN_AUTH_SECRET`, `HONEYCHAIN_DEVICE_MASTER_KEY`,
  `HONEYCHAIN_WEBHOOK_SECRET` (legacy), `PRIVATE_KEY`, `RPC_URL`, `CONTRACT_ADDRESS`,
  `CHAIN_ID`. Each one fails closed when unset.
* **Users**: scrypt password hashes; bearer tokens carry only a user id and a token id
  (role and status are re-read on every request); logout revokes the token server-side;
  login throttled per email (5/15 min) and per address (20/min).
* **Authorization**: role decorators on every non-public route; organization isolation
  and IDOR checks in the services (403); generic error messages; audit log for security
  and supply-chain actions.
* **Devices**: per-device secrets derived from a master key (none stored); signature over
  device, timestamp, nonce and body hash; signed-time window plus unique nonce (replay ->
  409); device bound to one hive; revocation and rotation.
* **QR**: per-unit HMAC tag; only `SHA-256(tag)` is stored; constant-time comparison;
  unknown unit and wrong tag are indistinguishable; payload in the URL fragment; public
  verification rate-limited (30/min per address).
* **Blockchain**: only Merkle roots of ids and hashes go on-chain; chain id is verified
  before sending; a dry run precedes each send; a checkpoint is CONFIRMED only after a
  status-1 receipt.
* **ML**: the artefact's SHA-256 and the scikit-learn version are checked *before*
  unpickling.
* **Web**: strict CSP on `/app/` (no inline scripts), `nosniff`, `no-referrer`;
  traversal-safe static serving; React escaping and `textContent` only on public pages.

Known limits: rate limits are per process; behind a reverse proxy they key on the proxy
address unless a trusted-proxy fix is configured. The token is kept in `sessionStorage`,
not an httpOnly cookie. There is no CSRF token, which bearer-header auth does not need.
Contract ownership is a single key.
