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
from .engine.serve import (
    answer_bundle,
    answer_projection,
    serve_evidence,
    serve_figure,
    serve_reading,
)
from .lang.plan import Library
from .lang.settings import fingerprint as settings_fingerprint
from .lang.source import declaration_source
from .results import BundleResult, Evidence, Result
from .schema import Schema
from .store import EngineStore, FactSource, Pointer
from .verify import verify_writes

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
        # A fact-taught library carries the world; the declared schema then
        # holds only settings and defaults. Completing it here, once, is what
        # keeps every consumer below -- the engine freezing labels, evidence
        # resolving links, `verify` checking kinds -- reading one world.
        self._schema = schema.taught_by(library)
        self._library = library
        self._store = store
        self._facts = facts
        self._engine = Engine(store, facts, library, self._schema)
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

    # ------------------------------------------------------------- writes --

    def verify(
        self,
        writes: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
        deletes: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """A batch against the world, before anything lands.

        Raises `FactError` naming the kind, key and field. The engine never
        writes facts, so the host (or the service's facts route) calls this
        at the door -- storage is the host's, but what a stored record may
        look like is the declaration's.
        """
        verify_writes(self._library, self._schema.kinds, writes, deletes)

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
        touched = {change.figure for change in outcome.changes}
        # A pass through the facts door (or `full`, or deleting) is the
        # host's sync moment: every projection re-serves on it, clock
        # refresh included -- and `is not None`, not truthiness, because a
        # scheduled sync whose batch deduplicated to nothing is still the
        # sync, and gating on the batch's contents would freeze every
        # clock-worded sentence exactly when the data goes quiet. A
        # definition-only pass is a deploy step whose reach is known with
        # certainty, so it serves exactly what the change can touch, and a
        # change that reaches nothing serves nothing and wakes nobody.
        sync = full or written is not None or deleted is not None
        document = self._schema.settings_for(settings)
        stamps = self._serve_stamps(document)
        if sync:
            projections = None
        else:
            held = await self._store.pointers(tenant)
            projections = self._reached(outcome.reindexed, touched, stamps, held)
        results = await self._serve(
            tenant,
            settings,
            touched=touched,
            projections=projections,
            trailing=trailing,
        )
        if outcome.changes or results:
            await self._notify(tenant, outcome, results)
        # The stamps settle only after the answers actually went out: a crash
        # between serving and stamping re-serves next pass, which is the safe
        # direction. Settled on sync passes too, or the first definition-only
        # pass after a sync would re-serve everything the sync already sent.
        await self._settle_serve_stamps(tenant, stamps)
        return RunReport(outcome=outcome, results=results)

    def _serve_stamps(self, document: Mapping[str, Any]) -> dict[str, Pointer]:
        """What each projection and summary currently serves under.

        The same two-part discipline as every other pointer in the engine:
        the declaration's version, and a fingerprint of the dials that can
        move its *rendered* answer without moving any stored value -- its own
        `value:`/`omit:`/flag dials, and the band dials of every figure it
        binds `band of` (a band is worded at serve time, deliberately outside
        the figure's compute fingerprint). This is what lets a definition-only
        pass know, rather than guess, that an edit or a settings save reached
        a projection: nothing about a projection is stored, so without a
        stamp there is nothing to compare and the only honest alternatives
        are serve-everything (the fixed tail this replaces) or a silent
        freeze (the bug three reviewers found in the first attempt).
        """
        lib = self._library
        stamps: dict[str, Pointer] = {}
        for plan in lib.projections:
            dials = set(plan.settings)
            for _, figure, _, band in plan.reads:
                if band:
                    read = lib.figure(figure)
                    if read is not None:
                        dials.update(read.band_settings)
            stamps[plan.name] = Pointer(
                version=plan.version,
                settings_fingerprint=settings_fingerprint(dict(document), sorted(dials)),
            )
        for summary in lib.summaries:
            stamps[summary.name] = Pointer(
                version=summary.version,
                settings_fingerprint=settings_fingerprint(
                    dict(document), list(summary.settings)
                ),
            )
        return stamps

    def _reached(
        self,
        reindexed: Sequence[str],
        moved: set[str],
        stamps: Mapping[str, Pointer],
        held: Mapping[str, Pointer],
    ) -> set[str]:
        """The projections a definition-only pass can have moved.

        Four ways exist, and all four are consulted: a grouping it filters
        through was re-bucketed; a figure it reads changed value; its own
        text (or a summary's over it) moved; a dial it renders under moved.
        The last two compare serve stamps -- see `_serve_stamps`. Everything
        outside the four is certain to serve the same rows it served before:
        the clock excepted, and a deploy is not the moment the clock contract
        pays out (the sync is)."""
        rebuilt = set(reindexed)
        out: set[str] = set()
        for plan in self._library.projections:
            reads = set(plan.figures) | {name for _, name, _, _ in plan.reads}
            if (
                set(plan.indexes) & rebuilt
                or reads & moved
                or held.get(plan.name) != stamps[plan.name]
            ):
                out.add(plan.name)
        for summary in self._library.summaries:
            if held.get(summary.name) != stamps[summary.name]:
                out.add(summary.over)
        return out

    async def _settle_serve_stamps(
        self, tenant: str, stamps: Mapping[str, Pointer]
    ) -> None:
        lib = self._library
        for plan in lib.projections:
            await self._store.ensure_definition(
                plan.version,
                plan.name,
                "projection",
                plan.doc,
                "",
                declaration_source(lib, plan.name) or "",
                {"kind": plan.kind},
            )
            await self._store.set_pointer(tenant, plan.name, stamps[plan.name])
        for summary in lib.summaries:
            await self._store.ensure_definition(
                summary.version,
                summary.name,
                "summary",
                summary.doc,
                "",
                declaration_source(lib, summary.name) or "",
                {"over": summary.over},
            )
            await self._store.set_pointer(tenant, summary.name, stamps[summary.name])

    # ------------------------------------------------------------- serving --

    async def results(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        touched: set[str] | None = None,
        trailing: Sequence[int] = DEFAULT_TRAILING,
        at: str | None = None,
    ) -> tuple[Result, ...]:
        """The current answers: touched figures and their readings, or all of
        them when `touched` is None (a client's first paint).

        `at` (an ISO date) anchors every window reading on that day's end in
        its own zone instead of on now -- an argument like `trailing`, moving
        which stored days take part and nothing else. Figures and projections
        ignore it: they are point-in-time answers with no window to move, and
        each result's own `at` says when it was computed.

        Bundles are deliberately not served here: every member already is,
        under its own name, and a tile is a by-name request (`answer`) --
        pushing each bundle too would put every answer on the wire twice.

        Every projection serves, every time, from here: a projection stores
        nothing and is evaluated at the instant it is asked, so its rows move
        when a record of its kind changes, when a figure it reads changes, or
        when *the clock advances*. The one caller allowed to narrow that is
        `run`, for the definition-only pass whose reach is provable -- and it
        goes through the private `_serve` precisely so that no public caller
        can reach the narrowing and reintroduce the silent freeze (figures
        moving over a socket while the list they sit on keeps yesterday's
        ranking).
        """
        return await self._serve(
            tenant, settings, touched=touched, projections=None, trailing=trailing, at=at
        )

    async def _serve(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        touched: set[str] | None = None,
        projections: set[str] | None = None,
        trailing: Sequence[int] = DEFAULT_TRAILING,
        at: str | None = None,
    ) -> tuple[Result, ...]:
        document = self._schema.settings_for(settings)
        lib = self._library
        out: list[Result] = []

        for plan in lib.figures:
            if plan.grain is not None or plan.across is not None:
                # Both serve by name (`answer`) -- time-bucket or dimension
                # rows for a host's evidence pane -- but this is the bulk
                # surface a pass pushes and a first paint reads, and no screen
                # subscribes to those rows: what a card wants is the reading
                # or the rollup, and both are below. Shipping every stored
                # person-day here would spend every pass on history nobody is
                # watching.
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
                    self._store, lib, tenant, reading, document, list(trailing), at_day=at
                )
            )

        for projection in lib.projections:
            if projections is not None and projection.name not in projections:
                continue
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
        at: str | None = None,
    ) -> Result | BundleResult | None:
        """One definition's current answer, by name. None when nothing is
        called that; a live reading raises, because "no such definition" and
        "not built yet" send a caller to different fixes.

        `at` anchors a window reading's spans on that ISO date's end instead
        of on now; everything else serves as it always did and ignores it.

        A bundle answers a `BundleResult`: its members' ordinary results, in
        declaration order, evaluated at one instant. A reading member's
        windows come from the bundle's own definition (or the serving default
        when unwritten) -- `trailing` deliberately does not reach inside a
        bundle, because a tile whose windows the caller could move would be a
        different tile under the same hash. `at` does reach in: it is an
        anchor, not a definition change, exactly as it is for a reading
        served alone."""
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
                self._store, lib, tenant, reading, document, list(trailing), at_day=at
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

        bundle = lib.bundle(name)
        if bundle is not None:
            return await answer_bundle(
                self._store,
                self._facts,
                lib,
                tenant,
                bundle,
                document,
                default_trailing=DEFAULT_TRAILING,
                at_day=at,
            )

        return None

    async def evidence(
        self,
        tenant: str,
        name: str,
        subject: str,
        settings: Mapping[str, Any] | None = None,
    ) -> Evidence | None:
        """The citation behind one stored value: the records (or parts) a
        figure's row was computed from, joined back to what they cite.

        Figures only, because a figure is the only declaration that stores.
        Anything else raises `LookupError` with a forwarding address -- where
        the evidence actually lives -- because "no" with no forwarding address
        dead-ends the exact reader this surface exists for. `None` means the
        figure is available and this subject has no row.
        """
        document = self._schema.settings_for(settings)
        lib = self._library

        plan = lib.figure(name)
        if plan is not None:
            return await serve_evidence(
                self._store, self._facts, lib, self._schema, tenant, plan, document, subject
            )

        reading = lib.reading(name)
        if reading is not None and reading.source is not None:
            raise LookupError(
                f"{name} is a reading: it stores nothing and is recomputed when asked. "
                f"Its windows summarise {reading.source}'s stored days -- the evidence "
                "lives there."
            )
        if reading is not None:
            raise LookupError(
                f"{name} is a live reading: it measures records against the clock and "
                "stores nothing, so there is no stored citation to show."
            )
        if lib.projection(name) is not None or lib.summary(name) is not None:
            raise LookupError(
                f"{name} is re-evaluated on every request and stores no values; its "
                "rows are the evidence, and the results route serves them."
            )
        if lib.bundle(name) is not None:
            raise LookupError(
                f"{name} is a bundle: it composes definitions, computes nothing and "
                "cites nothing. Each member's evidence lives on the member, under the "
                "member's own name."
            )
        raise LookupError(f"No figure called {name}")
