"""Projections and summaries: one row per record, and one row about them all.

A projection is the only thing here that may read a clock *and* produce prose,
and it is safe with both for exactly one reason: **it stores nothing**. A row is
assembled when somebody asks for it, at one instant passed in by the caller, so
the question of a value going stale never arises.

`at` is a parameter and never the current time, so one instant reaches every row
and the page cannot disagree with itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..lang.ast import (
    Arith,
    CalcExpr,
    Condition,
    DaysBetween,
    FigureUnit,
    FlagDecl,
    Ladder,
    Number,
    Part,
    Pick,
    Setting,
    Text,
)
from ..lang.plan import ProjectPlan, SummarisePlan, Value
from ..lang.settings import setting_value
from .buckets import days_between, parse_instant, read_number, read_path
from .evaluate import _arith, _compare  # one implementation of each, shared deliberately


@dataclass(frozen=True)
class RenderedFlag:
    name: str
    label: str
    detail: str
    action: str | None
    severity: str


@dataclass(frozen=True)
class ProjectedRow:
    id: str
    values: dict[str, Value]
    units: dict[str, FigureUnit]
    flags: tuple[RenderedFlag, ...]
    sort_key: float | str | None


JoinResolver = Any
"""(kind, path, value) -> the records holding it. Supplied by the caller, which
is what keeps this module pure."""


def project(
    plan: ProjectPlan,
    record_id: str,
    record: Mapping[str, Any],
    # This subject's figure answers, keyed by (figure name, is this the band).
    # The band is resolved by the *caller* rather than here, because it is
    # derived from the figure's plan and this function is handed values only --
    # passing the whole library in so one column could be computed would make a
    # row's evaluation depend on every definition on the board.
    figures: Mapping[tuple[str, bool], Value],
    joins: Mapping[tuple[str, str], Mapping[str, list[Mapping[str, Any]]]],
    settings: Mapping[str, Any],
    at_ms: float,
) -> ProjectedRow:
    values: dict[str, Value] = {}
    units: dict[str, FigureUnit] = {}
    moments: dict[str, float | None] = {}

    for name, path, ftype, join in plan.fields:
        raw = _field_value(record, path, ftype, join, joins)
        if ftype == "date":
            moments[name] = parse_instant(raw) if isinstance(raw, str) else None
            values[name] = raw
            units[name] = "moment"
        elif ftype == "number":
            values[name] = raw if isinstance(raw, (int, float)) else None
            units[name] = "count"
        elif ftype == "flag":
            values[name] = 1.0 if raw is True else 0.0 if raw is False else None
            units[name] = "count"
        else:
            values[name] = raw if isinstance(raw, str) else None
            units[name] = "count"

    for name, figure, unit, band in plan.reads:
        # Keyed by (figure, band) rather than by name alone: one projection may
        # bind both a figure's number and its band, and a single key would give
        # whichever the caller wrote last to both columns.
        held = figures.get((figure, band))
        values[name] = held
        units[name] = unit
        if unit == "moment":
            moments[name] = held if isinstance(held, (int, float)) else None

    for name, expr, unit in plan.values:
        values[name] = _eval(expr, values, moments, settings, at_ms)
        units[name] = unit

    flags = tuple(
        rendered
        for flag in plan.flags
        if (rendered := _flag(flag, values, units, settings, at_ms)) is not None
    )

    sort_key: float | str | None = None
    if plan.sort is not None:
        held = values.get(plan.sort.name)
        sort_key = held if isinstance(held, (int, float, str)) else None

    return ProjectedRow(
        id=record_id, values=values, units=units, flags=flags, sort_key=sort_key
    )


def _field_value(
    record: Mapping[str, Any],
    path: str,
    ftype: str,
    join: Any,
    joins: Mapping[tuple[str, str], Mapping[str, list[Mapping[str, Any]]]],
) -> Any:
    if join is not None:
        # **Anything other than exactly one match is nothing**, at both ends. A
        # relation resolves to every owner on purpose -- an id claimed by two
        # records is a data problem an index should reflect rather than pick a
        # winner for -- but a field holds one value, so the choice here is between
        # picking one and admitting there is no answer. Picking would be stable
        # and would still be a fabrication, and worse than for a plain field:
        # this picks among several *records*, so the row would be about the wrong
        # thing with nothing saying a second candidate existed.
        local = read_path(record, join.field)
        if len(local) != 1:
            return None
        found = joins.get((join.kind, join.path), {}).get(local[0], [])
        if len(found) != 1:
            return None
        record = found[0]

    if ftype == "number":
        return read_number(record, path)
    if ftype == "flag":
        raw = read_path(record, path)
        if len(raw) != 1:
            return None
        return raw[0] == "true"
    raw = read_path(record, path)
    # A plain field with several values takes the first in sorted order: the
    # ambiguity is inside one record and the row is at least about the right
    # thing, which is not true of a join.
    return sorted(raw)[0] if raw else None


def _eval(
    e: CalcExpr,
    values: Mapping[str, Value],
    moments: Mapping[str, float | None],
    settings: Mapping[str, Any],
    at_ms: float,
) -> Value:
    if isinstance(e, Number):
        return e.value
    if isinstance(e, Text):
        return e.value
    if isinstance(e, Setting):
        return float(setting_value(dict(settings), e.path))
    if isinstance(e, Part):
        return values.get(e.name)
    if isinstance(e, DaysBetween):
        start = at_ms if e.frm == "now" else moments.get(e.frm)
        end = at_ms if e.to == "now" else moments.get(e.to)
        return days_between(start, end)
    if isinstance(e, Arith):
        return _arith(
            e.op,
            _eval(e.left, values, moments, settings, at_ms),
            _eval(e.right, values, moments, settings, at_ms),
        )
    if isinstance(e, Pick):
        left = _eval(e.left, values, moments, settings, at_ms)
        right = _eval(e.right, values, moments, settings, at_ms)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return None
        return max(left, right) if e.which == "max" else min(left, right)
    if isinstance(e, Ladder):
        for rung in e.rungs:
            left = _eval(rung.left, values, moments, settings, at_ms)
            right = (
                _eval(rung.right, values, moments, settings, at_ms)
                if rung.right is not None
                else None
            )
            verdict = _compare(left, rung.op, right)
            if verdict is None:
                return None
            if verdict:
                return _eval(rung.then, values, moments, settings, at_ms)
        return _eval(e.otherwise, values, moments, settings, at_ms)
    # `count`, `list`, `sum` and an extreme are refused to a projection by the
    # checker: a projection aggregates nothing.
    raise AssertionError(f"{type(e).__name__} cannot appear in a projection")


def holds(
    condition: Condition,
    values: Mapping[str, Value],
    settings: Mapping[str, Any],
    at_ms: float,
) -> bool | None:
    left = _eval(condition.left, values, {}, settings, at_ms)
    right = (
        _eval(condition.right, values, {}, settings, at_ms)
        if condition.right is not None
        else None
    )
    return _compare(left, condition.op, right)


def _flag(
    flag: FlagDecl,
    values: Mapping[str, Value],
    units: Mapping[str, FigureUnit],
    settings: Mapping[str, Any],
    at_ms: float,
) -> RenderedFlag | None:
    if holds(flag.when, values, settings, at_ms) is not True:
        # **An unknown does not fire.** A flag is a claim, and the engine having
        # no answer is not evidence for one.
        #
        # `is not True` and not `is False`: `holds` answers three ways, and the
        # third is the whole point. Written as `is False` an unknown *fires*, so
        # a person whose band is withheld gets "— reviews waiting" rendered
        # against them -- a sentence about a number the engine does not have.
        return None
    return RenderedFlag(
        name=flag.name,
        label=render(flag.label, values, units, settings),
        detail=render(flag.detail, values, units, settings),
        action=render(flag.action, values, units, settings) if flag.action else None,
        severity=flag.severity,
    )


def render(
    template: str,
    values: Mapping[str, Value],
    units: Mapping[str, FigureUnit],
    settings: Mapping[str, Any],
) -> str:
    """Substitution and one plural form. Not a language.

    `{name}` prints a value; `{name|singular:plural}` picks a word from the same
    binding it prints, so a sentence cannot pluralise on one number and print
    another.
    """
    out: list[str] = []
    i = 0
    while i < len(template):
        if template[i] != "{":
            out.append(template[i])
            i += 1
            continue
        end = template.find("}", i)
        if end == -1:
            out.append(template[i:])
            break
        inner = template[i + 1 : end]
        i = end + 1
        name, _, forms = inner.partition("|")
        value = values.get(name)
        if forms:
            singular, _, plural = forms.partition(":")
            out.append(singular if _is_one(value) else plural)
        else:
            out.append(format_value(value, units.get(name, "count"), settings))
    return "".join(out)


def _is_one(value: Value) -> bool:
    return isinstance(value, (int, float)) and abs(float(value) - 1.0) < 1e-9


def format_value(value: Value, unit: FigureUnit, settings: Mapping[str, Any]) -> str:
    """The one place a number becomes text.

    Server-side because a browser that formats has to know the tenant's working
    day, and a browser that knows the working day is one step from banding
    against a threshold.
    """
    if value is None:
        return "—"
    if isinstance(value, str):
        if unit == "moment":
            # A `date` field carries the provider's raw string in `values` --
            # sometimes a bare day, sometimes a full instant, depending on
            # which field the operator pointed the tracker mapping at. A
            # reader is owed the day either way: "ended
            # 2026-06-30T00:00:00.000+0000, before this window" is a
            # timestamp wearing a sentence. Unparseable stays verbatim rather
            # than becoming a dash, because the raw string is still the
            # truest thing held and a dash claims there is nothing.
            parsed = parse_instant(value)
            if parsed is not None:
                from datetime import UTC, datetime

                return datetime.fromtimestamp(parsed / 1000.0, tz=UTC).date().isoformat()
        return value
    if isinstance(value, list):
        # An empty day reads as the dash every other absence reads as -- ""
        # under a heading looks like a rendering glitch, and a glitch on the
        # proof surface is the one nobody reports.
        if not value:
            return "—"
        return ", ".join(format_value(v, unit, settings) for v in value)
    if unit == "share":
        return f"{value * 100:.1f}%"
    if unit == "days":
        return f"{round(value)}d"
    if unit == "duration":
        return _duration(value)
    if unit == "effort":
        from ..schema import EFFORT_HOURS_SETTING

        hours_per_day = float(setting_value(dict(settings), EFFORT_HOURS_SETTING))
        days = value / (hours_per_day * 3600.0)
        if days >= 1:
            return f"{days:.1f}d"
        return f"{value / 3600.0:.1f}h"
    if unit == "moment":
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value / 1000.0, tz=UTC).date().isoformat()
    return f"{value:g}"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)}s"
    if seconds < 3600:
        return f"{round(seconds / 60)}m"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


# ------------------------------------------------------------- summary --


@dataclass(frozen=True)
class Summary:
    values: dict[str, Value]
    units: dict[str, FigureUnit]
    flags: tuple[RenderedFlag, ...]


def summarise(
    plan: SummarisePlan,
    rows: Sequence[ProjectedRow],
    settings: Mapping[str, Any],
    at_ms: float,
) -> Summary:
    """Aggregate **every** row, never the page.

    A summary of the first three hundred epics under a heading naming the whole
    roadmap is a wrong number that reads as a right one, and nothing downstream
    could detect it. So the caller summarises before it orders and limits, and
    this function is never handed a page.
    """
    values: dict[str, Value] = {}
    units: dict[str, FigureUnit] = {}

    for name, when in plan.counts:
        # **An unknown does not count, so a count is a floor.** A row the engine
        # has not measured is not evidence that the thing being counted is true.
        total = 0
        for row in rows:
            if when is None or holds(when, row.values, settings, at_ms) is True:
                total += 1
        values[name] = float(total)
        units[name] = "count"

    for name, of, unit, when in plan.totals:
        # **An absent contribution makes the whole total absent**, which is the
        # opposite decision to a count and deliberately so. A sum over whoever
        # happened to have a number is real arithmetic over a population nobody
        # chose: it reads low, plausibly, and repairs itself later, which is the
        # sawtooth signature.
        running = 0.0
        unknown = False
        for row in rows:
            if when is not None and holds(when, row.values, settings, at_ms) is not True:
                continue
            held = row.values.get(of)
            if held is None:
                unknown = True
                break
            if isinstance(held, (int, float)):
                running += float(held)
        values[name] = None if unknown else running
        units[name] = unit

    for name, expr, unit in plan.values:
        values[name] = _eval(expr, values, {}, settings, at_ms)
        units[name] = unit

    flags = tuple(
        rendered
        for flag in plan.flags
        if (rendered := _flag(flag, values, units, settings, at_ms)) is not None
    )
    return Summary(values=values, units=units, flags=flags)


def ordered(plan: ProjectPlan, rows: Sequence[ProjectedRow]) -> list[ProjectedRow]:
    if plan.sort is None:
        return list(rows)
    reverse = plan.sort.direction == "descending"

    def key(row: ProjectedRow) -> tuple[int, float | str]:
        value = row.sort_key
        if value is None:
            # An unsorted row goes last in **either** direction, rather than
            # counting as nought and landing in the middle of the list.
            #
            # The rank flips with `reverse`, which is why it cannot be a
            # constant: written as a plain `(1, ...)` an unsorted row sorts
            # *first* descending, and with a `limit` it then pushes real rows
            # off the page -- a short list that reads as a complete one.
            return (-1 if reverse else 1, 0.0)
        return (0, value)

    return sorted(rows, key=key, reverse=reverse)
