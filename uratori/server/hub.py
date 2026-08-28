"""The websocket hub: who is watching what, and delivery to them.

The socket carries exactly what the routes carry -- `Result` and
`BundleResult` objects, byte for byte the ones the results routes return.
What it adds is only *when*: a subscription entry is answered the moment it
is made, and again whenever a pass says it moved. There is no socket-only
shape, because a second shape is where republishing steps -- and with them
duplicate arithmetic -- come back.

Two modes of interest coexist on one client, deliberately:

- **The firehose** (a bare subscribe, the original contract): every re-served
  answer of every pass, at the serving defaults.
- **Entries** (a named subscribe): standing GETs. Each is delivered only when
  the pass's `moved` set names it, evaluated at the entry's own arguments --
  and evaluated once per distinct entry, however many clients hold it.

An entry narrows which answers travel to a client, never the population
inside any answer: every result is served whole by the same code that serves
it over HTTP, and the hub only decides who hears it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from fastapi import WebSocket

from ..results import BundleResult, Result
from ..windows import WindowSpec, window_token
from .contract import Envelope

log = logging.getLogger("uratori.stream")

EntryKey = tuple[str, tuple[str, ...]]
"""A subscription entry's identity: the definition name, plus the canonical
spelling of its window arguments (empty when the entry named none). Canonical
so `over 7` and `over 1-7` cannot become two entries answering one question,
and so unsubscribe removes what subscribe added whatever the client's exact
spelling was."""


@dataclass(frozen=True)
class Entry:
    """One standing GET: a name, and the windows to serve it over (None means
    the serving defaults decide, exactly as the HTTP route treats an absent
    `trailing`)."""

    name: str
    windows: tuple[WindowSpec, ...] | None

    def key(self) -> EntryKey:
        return (
            self.name,
            tuple(window_token(spec) for spec in self.windows or ()),
        )


@dataclass
class Client:
    socket: WebSocket
    tenant_id: str | None = None
    firehose: bool = False
    entries: dict[EntryKey, Entry] = field(default_factory=dict)


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

    async def publish(
        self, tenant_id: str, results: Iterable[Result | BundleResult]
    ) -> None:
        """One envelope per moved definition, to every firehose subscriber.

        Per definition rather than one batched message, because a client stores
        results keyed by name and a batch would be a second shape to unpack.
        Entry subscribers are deliberately not here: their answers are
        evaluated at their own arguments by `serve_entries`, and handing them
        the default-argument copies too would deliver every name twice with
        the two disagreeing about the windows.
        """
        async with self._lock:
            watching = [
                c for c in self.clients if c.tenant_id == tenant_id and c.firehose
            ]
        for result in results:
            for client in watching:
                await self.send(
                    client, Envelope(type="result", tenant=client.tenant_id, result=result)
                )

    async def serve_entries(
        self,
        tenant_id: str,
        moved: frozenset[str],
        evaluate: Callable[[Entry], Awaitable[Result | BundleResult | None]],
    ) -> None:
        """Deliver each watched-and-moved entry, evaluated once per identity.

        The intersection is the whole design: an impacted-but-unwatched name
        is never evaluated (the lazy payoff), and a watched-but-unimpacted
        entry stays silent (the flicker guard). One evaluation fans out to
        every client holding the same entry, so a popular tile costs one
        serve, not one per subscriber.

        An evaluation failing is reported to exactly the clients that asked
        for that entry, by name -- a silent skip would be a subscription that
        quietly stopped meaning anything.
        """
        async with self._lock:
            holders: dict[EntryKey, tuple[Entry, list[Client]]] = {}
            for client in self.clients:
                if client.tenant_id != tenant_id:
                    continue
                for key, entry in client.entries.items():
                    if entry.name not in moved:
                        continue
                    held = holders.get(key)
                    if held is None:
                        holders[key] = (entry, [client])
                    else:
                        held[1].append(client)
        for _key, (entry, clients) in holders.items():
            try:
                result = await evaluate(entry)
            except Exception as failure:
                log.exception("serving subscription entry %s failed", entry.name)
                for client in clients:
                    await self.send(
                        client,
                        Envelope(
                            type="error",
                            tenant=tenant_id,
                            name=entry.name,
                            message=str(failure),
                        ),
                    )
                continue
            if result is None:
                continue
            for client in clients:
                await self.send(
                    client, Envelope(type="result", tenant=tenant_id, result=result)
                )

    def wants_everything(self, tenant_id: str) -> bool:
        """Whether any firehose subscriber is on this tenant -- what decides
        if a `serve: false` pass must still evaluate the full default results
        for the socket's sake."""
        return any(c.tenant_id == tenant_id and c.firehose for c in self.clients)

    def watching(self, tenant_id: str) -> int:
        """How many clients are on a tenant. Exposed so a test can assert a push
        was actually delivered rather than merely attempted."""
        return sum(1 for c in self.clients if c.tenant_id == tenant_id)
