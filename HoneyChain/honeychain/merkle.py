"""
honeychain.merkle
=================
Deterministic binary Merkle tree over chain-event hashes (Phase 6).

Construction (documented so anyone can re-implement the verifier):

* leaf  = SHA-256( 0x00 || event_hash_bytes )
* node  = SHA-256( 0x01 || left || right )
* A level with an odd count PROMOTES its last node unchanged to the next
  level (it is NOT duplicated).

The 0x00/0x01 prefixes (domain separation, as in RFC 6962 Certificate
Transparency) make it impossible to present an internal node as a leaf -- a
second-preimage attack. Promoting instead of duplicating avoids the
duplicate-leaf ambiguity behind Bitcoin's CVE-2012-2459, where two different
leaf lists could produce the same root.

A proof is a list of ``{"side": "L"|"R", "hash": hex}`` steps from the leaf
up; ``side`` says where the sibling sits. Promoted levels add no step.
"""

from __future__ import annotations

import hashlib
import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check(hex_hash: str) -> bytes:
    if not isinstance(hex_hash, str) or not _HEX64.match(hex_hash):
        raise ValueError("expected a 64-character lowercase hex SHA-256")
    return bytes.fromhex(hex_hash)


def leaf_hash(event_hash_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + _check(event_hash_hex)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(event_hashes: list[str]) -> list[list[bytes]]:
    if not event_hashes:
        raise ValueError("cannot build a Merkle tree over zero events")
    level = [leaf_hash(h) for h in event_hashes]
    levels = [level]
    while len(level) > 1:
        nxt = [node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])  # promote, never duplicate
        levels.append(nxt)
        level = nxt
    return levels


def merkle_root(event_hashes: list[str]) -> str:
    return _levels(event_hashes)[-1][0].hex()


def merkle_proof(event_hashes: list[str], index: int) -> list[dict[str, str]]:
    if not 0 <= index < len(event_hashes):
        raise IndexError("leaf index out of range")
    proof: list[dict[str, str]] = []
    for level in _levels(event_hashes)[:-1]:
        sibling = index ^ 1
        if sibling < len(level):
            proof.append({"side": "L" if sibling < index else "R", "hash": level[sibling].hex()})
        index //= 2
    return proof


def verify_proof(event_hash_hex: str, proof: list[dict[str, str]], root_hex: str) -> bool:
    """True iff ``event_hash_hex`` is included under ``root_hex``. Never raises
    on malformed input -- returns False."""
    try:
        current = leaf_hash(event_hash_hex)
        for step in proof:
            sibling = _check(step["hash"])
            if step["side"] == "L":
                current = node_hash(sibling, current)
            elif step["side"] == "R":
                current = node_hash(current, sibling)
            else:
                return False
        return current.hex() == _check(root_hex).hex()
    except (ValueError, KeyError, TypeError):
        return False
