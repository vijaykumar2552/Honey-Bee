"""
honeychain.chain_anchor
=======================
THE single blockchain client for HoneyChain. Nothing else in the codebase
talks to an RPC endpoint; supply-chain modules emit chain events
(honeychain.chain_events), and only Merkle checkpoint roots over those
events are anchored here via ``HoneyChainRegistry.anchorCheckpoint``.

Enable with::

    HONEYCHAIN_CHAIN_ANCHOR=true
    RPC_URL=https://...            # or http://127.0.0.1:8545 for local Hardhat
    CHAIN_ID=80002                 # must match the RPC endpoint's chain id
    CONTRACT_ADDRESS=0x...
    PRIVATE_KEY=0x...              # never commit this

Design rules (and the pre-Phase-6 bugs they fix):

* Lazy and defensive: nothing connects at import time; a missing or
  unreachable RPC never prevents the application from starting.
* The connected chain id is compared with CHAIN_ID before anything is sent
  (previously SEPOLIA_CHAIN_ID was hardcoded into every transaction).
* A transaction counts as successful ONLY when its receipt has status == 1.
  Previously a reverted transaction still returned its hash, and the caller
  stored that hash as if the batch had been anchored.
* Failures are raised as typed errors (never swallowed into ``None``) so the
  caller can record an honest FAILED state and retry.
* Every write is dry-run with ``eth_call`` first, so a call that would revert
  (e.g. an already-anchored checkpoint) is detected without spending gas.
* The private key is read from the environment at call time, never stored on
  a module constant, logged, or returned.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from . import config

logger = logging.getLogger(__name__)

_ABI_PATH = config.PACKAGE_DIR / "abi.json"

_lock = threading.Lock()        # guards lazy initialisation
_send_lock = threading.Lock()   # serialises sends: one nonce sequence per key
_state: dict[str, Any] = {"initialised": False, "contract": None, "w3": None, "error": None}


class AnchorError(Exception):
    """Base class. ``str(exc)`` is safe to store and show (no secrets)."""


class AnchorUnavailable(AnchorError):
    """Anchoring is disabled, misconfigured, or the RPC is unreachable."""


class AnchorRejected(AnchorError):
    """The contract refused the call (it would revert) -- nothing was sent."""


class AnchorFailed(AnchorError):
    """A transaction was sent but did not confirm successfully."""

    def __init__(self, message: str, tx_hash: str | None = None) -> None:
        super().__init__(message)
        self.tx_hash = tx_hash


def _hex(value: Any) -> str:
    """Lowercase hex WITHOUT 0x, independent of the hexbytes version (1.x
    dropped the 0x prefix from .hex(); older versions included it)."""
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text[2:].lower() if text[:2].lower() == "0x" else text.lower()


def is_enabled() -> bool:
    return bool(config.CHAIN_ANCHOR_ENABLED)


def reset() -> None:
    """Forget the cached connection (used when configuration changes)."""
    with _lock:
        _state.update({"initialised": False, "contract": None, "w3": None, "error": None})


def _initialise() -> bool:
    """Connect lazily. Returns True when a verified contract handle is available."""
    if _state["initialised"]:
        return _state["contract"] is not None

    with _lock:
        if _state["initialised"]:
            return _state["contract"] is not None
        _state["initialised"] = True

        if not is_enabled():
            _state["error"] = "anchoring disabled (set HONEYCHAIN_CHAIN_ANCHOR=true)"
            return False
        if not config.RPC_URL or not config.CONTRACT_ADDRESS:
            _state["error"] = "RPC_URL and CONTRACT_ADDRESS must both be set"
            logger.warning("Chain anchoring unavailable: %s", _state["error"])
            return False
        if config.CHAIN_ID is None:
            _state["error"] = "CHAIN_ID must be set to the contract's network id"
            logger.warning("Chain anchoring unavailable: %s", _state["error"])
            return False

        try:
            from web3 import Web3  # type: ignore
        except ImportError:
            _state["error"] = "web3 is not installed"
            logger.warning("Chain anchoring unavailable: %s", _state["error"])
            return False

        try:
            w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 15}))
            if not w3.is_connected():
                _state["error"] = "RPC endpoint unreachable"
                logger.warning("Chain anchoring unavailable: %s", _state["error"])
                return False
            actual_chain = int(w3.eth.chain_id)
            if actual_chain != config.CHAIN_ID:
                _state["error"] = (
                    f"RPC endpoint is on chain {actual_chain}, but CHAIN_ID={config.CHAIN_ID}"
                )
                logger.error("Chain anchoring refused: %s", _state["error"])
                return False
            address = Web3.to_checksum_address(config.CONTRACT_ADDRESS)
            if not w3.eth.get_code(address):
                _state["error"] = "no contract deployed at CONTRACT_ADDRESS on this chain"
                logger.error("Chain anchoring refused: %s", _state["error"])
                return False
            abi = json.loads(_ABI_PATH.read_text(encoding="utf-8"))
            contract = w3.eth.contract(address=address, abi=abi)
        except Exception as exc:
            _state["error"] = f"initialisation failed: {type(exc).__name__}"
            logger.warning("Chain anchoring unavailable: %s", _state["error"])
            return False

        _state["w3"] = w3
        _state["contract"] = contract
        _state["error"] = None
        logger.info("Blockchain anchoring ready (chain %s)", config.CHAIN_ID)
        return True


def status() -> dict[str, Any]:
    """Anchor availability for /api/health. Never exposes credentials."""
    return {
        "enabled": is_enabled(),
        "connected": _state["contract"] is not None,
        "chain_id": config.CHAIN_ID,
        "contract_address": config.CONTRACT_ADDRESS,
        "rpc_configured": bool(config.RPC_URL),
        "contract_configured": bool(config.CONTRACT_ADDRESS),
        "error": _state["error"],
    }


def _require_connection() -> tuple[Any, Any]:
    if not _initialise():
        raise AnchorUnavailable(_state["error"] or "anchoring unavailable")
    return _state["w3"], _state["contract"]


def anchor_checkpoint(checkpoint_id: str, from_seq: int, to_seq: int,
                      merkle_root_hex: str) -> dict[str, Any]:
    """Anchor one Merkle root. Returns ``{tx_hash, block_number, chain_id}``
    ONLY for a mined transaction with receipt status 1; otherwise raises."""
    w3, contract = _require_connection()
    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        raise AnchorUnavailable("PRIVATE_KEY is not configured")

    call = contract.functions.anchorCheckpoint(
        checkpoint_id, int(from_seq), int(to_seq), bytes.fromhex(merkle_root_hex)
    )
    tx_hash_hex: str | None = None
    try:
        with _send_lock:
            account = w3.eth.account.from_key(private_key)
            try:
                call.call({"from": account.address})  # dry run: would it revert?
            except Exception as exc:
                raise AnchorRejected(f"contract would reject the checkpoint ({type(exc).__name__})") from None
            transaction = call.build_transaction({
                "chainId": config.CHAIN_ID,
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            })
            signed = w3.eth.account.sign_transaction(transaction, private_key=private_key)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash_hex = _hex(w3.eth.send_raw_transaction(raw))
        try:
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash_hex, timeout=config.CHAIN_TX_TIMEOUT_SECONDS
            )
        except Exception as exc:
            raise AnchorFailed(f"no receipt within timeout ({type(exc).__name__})", tx_hash_hex) from None
        if int(receipt.status) != 1:
            raise AnchorFailed("transaction reverted on-chain (receipt status 0)", tx_hash_hex)
    except AnchorError:
        raise
    except Exception as exc:
        raise AnchorFailed(f"transaction submission failed ({type(exc).__name__})", tx_hash_hex) from None
    finally:
        private_key = None  # drop the reference promptly

    tx = "0x" + tx_hash_hex
    logger.info("Anchored checkpoint %s on chain %s (tx %s...)", checkpoint_id, config.CHAIN_ID, tx[:14])
    return {"tx_hash": tx, "block_number": int(receipt.blockNumber), "chain_id": config.CHAIN_ID}


def read_checkpoint(checkpoint_id: str) -> dict[str, Any] | None:
    """On-chain checkpoint, or ``None`` if it does not exist. Raises
    AnchorUnavailable when the chain cannot be reached."""
    _, contract = _require_connection()
    try:
        if not contract.functions.checkpointExists(checkpoint_id).call():
            return None
        from_seq, to_seq, root, timestamp, block_number = (
            contract.functions.getCheckpoint(checkpoint_id).call()
        )
    except Exception as exc:
        raise AnchorUnavailable(f"on-chain read failed ({type(exc).__name__})") from None
    return {"from_seq": int(from_seq), "to_seq": int(to_seq), "merkle_root": _hex(root),
            "timestamp": int(timestamp), "block_number": int(block_number)}
