#!/usr/bin/env python3
"""
HoneyChain -- production entry point.
SIH 2026, Problem Statement 26021.

Development::

    python app.py

Production (Gunicorn + gevent, run from the repository root)::

    gunicorn --workers 4 --worker-class gevent --bind 0.0.0.0:5000 app:app

``gevent`` is required rather than optional: the SSE endpoint holds one
long-lived connection per dashboard tab, and the default synchronous worker
would dedicate an entire worker process to each of them.

The module-level ``app`` object below is what Gunicorn imports.
"""

from __future__ import annotations

from honeychain import config
from honeychain.app import create_app

#: WSGI callable. Built at import time so `app:app` resolves under Gunicorn.
app = create_app()


if __name__ == "__main__":
    # threaded=True lets the development server serve the SSE stream and normal
    # requests at the same time. Never enable the reloader alongside SSE: it
    # duplicates the process and therefore the in-memory event broker.
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        threaded=True,
        use_reloader=False,
    )
