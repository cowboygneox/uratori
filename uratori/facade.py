"""The front door: one object that runs the engine and serves its answers.

A host constructs this with the four things it declared -- schema, compiled
library, engine store, fact source -- and gets three verbs:

- `execute`: facts moved (or a full pass is due); recompute what followed from
  them, cascading through every figure built on a figure that moved.
- `results`: the current answers (`Result`s, and `BundleResult`s for the
  bundles) for definitions worth serving, the same objects a route would
  return and a listener receives.
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
from .lang.plan import BundlePlan, Library
from .lang.settings import fingerprint as settings_fingerprint
from .lang.source import declaration_source
from .results import BundleResult, Evidence, Result
from .schema import EFFORT_HOURS_SETTING, Schema
from .store import EngineStore, FactSource, Pointer
from .verify import verify_writes
from .windows import WindowSpec

log = logging.getLogger("uratori")

Listener = Callable[[str, Outcome, tuple[Result | BundleResult, ...]], Awaitable[None] | None]
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
    results: tuple[Result | BundleResult, ...]
    moved: frozenset[str] = frozenset()
    """Every definition whose *served* answer this pass may have changed --
    touched figures, dial-refreshed figures and readings, readings over a
    touched source, re-served projections and their summaries, and every
    bundle a moved member sits in. Computed without evaluating anything, so a
    host that owns its own delivery (per-client subscriptions) can intersect
    this with what is actually watched and evaluate only that -- `serve=False`
    is the other half of that seam."""


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

    async def _notify(
        self, tenant: str, outcome: Outcome, results: tuple[Result | BundleResult, ...]
    ) -> None:
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
        at_ms: float | None = None,
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
            at_ms=at_ms,
        )

    def _through_kinds(self) -> frozenset[str]:
        """Every kind a grouping resolves *through* rather than buckets.

        A record of one of these moving changes where other records land,
        which the incremental path cannot see -- so it escalates to a full
        pass. An age filter reading its threshold off an owner is the same
        case one construct along: move a repository's staleness rule and
        every change in that repository crosses the line or stops crossing
        it, with nothing about the changes themselves having moved.
        """
        from .lang.ast import ByAge
        from .lang.check import _index_fields

        specs = [index.spec for index in self._library.indexes.values()]
        parts = [part for spec in specs for part in _index_fields(spec)]
        return frozenset(
            [part.through.kind for part in parts if part.through is not None]
            # A calendar is a field on the subject's record, so moving it
            # refiles every one of that subject's buckets while the records
            # themselves sit still -- the same blindness the identity hop has,
            # and it needs the same escalation.
            + [part.zone.kind for part in parts if part.zone is not None]
            + [
                spec.through.kind
                for spec in specs
                if isinstance(spec, ByAge) and spec.through is not None
            ]
        )

    async def run(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        written: Mapping[str, Sequence[str]] | None = None,
        deleted: Mapping[str, Sequence[str]] | None = None,
        full: bool = False,
        at_ms: float | None = None,
        trailing: Sequence[int | str | WindowSpec] = DEFAULT_TRAILING,
        serve: bool = True,
    ) -> RunReport:
        """A pass, its re-served answers, and delivery, in that order.

        Listeners are notified only when there is something to hear: a change,
        or a served result (a projection re-serves on every pass because the
        clock is one of its inputs). A poll in which nothing happened notifying
        every listener is how listeners stop being read.

        `serve=False` skips the evaluation and the listeners entirely and
        reports only `moved`: the caller has said it owns delivery -- it will
        intersect the moved set with what its clients actually watch and
        evaluate exactly that, by name, at each watcher's own arguments. The
        stamps still settle, because the movement *was* reported; leaving them
        would re-report the same dial move on every pass for ever.
        """
        outcome = await self.execute(
            tenant, settings, written=written, deleted=deleted, full=full, at_ms=at_ms
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
        held = await self._store.pointers(tenant)
        refreshed = self._refreshed(stamps, held)
        projections = (
            None if sync else self._reached(outcome.reindexed, touched, stamps, held)
        )
        moved = self._moved(touched, refreshed, projections)
        if serve:
            results = await self._serve(
                tenant,
                settings,
                touched=touched,
                refreshed=refreshed,
                projections=projections,
                trailing=trailing,
            )
        else:
            results = ()
        if serve and (outcome.changes or results):
            await self._notify(tenant, outcome, results)
        # The stamps settle only after the answers actually went out: a crash
        # between serving and stamping re-serves next pass, which is the safe
        # direction. Settled on sync passes too, or the first definition-only
        # pass after a sync would re-serve everything the sync already sent.
        await self._settle_serve_stamps(tenant, stamps, held)
        return RunReport(outcome=outcome, results=results, moved=moved)

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
            dials: set[str]
            # A `band of X` column used to add X's band *dials* here, because
            # turning one re-worded the column while nothing stored moved.
            # A band's threshold is a figure now, so what moves the column is
            # a value -- caught by `_reached`, which follows the same edge.
            dials = set(plan.settings)
            # The effort rendering dial, when any row value is an effort:
            # `format_value` divides by it on the way to `display`, so the
            # rendered rows move the moment it does -- the same edge the UI's
            # dependency closure draws, and for a release the one dial that
            # re-served the figures while every projection rendering the same
            # efforts kept the old text.
            if any(
                unit == "effort" for _, _, unit, band in plan.reads if not band
            ) or any(unit == "effort" for _, _, unit in plan.values):
                dials.add(EFFORT_HOURS_SETTING)
            stamps[plan.name] = Pointer(
                version=plan.version,
                settings_fingerprint=_serve_fingerprint(
                    document, sorted(dials), plan.doc
                ),
            )
        for summary in lib.summaries:
            dials = set(summary.settings)
            if any(unit == "effort" for _, _, unit, _ in summary.totals) or any(
                unit == "effort" for _, _, unit in summary.values
            ):
                dials.add(EFFORT_HOURS_SETTING)
            stamps[summary.name] = Pointer(
                version=summary.version,
                settings_fingerprint=_serve_fingerprint(
                    document, sorted(dials), summary.doc
                ),
            )
        # Figures and readings carry serve stamps too, under a prefixed key
        # because a figure's own name already holds its *compute* pointer.
        # The dials here are exactly the ones that move a served answer
        # without moving a stored value: a figure's band dials (a band is
        # worded at serve time, deliberately outside the compute
        # fingerprint), a reading's own dials (its band -- a reading stores
        # nothing, so no pointer ever noticed for it), and the effort
        # rendering dial where the unit is effort (`format_value` divides by
        # it, so the rendered text moves the moment it does -- the same edge
        # the UI's dependency closure draws). Before these stamps existed, a
        # band threshold save updated nothing stored and pushed nothing, and
        # every connected screen kept the old colour until a reload.
        for figure_plan in lib.figures:
            dials = set()
            if figure_plan.unit == "effort":
                dials.add(EFFORT_HOURS_SETTING)
            stamps[_serve_key(figure_plan.name)] = Pointer(
                version=figure_plan.version,
                settings_fingerprint=_serve_fingerprint(
                    document, sorted(dials), figure_plan.doc, figure_plan.display
                ),
            )
        for reading in lib.readings:
            dials = set(reading.settings)
            if reading.unit == "effort":
                dials.add(EFFORT_HOURS_SETTING)
            stamps[_serve_key(reading.name)] = Pointer(
                version=reading.version,
                settings_fingerprint=_serve_fingerprint(
                    document, sorted(dials), reading.doc, reading.display
                ),
            )
        # Bundles carry a serve stamp too, for the one thing on their wire
        # that is theirs alone: the prose. A tile's `doc` is deliberately
        # outside its version hash (prose is not composition), so without a
        # stamp an edited explanation would reach the artifact and never a
        # connected screen.
        for bundle in lib.bundles:
            stamps[_serve_key(bundle.name)] = Pointer(
                version=bundle.version,
                settings_fingerprint=_serve_fingerprint(document, [], bundle.doc),
            )
        return stamps

    def _refreshed(
        self, stamps: Mapping[str, Pointer], held: Mapping[str, Pointer]
    ) -> frozenset[str]:
        """The figures and readings whose *served* answer a dial (or their own
        redefinition) has moved since they last went out. On the first pass
        after this stamp discipline arrives nothing is held, so everything
        reads as refreshed and re-serves once -- the safe direction, exactly
        as the projection stamps behaved when they were introduced."""
        out: set[str] = set()
        for plan in self._library.figures:
            if held.get(_serve_key(plan.name)) != stamps[_serve_key(plan.name)]:
                out.add(plan.name)
        for reading in self._library.readings:
            if held.get(_serve_key(reading.name)) != stamps[_serve_key(reading.name)]:
                out.add(reading.name)
        for bundle in self._library.bundles:
            if held.get(_serve_key(bundle.name)) != stamps[_serve_key(bundle.name)]:
                out.add(bundle.name)
        return frozenset(out)

    def _moved(
        self,
        touched: set[str],
        refreshed: frozenset[str],
        projections: set[str] | None,
    ) -> frozenset[str]:
        """Every name whose current answer this pass may have changed --
        the impact set, computed from what the pass already knows and
        evaluating nothing. `projections` is None on a sync pass, meaning
        all of them: a projection's inputs include the clock, and the sync
        is the moment that contract pays out."""
        lib = self._library
        out: set[str] = set(touched) | set(refreshed)
        for reading in lib.readings:
            if reading.source is not None and reading.source in touched:
                out.add(reading.name)
            if set(reading.band_reads) & touched:
                out.add(reading.name)
        # A band's threshold is a figure, so a goal moving re-words a card
        # whose own stored value did not move. Nothing in the change stream
        # says so -- the metric is byte-identical -- and without this edge
        # every connected screen keeps yesterday's word until a reload. It is
        # the same edge a band dial used to get through the serve stamps,
        # redrawn to follow a value instead of a setting.
        for plan in lib.figures:
            if set(plan.band_reads) & touched:
                out.add(plan.name)
        if projections is None:
            out.update(plan.name for plan in lib.projections)
            out.update(summary.name for summary in lib.summaries)
        else:
            out.update(projections)
            out.update(s.name for s in lib.summaries if s.over in projections)
        out.update(self._bundles_impacted(touched, refreshed, projections))
        return frozenset(out)

    def _bundles_impacted(
        self,
        touched: set[str],
        refreshed: frozenset[str],
        projections: set[str] | None,
    ) -> set[str]:
        """A bundle is impacted exactly when a member is, by the member's own
        kind's test: a figure member that was touched or dial-refreshed, a
        reading member whose source was touched (or that was itself
        refreshed), a projection or summary member whose projection is being
        re-served. The union and nothing wider -- a tile containing none of
        what moved must stay off the wire, or every card flickers on every
        pass and the push surface stops meaning anything."""
        lib = self._library
        out: set[str] = set()
        for bundle in lib.bundles:
            if not _servable(lib, bundle):
                continue
            if bundle.name in refreshed:
                # The tile's own prose moved -- nothing about any member did,
                # but the wrapper on the wire changed and must re-serve.
                out.add(bundle.name)
                continue
            for member in bundle.members:
                if member.kind == "figure":
                    figure = lib.figure(member.name)
                    banded = set(figure.band_reads) if figure is not None else set()
                    if member.name in touched or member.name in refreshed or banded & touched:
                        break
                elif member.kind == "reading":
                    reading = lib.reading(member.name)
                    source = reading.source if reading is not None else None
                    banded = set(reading.band_reads) if reading is not None else set()
                    if (
                        member.name in refreshed
                        or (source is not None and source in touched)
                        or banded & touched
                    ):
                        break
                elif member.kind == "projection":
                    if projections is None or member.name in projections:
                        break
                else:  # summary
                    summary = lib.summary(member.name)
                    over = summary.over if summary is not None else None
                    if projections is None or (over is not None and over in projections):
                        break
            else:
                continue
            out.add(bundle.name)
        return out

    def _reached(
        self,
        reindexed: Sequence[str],
        touched: set[str],
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
            # A `band of X` column is worded from X's goals, so a goal moving
            # re-words the column though X itself is untouched.
            for _, name, _, band in plan.reads:
                read = self._library.figure(name)
                if band and read is not None:
                    reads |= set(read.band_reads)
            if (
                set(plan.indexes) & rebuilt
                or reads & touched
                or held.get(plan.name) != stamps[plan.name]
            ):
                out.add(plan.name)
        for summary in self._library.summaries:
            if held.get(summary.name) != stamps[summary.name]:
                out.add(summary.over)
        return out

    async def _settle_serve_stamps(
        self, tenant: str, stamps: Mapping[str, Pointer], held: Mapping[str, Pointer]
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
        # Figure, reading and bundle serve stamps, under their prefixed keys.
        # The figure's definition row already exists on any pass that computed
        # it, but a warm pass writes none -- and a reading or bundle has never
        # had one -- so each is ensured for the reason the projections above
        # are: the pointer table cites a definition version, and the stamp
        # must be able to land on every pass, not only cold ones. Settled only
        # when the stamp actually moved, so a quiet poll over a large library
        # costs a read it already paid rather than two writes per definition.
        for figure_plan in lib.figures:
            key = _serve_key(figure_plan.name)
            if held.get(key) == stamps[key]:
                continue
            await self._store.ensure_definition(
                figure_plan.version,
                figure_plan.name,
                "figure",
                figure_plan.doc,
                figure_plan.display,
                declaration_source(lib, figure_plan.name) or "",
                {
                    "unit": figure_plan.unit,
                    "scope": figure_plan.scope,
                    "depth": figure_plan.depth,
                },
            )
            await self._store.set_pointer(tenant, key, stamps[key])
        for reading in lib.readings:
            key = _serve_key(reading.name)
            if held.get(key) == stamps[key]:
                continue
            await self._store.ensure_definition(
                reading.version,
                reading.name,
                "reading",
                reading.doc,
                reading.display,
                declaration_source(lib, reading.name) or "",
                {"unit": reading.unit, "scope": reading.scope, "mode": reading.mode},
            )
            await self._store.set_pointer(tenant, key, stamps[key])
        for bundle in lib.bundles:
            key = _serve_key(bundle.name)
            if held.get(key) == stamps[key]:
                continue
            await self._store.ensure_definition(
                bundle.version,
                bundle.name,
                "bundle",
                bundle.doc,
                "",
                declaration_source(lib, bundle.name) or "",
                {"members": [member.name for member in bundle.members]},
            )
            await self._store.set_pointer(tenant, key, stamps[key])

    # ------------------------------------------------------------- serving --

    async def results(
        self,
        tenant: str,
        settings: Mapping[str, Any] | None = None,
        *,
        touched: set[str] | None = None,
        trailing: Sequence[int | str | WindowSpec] = DEFAULT_TRAILING,
        at: str | None = None,
    ) -> tuple[Result | BundleResult, ...]:
        """The current answers: touched figures and their readings, or all of
        them when `touched` is None (a client's first paint).

        `at` (an ISO date) anchors every window reading on that day's end in
        its own zone instead of on now -- an argument like `trailing`, moving
        which stored days take part and nothing else. Figures and projections
        ignore it: they are point-in-time answers with no window to move, and
        each result's own `at` says when it was computed.

        Bundles serve here too -- a first paint that omitted the tiles would
        leave every screen bound to one blank until a pass happened to touch
        a member. Each member also serves under its own name, so a tile's
        numbers do travel twice; that redundancy is the price of both
        surfaces being complete, and it is bytes, not arithmetic. The one
        exception is an anchored read (`at`): a bundle refuses an anchor by
        name -- its non-reading members can only be served as they stand, so
        an anchored tile would disagree with itself under a wrapper claiming
        one clock -- and this surface honours the same refusal by serving an
        anchored list without the tiles rather than quietly serving them
        unanchored beside anchored readings of the same names.

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
        refreshed: frozenset[str] = frozenset(),
        projections: set[str] | None = None,
        trailing: Sequence[int | str | WindowSpec] = DEFAULT_TRAILING,
        at: str | None = None,
    ) -> tuple[Result | BundleResult, ...]:
        document = self._schema.settings_for(settings)
        lib = self._library
        out: list[Result | BundleResult] = []

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
            if (
                touched is not None
                and plan.name not in touched
                and plan.name not in refreshed
                # A goal moving re-words this figure without moving its stored
                # value, so the change stream is silent about it. Left out, a
                # raised target would colour nothing until something unrelated
                # happened to touch the figure -- the screen-keeps-lying bug
                # the band dials' serve stamps were introduced to close, now
                # that the threshold is a fact rather than a dial.
                and not (set(plan.band_reads) & touched)
            ):
                continue
            out.append(await serve_figure(self._store, lib, tenant, plan, document))

        for reading in lib.readings:
            if reading.mode != "window" or reading.source is None:
                continue
            if (
                touched is not None
                and reading.source not in touched
                and reading.name not in refreshed
                and not (set(reading.band_reads) & touched)
            ):
                continue
            out.append(
                await serve_reading(
                    self._store,
                    lib,
                    tenant,
                    reading,
                    document,
                    list(trailing),
                    at_day=at,
                    facts=self._facts,
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

        # Bundles last, after every kind they compose: impacted ones on a
        # narrowed serve (the member-union in `_bundles_impacted`), all of
        # them on a first paint -- and none on an anchored read, per the
        # refusal `results` explains. Members are evaluated at their DECLARED
        # windows (`answer_bundle`'s own contract), never at this call's
        # `trailing`: a tile whose windows the transport could move would be
        # a different tile under the same hash.
        if at is None:
            narrowed = touched is not None or projections is not None
            impacted = (
                self._bundles_impacted(touched or set(), refreshed, projections)
                if narrowed
                else None
            )
            for bundle in lib.bundles:
                if not _servable(lib, bundle):
                    continue
                if impacted is not None and bundle.name not in impacted:
                    continue
                out.append(
                    await answer_bundle(
                        self._store,
                        self._facts,
                        lib,
                        tenant,
                        bundle,
                        document,
                        default_trailing=DEFAULT_TRAILING,
                    )
                )

        return tuple(out)

    async def answer(
        self,
        tenant: str,
        name: str,
        settings: Mapping[str, Any] | None = None,
        *,
        trailing: Sequence[int | str | WindowSpec] = DEFAULT_TRAILING,
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
        different tile under the same hash. `at` is refused for a bundle:
        an anchor moves only a reading's windows, and the other members --
        stored figures, live pages -- can only be served as they stand, so an
        anchored tile would put June's reading beside today's page under a
        wrapper claiming one clock. Anchor the reading by its own name."""
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
                self._store,
                lib,
                tenant,
                reading,
                document,
                list(trailing),
                at_day=at,
                facts=self._facts,
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
            if at is not None:
                raise ValueError(
                    f"{name} is a bundle, and a bundle is served at one instant -- now. "
                    "An anchor moves only a reading's windows; the other members can "
                    "only be served as they stand, so an anchored tile would disagree "
                    "with itself under a wrapper claiming one clock. Anchor the reading "
                    "member by its own name instead."
                )
            return await answer_bundle(
                self._store,
                self._facts,
                lib,
                tenant,
                bundle,
                document,
                default_trailing=DEFAULT_TRAILING,
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


def _serve_fingerprint(
    document: Mapping[str, Any], dials: Sequence[str], *prose: str
) -> str:
    """What a definition's SERVED answer is rendered under: the dials it
    renders with, and the prose that travels on the wire beside the numbers
    (`doc`, and the display template where one exists).

    The prose rides in the fingerprint because it is deliberately outside
    every version hash -- an explanation is not a calculation -- yet it IS on
    the wire: without this, editing the sentence above a figure updated the
    artifact and the routes while every connected screen kept the old words
    until a reload, and `moved` claimed nothing had."""
    import hashlib

    h = hashlib.sha256()
    for part in prose:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return f"{settings_fingerprint(dict(document), list(dials))}|{h.hexdigest()[:16]}"


def _serve_key(name: str) -> str:
    """The pointer key a figure's or reading's serve stamp lives under.

    Prefixed because a figure's bare name already holds its compute pointer,
    and the two answer different questions: the compute pointer says what the
    STORED values were built under, the serve stamp says what the last-served
    answer was rendered under. A `:` cannot appear in a declaration name, so
    the prefix cannot collide with a future definition."""
    return f"serve:{name}"


def _servable(lib: Library, bundle: BundlePlan) -> bool:
    """Whether this bundle can be served at all today.

    A live reading is not servable yet, and `answer_bundle` refuses a tile
    holding one rather than serving the rest around a silently dropped
    member. The bulk surface honours the same refusal by leaving the whole
    tile off it -- the by-name route still answers 501 for it, so the absence
    is stated where the tile is actually asked for, instead of one member's
    gap failing the entire first paint."""
    for member in bundle.members:
        if member.kind == "reading":
            reading = lib.reading(member.name)
            if reading is not None and reading.mode == "live":
                return False
    return True
