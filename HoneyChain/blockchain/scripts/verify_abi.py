#!/usr/bin/env python3
"""
verify_abi.py -- check HoneyChainRegistry.sol against honeychain/abi.json.

    python3 scripts/verify_abi.py

Why this exists
---------------
The project previously carried an `abi.json` declaring `createBatch/getBatch`
alongside a `blockchain.js` declaring `recordHash/getHash`. Nothing checked
that either matched a real contract, so the drift went unnoticed. This script
parses the Solidity source directly and compares its external interface with
the ABI the Python service loads.

Two modes:

* **source mode** (default) parses the `.sol` file. No compiler needed, so it
  runs in CI and offline. It checks names, parameter types, return types and
  mutability -- enough to catch drift, though it is not a substitute for
  compiling.
* **artifact mode** (`--artifact`) compares the *compiled* ABI from
  `artifacts/` against `honeychain/abi.json`. Use this after `npx hardhat
  compile` for an exact check.

Exit code 0 on success, 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CONTRACTS_DIR.parent
SOURCE = CONTRACTS_DIR / "contracts" / "HoneyChainRegistry.sol"
PYTHON_ABI = REPO_ROOT / "honeychain" / "abi.json"
ARTIFACT = CONTRACTS_DIR / "artifacts" / "contracts" / "HoneyChainRegistry.sol" / "HoneyChainRegistry.json"

# Solidity aliases that the compiler canonicalises in the ABI.
CANONICAL = {"uint": "uint256", "int": "int256", "byte": "bytes1"}


def canonical_type(text: str) -> str:
    """Reduce a declared parameter to its canonical ABI type."""
    text = text.strip()
    # Drop data location and visibility keywords, keep the type and array suffix.
    for keyword in ("calldata", "memory", "storage", "payable", "indexed"):
        text = re.sub(rf"\b{keyword}\b", " ", text)
    parts = text.split()
    if not parts:
        return ""
    base = parts[0]
    return CANONICAL.get(base, base)


def split_params(block: str) -> list[str]:
    """Split a parameter list on top-level commas."""
    params, depth, current = [], 0, ""
    for char in block:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            params.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        params.append(current)
    return [p for p in params if p.strip()]


def parse_source(path: Path) -> dict[str, dict]:
    """Extract external/public functions and events from Solidity source."""
    text = path.read_text(encoding="utf-8")
    # Strip comments so commented-out signatures are not picked up.
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    interface: dict[str, dict] = {}

    func_pattern = re.compile(
        r"function\s+(\w+)\s*\(([^)]*)\)\s*([^{;]*?)(?:returns\s*\(([^)]*)\))?\s*[{;]",
        re.DOTALL,
    )
    for match in func_pattern.finditer(text):
        name, params, modifiers, returns = match.groups()
        if not re.search(r"\b(external|public)\b", modifiers or ""):
            continue  # internal/private functions are not in the ABI
        mutability = "nonpayable"
        for candidate in ("view", "pure", "payable"):
            if re.search(rf"\b{candidate}\b", modifiers or ""):
                mutability = candidate
                break
        interface[f"function:{name}"] = {
            "kind": "function",
            "name": name,
            "inputs": [canonical_type(p) for p in split_params(params)],
            "outputs": [canonical_type(p) for p in split_params(returns or "")],
            "mutability": mutability,
        }

    for match in re.finditer(r"event\s+(\w+)\s*\(([^)]*)\)\s*;", text, re.DOTALL):
        name, params = match.groups()
        interface[f"event:{name}"] = {
            "kind": "event",
            "name": name,
            "inputs": [canonical_type(p) for p in split_params(params)],
        }

    # Public state variables get an implicit getter in the ABI.
    for match in re.finditer(r"^\s*(mapping\([^)]*\)|\w+)\s+public\s+(\w+)\s*;", text, re.M):
        _type, name = match.groups()
        interface.setdefault(f"function:{name}", {
            "kind": "function", "name": name,
            "inputs": [], "outputs": ["?"], "mutability": "view", "implicit_getter": True,
        })

    return interface


def parse_abi(path: Path) -> dict[str, dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        # A Hardhat artifact wraps the ABI: {"contractName": ..., "abi": [...]}.
        # Artifact mode previously passed the wrapper itself and crashed.
        entries = entries.get("abi", [])
    interface: dict[str, dict] = {}
    for entry in entries:
        kind = entry.get("type")
        if kind not in ("function", "event"):
            continue
        record = {
            "kind": kind,
            "name": entry["name"],
            "inputs": [i["type"] for i in entry.get("inputs", [])],
        }
        if kind == "function":
            record["outputs"] = [o["type"] for o in entry.get("outputs", [])]
            record["mutability"] = entry.get("stateMutability", "nonpayable")
        interface[f"{kind}:{entry['name']}"] = record
    return interface


def compare(source: dict, abi: dict) -> tuple[list[str], list[str]]:
    """Return (errors, notes). The source must be a superset of the ABI."""
    errors: list[str] = []
    notes: list[str] = []

    for key, expected in sorted(abi.items()):
        actual = source.get(key)
        if actual is None:
            errors.append(
                f"MISSING: abi.json declares {key.replace(':', ' ')}(...) "
                f"but the contract does not implement it"
            )
            continue
        if actual.get("implicit_getter"):
            # A compiler-generated getter for a public state variable: the
            # static parse cannot see its signature (it records "?"), so only
            # existence is checkable here. --artifact mode checks it exactly.
            notes.append(f"getter: {expected['name']} exists (signature checked only with --artifact)")
            continue
        if actual["inputs"] != expected["inputs"]:
            errors.append(
                f"PARAMS:  {expected['name']} expects ({', '.join(expected['inputs'])}) "
                f"but source has ({', '.join(actual['inputs'])})"
            )
        if expected["kind"] == "function":
            if actual.get("outputs") != expected.get("outputs"):
                errors.append(
                    f"RETURNS: {expected['name']} expects "
                    f"({', '.join(expected.get('outputs', []))}) but source has "
                    f"({', '.join(actual.get('outputs', []))})"
                )
            if actual.get("mutability") != expected.get("mutability"):
                errors.append(
                    f"MUTABLE: {expected['name']} expects {expected.get('mutability')} "
                    f"but source is {actual.get('mutability')}"
                )

    for key in sorted(set(source) - set(abi)):
        notes.append(f"additive: {key.replace(':', ' ')} is new in the contract")

    return errors, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact", action="store_true",
        help="compare the compiled artifact ABI instead of parsing the source",
    )
    args = parser.parse_args()

    if not PYTHON_ABI.is_file():
        print(f"FAIL: {PYTHON_ABI} not found")
        return 1

    if args.artifact:
        if not ARTIFACT.is_file():
            print(f"FAIL: {ARTIFACT} not found -- run `npx hardhat compile` first")
            return 1
        source = parse_abi(Path(ARTIFACT))
        origin = "compiled artifact"
    else:
        if not SOURCE.is_file():
            print(f"FAIL: {SOURCE} not found")
            return 1
        source = parse_source(SOURCE)
        origin = "Solidity source (static parse)"

    abi = parse_abi(PYTHON_ABI)
    errors, notes = compare(source, abi)

    print(f"Comparing {origin}")
    print(f"  contract : {SOURCE.name if not args.artifact else ARTIFACT.name}")
    print(f"  against  : honeychain/abi.json ({len(abi)} entries)\n")

    for key, entry in sorted(abi.items()):
        status = "OK  " if not any(entry["name"] in e for e in errors) else "FAIL"
        signature = f"{entry['name']}({', '.join(entry['inputs'])})"
        print(f"  [{status}] {entry['kind']:<8} {signature}")

    if notes:
        print("\nAdditive members (present in the contract, not in abi.json):")
        for note in notes:
            print(f"  {note}")
        print(
            "\n  These are fine -- the compiled ABI is a superset. Regenerate\n"
            "  honeychain/abi.json from artifacts/ after compiling; deploy.js\n"
            "  does this automatically."
        )

    if errors:
        print(f"\n{len(errors)} MISMATCH(ES) -- the Python service would fail at runtime:\n")
        for error in errors:
            print(f"  {error}")
        return 1

    print("\nPASS: every function and event the Python service calls is implemented,")
    print("      with matching parameter types, return types and mutability.")
    if not args.artifact:
        print("\nNote: this is a static parse, not a compile. Run `npx hardhat compile`")
        print("      and re-run with --artifact for an exact check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
