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
    read_path,
    subject_of,
)
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
    ) -> Outcome:
        """One pass. Raises on failure -- it never reports "nothing changed".

        v1 caught everything here and returned an empty list, on the reasoning
        that a debug surface must not break a sync. That was true when nothing
        rendered from the engine. Now the engine *is* the board, so a swallowed
        failure is a screen that silently stops updating, and the caller is the
        only thing that can decide what to do about it.
        """
        lib = self._library
        if not lib.figures:
            return Outcome(changes=(), covered=frozenset(), rebuilt=())

        pending = await self._pending(tenant, settings)
        cold = bool(pending) or full

        changes: list[Change] = []
        rebuilt: tuple[str, ...] = ()

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
            # Reindex only when a pending figure actually reads an index. Saving
            # a threshold rebuilds one figure's values and touches no index row;
            # saving the timezone re-buckets the lot, which is right. The
            # observable difference is *work*, so it is reported.
            if full or any(p.indexes for p in pending):
                await self._reindex(tenant, settings)
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
            changes.extend(
                await self._apply(tenant, settings, written or {}, deleted or {})
            )
            if deleted:
                # A departed *subject* is not a moved bucket, and the warm path
                # is driven by bucket movement -- so without this a person
                # deleted between reconciles keeps every value they had, and the
                # change stream says nothing. The scan is skipped entirely when
                # nothing was deleted, which is every ordinary sync.
                changes.extend(await self._remove_departed(tenant, settings))
            changes.extend(await self._backfill(tenant, settings))

        covered = frozenset(written or {}) | frozenset(deleted or {})
        return Outcome(changes=tuple(changes), covered=covered, rebuilt=rebuilt)

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

    async def _resolver(self, tenant: str) -> ThroughResolver:
        """Resolve a value to the ids of the records that own it.

        Built once per pass and cached, because a join is asked for every record
        of every index that names one, and doing it per record is the shape that
        turns a sync into a minute.
        """
        cache: dict[tuple[str, str], dict[str, list[str]]] = {}

        async def build(kind: str, path: str) -> dict[str, list[str]]:
            table: dict[str, list[str]] = {}
            for row in await self._facts.of_kind(tenant, kind):
                for value in read_path(row.value, path):
                    table.setdefault(value, []).append(row.key)
            return {k: sorted(set(v)) for k, v in table.items()}

        for index in self._library.indexes.values():
            for part in _parts_of(index):
                if part.through is not None:
                    key = (part.through.kind, part.through.path)
                    if key not in cache:
                        cache[key] = await build(*key)

        def resolve(kind: str, path: str, value: str) -> list[str]:
            return cache.get((kind, path), {}).get(value, [])

        return resolve

    async def _reindex(self, tenant: str, settings: Mapping[str, Any]) -> None:
        resolve = await self._resolver(tenant)
        now_ms = _now_ms()
        for index in self._library.indexes.values():
            wanted: dict[str, list[str]] = {}
            for row in await self._facts.of_kind(tenant, index.kind):
                buckets = buckets_of(index, row.value, settings, resolve, now_ms)
                if buckets:
                    wanted[row.key] = buckets
            await self._store.replace_index(tenant, index.name, wanted)

    # -------------------------------------------------------- incremental --

    async def _apply(
        self,
        tenant: str,
        settings: Mapping[str, Any],
        written: Mapping[str, Sequence[str]],
        deleted: Mapping[str, Sequence[str]],
    ) -> list[Change]:
        resolve = await self._resolver(tenant)
        now_ms = _now_ms()
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
            # bucket**, because a measure reads a field. Typing an estimate into
            # Jira moves nothing -- same assignee, still active -- and is the most
            # ordinary thing that can happen to an effort figure. Unconditional,
            # because the store knows a record's old *buckets* and not its old
            # *fields*: a subject marked whose measure did not move costs a
            # recompute that reports nothing, and getting it wrong the other way
            # costs a stale number nobody can see.
            for plan in self._library.figures:
                if not any(self._library.measures[m].kind == kind for m in plan.measures):
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
            for subject in sorted(subjects):
                change = await self._recompute_one(tenant, plan, subject, settings)
                if change is None:
                    continue
                changes.append(change)
                # A part that moved makes its totals stale. Keyed off the
                # *movement* rather than the attempt, so an unchanged recompute
                # propagates nothing.
                for other in self._library.figures:
                    if plan.name in other.reads:
                        pending.setdefault(subject_of(subject), set()).add(other.name)
        return changes

    async def _recompute_one(
        self, tenant: str, plan: FigurePlan, subject: str, settings: Mapping[str, Any]
    ) -> Change | None:
        readers = await self._readers(tenant, plan, settings)
        result = evaluate(plan, subject, readers)
        held = await self._store.value(tenant, plan.name, plan.version, subject)

        label = await self._label(tenant, plan, subject)

        if isinstance(result.value, list) and not result.value and held is not None:
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
        if plan.day_keyed and plan.scope_index is not None:
            # A day-keyed figure's subjects are the days something happened on --
            # there is no roster of days, and writing a nought for every calendar
            # day would be a table of manufactured zeroes. Gated by the scope
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
            held = set(roster)
            for source, _ in plan.combines.values():
                source_plan = self._library.figure(source)
                if source_plan is None:
                    continue
                for stored in await self._store.values(tenant, source, source_plan.version):
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
            for subject in await self._scopes_of(tenant, plan, settings):
                if gaps_only and subject in have:
                    continue
                change = await self._recompute_one(tenant, plan, subject, settings)
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

        parts: dict[str, dict[str, list[tuple[str, float]]]] = {}
        for source, _ in plan.combines.values():
            source_plan = library.figure(source)
            if source_plan is None:
                continue
            table: dict[str, list[tuple[str, float]]] = {}
            for stored in await store.values(tenant, source, source_plan.version):
                if isinstance(stored.value, (int, float)):
                    table.setdefault(subject_of(stored.subject), []).append(
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

        return Readers(
            buckets=read_bucket,
            measures=read_measure,
            moments=read_moment,
            parts=read_parts,
            settings=read_setting,
        )

    def _indexes_over(self, kind: str) -> list[CompiledIndex]:
        return [i for i in self._library.indexes.values() if i.kind == kind]



def _parts_of(index: CompiledIndex) -> list[Any]:
    from ..lang.check import _index_fields  # local import: avoids a cycle at import time

    return _index_fields(index.spec)


def _pointer_for(plan: FigurePlan, settings: Mapping[str, Any]) -> Any:
    from ..store import Pointer

    return Pointer(
        version=plan.version,
        settings_fingerprint=settings_fingerprint(dict(settings), list(plan.settings)),
    )


def _now_ms() -> float:
    import time

    return time.time() * 1000.0
