"""
honeychain.events
=================
In-process publish/subscribe broker backing ``GET /api/telemetry/stream``.

Each connected browser gets its own bounded ``queue.Queue``. Publishing walks
the subscriber set under a lock and does a non-blocking put; a client that has
stopped draining (closed laptop, throttled tab) overflows its queue and is
dropped rather than being allowed to grow memory without bound.

The stream generator blocks on ``Queue.get(timeout=...)`` -- there is no busy
loop and no polling of the database. The timeout doubles as the keepalive tick.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Iterator

from . import config

logger = logging.getLogger(__name__)


class EventBroker:
    """Fan-out of telemetry events to every connected SSE client."""

    def __init__(self, queue_size: int, max_clients: int) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._queue_size = queue_size
        self._max_clients = max_clients
        self._published = 0

    def subscribe(self) -> queue.Queue | None:
        """Register a client. Returns None when the connection cap is reached."""
        with self._lock:
            if len(self._subscribers) >= self._max_clients:
                logger.warning(
                    "SSE connection refused: %d clients already attached",
                    len(self._subscribers),
                )
                return None
            client: queue.Queue = queue.Queue(maxsize=self._queue_size)
            self._subscribers.add(client)
            logger.info("SSE client connected (%d active)", len(self._subscribers))
            return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(client)
            logger.info("SSE client disconnected (%d active)", len(self._subscribers))

    def publish(self, event: str, payload: Any) -> None:
        """Broadcast one event. Never blocks and never raises to the caller."""
        message = {"event": event, "payload": payload}
        with self._lock:
            self._published += 1
            stale: list[queue.Queue] = []
            for client in self._subscribers:
                try:
                    client.put_nowait(message)
                except queue.Full:
                    stale.append(client)
            for client in stale:
                self._subscribers.discard(client)
        if stale:
            logger.warning("Dropped %d unresponsive SSE client(s)", len(stale))

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def published_count(self) -> int:
        return self._published

    def stream(self) -> Iterator[str]:
        """Yield a text/event-stream body for one client.

        Emits an initial ``ready`` event so the browser can flip its indicator
        immediately, then blocks for real events, emitting a comment keepalive
        whenever the wait times out. Closing the generator (client disconnect,
        server shutdown) unsubscribes in the ``finally`` block.
        """
        client = self.subscribe()
        if client is None:
            yield _format_sse(
                {"event": "error", "payload": {"message": "stream capacity reached"}}
            )
            return

        try:
            # Tell the browser to reconnect after 3s if the connection drops.
            yield "retry: 3000\n\n"
            yield _format_sse({"event": "ready", "payload": {"status": "connected"}})
            while True:
                try:
                    message = client.get(timeout=config.SSE_KEEPALIVE_SECONDS)
                except queue.Empty:
                    # Comment frame: keeps proxies and the browser from timing out
                    # without being delivered to onmessage handlers.
                    yield ": keepalive\n\n"
                    continue
                yield _format_sse(message)
        except GeneratorExit:  # client went away
            raise
        finally:
            self.unsubscribe(client)


def _format_sse(message: dict[str, Any]) -> str:
    """Serialise one SSE frame. ``default=str`` keeps non-JSON types from raising."""
    return "data: " + json.dumps(message, default=str) + "\n\n"


#: Process-wide broker instance.
broker = EventBroker(
    queue_size=config.SSE_CLIENT_QUEUE_SIZE,
    max_clients=config.SSE_MAX_CLIENTS,
)
