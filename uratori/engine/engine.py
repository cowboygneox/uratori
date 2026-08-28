"""The engine: index on write, recompute what moved, report all of it.

One entry point, `run`. Everything else is a step inside it.

The shape to hold on to: **the cheap incremental path may never narrow the
population a calculation is performed over, nor the population it is reported
over.** Both halves have bitten this product. The first produced a person
rebuilt from one account and written over their real record. The second produced
a screen that froze because the value moved and nothing said so.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..lang.plan import CompiledIndex, FigurePlan, Library
from ..lang.settings import fingerprint as settings_fingerprint
from ..lang.settings import setting_value
from ..lang.source import declaration_source
from ..schema import Schema
from ..store import EngineStore, FactSource
from .buckets import (
    SEPARATOR,
    ThroughResolver,
    buckets_of,
    measure_of,
    read_instant,
    read_number,
    read_path,
    subject_of,
)
from .carry import CarryReachExceeded, materialise
from .change import Change, Outcome
from .evaluate import Parts, Readers, evaluate, same_value

log = logging.getLogger("uratori.engine")


class Engine:
    def __init__(
        self, store: EngineStore, facts: FactSource, library: Library, schema: Schema
    ) -> None:
        self._store = store
        self._facts = facts
        self._library = library
        self._schema = schema

    # ------------------------------------------------------------------ run --

    async def run(
        self,
        tenant: str,
        settings: Mapping[str, Any],
        *,
        written: Mapping[str, Sequence[str]] | None = None,
        deleted: Mapping[str, Sequence[str]] | None = None,
        full: bool = False,
        at_ms: float | None = None,
    ) -> Outcome:
        """One pass. Raises on failure -- it never reports "nothing changed".

        v1 caught everything here and returned an empty list, on the reasoning
        that a debug surface must not break a sync. That was true when nothing
        rendered from the engine. Now the engine *is* the board, so a swallowed
        failure is a screen that silently stops updating, and the caller is the
        only thing that can decide what to do about it.
        """
        lib = self._library
        # The pass's own instant, read once. Every age bucket and every
        # carried extension in this pass answers to it, so a pass cannot
        # disagree with itself about what "now" is -- the same
        # one-instant rule a projection has always had, one layer up.
        # Injectable because "how far has the sequence been extended" is
        # otherwise untestable without waiting.
        now = at_ms if at_ms is not None else _now_ms()
        # Not `not lib.figures`: a projection's `from` reads index buckets with
        # no figure anywhere near them, so a library of indexes and projections
        # alone still has indexing work to do -- returning early would serve
        # every such projection an empty page for ever.
        if not lib.figures and not lib.indexes:
            return Outcome(changes=(), covered=frozenset(), reindexed=(), rebuilt=())

        pending = await self._pending(tenant, settings)
        cold = bool(pending) or full

        # A figure notices its indexes changed because their specs are hashed
        # into its version and the stale pointer forces a cold pass. An index
        # read only by a projection's `from` has no figure and therefore no
        # pointer -- so its arrival or redefinition has to be noticed here, or
        # nothing ever builds it and the projection filters through an empty
        # bucket: an empty-then-partial page with confident headline numbers
        # until the next *full* sync, over a population nobody chose. The
        # exact failure class this file's header names, through the rebuild
        # path instead of the calculation.
        #
        # Noticed PER GROUPING, deliberately. One stamp over the whole set
        # made any index change re-bucket every grouping over every record --
        # a million-fact tenant paid a rebuild of the world to answer a
        # one-line filter. Staleness is the unit of work, so each grouping
        # carries the spec version its stored buckets were built under, and
        # a pass rebuilds exactly the stale ones and retires exactly the
        # removed ones.
        built = await self._store.index_stamps(tenant)
        wanted = {
            name: _index_stamp(idx, settings) for name, idx in lib.indexes.items()
        }
        if not built:
            # The one-time upgrade window: no per-index stamps yet, but a
            # pre-0.7 whole-set stamp may still stand. A stamp matching this
            # library proves every grouping current -- seeded rather than
            # rebuilt, so no tenant pays for the upgrade itself. A mismatch
            # proves nothing and everything below reads as stale. Either way
            # the stamp retires here; kept, it would shadow every later save.
            legacy = await self._store.legacy_index_set(tenant)
            if legacy is not None:
                if _versions_if_legacy_current(legacy, lib) is not None:
                    for name, stamp in wanted.items():
                        await self._store.set_index_stamp(tenant, name, stamp)
                    built = dict(wanted)
                await self._store.drop_legacy_index_set(tenant)
        # Version OR fingerprint: the spec hash excludes settings, so an age
        # filter or a zoned calendar grouping goes stale when its dial moves
        # even though no declaration changed -- whether or not any figure
        # reads it. This is what lets a settings save reach the groupings no
        # pointer notices for, at the next pass, without rebuilding the rest.
        stale = {name for name, stamp in wanted.items() if built.get(name) != stamp}
        for name in built:
            if name not in wanted:
                # Retired deliberately -- new behaviour, not preservation:
                # before per-index staleness nothing ever removed a dead
                # grouping's rows, they simply accumulated. (One corner
                # remains: a grouping removed in the same deploy as the 0.7
                # upgrade is invisible here, because `built` is empty or
                # freshly seeded. Its rows are unreachable -- every reader
                # resolves names through the library -- so they are storage
                # debt, cleaned by the row's tenant being removed.)
                await self._store.drop_index(tenant, name)

        changes: list[Change] = []
        rebuilt: tuple[str, ...] = ()
        reindexed: tuple[str, ...] = ()

        if cold:
            for plan in lib.figures:
                await self._store.ensure_definition(
                    plan.version,
                    plan.name,
                    "figure",
                    plan.doc,
                    plan.display,
                    declaration_source(lib, plan.name) or "",
                    {"unit": plan.unit, "scope": plan.scope, "depth": plan.depth},
                )
            if full:
                await self._reindex(tenant, settings, now_ms=now)
                reindexed = tuple(sorted(lib.indexes))
            elif stale:
                await self._reindex(tenant, settings, only=stale, now_ms=now)
                reindexed = tuple(sorted(stale))
            changes.extend(await self._remove_departed(tenant, settings))
            only = None if full else {p.name for p in pending}
            # A cold pass **recomputes** rather than fills gaps: the reason it is
            # cold is that a definition or a dial moved, so the values that
            # already exist are exactly the ones that may now be wrong.
            changes.extend(await self._backfill(tenant, settings, only=only, gaps_only=False))
            rebuilt = tuple(sorted(only)) if only is not None else tuple(p.name for p in lib.figures)
            for plan in lib.figures:
                await self._store.set_pointer(
                    tenant,
                    plan.name,
                    _pointer_for(plan, settings),
                )
        else:
            if written or deleted:
                # Not just an optimisation of a no-op: `_apply` builds the
                # full through-hop resolver up front, scanning every hop kind
                # in the library, and a definition-only pass would pay those
                # scans to apply an empty batch.
                changes.extend(
                    await self._apply(tenant, settings, written or {}, deleted or {}, now_ms=now)
                )
            if stale:
                # A grouping moved with no figure pointer moving -- an index
                # only a `from` reads arrived or changed, a stamp is missing
                # (a retired filter reinstated), or a bucket dial moved.
                # Rebuild it now rather than waiting for the next full pass:
                # until it is built, every projection filtering through it
                # serves whichever records the deltas since the deploy
                # happened to touch. After `_apply`, deliberately -- `_apply`
                # diffs a record's old buckets against its new ones to decide
                # whose figures moved, and a rebuild beforehand would erase
                # the "old" half of that comparison and silence the stream.
                await self._reindex(tenant, settings, only=stale, now_ms=now)
                reindexed = tuple(sorted(stale))
                # A wholesale rebuild is diffless -- `replace_index` cannot
                # say whose buckets moved -- so every figure reading a
                # rebuilt grouping recomputes outright. Without this, the
                # deltas above were applied against the pre-rebuild rows: a
                # reinstated filter was empty when `_apply` counted through
                # it, and the figures it feeds would keep that narrowed
                # count until a full pass -- the cardinal sin, permanent.
                healed = {
                    plan.name for plan in lib.figures if set(plan.indexes) & stale
                }
                # Closed over the rollups: a figure combining a healed one
                # totals the very values the heal is about to rewrite.
                # Figures are compiled in depth order, so one forward walk
                # closes the set.
                for plan in lib.figures:
                    sources = set(plan.reads) | {
                        src for src, _ in plan.combines.values()
                    }
                    if sources & healed:
                        healed.add(plan.name)
                if healed:
                    changes.extend(
                        await self._backfill(
                            tenant, settings, only=healed, gaps_only=False
                        )
                    )
                    rebuilt = tuple(sorted(healed))
            if deleted:
                # A departed *subject* is not a moved bucket, and the warm path
                # is driven by bucket movement -- so without this a person
                # deleted between reconciles keeps every value they had, and the
                # change stream says nothing. The scan is skipped entirely when
                # nothing was deleted, which is every ordinary sync.
                changes.extend(await self._remove_departed(tenant, settings))
            if written is not None or deleted is not None:
                # The gap sweep runs on every pass through the facts door --
                # `is not None`, the same rule the facade's sync gate uses,
                # so an empty scheduled batch still repairs what a crashed
                # earlier pass may have left half-written. A definition-only
                # pass skips it: it wrote no facts, so it can have opened no
                # gap, and everything it invalidated was recomputed above.
                # This moves crash repair from every pass to every facts
                # pass; an embedding host that mutates its FactSource out of
                # band and polls a bare run() must reconcile with `written=`
                # or `full` -- see `Uratori.run`.
                changes.extend(await self._backfill(tenant, settings))

        # **Extension on every pass.** The pass is the event that notices
        # time -- the clock itself never is one, which is the whole reason a
        # stored value may not read it. So a carried figure reaches the
        # bucket containing *this pass's* instant here, whatever else the
        # pass did, and a tenant that syncs daily gets a new bucket a day
        # after it exists rather than never.
        #
        # After the recomputes above, deliberately: the anchors this reads
        # are the values those just wrote, and carrying from a bucket whose
        # own value is still stale would spread the stale number forward.
        carried: list[str] = []
        downstream: dict[str, set[str]] = {}
        for plan in lib.figures:
            if not plan.carried or plan.scope_index is None:
                continue
            bases = {
                subject_of(key)
                for key in await self._store.bucket_keys(tenant, plan.scope_index)
            }
            alive = {row.key for row in await self._facts.of_kind(tenant, plan.scope)}
            try:
                rows = await materialise(
                    self._store,
                    plan,
                    tenant,
                    bases & alive,
                    at_ms=now,
                    zone=_zone_of(lib, plan, settings),
                    trigger="pass",
                )
            except CarryReachExceeded as refused:  # pragma: no cover - belt
                # `materialise` contains a reach refusal to the subject that
                # caused it, so this is the belt: whatever gets past that must
                # still not take every unrelated figure in the library down,
                # write no run report and push nothing -- identically on every
                # pass after, with no recovery short of editing the definition.
                log.warning("%s: %s", plan.name, refused)
                continue
            if rows:
                carried.append(plan.name)
            for subject, before, after in rows:
                changes.append(
                    Change(
                        figure=plan.name,
                        subject=subject,
                        kind="moved" if after is not None else "removed",
                        before=before,
                        after=after,
                        label=await self._label(tenant, plan, subject),
                        display=plan.display,
                    )
                )
                # **A carried bucket is a part like any other, so its totals
                # are stale the moment it lands.** The carry necessarily runs
                # after the ordinary recomputes -- it reads the anchor values
                # those just wrote -- so a figure reading a carried source at
                # `:{bucket}` would otherwise see anchor-only rows and answer
                # an absence at exactly the carried coordinates, healing on
                # the *next* pass. That is the worst shape a bug can have:
                # right in every test that runs twice, and wrong on every
                # first build.
                for other in lib.figures:
                    if plan.name in other.reads:
                        downstream.setdefault(subject, set()).add(other.name)

        if downstream:
            # Depth order is `_recompute`'s own, so a total is never computed
            # before the part it reads -- including a chain of them.
            changes.extend(await self._recompute(tenant, settings, downstream))

        # `covered` is what the host re-dates evidence on, so it must name
        # the kinds this pass *read*, not the kinds the batch happened to
        # mention. A full pass recomputed every figure from every fact its
        # definitions can see; reporting the batch instead tells the host a
        # batch-less reconcile confirmed nothing. The warm path stays exactly
        # the batch -- widening it there would re-date evidence that was
        # never checked, which is the narrowed-population sin in reverse.
        covered = frozenset(written or {}) | frozenset(deleted or {})
        if full:
            covered |= _kinds_read(lib)
        return Outcome(
            changes=tuple(changes),
            covered=covered,
            reindexed=reindexed,
            rebuilt=rebuilt,
            carried=tuple(sorted(carried)),
        )

    # -------------------------------------------------------------- pending --

    async def _pending(
        self, tenant: str, settings: Mapping[str, Any]
    ) -> list[FigurePlan]:
        held = await self._store.pointers(tenant)
        out: list[FigurePlan] = []
        for plan in self._library.figures:
            pointer = held.get(plan.name)
            wanted = _pointer_for(plan, settings)
            if (
                pointer is None
                or pointer.version != wanted.version
                or pointer.settings_fingerprint != wanted.settings_fingerprint
            ):
                out.append(plan)
        return out

    # ------------------------------------------------------------ indexing --

    async def _resolver(
        self, tenant: str, only: set[str] | None = None
    ) -> ThroughResolver:
        """Resolve a value to the ids of the records that own it.

        Built once per pass and cached, because a join is asked for every record
        of every index that names one, and doing it per record is the shape that
        turns a sync into a minute. `only` narrows the hop tables to the
        groupings actually being rebuilt -- a narrow rebuild should not scan
        the hop kinds of the groupings it is leaving alone.
        """
        cache: dict[tuple[str, str], dict[str, list[str]]] = {}

        async def build(kind: str, path: str) -> dict[str, list[str]]:
            table: dict[str, list[str]] = {}
            for row in await self._facts.of_kind(tenant, kind):
                for value in read_path(row.value, path):
                    table.setdefault(value, []).append(row.key)
            return {k: sorted(set(v)) for k, v in table.items()}

        for index in self._library.indexes.values():
            if only is not None and index.name not in only:
                continue
            for part in _parts_of(index):
                if part.through is not None:
                    key = (part.through.kind, part.through.path)
                    if key not in cache:
                        cache[key] = await build(*key)

        def resolve(kind: str, path: str, value: str) -> list[str]:
            return cache.get((kind, path), {}).get(value, [])

        return resolve

    async def _reindex(
        self,
        tenant: str,
        settings: Mapping[str, Any],
        only: set[str] | None = None,
        *,
        now_ms: float,
    ) -> None:
        resolve = await self._resolver(tenant, only=only)
        for index in self._library.indexes.values():
            if only is not None and index.name not in only:
                continue
            wanted: dict[str, list[str]] = {}
            for row in await self._facts.of_kind(tenant, index.kind):
                buckets = buckets_of(index, row.value, settings, resolve, now_ms)
                if buckets:
                    wanted[row.key] = buckets
            await self._store.replace_index(tenant, index.name, wanted)
            # Recorded per grouping, after ITS rebuild ran: a pass that dies
            # mid-way leaves exactly the unbuilt ones stale, and the next
            # pass pays exactly the remaining debt.
            await self._store.set_index_stamp(
                tenant, index.name, _index_stamp(index, settings)
            )

    # -------------------------------------------------------- incremental --

    async def _apply(
        self,
        tenant: str,
        settings: Mapping[str, Any],
        written: Mapping[str, Sequence[str]],
        deleted: Mapping[str, Sequence[str]],
        *,
        now_ms: float,
    ) -> list[Change]:
        resolve = await self._resolver(tenant)
        touched: dict[str, set[str]] = {}

        def touch(subject: str, figure: str) -> None:
            touched.setdefault(subject, set()).add(figure)

        # Deletions first, and before any reindex: the buckets that held the key
        # are what say whose numbers move, and removing the rows first would
        # lose exactly that.
        for kind, keys in deleted.items():
            for index in self._indexes_over(kind):
                for key in keys:
                    await self._mark(tenant, touch, index, key, buckets=None)
                    await self._store.remove_member(tenant, index.name, key)

        for kind, keys in written.items():
            rows = await self._facts.some(tenant, kind, list(keys))
            for index in self._indexes_over(kind):
                wanted = {
                    row.key: buckets_of(index, row.value, settings, resolve, now_ms)
                    for row in rows
                }
                # The buckets a record *left* have to be read before the write,
                # because they are what say whose numbers move, and after the
                # write they are gone.
                before = {
                    member: await self._store.buckets_holding(tenant, index.name, member)
                    for member in wanted
                }
                for change in await self._store.set_buckets_many(tenant, index.name, wanted):
                    moved = sorted(
                        set(before[change.member]) | set(change.added) | set(change.removed)
                    )
                    await self._mark(tenant, touch, index, change.member, buckets=moved)

            # **A written record can change a figure's value while moving no
            # bucket**, because a measure -- or a declared-field read --
            # reads a field. Correcting a target from 30m to 25m is the most
            # ordinary edit an on-change stream gets and moves nothing: same
            # subject, same month. Missed here it is missed for ever on the
            # warm path, and a carry then spreads the stale number over every
            # later bucket. Typing an estimate into
            # Jira moves nothing -- same assignee, still active -- and is the most
            # ordinary thing that can happen to an effort figure. Unconditional,
            # because the store knows a record's old *buckets* and not its old
            # *fields*: a subject marked whose measure did not move costs a
            # recompute that reports nothing, and getting it wrong the other way
            # costs a stale number nobody can see.
            for plan in self._library.figures:
                reads_kind = any(
                    self._library.measures[m].kind == kind for m in plan.measures
                ) or kind in _field_kinds(plan)
                if not reads_kind:
                    continue
                if plan.scope_index is None:
                    continue
                for key in keys:
                    for bucket in await self._store.buckets_holding(
                        tenant, plan.scope_index, key
                    ):
                        touch(bucket, plan.name)

        return await self._recompute(tenant, settings, touched)

    async def _mark(
        self,
        tenant: str,
        touch: Any,
        index: CompiledIndex,
        member: str,
        buckets: Sequence[str] | None,
    ) -> None:
        """Say whose values a record's movement could have changed.

        Two cases, and getting the second wrong is the difference between a
        precise warm path and one that recomputes the board on every webhook.

        When the moved index is the one that *fans the figure out*, the bucket
        keys are subjects and the answer is exactly those.

        When it is not -- a predicate the figure intersects, say -- the bucket key
        means nothing to this figure. The subjects affected are the ones whose
        **scope** bucket holds this record, which is a lookup rather than a
        guess. Marking every subject instead is safe and turns each webhook into
        a full recompute, which is the cheap path wearing the expensive path's
        cost, and it would make the scoped-versus-full property pass for the
        wrong reason.
        """
        for plan in self._library.figures:
            if index.name not in plan.indexes:
                continue
            if plan.scope_index == index.name and buckets is not None:
                for bucket in buckets:
                    touch(bucket, plan.name)
                continue
            if plan.scope_index == index.name:
                for bucket in await self._store.buckets_holding(tenant, index.name, member):
                    touch(bucket, plan.name)
                continue
            if plan.scope_index is None:
                # A figure with an index it is not fanned out by, and no scope
                # index at all, cannot exist: the checker refuses a figure with
                # indexes unless exactly one is scope-bucketed, and a rollup has
                # no indexes so the `continue` above already fired. Asserted
                # rather than handled, because a silent branch here would mark
                # nobody and the figure would go stale with nothing to see.
                raise AssertionError(
                    f"{plan.name} reads {index.name} and has no scope index; the checker "
                    "should have refused it"
                )
            for bucket in await self._store.buckets_holding(tenant, plan.scope_index, member):
                touch(bucket, plan.name)

    async def _recompute(
        self, tenant: str, settings: Mapping[str, Any], touched: Mapping[str, set[str]]
    ) -> list[Change]:
        """Recompute in `depth` order so a part is never computed after its total.

        Declaration order is not dependency order, and on a cold build the wrong
        order stores a nought for everybody and never revisits it.
        """
        changes: list[Change] = []
        by_depth = sorted(self._library.figures, key=lambda p: p.depth)
        pending: dict[str, set[str]] = {k: set(v) for k, v in touched.items()}

        for plan in by_depth:
            subjects: set[str] = set()
            for subject, figures in pending.items():
                if plan.name in figures:
                    subjects.add(subject)
            if not subjects:
                continue
            # One reader context per figure per pass, not per subject. Nothing
            # a reader serves moves while this figure's subjects recompute:
            # buckets were rebuilt before any recompute began, facts do not
            # move mid-pass, and depth order means every part a rollup reads
            # was written before the rollup's turn. Per-subject rebuilding is
            # the quadratic shape that turned a season-sized bulk load into
            # tens of minutes -- tests/test_pass_cost.py holds the line.
            readers = await self._readers(tenant, plan, settings)
            for subject in sorted(subjects):
                change = await self._recompute_one(tenant, plan, subject, readers)
                if change is None:
                    continue
                changes.append(change)
                # A part that moved makes its totals stale. Keyed off the
                # *movement* rather than the attempt, so an unchanged recompute
                # propagates nothing.
                #
                # **In the reader's own subject space, not the writer's.** A
                # roster-keyed total is one value per subject, so a moved
                # coordinate makes the total for its *base* stale. A
                # sequenced reader is one value per coordinate, and handing
                # it the base asks it for a subject it does not have --
                # `evaluate` then finds every coordinate row under that base
                # and aborts rather than pick one, taking the whole pass with
                # it. Both shapes existed before carried figures; only a
                # sequenced figure reading another sequenced figure reaches
                # the second, which `:{bucket}` is what made writable.
                for other in self._library.figures:
                    if plan.name not in other.reads:
                        continue
                    reader_subject = subject if other.grain is not None else subject_of(subject)
                    pending.setdefault(reader_subject, set()).add(other.name)
        return changes

    async def _recompute_one(
        self, tenant: str, plan: FigurePlan, subject: str, readers: Readers
    ) -> Change | None:
        result = evaluate(plan, subject, readers)
        held = await self._store.value(tenant, plan.name, plan.version, subject)

        label = await self._label(tenant, plan, subject)

        if plan.grain is not None and isinstance(result.value, list) and not result.value:
            # A bucket every member was gated off is a bucket nothing happened
            # in, and a time-keyed figure's subjects are the buckets something
            # happened in -- so the subject is absent, not an empty list.
            # Removing a held value keeps that true when a record leaves; *not
            # writing one* keeps the cold pass in agreement -- it used to store
            # the empty list, so the next full run removed what the previous
            # run wrote and the two reported that difference at each other for
            # ever. Time-keyed only: a roster-scoped subject exists regardless
            # of its population, so its empty list is a measured "none of it"
            # and is stored like any other nought.
            if held is None:
                return None
            await self._store.remove(tenant, plan.name, plan.version, subject)
            return Change(
                figure=plan.name,
                subject=subject,
                kind="removed",
                before=held.value,
                after=None,
                label=label,
                display=plan.display,
            )

        if (
            held is not None
            and same_value(held.value, result.value)
            and held.members == result.members
            and held.label == label
        ):
            # **An unchanged recompute reports nothing and writes nothing.** A
            # sync in which nothing happened filling the log is how the log stops
            # being read.
            return None

        await self._store.save(
            tenant, plan.name, plan.version, subject, result.value, result.members, label
        )
        if held is not None and same_value(held.value, result.value):
            # Membership or the rendered name moved and the number did not. Worth
            # storing (the evidence changed) and not worth reporting as a
            # movement, which is what a reader would take it for.
            return None
        return Change(
            figure=plan.name,
            subject=subject,
            kind="moved",
            before=held.value if held is not None else None,
            after=result.value,
            label=label,
            display=plan.display,
        )

    # -------------------------------------------------------------- roster --

    async def _scopes_of(
        self, tenant: str, plan: FigurePlan, settings: Mapping[str, Any]
    ) -> list[str]:
        """Every subject this figure should have a value for.

        Three shapes, and the differences are decisions rather than plumbing:

        - **A plain figure walks the scope's roster**, so a measured nought is
          written for everybody. That is what makes a nought an ordinary value
          and an absence mean *not computed*.
        - **A dimensioned figure takes its subjects from the index, never from a
          cross product.** Crossing every person with every source would write a
          nought against connections that categorically cannot hold the record.
          The consequence is that a pair reads nought once it has *ever*
          appeared and is absent until then.
        - **A rollup has no index at all**, so its subjects are the roster
          unioned with whatever its parts are stored under.
        """
        if plan.across is not None and plan.scope_index is not None:
            # A pair's roster is the index, and it is gated at **both** ends by
            # the facts that are actually still here. The index buckets whatever
            # the records name, and a record goes on naming a connection after
            # that connection has been deleted -- so without the gate a departed
            # source keeps its column on the board until every record that ever
            # named it ages out.
            alive = {row.key for row in await self._facts.of_kind(tenant, plan.scope)}
            dimension = {row.key for row in await self._facts.of_kind(tenant, plan.across)}
            out: list[str] = []
            for key in await self._store.bucket_keys(tenant, plan.scope_index):
                base, _, tail = key.partition(SEPARATOR)
                if base in alive and tail in dimension:
                    out.append(key)
            return sorted(out)

        roster = [row.key for row in await self._facts.of_kind(tenant, plan.scope)]
        if plan.grain is not None and plan.scope_index is not None:
            # A time-keyed figure's subjects are the buckets something happened
            # in -- there is no roster of days or quarter-hours, and writing a
            # nought for every one would be a table of manufactured zeroes. Gated by the scope
            # roster for the reason the pair case is: the index goes on holding
            # a departed person's days, and the backfill would put them back
            # immediately after the removal pass took them out.
            live = set(roster)
            return sorted(
                key
                for key in await self._store.bucket_keys(tenant, plan.scope_index)
                if key.partition(SEPARATOR)[0] in live
            )
        if plan.scope_index is None:
            held: set[str] = set() if plan.grain is not None else set(roster)
            for source, _ in plan.combines.values():
                source_plan = self._library.figure(source)
                if source_plan is None:
                    continue
                for stored in await self._store.values(tenant, source, source_plan.version):
                    if plan.grain is not None:
                        # A figure built on sequenced sources is keyed by
                        # coordinate, so its subjects are the coordinates its
                        # sources actually hold -- never the bare roster,
                        # which would write one row per site under a key that
                        # names no bucket. The union across sources is
                        # deliberate: a coordinate only one side has is a
                        # coordinate the answer is *absent* at, and it has to
                        # exist to say so.
                        if SEPARATOR in stored.subject and subject_of(stored.subject) in roster:
                            held.add(stored.subject)
                    else:
                        held.add(subject_of(stored.subject))
            return sorted(held)
        return sorted(roster)

    async def _label(self, tenant: str, plan: FigurePlan, subject: str) -> str:
        """The subject's rendered name, frozen at write time.

        Resolved from the fact, falling back to the raw id. A subject whose fact
        has just departed has no name to use, and printing the id is honest --
        inventing one would be worse.
        """
        base = subject_of(subject)
        field = self._schema.name_fields.get(plan.scope)
        if field is None:
            return base
        rows = await self._facts.some(tenant, plan.scope, [base])
        if not rows:
            return base
        found = read_path(rows[0].value, field)
        return found[0] if found else base

    # --------------------------------------------------------------- departed --

    async def _remove_departed(
        self, tenant: str, settings: Mapping[str, Any]
    ) -> list[Change]:
        """Delete values whose subject no longer exists, **and report each one**.

        This is the half v1 lost. `remove_departed` there returned nothing, so a
        departed subject vanished with no trace in the change stream -- which is
        precisely why the socket could not be fed from it, and why a
        re-read-everything step existed instead.
        """
        changes: list[Change] = []
        for plan in self._library.figures:
            alive = {row.key for row in await self._facts.of_kind(tenant, plan.scope)}
            dimension = (
                {row.key for row in await self._facts.of_kind(tenant, plan.across)}
                if plan.across is not None
                else None
            )
            for stored in await self._store.values(tenant, plan.name, plan.version):
                base = subject_of(stored.subject)
                tail = stored.subject.split(SEPARATOR, 1)[1] if SEPARATOR in stored.subject else None
                gone = base not in alive
                if not gone and dimension is not None and tail is not None:
                    gone = tail not in dimension
                if not gone:
                    continue
                await self._store.remove(tenant, plan.name, plan.version, stored.subject)
                changes.append(
                    Change(
                        figure=plan.name,
                        subject=stored.subject,
                        kind="removed",
                        before=stored.value,
                        after=None,
                        label=stored.label,
                        display=plan.display,
                    )
                )
        return changes

    # ------------------------------------------------------------- backfill --

    async def _backfill(
        self,
        tenant: str,
        settings: Mapping[str, Any],
        only: set[str] | None = None,
        gaps_only: bool = True,
    ) -> list[Change]:
        """Compute subjects that have no value, or every subject on a cold pass.

        `gaps_only` is the whole difference between the two callers. After a warm
        apply it fills in somebody who joined since the last sync and leaves the
        rest alone -- recomputing the board on every webhook would be the
        expensive path wearing the cheap path's name. On a cold pass it is false,
        because the values that already exist are the ones a moved definition
        has just invalidated.
        """
        changes: list[Change] = []
        for plan in sorted(self._library.figures, key=lambda p: p.depth):
            if only is not None and plan.name not in only:
                continue
            have = set(await self._store.subjects(tenant, plan.name, plan.version))
            subjects = [
                subject
                for subject in await self._scopes_of(tenant, plan, settings)
                if not (gaps_only and subject in have)
            ]
            if not subjects:
                continue
            # Hoisted for the reason `_recompute` gives: the context is fixed
            # for the whole of a figure's turn, and a cold pass over a
            # season-sized tenant recomputes every subject there is.
            readers = await self._readers(tenant, plan, settings)
            for subject in subjects:
                change = await self._recompute_one(tenant, plan, subject, readers)
                if change is not None:
                    changes.append(change)
        return changes

    # -------------------------------------------------------------- readers --

    async def _readers(
        self, tenant: str, plan: FigurePlan, settings: Mapping[str, Any]
    ) -> Readers:
        bucket_cache: dict[tuple[str, str | None], frozenset[str]] = {}

        store = self._store
        facts = self._facts
        library = self._library

        loaded: dict[tuple[str, str | None], frozenset[str]] = {}
        for name in plan.indexes:
            spec = library.indexes[name]
            if spec.bucketed:
                for bucket, members in (await store.all_buckets(tenant, name)).items():
                    loaded[(name, bucket)] = members
            else:
                loaded[(name, None)] = await store.members(tenant, name, "")
        bucket_cache.update(loaded)

        records: dict[str, dict[str, Mapping[str, Any]]] = {}
        for measure_name in plan.measures:
            measure = library.measures[measure_name]
            if measure.kind not in records:
                records[measure.kind] = {
                    row.key: row.value for row in await facts.of_kind(tenant, measure.kind)
                }
        # A declared-field read loads its kind the same way a measure does --
        # once per figure per pass, not per subject, which is the shape
        # test_pass_cost.py holds the line on.
        for kind in _field_kinds(plan):
            if kind not in records:
                records[kind] = {
                    row.key: row.value for row in await facts.of_kind(tenant, kind)
                }

        parts: dict[str, dict[str, list[tuple[str, float]]]] = {}
        for source, _ in plan.combines.values():
            source_plan = library.figure(source)
            if source_plan is None:
                continue
            table: dict[str, list[tuple[str, float]]] = {}
            for stored in await store.values(tenant, source, source_plan.version):
                if isinstance(stored.value, (int, float)):
                    # Filed under the subject's base *and* -- for a sequenced
                    # source -- under its full coordinate. A bare read looks
                    # up the base and a `:{bucket}` read looks up the
                    # coordinate; a key holding `@` can never collide with one
                    # that does not, so one table serves both without either
                    # having to know about the other.
                    table.setdefault(subject_of(stored.subject), []).append(
                        (stored.subject, float(stored.value))
                    )
                    if SEPARATOR in stored.subject:
                        table.setdefault(stored.subject, []).append(
                            (stored.subject, float(stored.value))
                        )
                elif stored.value is None:
                    # **A null part is skipped, not treated as corrupt.** A share
                    # figure answers nothing for an unmeasurable subject and a
                    # rollup above it must not abort the whole run over that -- v1
                    # threw here, and the exception froze a tenant's engine at its
                    # last values.
                    continue
                elif isinstance(stored.value, list):
                    raise ValueError(
                        f"{source} stores a list for {stored.subject}, which cannot be a part "
                        "of another figure. The checker refuses this, so reaching here means "
                        "the stored values disagree with the plan."
                    )
            parts[source] = table

        def read_bucket(index: str, bucket: str | None) -> frozenset[str]:
            return bucket_cache.get((index, bucket), frozenset())

        def read_measure(name: str, member: str) -> float | None:
            measure = library.measures[name]
            record = records.get(measure.kind, {}).get(member)
            if record is None:
                return None
            return measure_of(measure, record, None)

        def read_moment(name: str, member: str) -> float | None:
            measure = library.measures[name]
            record = records.get(measure.kind, {}).get(member)
            if record is None:
                return None
            return measure_of(measure, record, None)

        def read_parts(figure: str, subject: str) -> Parts:
            held = parts.get(figure, {}).get(subject, [])
            return Parts(
                values=tuple(v for _, v in held), subjects=tuple(s for s, _ in held)
            )

        def read_setting(path: str) -> float:
            return float(setting_value(dict(settings), path))

        def read_field(kind: str, path: str, member: str) -> float | None:
            record = records.get(kind, {}).get(member)
            return None if record is None else read_number(record, path)

        def read_when(kind: str, path: str, member: str) -> float | None:
            record = records.get(kind, {}).get(member)
            return None if record is None else read_instant(record, path)

        return Readers(
            buckets=read_bucket,
            measures=read_measure,
            moments=read_moment,
            parts=read_parts,
            settings=read_setting,
            fields=read_field,
            instants=read_when,
        )

    def _indexes_over(self, kind: str) -> list[CompiledIndex]:
        return [i for i in self._library.indexes.values() if i.kind == kind]



def _zone_of(
    lib: Library, plan: FigurePlan, settings: Mapping[str, Any]
) -> str | None:
    """Whose calendar the figure's buckets were written in.

    Resolved from the scope group's own zone dial, which is the only thing
    that decided the labels at write time -- so extension and reading walk
    the same sequence. A second answer here would materialise labels a
    window never asks for.
    """
    from ..lang.ast import ByComposite

    if plan.scope_index is None:
        return None
    spec = lib.indexes[plan.scope_index].spec
    if isinstance(spec, ByComposite):
        for part in spec.parts:
            if part.zone is not None:
                return str(setting_value(dict(settings), part.zone))
    return None


def _field_kinds(plan: FigurePlan) -> frozenset[str]:
    """Fact kinds a figure reads declared fields off, which its readers must
    load. Distinct from `plan.measures` because a field read names no
    measure -- that is the whole point of it."""
    from ..lang.ast import Arith, FieldPick, Ladder, Pick

    found: set[str] = set()

    def walk(e: Any) -> None:
        if isinstance(e, FieldPick):
            found.add(e.kind)
        elif isinstance(e, Ladder):
            for rung in e.rungs:
                walk(rung.left)
                walk(rung.then)
                if rung.right is not None:
                    walk(rung.right)
            walk(e.otherwise)
        elif isinstance(e, (Arith, Pick)):
            walk(e.left)
            walk(e.right)

    walk(plan.calculate)
    return frozenset(found)


def _kinds_read(lib: Library) -> frozenset[str]:
    """Every fact kind a full pass over this library reads: each index's
    source kind, each kind an index resolves `through`, each measure's kind.
    Subject kinds appear only if something reads them -- names resolve at
    serve time, so a kind nobody indexes or measures is genuinely unread."""
    from ..lang.check import _index_fields

    kinds = {index.kind for index in lib.indexes.values()}
    kinds |= {
        part.through.kind
        for index in lib.indexes.values()
        for part in _index_fields(index.spec)
        if part.through is not None
    }
    kinds |= {measure.kind for measure in lib.measures.values()}
    return frozenset(kinds)


def _parts_of(index: CompiledIndex) -> list[Any]:
    from ..lang.check import _index_fields  # local import: avoids a cycle at import time

    return _index_fields(index.spec)


def _index_version(index: CompiledIndex) -> str:
    """One grouping's spec, as a version -- the unit of membership staleness.

    The same `_index_hash` a figure's version is built from, so the two ways
    of noticing an index changed cannot disagree about what "changed" means.
    Prose (a label) is not in it. Settings are not either, and that is safe
    because a settings move reaches the dialled groupings through the pending
    figures that read them (the cold pass's `dialled` set), and the checker
    refuses age and fan-out indexes to a projection's `from` -- the one
    reader with no figure to notice for it.
    """
    from ..lang.check import _index_hash  # local import: avoids a cycle at import time
    from ..lang.hash import version_of

    return version_of(_index_hash(index))


def _index_dials(index: CompiledIndex) -> list[str]:
    """The dials this grouping's buckets can move with: an age threshold, or
    a calendar zone on a truncated part. Empty for the dial-free majority."""
    from ..lang.ast import ByAge, ByComposite, ByField

    spec = index.spec
    if isinstance(spec, ByAge):
        return [spec.setting]
    if isinstance(spec, ByField):
        return [spec.part.zone] if spec.part.zone is not None else []
    if isinstance(spec, ByComposite):
        return [part.zone for part in spec.parts if part.zone is not None]
    return []


def _index_stamp(index: CompiledIndex, settings: Mapping[str, Any]) -> Any:
    """What a grouping's buckets are built under: spec version plus a
    fingerprint of the dials the spec reads -- a figure pointer's two-part
    discipline, applied to membership."""
    from ..store import Pointer

    return Pointer(
        version=_index_version(index),
        settings_fingerprint=settings_fingerprint(dict(settings), _index_dials(index)),
    )


def _versions_if_legacy_current(legacy: str | None, library: Library) -> dict[str, str] | None:
    """The one legacy rule, shared by the pass's seed and every read-only
    consumer (the projection gate, the UI's membership state): a pre-0.7
    whole-set stamp equal to this library's proves every grouping current.
    One function so the writer and the readers cannot drift into accepting
    different proofs during the upgrade window.
    """
    if legacy is None or legacy != _index_set_version(library):
        return None
    return {name: _index_version(idx) for name, idx in library.indexes.items()}


def _index_set_version(library: Library) -> str:
    """Every index's name and spec, as one version -- the pre-0.7 stamp.

    Alive only for the upgrade seed: a stored whole-set stamp equal to this
    proves the tenant was fully built under exactly these specs, so its
    per-index versions can be written without a rebuild. Nothing else may
    grow a dependency on it; the per-index versions are the truth now.
    """
    from ..lang.check import _index_hash  # local import: avoids a cycle at import time
    from ..lang.hash import version_of

    return version_of([[name, _index_hash(idx)] for name, idx in sorted(library.indexes.items())])


def _pointer_for(plan: FigurePlan, settings: Mapping[str, Any]) -> Any:
    from ..store import Pointer

    return Pointer(
        version=plan.version,
        settings_fingerprint=settings_fingerprint(dict(settings), list(plan.settings)),
    )


def _now_ms() -> float:
    import time

    return time.time() * 1000.0
