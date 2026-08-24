"""The websocket hub: who is watching which tenant, and delivery to them.

The socket carries exactly what the routes carry -- `Result` objects, byte for
byte the ones `GET /tenants/{t}/results` returns. What it adds is only *when*:
on subscribe a client gets the current answer for every definition, and
thereafter one whenever a pass says it moved. There is no socket-only shape,
because a second shape is where republishing steps -- and with them duplicate
arithmetic -- come back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import WebSocket

from ..results import Result
from .contract import Envelope

log = logging.getLogger("uratori.stream")


@dataclass
class Client:
    socket: WebSocket
    tenant_id: str | None = None


@dataclass
class Hub:
    clients: list[Client] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def join(self, client: Client) -> None:
        async with self._lock:
            self.clients.append(client)

    async def leave(self, client: Client) -> None:
        async with self._lock:
            if client in self.clients:
                self.clients.remove(client)

    async def send(self, client: Client, envelope: Envelope) -> None:
        try:
            await client.socket.send_text(envelope.model_dump_json(exclude_none=True))
        except Exception:
            # A client that cannot be written to has gone; delivery to the
            # others must not die with it.
            await self.leave(client)

    async def publish(self, tenant_id: str, results: Iterable[Result]) -> None:
        """One envelope per moved definition, to whoever is watching.

        Per definition rather than one batched message, because a client stores
        results keyed by name and a batch would be a second shape to unpack.
        """
        async with self._lock:
            watching = [c for c in self.clients if c.tenant_id == tenant_id]
        for result in results:
            for client in watching:
                await self.send(
                    client, Envelope(type="result", tenant=client.tenant_id, result=result)
                )

    def watching(self, tenant_id: str) -> int:
        """How many clients are on a tenant. Exposed so a test can assert a push
        was actually delivered rather than merely attempted."""
        return sum(1 for c in self.clients if c.tenant_id == tenant_id)
