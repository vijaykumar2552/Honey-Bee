#!/usr/bin/env python3
"""
Run the whole HoneyChain test suite.

    python tests/run_tests.py            # from the repository root
    python -m unittest discover -s tests -t .

Uses only the standard library -- no pytest required.
"""

from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    # Flask's test client leaves static-file handles for the GC to reap; that
    # is not a defect under test, so keep the report readable.
    warnings.simplefilter("ignore", ResourceWarning)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
