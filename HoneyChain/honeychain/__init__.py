"""
HoneyChain -- blockchain-based honey traceability and smart beekeeping management.

SIH 2026, Problem Statement 26021.

The package is import-safe: importing it performs no network calls, opens no
database connection and reads no credentials. All of that happens inside
:func:`honeychain.app.create_app`.
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["__version__", "create_app"]


def create_app(*args, **kwargs):
    """Lazy re-export of the application factory (keeps ``import honeychain`` cheap)."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
