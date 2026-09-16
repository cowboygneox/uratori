"""One value's working: the tree the evaluator actually walked, for one subject.

Everything here reads off the trace `evaluate()` leaves behind when its
`Readers` carries one -- see `evaluate.Readers.trace`. Nothing in this module
computes a figure's arithmetic a second way: the value beside every node comes
from `trace[id(node)]`, the exact number `_eval`/`_resolve` produced for that
AST node during the one real evaluation this call makes. Where a number is
needed that the calculation shape never puts through `_eval` at all -- a
record's raw measurement, a set's size before an operator narrowed it -- this
module reads the same reader callbacks the evaluator was given, never a
second implementation of them.

The store reads here are narrow on purpose: one subject's working needs a
sliver of what a full pass loads for every subject, so this is its own small
builder rather than a slice of `Engine._readers`, which loads whole tables
because it answers for every subject in one pass and narrowing it per call
would make the common path slower to save the rare one nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..lang.ast import (
    Arith,
    BucketAll,
    BucketScope,
    BucketStat,
    CalcExpr,
    Coord,
    Count,
    DaysBetween,
    Extreme,
    FieldPick,
    FieldTotal,
    FigureRef,
    FigureTotal,
    Ladder,
    ListOf,
    Number,
    Part,
    Pick,
    Rung,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    Setting,
    Spread,
    SubjectField,
    Sum,
    Text,
)
from ..lang.plan import FigurePlan, Library, Value
from ..results import Ok, Unavailable
from ..schema import Schema
from ..store import EngineStore, FactSource, StoredValue
from .buckets import SEPARATOR, measure_of, read_number, read_path, subject_of, tail_of
from .evaluate import Parts as _Parts
from .evaluate import Readers, Result, band_of, evaluate, same_value
from .evaluate import _band_operand as band_operand
from .evaluate import _band_word as band_word
from .evaluate import _compare as compare
from .project import format_value
from .serve import availability, band_thresholds, thresholds_for

RECORD_CAP = 60


@dataclass(frozen=True)
class RecordLine:
    key: str
    title: str | None
    url: str | None
    held: bool
    display: str | None
    role: Literal["counted", "nothing", "removed", "absent", "winner", "listed"]
    note: str | None = None


@dataclass(frozen=True)
class Step:
    op: Literal[
        "set", "set-index", "set-op",
        "count", "sum-measure", "list", "extreme", "stat", "field-total", "field-pick",
        "figure-total", "spread", "rollup", "part", "coord", "figure", "subject-field",
        "number", "text", "arith", "pick", "ladder", "rung", "otherwise", "days-between", "band",
    ]
    label: str
    display: str | None
    note: str | None = None
    verdict: Literal["matched", "failed", "unknown", "not-reached"] | None = None
    definition: str | None = None
    figure: str | None = None
    figure_subject: str | None = None
    bucket: str | None = None
    record_kind: str | None = None
    records: tuple[RecordLine, ...] = ()
    records_total: int = 0
    records_more: bool = False
    children: tuple[Step, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Working:
    figure: str
    version: str
    unit: str
    scope: str
    subject: str
    subject_key: str
    subject_name: str | None
    coordinate: str | None
    dimension: str | None
    sentence: str
    state: Ok | Unavailable
    stored: str | None
    level: str | None
    live: str | None
    agrees: bool | None
    note: str | None
    root: Step | None
    band: Step | None


# ----------------------------------------------------------------- fetch --


def _live_version(library: Library, name: str) -> str:
    below = library.figure(name)
    return below.version if below is not None else ""


def _file_part(
    parts: dict[str, dict[str, list[tuple[str, float]]]],
    source: str,
    stored: StoredValue | None,
) -> None:
    """The same filing `Engine._readers` does, narrowed to the rows this
    subject's working actually touches: under the value's own base subject,
    and -- for a coordinate -- under the full key too, so a bare read and a
    coordinate read both find it."""
    if stored is None or stored.value is None:
        return
    if not isinstance(stored.value, (int, float)):
        return
    table = parts.setdefault(source, {})
    table.setdefault(subject_of(stored.subject), []).append(
        (stored.subject, float(stored.value))
    )
    if SEPARATOR in stored.subject:
        table.setdefault(stored.subject, []).append((stored.subject, float(stored.value)))


async def _resolve_prefetch(
    store: EngineStore,
    library: Library,
    tenant: str,
    expr: SetExpr,
    subject: str,
    defined: dict[str, frozenset[str]],
    bucket_cache: dict[tuple[str, str | None], frozenset[str]],
) -> frozenset[str]:
    """Fetches the leaf bucket membership this expression needs and answers
    the resolved set -- the async twin of `evaluate._resolve_body`, kept
    separate because the real one is synchronous by design ("pure and
    synchronous") and a store read cannot be."""
    if isinstance(expr, SetIndex):
        if isinstance(expr.bucket, BucketScope):
            key: tuple[str, str | None] = (expr.index, subject)
            if key not in bucket_cache:
                bucket_cache[key] = await store.members(tenant, expr.index, subject)
            return bucket_cache[key]
        if isinstance(expr.bucket, BucketAll):
            none_key: tuple[str, str | None] = (expr.index, None)
            if none_key not in bucket_cache:
                spec = library.indexes[expr.index]
                if spec.bucketed:
                    merged: set[str] = set()
                    for members in (await store.all_buckets(tenant, expr.index)).values():
                        merged |= set(members)
                    bucket_cache[none_key] = frozenset(merged)
                else:
                    bucket_cache[none_key] = await store.members(tenant, expr.index, "")
            return bucket_cache[none_key]
        raise AssertionError("unreachable: every Bucket is Scope or All")
    if isinstance(expr, SetRef):
        return defined.get(expr.name, frozenset())
    if isinstance(expr, SetOp):
        left = await _resolve_prefetch(store, library, tenant, expr.left, subject, defined, bucket_cache)
        right = await _resolve_prefetch(store, library, tenant, expr.right, subject, defined, bucket_cache)
        if expr.op == "intersect":
            return left & right
        if expr.op == "union":
            return left | right
        if expr.op == "difference":
            return left - right
        raise AssertionError(f"unknown set op {expr.op}")
    raise AssertionError("unreachable: every SetExpr is Index, Ref or Op")


async def _prefetch_calc(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    subject: str,
    resolved: dict[str, frozenset[str]],
    parts: dict[str, dict[str, list[tuple[str, float]]]],
    records_needed: dict[str, set[str]],
    span_counts: dict[tuple[str, str], int],
    e: CalcExpr,
) -> None:
    """Walks one calculation, fetching from the store exactly what evaluating
    it -- and explaining it -- will need: a set's members are already
    resolved by the time this runs, so what remains is records for a measure
    or field read, and stored parts for anything naming another figure."""
    if isinstance(e, (Count, Number, Text, Setting, FigureRef, DaysBetween)):
        return
    if isinstance(e, ListOf):
        measure = library.measures[e.measure]
        records_needed.setdefault(measure.kind, set()).update(resolved.get(e.set, frozenset()))
        return
    if isinstance(e, Sum):
        if e.measure is not None:
            measure = library.measures[e.measure]
            records_needed.setdefault(measure.kind, set()).update(resolved.get(e.set, frozenset()))
            return
        source = plan.combines[e.set][0]
        version = _live_version(library, source)
        _file_part(parts, source, await store.value(tenant, source, version, subject))
        for row in await store.values_under(tenant, source, version, f"{subject}{SEPARATOR}"):
            _file_part(parts, source, row)
        return
    if isinstance(e, Extreme):
        measure = library.measures[e.measure]
        records_needed.setdefault(measure.kind, set()).update(resolved.get(e.set, frozenset()))
        return
    if isinstance(e, BucketStat):
        measure = library.measures[e.measure]
        records_needed.setdefault(measure.kind, set()).update(resolved.get(e.set, frozenset()))
        return
    if isinstance(e, FieldTotal):
        records_needed.setdefault(e.kind, set()).update(resolved.get(e.set, frozenset()))
        return
    if isinstance(e, FieldPick):
        records_needed.setdefault(e.kind, set()).update(resolved.get(e.set, frozenset()))
        return
    if isinstance(e, SubjectField):
        records_needed.setdefault(e.kind, set()).add(subject_of(subject))
        return
    if isinstance(e, Spread):
        version = _live_version(library, e.figure)
        base = subject_of(subject)
        _file_part(parts, e.figure, await store.value(tenant, e.figure, version, base))
        index = plan.scope_index or ""
        # Counted the way the pass counts it (`Engine._readers`): how many of
        # the grouping's buckets belong to this SUBJECT, by the bucket key's
        # subject part -- never how many buckets hold the subject as a
        # member. The two coincide only where a grouping's subject and its
        # members share ids; under `from (account_id, <span>)` the members
        # are campaigns, membership finds nothing, and the divisor is nought
        # while the pass's is five -- a blank worksheet under a computed
        # value, with nothing thrown.
        keys = await store.bucket_keys(tenant, index)
        span_counts[(index, base)] = sum(1 for key in keys if subject_of(key) == base)
        return
    if isinstance(e, FigureTotal):
        version = _live_version(library, e.figure)
        tail = tail_of(subject)
        for member in resolved.get(e.set, frozenset()):
            key = f"{member}{SEPARATOR}{tail}" if tail is not None else member
            _file_part(parts, e.figure, await store.value(tenant, e.figure, version, key))
        return
    if isinstance(e, (Part, Coord)):
        source = plan.combines[e.name][0]
        version = _live_version(library, source)
        _file_part(parts, source, await store.value(tenant, source, version, subject))
        return
    if isinstance(e, Ladder):
        for rung in e.rungs:
            await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, rung.left)
            if rung.right is not None:
                await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, rung.right)
            await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, rung.then)
        await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, e.otherwise)
        return
    if isinstance(e, (Arith, Pick)):
        await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, e.left)
        await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, e.right)
        return
    raise AssertionError(f"unhandled calculation shape {type(e)}")


# ------------------------------------------------------------------ ctx --


@dataclass
class _Ctx:
    library: Library
    schema: Schema
    plan: FigurePlan
    subject: str
    subject_name: str
    resolved: dict[str, frozenset[str]]
    records: dict[str, dict[str, Mapping[str, Any]]]
    parts: dict[str, dict[str, list[tuple[str, float]]]]
    span_counts: dict[tuple[str, str], int]
    trace: dict[int, object]
    bucket_cache: dict[tuple[str, str | None], frozenset[str]]


def _fmt(value: Value, unit: str) -> str | None:
    if value is None:
        return None
    return format_value(value, unit)  # type: ignore[arg-type]


def _traced(trace: Mapping[int, object], node: object) -> Value:
    """A node's value as the real evaluation left it, cast back from the
    trace's `object` storage to the `Value` it always actually holds --
    `evaluate.Readers.trace` is typed loosely because it also carries
    frozensets for set nodes, which `Value` does not include."""
    got = trace.get(id(node))
    return got  # type: ignore[return-value]


def _traced_set(trace: Mapping[int, object], node: object) -> frozenset[str]:
    got = trace.get(id(node), frozenset())
    return got  # type: ignore[return-value]


def _measure_noun(name: str) -> str:
    """The measure's own name, the way a reader would ask for it: the part
    after the kind, because "no estimate" reads like an answer and "no
    work_issue.estimate" reads like a stack trace."""
    return name.split(".", 1)[1] if "." in name else name


def _article(noun: str) -> str:
    """`an estimate`, `a cost`: the indefinite article a sentence about one
    measurement needs, decided by the noun's first letter."""
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _field_text(record: Mapping[str, Any] | None, field_name: str | None) -> str | None:
    # `read_path`, not `read_values`: this produces a record's *display name*,
    # and a blank name is not a name -- the next line already strips `""`
    # explicitly, so this does not depend on which reader dropped it first.
    if record is None or field_name is None:
        return None
    found = read_path(record, field_name)
    text = found[0] if found else None
    return text if isinstance(text, str) and text else None


def _record_lines(
    ctx: _Ctx,
    kind: str | None,
    keys: list[str],
    displays: Mapping[str, str | None],
    roles: Mapping[str, str],
    notes: Mapping[str, str | None],
) -> tuple[tuple[RecordLine, ...], int, bool]:
    total = len(keys)
    capped = keys[:RECORD_CAP]
    name_field = ctx.schema.name_fields.get(kind) if kind else None
    url_field = ctx.schema.url_fields.get(kind) if kind else None
    kind_records = ctx.records.get(kind, {}) if kind else {}
    lines = []
    for key in capped:
        record = kind_records.get(key)
        role = roles.get(key, "counted")
        assert role in ("counted", "nothing", "removed", "absent", "winner", "listed")
        lines.append(
            RecordLine(
                key=key,
                title=_field_text(record, name_field),
                url=_field_text(record, url_field),
                held=record is not None,
                display=displays.get(key),
                role=role,  # type: ignore[arg-type]
                note=notes.get(key),
            )
        )
    return tuple(lines), total, total > RECORD_CAP


# ------------------------------------------------------------- unparser --

_OP_SYMBOL = {"intersect": "&", "union": "|", "difference": "-"}


def _set_leaf_label(expr: SetIndex, subject_name: str) -> str:
    if isinstance(expr.bucket, BucketScope):
        return f"{expr.index}:{{{subject_name}}}"
    return expr.index


def _set_expr_label(expr: SetExpr, subject_name: str) -> str:
    if isinstance(expr, SetIndex):
        return _set_leaf_label(expr, subject_name)
    if isinstance(expr, SetRef):
        return expr.name
    if isinstance(expr, SetOp):
        sym = _OP_SYMBOL[expr.op]
        return f"{_set_expr_label(expr.left, subject_name)} {sym} {_set_expr_label(expr.right, subject_name)}"
    raise AssertionError("unreachable: every SetExpr is Index, Ref or Op")


def _spine(expr: SetExpr) -> list[SetExpr]:
    """The chain of nodes the pass actually recurses through, left first: a
    leaf, then each `SetOp` along the left spine in the order it was
    applied. Each `SetOp`'s own trace entry is the resolved set *after* that
    operator, and its `.left`'s trace entry is the set *before* it -- which
    is what lets the working show what an operator removed without doing
    the set algebra a second time."""
    if isinstance(expr, SetOp):
        return [*_spine(expr.left), expr]
    return [expr]


def _calc_label(e: CalcExpr, ctx: _Ctx) -> str:
    if isinstance(e, Count):
        return f"count({e.set})"
    if isinstance(e, ListOf):
        return f"list({e.measure} over {e.set})"
    if isinstance(e, Sum):
        if e.measure is not None:
            return f"sum({e.measure} over {e.set})"
        return f"sum({e.set})"
    if isinstance(e, Extreme):
        return f"{e.which}({e.measure} over {e.set})"
    if isinstance(e, BucketStat):
        return f"{e.fn}({e.measure} over {e.set})"
    if isinstance(e, FieldTotal):
        return f"sum({e.kind}.{e.field} over {e.set})"
    if isinstance(e, FieldPick):
        return f"{e.which}({e.kind}.{e.field} over {e.set})"
    if isinstance(e, FigureTotal):
        return f"sum({e.figure} over {e.set})"
    if isinstance(e, Spread):
        return f"spread({e.figure} over {e.set})"
    if isinstance(e, (Part, Coord)):
        return ctx.plan.combines.get(e.name, (e.name, None))[0]
    if isinstance(e, Number):
        return format_value(e.value, "count")
    if isinstance(e, Text):
        return f'"{e.value}"'
    if isinstance(e, FigureRef):
        return e.name
    if isinstance(e, SubjectField):
        return f"{e.kind}.{e.field}"
    if isinstance(e, Arith):
        return f"{_calc_label(e.left, ctx)} {e.op} {_calc_label(e.right, ctx)}"
    if isinstance(e, Pick):
        return f"{e.which}({_calc_label(e.left, ctx)}, {_calc_label(e.right, ctx)})"
    if isinstance(e, DaysBetween):
        return f"days from {e.frm} to {e.to}"
    if isinstance(e, Ladder):
        return "ladder"
    raise AssertionError(f"unhandled label shape {type(e)}")


def _rung_label(rung: Rung, ctx: _Ctx) -> str:
    left = _calc_label(rung.left, ctx)
    then = _calc_label(rung.then, ctx)
    if rung.op == "nothing":
        return f"when {left} is nothing then {then}"
    if rung.op == "something":
        return f"when {left} is something then {then}"
    right = _calc_label(rung.right, ctx) if rung.right is not None else ""
    return f"when {left} {rung.op} {right} then {then}"


def _kind_for_name(name: str, ctx: _Ctx) -> str | None:
    return _kind_for_expr(ctx.plan.sets.get(name), ctx)


def _kind_for_expr(expr: SetExpr | None, ctx: _Ctx) -> str | None:
    if expr is None:
        return None
    if isinstance(expr, SetIndex):
        return ctx.library.indexes[expr.index].kind
    if isinstance(expr, SetRef):
        return _kind_for_name(expr.name, ctx)
    return _kind_for_expr(expr.left, ctx) or _kind_for_expr(expr.right, ctx)


# -------------------------------------------------------------- sets step --


def _set_operand_step(node: SetExpr, members: frozenset[str], ctx: _Ctx) -> Step:
    if isinstance(node, SetIndex):
        kind = ctx.library.indexes[node.index].kind
        lines, total, more = _record_lines(
            ctx, kind, sorted(members), {}, dict.fromkeys(members, "counted"), {}
        )
        return Step(
            op="set-index",
            label=_set_leaf_label(node, ctx.subject_name),
            display=f"{len(members)} records",
            definition=node.index,
            bucket=ctx.subject if isinstance(node.bucket, BucketScope) else None,
            record_kind=kind,
            records=lines,
            records_total=total,
            records_more=more,
        )
    if isinstance(node, SetRef):
        return Step(op="set", label=node.name, display=f"{len(members)} records")
    raise AssertionError("unreachable: a spine's first node is a leaf")


def _set_step(name: str, ctx: _Ctx) -> Step:
    expr = ctx.plan.sets.get(name)
    members = ctx.resolved.get(name, frozenset())
    if expr is None or isinstance(expr, SetRef):
        return Step(op="set", label=name, display=f"{len(members)} records")

    label = f"{name} = {_set_expr_label(expr, ctx.subject_name)}"
    spine = _spine(expr)
    children = [_set_operand_step(spine[0], _traced_set(ctx.trace, spine[0]), ctx)]
    for node in spine[1:]:
        assert isinstance(node, SetOp)
        before = _traced_set(ctx.trace, node.left)
        after = _traced_set(ctx.trace, node)
        removed = before - after if node.op != "union" else frozenset()
        sym = _OP_SYMBOL[node.op]
        operand_label = _set_expr_label(node.right, ctx.subject_name)
        lines, total, more = _record_lines(
            ctx,
            None,
            sorted(removed),
            {},
            dict.fromkeys(removed, "removed"),
            {k: f"removed by {sym} {operand_label}" for k in removed},
        )
        children.append(
            Step(
                op="set-op",
                label=f"{sym} {operand_label}",
                display=f"{len(after)} remain",
                records=lines,
                records_total=total,
                records_more=more,
            )
        )
    return Step(op="set", label=label, display=f"{len(members)} records", children=tuple(children))


# ------------------------------------------------------------- main step --


def _step(e: CalcExpr, ctx: _Ctx) -> Step:
    unit = ctx.plan.unit
    value = _traced(ctx.trace, e)

    if isinstance(e, Count):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        kind = _kind_for_name(e.set, ctx)
        lines, total, more = _record_lines(
            ctx, kind, members, {}, dict.fromkeys(members, "counted"), {}
        )
        note = (
            f"showing {RECORD_CAP} of {total}; the whole bucket is on the group's page."
            if more
            else None
        )
        return Step(
            op="count", label=_calc_label(e, ctx), display=_fmt(value, "count"), note=note,
            record_kind=kind, records=lines, records_total=total, records_more=more,
            children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, ListOf):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        measure = ctx.library.measures[e.measure]
        kind = measure.kind
        displays: dict[str, str | None] = {}
        roles: dict[str, str] = {}
        notes: dict[str, str | None] = {}
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = measure_of(measure, record, None) if record is not None else None
            if got is None:
                roles[m] = "nothing"
                notes[m] = f"no {_measure_noun(e.measure)} -- left out of the list."
            else:
                roles[m] = "listed"
                displays[m] = _fmt(got, unit)
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, notes)
        return Step(
            op="list", label=_calc_label(e, ctx), display=_fmt(value, unit),
            definition=e.measure, record_kind=kind, records=lines, records_total=total,
            records_more=more, children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, Sum) and e.measure is not None:
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        measure = ctx.library.measures[e.measure]
        kind = measure.kind
        displays = {}
        roles = {}
        notes = {}
        carrying = 0
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = measure_of(measure, record, None) if record is not None else None
            if got is None:
                roles[m] = "nothing"
                notes[m] = f"no {_measure_noun(e.measure)}"
            else:
                roles[m] = "counted"
                displays[m] = _fmt(got, unit)
                carrying += 1
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, notes)
        n = len(members)
        noun = _measure_noun(e.measure)
        if n == 0:
            summary = "nothing was measurable, so the answer is absent -- never 1970, never nought."
        elif carrying == n:
            summary = f"all {n} records carry {_article(noun)} {noun}."
        else:
            summary = (
                f"{carrying} of {n} records carry {_article(noun)} {noun} and add up to "
                f"{_fmt(value, unit)}; {n - carrying} carry none and contributed nothing."
            )
        cap_note = (
            f"showing {RECORD_CAP} of {total}; the whole bucket is on the group's page."
            if more
            else None
        )
        note = " ".join(x for x in (summary, cap_note) if x)
        return Step(
            op="sum-measure", label=_calc_label(e, ctx), display=_fmt(value, unit), note=note,
            definition=e.measure, record_kind=kind, records=lines, records_total=total,
            records_more=more, children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, Sum) and e.measure is None:
        source = ctx.plan.combines[e.set][0]
        source_plan = ctx.library.figure(source)
        source_unit = source_plan.unit if source_plan is not None else unit
        held = ctx.parts.get(source, {}).get(ctx.subject, [])
        children = [
            Step(
                op="part",
                label=tail_of(key) or key,
                display=_fmt(v, source_unit),
                figure=source,
                figure_subject=key,
                definition=source,
            )
            for key, v in sorted(held)
        ]
        return Step(op="rollup", label=_calc_label(e, ctx), display=_fmt(value, unit), children=tuple(children))

    if isinstance(e, Extreme):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        measure = ctx.library.measures[e.measure]
        kind = measure.kind
        displays = {}
        roles = {}
        winner: str | None = None
        best: float | None = None
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = measure_of(measure, record, None) if record is not None else None
            if got is None:
                roles[m] = "nothing"
            else:
                roles[m] = "counted"
                displays[m] = _fmt(got, unit)
                if best is None or (got > best if e.which == "latest" else got < best):
                    best = got
                    winner = m
        if winner is not None:
            roles[winner] = "winner"
        note = (
            "nothing was measurable, so the answer is absent -- never 1970, never nought."
            if winner is None
            else None
        )
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, {})
        return Step(
            op="extreme", label=_calc_label(e, ctx), display=_fmt(value, unit), note=note,
            definition=e.measure, record_kind=kind, records=lines, records_total=total,
            records_more=more, children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, BucketStat):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        measure = ctx.library.measures[e.measure]
        kind = measure.kind
        displays = {}
        roles = {}
        any_measured = False
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = measure_of(measure, record, None) if record is not None else None
            if got is None:
                roles[m] = "nothing"
            else:
                roles[m] = "counted"
                displays[m] = _fmt(got, unit)
                any_measured = True
        note = (
            None
            if any_measured
            else "nothing was measurable, so the answer is absent -- never 1970, never nought."
        )
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, {})
        return Step(
            op="stat", label=_calc_label(e, ctx), display=_fmt(value, unit), note=note,
            definition=e.measure, record_kind=kind, records=lines, records_total=total,
            records_more=more, children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, FieldTotal):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        kind = e.kind
        displays = {}
        roles = {}
        notes = {}
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = read_number(record, e.field) if record is not None else None
            if got is None:
                roles[m] = "nothing"
                notes[m] = f"no {e.field}"
            else:
                roles[m] = "counted"
                displays[m] = _fmt(got, unit)
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, notes)
        return Step(
            op="field-total", label=_calc_label(e, ctx), display=_fmt(value, unit),
            record_kind=kind, records=lines, records_total=total, records_more=more,
            children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, FieldPick):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        kind = e.kind
        roles = dict.fromkeys(members, "counted")
        displays = {}
        for m in members:
            record = ctx.records.get(kind, {}).get(m)
            got = read_number(record, e.field) if record is not None else None
            if got is not None:
                displays[m] = _fmt(got, unit)
        lines, total, more = _record_lines(ctx, kind, members, displays, roles, {})
        return Step(
            op="field-pick", label=_calc_label(e, ctx), display=_fmt(value, unit),
            record_kind=kind, records=lines, records_total=total, records_more=more,
            children=(_set_step(e.set, ctx),),
        )

    if isinstance(e, FigureTotal):
        members = sorted(ctx.resolved.get(e.set, frozenset()))
        source_plan = ctx.library.figure(e.figure)
        source_unit = source_plan.unit if source_plan is not None else unit
        tail = tail_of(ctx.subject)
        children = []
        for m in members:
            key = f"{m}{SEPARATOR}{tail}" if tail is not None else m
            held = ctx.parts.get(e.figure, {}).get(key, [])
            v = held[0][1] if held else None
            note = (
                None
                if v is not None
                else f"no value stored at {tail} -- an absent contribution makes the whole total absent."
            )
            children.append(
                Step(
                    op="part", label=m, display=_fmt(v, source_unit), figure=e.figure,
                    figure_subject=key, definition=e.figure, note=note,
                )
            )
        return Step(op="figure-total", label=_calc_label(e, ctx), display=_fmt(value, unit), children=tuple(children))

    if isinstance(e, Spread):
        source_plan = ctx.library.figure(e.figure)
        source_unit = source_plan.unit if source_plan is not None else unit
        base = subject_of(ctx.subject)
        held = ctx.parts.get(e.figure, {}).get(base, [])
        v = held[0][1] if held else None
        part_step = Step(
            op="part", label=e.figure, display=_fmt(v, source_unit), figure=e.figure,
            figure_subject=base, definition=e.figure,
        )
        buckets_held = ctx.span_counts.get((ctx.plan.scope_index or "", base), 0)
        divisor_step = Step(op="number", label="buckets held", display=f"{buckets_held} buckets")
        return Step(op="spread", label=_calc_label(e, ctx), display=_fmt(value, unit), children=(part_step, divisor_step))

    if isinstance(e, (Part, Coord)):
        source = ctx.plan.combines[e.name][0]
        source_plan = ctx.library.figure(source)
        source_unit = source_plan.unit if source_plan is not None else unit
        held = ctx.parts.get(source, {}).get(ctx.subject, [])
        v = held[0][1] if held else None
        version = source_plan.version if source_plan is not None else "?"
        note = None if v is not None else f"no value stored for {ctx.subject} under {source}@{version}."
        op: Literal["part", "coord"] = "part" if isinstance(e, Part) else "coord"
        return Step(
            op=op, label=_calc_label(e, ctx), display=_fmt(v, source_unit), figure=source,
            figure_subject=ctx.subject, definition=source, note=note,
        )

    if isinstance(e, Number):
        return Step(op="number", label=_calc_label(e, ctx), display=_calc_label(e, ctx))

    if isinstance(e, Text):
        return Step(op="text", label=e.value, display=e.value)

    if isinstance(e, SubjectField):
        base = subject_of(ctx.subject)
        lines, total, more = _record_lines(
            ctx, e.kind, [base], {base: _fmt(value, unit)}, {base: "counted"}, {}
        )
        return Step(
            op="subject-field", label=_calc_label(e, ctx), display=_fmt(value, unit),
            record_kind=e.kind, records=lines, records_total=total, records_more=more,
        )

    if isinstance(e, Arith):
        left_step = _step(e.left, ctx)
        right_step = _step(e.right, ctx)
        note = None
        if value is None:
            rv = _traced(ctx.trace, e.right)
            if e.op == "/" and isinstance(rv, (int, float)) and float(rv) == 0.0:
                note = "division by nought answers nothing."
            else:
                note = "an absent operand makes the result absent."
        return Step(op="arith", label=_calc_label(e, ctx), display=_fmt(value, unit), note=note, children=(left_step, right_step))

    if isinstance(e, Pick):
        left_step = _step(e.left, ctx)
        right_step = _step(e.right, ctx)
        note = None if value is not None else "an absent operand makes the result absent."
        return Step(op="pick", label=_calc_label(e, ctx), display=_fmt(value, unit), note=note, children=(left_step, right_step))

    if isinstance(e, Ladder):
        children = []
        for rung in e.rungs:
            verdict = _rung_verdict(rung, ctx.trace)
            rung_children = [_step(rung.left, ctx)]
            if rung.right is not None:
                rung_children.append(_step(rung.right, ctx))
            note = (
                "a ladder stops on an unknown rather than falling through."
                if verdict == "unknown"
                else None
            )
            children.append(
                Step(
                    op="rung", label=_rung_label(rung, ctx),
                    display=_calc_label(rung.then, ctx) if verdict == "matched" else None,
                    verdict=verdict, note=note, children=tuple(rung_children),
                )
            )
        stopped = any(c.verdict in ("matched", "unknown") for c in children)
        # The same rule the rungs follow: a word is displayed only by the rung
        # that answered it. `otherwise` showing its word while unreached would
        # print two answers in one ladder's value column.
        children.append(
            Step(
                op="otherwise", label=f"otherwise {_calc_label(e.otherwise, ctx)}",
                display=None if stopped else _calc_label(e.otherwise, ctx),
                verdict="not-reached" if stopped else "matched",
            )
        )
        return Step(op="ladder", label="ladder", display=_fmt(value, unit), children=tuple(children))

    raise AssertionError(f"unhandled calculation shape {type(e)}")


def _rung_verdict(rung: Rung, trace: Mapping[int, object]) -> Literal["matched", "failed", "unknown", "not-reached"]:
    if id(rung) not in trace:
        return "not-reached"
    verdict = trace[id(rung)]
    if verdict is True:
        return "matched"
    if verdict is False:
        return "failed"
    return "unknown"


# --------------------------------------------------------------- readers --


def _make_readers(ctx: _Ctx, trace: dict[int, object]) -> Readers:
    # The prefetch pass filled `bucket_cache` with every leaf `SetIndex`'s
    # membership; `evaluate()` itself redoes the set algebra (union, difference,
    # intersect) over this closure, which is what leaves a trace entry on
    # every set node -- the tree below reads those entries rather than
    # recomputing the algebra a second time.
    def read_bucket(index: str, bucket: str | None) -> frozenset[str]:
        return ctx.bucket_cache.get((index, bucket), frozenset())

    def read_measure(name: str, member: str) -> float | None:
        measure = ctx.library.measures[name]
        record = ctx.records.get(measure.kind, {}).get(member)
        if record is None:
            return None
        return measure_of(measure, record, None)

    def read_parts(figure: str, subj: str) -> _Parts:
        held = ctx.parts.get(figure, {}).get(subj, [])
        return _Parts(values=tuple(v for _, v in held), subjects=tuple(s for s, _ in held))

    def read_setting(path: str) -> float:  # pragma: no cover - no plan holds one
        raise KeyError(f'"{path}" is a settings dial, and no compiled definition can name one.')

    def read_subject_field(kind: str, field_name: str, subj: str) -> float | None:
        record = ctx.records.get(kind, {}).get(subj)
        return None if record is None else read_number(record, field_name)

    def read_field(kind: str, path: str, member: str) -> float | None:
        record = ctx.records.get(kind, {}).get(member)
        return None if record is None else read_number(record, path)

    def read_instant_field(kind: str, path: str, member: str) -> float | None:
        from .buckets import read_instant

        record = ctx.records.get(kind, {}).get(member)
        return None if record is None else read_instant(record, path)

    def read_spans(index: str, member: str) -> int:
        return ctx.span_counts.get((index, member), 0)

    return Readers(
        buckets=read_bucket,
        measures=read_measure,
        moments=read_measure,
        parts=read_parts,
        settings=read_setting,
        fields=read_field,
        instants=read_instant_field,
        subject_fields=read_subject_field,
        spans=read_spans,
        trace=trace,
    )


# ---------------------------------------------------------------- build --


def _level(word: str | None) -> str | None:
    return word


def _agreement(stored: Value, live: Value, unit: str) -> tuple[bool | None, str | None]:
    if stored is None:
        return None, None
    if isinstance(stored, (int, float)) and isinstance(live, (int, float)):
        agree = abs(float(stored) - float(live)) < 1e-6
    else:
        agree = same_value(stored, live)
    if agree:
        return True, None
    sd = _fmt(stored, unit) or "—"
    ld = _fmt(live, unit) or "absent"
    note = f"Stored {sd}; re-derived now {ld} -- a record moved since the pass that wrote this row."
    return False, note


def _sentence(plan: FigurePlan, subject_label: str, coordinate: str | None, stored_display: str | None) -> str:
    text = plan.display
    text = text.replace("{" + plan.scope + "}", subject_label)
    if plan.across is not None and coordinate is not None:
        text = text.replace("{" + plan.across + "}", coordinate)
    text = text.replace("{value}", stored_display if stored_display is not None else "—")
    return text


def _band_operand_step(e: CalcExpr, value: Value, thresholds: Mapping[str, Value], unit: str, ctx: _Ctx) -> Step:
    if isinstance(e, Part):
        return Step(op="number", label="value", display=_fmt(value, unit))
    if isinstance(e, Number):
        return Step(op="number", label=_calc_label(e, ctx), display=_calc_label(e, ctx))
    if isinstance(e, Text):
        return Step(op="text", label=e.value, display=e.value)
    if isinstance(e, (FigureRef, Coord)):
        v = thresholds.get(e.name)
        source_plan = ctx.library.figure(e.name)
        source_unit = source_plan.unit if source_plan is not None else unit
        return Step(
            op="figure", label=e.name, display=_fmt(v, source_unit), figure=e.name,
            figure_subject=ctx.subject, definition=e.name,
            note=None if v is not None else f"no value stored for {ctx.subject} under {e.name}.",
        )
    if isinstance(e, SubjectField):
        v = thresholds.get(f"{e.kind}.{e.field}")
        return Step(op="subject-field", label=f"{e.kind}.{e.field}", display=_fmt(v, unit))
    return Step(op="number", label="?", display=None)


def _band_step(ladder: Ladder, value: Value, thresholds: Mapping[str, Value], unit: str, ctx: _Ctx) -> Step:
    """The band ladder, walked with the exact same pure helpers `band_of`
    uses (`_band_operand`/`_compare`/`_band_word`, imported here rather than
    re-derived) so this narrates the one real judgement rather than a second
    one that could disagree with it."""
    children = []
    stopped = False
    for rung in ladder.rungs:
        left = band_operand(rung.left, value, thresholds)
        right = band_operand(rung.right, value, thresholds) if rung.right is not None else None
        if stopped:
            verdict: Literal["matched", "failed", "unknown", "not-reached"] = "not-reached"
        else:
            raw = compare(left, rung.op, right)
            if raw is None:
                verdict = "unknown"
                stopped = True
            elif raw:
                verdict = "matched"
                stopped = True
            else:
                verdict = "failed"
        rung_children = [_band_operand_step(rung.left, value, thresholds, unit, ctx)]
        if rung.right is not None:
            rung_children.append(_band_operand_step(rung.right, value, thresholds, unit, ctx))
        note = "a ladder stops on an unknown rather than falling through." if verdict == "unknown" else None
        children.append(
            Step(
                op="rung", label=_rung_label(rung, ctx),
                display=band_word(rung.then) if verdict == "matched" else None,
                verdict=verdict, note=note, children=tuple(rung_children),
            )
        )
    word = band_of(ladder, value, thresholds)
    children.append(
        Step(
            op="otherwise", label=f"otherwise \"{band_word(ladder.otherwise) or ''}\"",
            display=None if stopped else band_word(ladder.otherwise),
            verdict="not-reached" if stopped else "matched",
        )
    )
    return Step(op="band", label="band", display=word, children=tuple(children))


async def build(
    store: EngineStore,
    facts: FactSource,
    library: Library,
    schema: Schema,
    tenant: str,
    plan: FigurePlan,
    subject: str,
) -> Working | None:
    """One stored value's working, all the way to the facts.

    `None` means the figure is available and this subject has no row -- the
    same contract `serve_evidence` keeps, and for the same reason: an address
    naming nothing is a 404, not a confident empty tree.
    """
    state = await availability(store, library, tenant, plan)
    if not isinstance(state, Ok):
        return Working(
            figure=plan.name, version=plan.version, unit=plan.unit, scope=plan.scope,
            subject=subject, subject_key=subject_of(subject), subject_name=None,
            coordinate=tail_of(subject), dimension=plan.across, sentence=plan.display,
            state=state, stored=None, level=None, live=None, agrees=None, note=None,
            root=None, band=None,
        )

    stored = await store.value(tenant, plan.name, plan.version, subject)
    if stored is None:
        return None

    bucket_cache: dict[tuple[str, str | None], frozenset[str]] = {}
    resolved: dict[str, frozenset[str]] = {}
    for name, expr in plan.sets.items():
        resolved[name] = await _resolve_prefetch(store, library, tenant, expr, subject, resolved, bucket_cache)

    parts: dict[str, dict[str, list[tuple[str, float]]]] = {}
    records_needed: dict[str, set[str]] = {}
    span_counts: dict[tuple[str, str], int] = {}
    await _prefetch_calc(store, library, tenant, plan, subject, resolved, parts, records_needed, span_counts, plan.calculate)

    # Every set-index leaf touched needs its own records too, so the record
    # ledger under a `set`/`set-index`/`count` node can name titles and
    # links, not just bare keys.
    for (index_name, _bucket), members in bucket_cache.items():
        kind = library.indexes[index_name].kind
        records_needed.setdefault(kind, set()).update(members)

    records: dict[str, dict[str, Mapping[str, Any]]] = {}
    for kind, ids in records_needed.items():
        if not ids:
            continue
        rows = await facts.some(tenant, kind, sorted(ids))
        records[kind] = {row.key: row.value for row in rows}

    subject_name = stored.label
    ctx = _Ctx(
        library=library, schema=schema, plan=plan, subject=subject, subject_name=subject_name,
        resolved=resolved, records=records, parts=parts, span_counts=span_counts, trace={},
        bucket_cache=bucket_cache,
    )

    readers = _make_readers(ctx, ctx.trace)
    result: Result = evaluate(plan, subject, readers)
    live_value = result.value

    display_stored = _fmt(stored.value, plan.unit)
    display_live = _fmt(live_value, plan.unit)
    agrees, note = _agreement(stored.value, live_value, plan.unit)

    thresholds = await band_thresholds(store, library, tenant, plan, facts)
    row_thresholds = thresholds_for(thresholds, subject)
    level = band_of(plan.band, stored.value, row_thresholds) if plan.band is not None else None

    root = _step(plan.calculate, ctx)
    band_tree = _band_step(plan.band, stored.value, row_thresholds, plan.unit, ctx) if plan.band is not None else None

    sentence = _sentence(plan, subject_name, tail_of(subject), display_stored)

    return Working(
        figure=plan.name, version=plan.version, unit=plan.unit, scope=plan.scope,
        subject=subject, subject_key=subject_of(subject), subject_name=subject_name,
        coordinate=tail_of(subject), dimension=plan.across, sentence=sentence,
        state=state, stored=display_stored, level=level, live=display_live,
        agrees=agrees, note=note, root=root, band=band_tree,
    )
