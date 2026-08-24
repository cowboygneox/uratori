"""The front door: one object that runs the engine and serves its answers.

A host constructs this with the four things it declared -- schema, compiled
library, engine store, fact source -- and gets three verbs:

- `execute`: facts moved (or a full pass is due); recompute what followed from
  them, cascading through every figure built on a figure that moved.
- `results`: the current `Result` for definitions worth serving, the same
  object a route would return and a listener receives.
- `answer`: one definition by name, for a host wiring a request path.

`run` is `execute` + `results` + listener dispatch in the one correct order,
for hosts that do not need to interleave their own bookkeeping between the
steps. A host that must record the pass before responses are built (so a
failure rendering them still leaves the run on the record) calls the two halves
itself -- that ordering is a host decision, and bundling it here would take it
away.

**Listeners receive the same objects `results` returns.** There is no
listener-only shape, deliberately: a second shape is a second contract, and a
second contract is where a hand-written republishing step -- and with it
duplicate arithmetic -- comes back.

Settings arrive sparse and are completed over `schema.defaults` here, at the
boundary, exactly once. Everything below assumes a complete document and
raises on a missing dial rather than guessing.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .engine.change import Outcome
from .engine.engine import Engine
from .engine.serve import answer_projection, serve_figure, serve_reading
from .lang.plan import Library
from .results import Result
from .schema import Schema
from .store import EngineStore, FactSource

log = logging.getLogger("uratori")

Listener = Callable[[str, Outcome, tuple[Result, ...]], Awaitable[None] | None]
"""(tenant, what moved, the re-served answers). Sync or async, the host's choice."""

DEFAULT_TRAILING: tuple[int, ...] = (30, 14, 7)
"""The windows a reading is served over when the host does not say.

Which windows exist is presentation, not calculation -- a reading's statistics,
minimums and band are hashed into its version, and a window only narrows which
stored days take part. That is the only reason this may be a default at all.
"""


@dataclass(frozen=True)
class RunReport:
    outcome: Outcome
    results: tuple[Result, ...]


class Uratori:
    def __init__(
        self,
        *,
        schema: Schema,
        library: Library,
        store: EngineStore,
        facts: FactSource,
    ) -> None:
        self._schema = schema
        self._library = library
        self._store = store
        self._facts = facts
        self._engine = Engine(store, facts, library, schema)
        self._listeners: list[Listener] = []

    # ---------------------------------------------------------- listeners --

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Attach a listener; the return value detaches it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    async def _notify(self, tenant: str, outcome: Outcome, results: tuple[Result, ...]) -> None:
        """Deliver to every listener, isolating each.

        A listener raising must not break the run or starve the listeners after
        it: the values are already committed by the time delivery starts, and
        taking the engine down to protect a delivery would be backwards -- the
        same call the host project made about its activity log.
        """
        for listener in list(self._listeners):
            try:
                out = listener(tenant, outcome, results)
                if inspect.isawaitable(out):
                    await out
            except Exception:
                log.exception("a listener raised; the run is unaffected")

    # --------------------------------------------------------------- runs --

    async def execute(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        written: Mapping[str, Sequence[str]] | None = None,
        deleted: Mapping[str, Sequence[str]] | None = None,
        full: bool = False,
    ) -> Outcome:
        """One engine pass. Raises on failure -- it never reports "nothing changed".

        Two escalations to a full pass happen here, at the boundary, because
        each is a way the warm path can be silently wrong and neither should
        depend on the caller remembering:

        **A pass that observed a deletion runs full, whatever shape it had.**
        The warm path honours `deleted`, but the cold branch -- the ordinary
        state between a deploy or a settings save and the next sync -- never
        reads it, so a stale pointer would drop the list on the floor and the
        deleted record keeps its index memberships until the next full pass.
        Full is correct in every branch by construction, and a full recompute
        is a fair price on the rare pass that reports something gone.

        **A moved record of a kind that indexes only resolve *through* runs
        full, for the same reason from the other direction.** Such a kind (the
        origin project's person fact) is how other records' values map to
        bucket keys, but no index is over the kind itself -- so the warm path
        sees the write and rebuilds nothing, and every record that resolves
        through the moved row stays filed under the old answer until the next
        full pass. These moves are rare (operator actions, identity changes);
        the recompute is the same fair price.
        """
        through_kinds = self._through_kinds()
        escalate = bool(deleted and any(deleted.values())) or any(
            kind in through_kinds and keys for kind, keys in (written or {}).items()
        )
        return await self._engine.run(
            tenant,
            self._schema.settings_for(settings),
            written=written,
            deleted=deleted,
            full=full or escalate,
        )

    def _through_kinds(self) -> frozenset[str]:
        from .lang.check import _index_fields

        return frozenset(
            part.through.kind
            for index in self._library.indexes.values()
            for part in _index_fields(index.spec)
            if part.through is not None
        )

    async def run(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        written: Mapping[str, Sequence[str]] | None = None,
        deleted: Mapping[str, Sequence[str]] | None = None,
        full: bool = False,
        trailing: Sequence[int] = DEFAULT_TRAILING,
    ) -> RunReport:
        """A pass, its re-served answers, and delivery, in that order.

        Listeners are notified only when there is something to hear: a change,
        or a served result (a projection re-serves on every pass because the
        clock is one of its inputs). A poll in which nothing happened notifying
        every listener is how listeners stop being read.
        """
        outcome = await self.execute(
            tenant, settings, written=written, deleted=deleted, full=full
        )
        results = await self.results(
            tenant,
            settings,
            touched={change.figure for change in outcome.changes},
            trailing=trailing,
        )
        if outcome.changes or results:
            await self._notify(tenant, outcome, results)
        return RunReport(outcome=outcome, results=results)

    # ------------------------------------------------------------- serving --

    async def results(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        touched: set[str] | None = None,
        trailing: Sequence[int] = DEFAULT_TRAILING,
    ) -> tuple[Result, ...]:
        """The current answers: touched figures and their readings, or all of
        them when `touched` is None (a client's first paint).

        **Every projection, every time, with no `touched` gate**, and that is
        not laziness -- it is the only correct rule. A projection stores nothing
        and is evaluated at the instant it is asked, so its rows move for three
        reasons: a record of its kind changed, a figure it reads changed, or
        *the clock advanced*. The third is always true, so there is no gate that
        could be right -- and the failure of a wrong gate is silent: figures
        moving over a socket while the list they are attached to, its ranking
        and every sentence on it stay as they were at the last page load.
        """
        document = self._schema.settings_for(settings)
        lib = self._library
        out: list[Result] = []

        for plan in lib.figures:
            if plan.day_keyed or plan.across is not None:
                # Both serve by name (`answer`) -- day or dimension rows for a
                # host's evidence pane -- but this is the bulk surface a pass
                # pushes and a first paint reads, and no screen subscribes to
                # those rows: what a card wants is the reading or the rollup,
                # and both are below. Shipping every stored person-day here
                # would spend every pass on history nobody is watching.
                continue
            if touched is not None and plan.name not in touched:
                continue
            out.append(await serve_figure(self._store, lib, tenant, plan, document))

        for reading in lib.readings:
            if reading.mode != "window" or reading.source is None:
                continue
            if touched is not None and reading.source not in touched:
                continue
            out.append(
                await serve_reading(
                    self._store, lib, tenant, reading, document, list(trailing)
                )
            )

        for projection in lib.projections:
            out.append(
                await answer_projection(
                    self._store, self._facts, lib, tenant, projection, document
                )
            )

        return tuple(out)

    async def answer(
        self,
        tenant: str,
        name: str,
        settings: Mapping[str, Any] | None = None,
        *,
        trailing: Sequence[int] = DEFAULT_TRAILING,
    ) -> Result | None:
        """One definition's current answer, by name. None when nothing is
        called that; a live reading raises, because "no such definition" and
        "not built yet" send a caller to different fixes."""
        document = self._schema.settings_for(settings)
        lib = self._library

        figure = lib.figure(name)
        if figure is not None:
            # Every figure serves, whatever its shape. Day-keyed and split ones
            # were refused for a release, and the origin project's Data screen
            # wore the 400 as its values panel -- on the page whose whole claim
            # is that the evidence is visible. Their rows carry the day or the
            # dimension in `dimension`; what still never travels is a numeric
            # list (`_wire`).
            return await serve_figure(self._store, lib, tenant, figure, document)

        reading = lib.reading(name)
        if reading is not None:
            if reading.mode == "live":
                raise NotImplementedError("live readings are not servable yet")
            return await serve_reading(
                self._store, lib, tenant, reading, document, list(trailing)
            )

        projection = lib.projection(name)
        if projection is not None:
            return await answer_projection(
                self._store, self._facts, lib, tenant, projection, document
            )

        summary = lib.summary(name)
        if summary is not None:
            # A summary is answered by evaluating the projection it is over and
            # handing back what came with it. There is no cheaper path and there
            # should not be one: the counts are *defined* as being over the
            # rows, so a second route that computed them another way would be
            # duplicate arithmetic wearing a shortcut's name.
            over = lib.projection(summary.over)
            if over is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{name} summarises a projection that is not compiled")
            return await answer_projection(
                self._store, self._facts, lib, tenant, over, document
            )

        return None
