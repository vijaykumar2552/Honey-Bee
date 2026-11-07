# Blockchain tier — HoneyChainRegistry

The on-chain anchor for HoneyChain. This directory contains the Solidity source,
the Hardhat toolchain and the deployment scripts.

## Why this exists

The project shipped an `abi.json` but **no contract source**. Nothing could be
compiled, deployed, audited or verified on Etherscan, and two components
disagreed about what the interface even was:

| Component | Declared interface |
|---|---|
| `honeychain/abi.json` (Python) | `createBatch` / `getBatch` / `verifyBatch` |
| `node_service/blockchain.js` (old) | `recordHash` / `getHash` |

Those cannot both be right. `HoneyChainRegistry.sol` implements the `abi.json`
interface exactly, `blockchain.js` was rewritten to load that same shared ABI
file, and `scripts/verify_abi.py` now checks the two agree in CI.

## Layout

```
blockchain/
├── contracts/HoneyChainRegistry.sol   # the contract
├── scripts/deploy.js                  # deploy + smoke test + ABI sync
├── scripts/verify_abi.py              # ABI conformance check (no compiler needed)
├── test/HoneyChainRegistry.test.js    # 18 Hardhat tests
├── hardhat.config.js
├── package.json
├── .env.example
└── deployments/<network>.json         # written on deploy
```

## What goes on chain, and what does not

Only the **32-byte SHA-256 digest** of a batch record, plus a small amount of
public provenance metadata. The telemetry itself, the quality breakdown and the
apiary's operational history stay in SQLite.

That split is deliberate. Public chain data is permanent and world-readable;
harvest volumes and hive locations are commercially sensitive, and there is no
delete. The digest is sufficient to prove a record has not changed, which is the
entire job of the anchor.

Note what an anchor does **not** prove: that the data was true when it was
written. It proves only that nobody altered it afterwards. The off-chain
`HoneyBlockLedger` chains blocks so an edit anywhere breaks everything after it;
the on-chain anchor additionally stops someone rewriting the whole local chain
and passing it off as original. The two are complementary.

## Setup

```bash
cd blockchain
npm install
cp .env.example .env      # add SEPOLIA_RPC_URL, PRIVATE_KEY, ETHERSCAN_API_KEY
```

`npx hardhat compile` and `npx hardhat test` need **no credentials** — they run
against the in-process chain. Only deployment needs a funded key.

## Test

```bash
npx hardhat test
```

18 tests covering the properties the anchor exists to provide:

- a duplicate `batchId` **reverts** rather than overwriting an existing anchor
- `verifyBatch` accepts only the exact anchored hash; one changed byte fails
- an unknown batch returns `false` rather than reverting, so consumer-facing
  callers get a clean "not authentic" answer
- unauthorised addresses cannot register or update batches
- `updateBatchStatus` changes status without disturbing the hash
- a revoked key immediately loses write access

## Deploy

```bash
# local
npx hardhat node                                             # terminal 1
npx hardhat run scripts/deploy.js --network localhost        # terminal 2

# Sepolia
npx hardhat run scripts/deploy.js --network sepolia
```

`deploy.js` does four things beyond deploying:

1. **Refuses to deploy from a zero-balance account**, with a clear message
   rather than an opaque ethers failure.
2. **Runs a post-deploy smoke test** — registers a probe batch, reads it back,
   confirms the right hash verifies and a wrong hash does not. Deploying
   successfully is not the same as the bytecode behaving.
3. **Writes `deployments/<network>.json`** with the address, ABI, block and
   deployer.
4. **Regenerates `honeychain/abi.json`** from the compiled artifact. This is the
   step that prevents the drift described above from recurring — the ABI is
   never hand-copied.

Then verify the source publicly:

```bash
npx hardhat verify --network sepolia <address>
```

## Connect it to the running service

Add to the HoneyChain `.env` at the repository root (values printed by `deploy.js`):

```bash
HONEYCHAIN_CHAIN_ANCHOR=true
RPC_URL=https://sepolia.infura.io/v3/YOUR_PROJECT_ID
CONTRACT_ADDRESS=0x...
PRIVATE_KEY=0x...
```

Restart the Flask service. `POST /api/harvest` will now anchor each new block
after committing it locally, and return the transaction hash in `chain_anchor`.
Confirm with:

```bash
curl -s localhost:5000/api/health | python3 -m json.tool | grep -A5 chain_anchor
```

If the deployer and the backend signer are different addresses, authorise the
backend once:

```javascript
await contract.setAuthorized("0xBackendAddress", true);
```

**Anchoring is opt-in and never blocks a harvest.** If the RPC is unreachable,
the key is unfunded or `web3` is not installed, `honeychain/chain_anchor.py`
logs a warning and the harvest still succeeds locally. Losing your testnet RPC
must not stop a beekeeper recording a harvest.

## Verify the ABI stays in sync

```bash
python3 scripts/verify_abi.py              # static parse, no compiler needed
python3 scripts/verify_abi.py --artifact   # exact, after `npx hardhat compile`
```

Exits non-zero on any mismatch — suitable for CI.

## Gas and cost

At Sepolia rates, `createBatch` is the only routine write. Cost scales with the
length of the strings you pass, since each 32-byte word of `calldata` and
storage is charged. Keeping `beekeeper` and `harvestDate` short matters more
than it looks. Run `REPORT_GAS=true npx hardhat test` for per-function numbers.

For mainnet, anchor a **Merkle root of many batches** on a schedule rather than
one transaction per batch — same guarantee, cost amortised across the batch set.
The current per-batch design is right for a testnet demonstration and for
low-volume production on an L2.

## Status of this code

The contract, tests, config and scripts are complete and syntax-checked.
`verify_abi.py` confirms all seven ABI entries match the Solidity source.

**They have not been compiled or executed here** — this environment has no
network access, so `npm install` (and therefore Hardhat and solc) was
unavailable. Run `npx hardhat compile && npx hardhat test` as your first step;
that is where any remaining compile-level issue will surface.
