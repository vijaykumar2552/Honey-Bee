"""
Phase 6 -- LIVE blockchain integration (real node, real transactions).

Starts an actual local Hardhat JSON-RPC node, compiles and deploys
HoneyChainRegistry with web3, and drives the PRODUCTION client
(honeychain.chain_anchor) through honeychain.chain_events. Every success
asserted here is re-checked independently on-chain (transaction receipt
status, contract.verifyCheckpoint) -- nothing is taken on the client's word.

This is a local development chain (chain id 31337), NOT a public network.
It proves the integration code end-to-end; it says nothing about public
testnet/mainnet availability, gas prices, or funding.

Dev-account private keys are parsed from the node's own startup output at
runtime; no key is written in this repository. The module skips with an
explicit reason if Node/Hardhat or web3 is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import unittest
from datetime import date
from pathlib import Path

from tests.base import HoneyChainTestCase, REPO_ROOT

from honeychain import chain_anchor, chain_events, config, db, merkle

CONTRACTS = Path(REPO_ROOT) / "blockchain"
ARTIFACT = CONTRACTS / "artifacts/contracts/HoneyChainRegistry.sol/HoneyChainRegistry.json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveChainTestCase(HoneyChainTestCase):
    node: subprocess.Popen | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        try:
            import web3  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("web3 not installed -- live chain test not run")
        if not shutil.which("npx") or not (CONTRACTS / "node_modules").is_dir():
            raise unittest.SkipTest("Node/Hardhat not installed -- live chain test not run")
        compile_ = subprocess.run(["npx", "hardhat", "compile"], cwd=CONTRACTS,
                                  capture_output=True, text=True, timeout=300)
        if compile_.returncode != 0 or not ARTIFACT.is_file():
            raise unittest.SkipTest(f"contract compilation failed: {compile_.stderr[-300:]}")

        cls.port = _free_port()
        cls.log_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"hardhat-node-{cls.port}.log"
        cls._log = open(cls.log_path, "w")
        cls.node = subprocess.Popen(["npx", "hardhat", "node", "--port", str(cls.port)],
                                    cwd=CONTRACTS, stdout=cls._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        keys: list[str] = []
        while time.time() < deadline:
            text = cls.log_path.read_text()
            keys = re.findall(r"Private Key: (0x[0-9a-fA-F]{64})", text)
            if "Started HTTP and WebSocket JSON-RPC server" in text and len(keys) >= 2:
                break
            if cls.node.poll() is not None:
                break
            time.sleep(0.5)
        if len(keys) < 2:
            cls.tearDownClass()
            raise unittest.SkipTest("Hardhat node did not start -- live chain test not run")
        cls.owner_key, cls.stranger_key = keys[0], keys[1]

        from web3 import Web3
        cls.w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{cls.port}"))
        artifact = json.loads(ARTIFACT.read_text())
        cls.abi, cls.bytecode = artifact["abi"], artifact["bytecode"]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.node is not None:
            cls.node.terminate()
            try:
                cls.node.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.node.kill()
        if getattr(cls, "_log", None):
            cls._log.close()
        super().tearDownClass()

    def deploy(self) -> str:
        acct = self.w3.eth.account.from_key(self.owner_key)
        factory = self.w3.eth.contract(abi=self.abi, bytecode=self.bytecode)
        tx = factory.constructor().build_transaction({
            "from": acct.address, "nonce": self.w3.eth.get_transaction_count(acct.address, "pending"),
            "chainId": 31337})
        signed = acct.sign_transaction(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(signed.raw_transaction), timeout=30)
        self.assertEqual(receipt.status, 1)
        return receipt.contractAddress

    def setUp(self) -> None:
        super().setUp()
        self._saved = {k: getattr(config, k) for k in
                       ("CHAIN_ANCHOR_ENABLED", "RPC_URL", "CHAIN_ID", "CONTRACT_ADDRESS")}
        self._saved_key = os.environ.get("PRIVATE_KEY")
        self.address = self.deploy()          # fresh contract per test: no state leakage
        self.configure()
        org = self.create_org(org_id="ORG-LIVE", name="Live")
        self.h = self.auth_headers_for(self.create_user(
            user_id="USR-LIVE", email="live@hc.test", role="BEEKEEPER", organization_pk=org))
        self.seed_hive("HIVE-LIVE", count=3)
        self.own_hive("HIVE-LIVE", "ORG-LIVE")

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(config, k, v)
        if self._saved_key is None:
            os.environ.pop("PRIVATE_KEY", None)
        else:
            os.environ["PRIVATE_KEY"] = self._saved_key
        chain_anchor.reset()
        super().tearDown()

    def configure(self, *, rpc=None, chain_id=31337, address=None, key=None) -> None:
        config.CHAIN_ANCHOR_ENABLED = True
        config.RPC_URL = rpc or f"http://127.0.0.1:{self.port}"
        config.CHAIN_ID = chain_id
        config.CONTRACT_ADDRESS = address or self.address
        os.environ["PRIVATE_KEY"] = key or self.owner_key
        chain_anchor.reset()

    def harvests(self, n: int) -> list[dict]:
        out = []
        for _ in range(n):
            r = self.client.post("/api/harvest", headers=self.h, json={
                "hive_id": "HIVE-LIVE", "harvest_date": date.today().isoformat(),
                "total_weight": 11.5, "floral_type": "Sunflower"})
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            out.append(r.get_json())
        return out

    def contract(self):
        return self.w3.eth.contract(address=self.address, abi=self.abi)


class TestLiveAnchoring(LiveChainTestCase):
    def test_checkpoint_is_really_anchored_and_independently_verifiable(self) -> None:
        bodies = self.harvests(3)
        cp = chain_events.create_checkpoint()
        results = chain_events.submit_checkpoints()
        self.assertEqual(results[0]["status"], "CONFIRMED", results)

        stored = chain_events.get_checkpoint(cp["checkpoint_id"])
        # Independent on-chain checks -- not the client's word for it:
        receipt = self.w3.eth.get_transaction_receipt(stored["tx_hash"])
        self.assertEqual(receipt.status, 1)
        self.assertEqual(receipt.to.lower(), self.address.lower())
        self.assertEqual(receipt.blockNumber, stored["block_number"])
        self.assertEqual(stored["chain_id"], self.w3.eth.chain_id)
        root32 = bytes.fromhex(cp["merkle_root"])
        self.assertTrue(self.contract().functions.verifyCheckpoint(cp["checkpoint_id"], root32).call())
        f, t, onchain_root, _, _ = self.contract().functions.getCheckpoint(cp["checkpoint_id"]).call()
        self.assertEqual((f, t, bytes(onchain_root)), (1, 3, root32))

        v = chain_events.verify_checkpoint(cp["checkpoint_id"])
        self.assertEqual((v["onchain"], v["local_root_valid"]), ("VERIFIED", True))
        for body in bodies:
            proof = chain_events.event_proof(body["chain_event"]["event_id"])
            self.assertTrue(merkle.verify_proof(proof["event_hash"], proof["proof"],
                                                bytes(onchain_root).hex()))
            batch = db.query_one("SELECT anchor_tx_hash FROM honey_batches WHERE batch_id = ?;",
                                 (body["batch"]["batch_id"],))
            self.assertEqual(batch["anchor_tx_hash"], stored["tx_hash"])

    def test_no_duplicate_anchor_is_possible(self) -> None:
        self.harvests(1)
        cp = chain_events.create_checkpoint()
        chain_events.submit_checkpoints()
        acct = self.w3.eth.account.from_key(self.owner_key).address
        nonce_before = self.w3.eth.get_transaction_count(acct)
        self.assertEqual(chain_events.submit_checkpoints(), [])        # nothing retryable
        with self.assertRaises(chain_anchor.AnchorRejected):             # direct re-send refused
            chain_anchor.anchor_checkpoint(cp["checkpoint_id"], 1, 1, cp["merkle_root"])
        self.assertEqual(self.w3.eth.get_transaction_count(acct), nonce_before)  # no tx sent
        self.assertEqual(self.contract().functions.checkpointCount().call(), 1)

    def test_local_tampering_after_anchoring_is_detected_against_the_chain(self) -> None:
        self.harvests(2)
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        with db.transaction() as conn:
            conn.execute("UPDATE chain_events SET event_hash = ? WHERE seq = 1;", ("b" * 64,))
        v = chain_events.verify_checkpoint(cid)
        self.assertFalse(v["local_root_valid"])
        self.assertEqual(v["onchain"], "MISMATCH")

    def test_unauthorised_signer_is_rejected_without_sending(self) -> None:
        self.configure(key=self.stranger_key)
        self.harvests(1)
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual(cp["status"], "FAILED")
        self.assertIn("reject", cp["last_error"])
        self.assertIsNone(cp["tx_hash"])
        self.assertEqual(self.contract().functions.checkpointCount().call(), 0)

    def test_wrong_chain_id_refuses_to_send(self) -> None:
        self.configure(chain_id=80002)
        self.harvests(1)
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual(cp["status"], "FAILED")
        self.assertIn("31337", cp["last_error"])
        self.assertEqual(self.contract().functions.checkpointCount().call(), 0)

    def test_unreachable_rpc_is_failed_and_recoverable(self) -> None:
        self.configure(rpc=f"http://127.0.0.1:{_free_port()}")
        self.harvests(1)
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        self.assertEqual(chain_events.get_checkpoint(cid)["status"], "FAILED")
        self.configure()                                               # RPC back
        chain_events.submit_checkpoints()
        self.assertEqual(chain_events.get_checkpoint(cid)["status"], "CONFIRMED")
        self.assertEqual(self.contract().functions.checkpointCount().call(), 1)

    def test_address_without_a_contract_is_refused(self) -> None:
        self.configure(address="0x000000000000000000000000000000000000dEaD")
        self.harvests(1)
        cid = chain_events.create_checkpoint()["checkpoint_id"]
        chain_events.submit_checkpoints()
        cp = chain_events.get_checkpoint(cid)
        self.assertEqual(cp["status"], "FAILED")
        self.assertIn("no contract", cp["last_error"])


if __name__ == "__main__":
    unittest.main()
