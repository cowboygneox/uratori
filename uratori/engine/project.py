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
    at_ms: float,
) -> ProjectedRow | None:
    """One record's row, or None when the plan's `omit` gate holds for it.

    None rather than a marked row, so a caller cannot forget to drop it -- a
    row that travels with an "omitted" flag is a row a screen will render the
    day someone reads the list and not the flag.
    """
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
        values[name] = _eval(expr, values, moments, at_ms)
        units[name] = unit

    if plan.omit is not None and holds(plan.omit, values, at_ms) is True:
        # `is True`, never `is not False`: `holds` answers three ways, and a
        # gate the engine cannot answer keeps the row. A record that has not
        # been shown to satisfy the gate has not earned removal -- dropping on
        # the absence of evidence would narrow the population by a cheap
        # path, and a page quietly short one row corrects itself never. (The
        # flag path states the mirror rule: an unknown does not *fire*.)
        return None

    flags = tuple(
        rendered
        for flag in plan.flags
        if (rendered := _flag(flag, values, units, at_ms)) is not None
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
    at_ms: float,
) -> Value:
    if isinstance(e, Number):
        return e.value
    if isinstance(e, Text):
        return e.value
    if isinstance(e, Setting):  # pragma: no cover - the checker refuses one
        raise KeyError(
            f'"{e.path}" is a settings dial, and a projection cannot name one: a row '
            "value's threshold is a figure bound with `read:`, or a number written "
            "where the reader can see it."
        )
    if isinstance(e, Part):
        return values.get(e.name)
    if isinstance(e, DaysBetween):
        start = at_ms if e.frm == "now" else moments.get(e.frm)
        end = at_ms if e.to == "now" else moments.get(e.to)
        return days_between(start, end)
    if isinstance(e, Arith):
        return _arith(
            e.op,
            _eval(e.left, values, moments, at_ms),
            _eval(e.right, values, moments, at_ms),
        )
    if isinstance(e, Pick):
        left = _eval(e.left, values, moments, at_ms)
        right = _eval(e.right, values, moments, at_ms)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return None
        return max(left, right) if e.which == "max" else min(left, right)
    if isinstance(e, Ladder):
        for rung in e.rungs:
            left = _eval(rung.left, values, moments, at_ms)
            right = (
                _eval(rung.right, values, moments, at_ms)
                if rung.right is not None
                else None
            )
            verdict = _compare(left, rung.op, right)
            if verdict is None:
                return None
            if verdict:
                return _eval(rung.then, values, moments, at_ms)
        return _eval(e.otherwise, values, moments, at_ms)
    # `count`, `list`, `sum` and an extreme are refused to a projection by the
    # checker: a projection aggregates nothing.
    raise AssertionError(f"{type(e).__name__} cannot appear in a projection")


def holds(
    condition: Condition,
    values: Mapping[str, Value],
    at_ms: float,
) -> bool | None:
    left = _eval(condition.left, values, {}, at_ms)
    right = (
        _eval(condition.right, values, {}, at_ms)
        if condition.right is not None
        else None
    )
    return _compare(left, condition.op, right)


def _flag(
    flag: FlagDecl,
    values: Mapping[str, Value],
    units: Mapping[str, FigureUnit],
    at_ms: float,
) -> RenderedFlag | None:
    if holds(flag.when, values, at_ms) is not True:
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
        label=render(flag.label, values, units),
        detail=render(flag.detail, values, units),
        action=render(flag.action, values, units) if flag.action else None,
        severity=flag.severity,
    )


def render(
    template: str,
    values: Mapping[str, Value],
    units: Mapping[str, FigureUnit],
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
            out.append(format_value(value, units.get(name, "count")))
    return "".join(out)


def _is_one(value: Value) -> bool:
    return isinstance(value, (int, float)) and abs(float(value) - 1.0) < 1e-9


def format_value(value: Value, unit: FigureUnit) -> str:
    """The one place a number becomes text.

    Server-side because formatting a quantity is a division, and a browser
    dividing seconds into hours is one step from comparing them against a
    threshold -- which is banding in two places, the failure `level` exists to
    prevent. It took a settings document until an effort stopped being
    rendered against a tenant's working day; there is nothing left for it to
    read.
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
        return ", ".join(format_value(v, unit) for v in value)
    if unit == "share":
        return f"{value * 100:.1f}%"
    if unit == "days":
        return f"{round(value)}d"
    if unit == "duration":
        return _duration(value)
    if unit == "effort":
        # **Hours, always, and never days.** An effort is working time, so a
        # day of it is however long a working day is -- which used to be a
        # tenant dial the renderer divided by. That was the last number on a
        # screen a tenant could move from a form, and moving it re-worded
        # every effort on every board while no stored value went anywhere.
        #
        # Hours need no such input. "40h" says the same thing as "5d" to
        # anybody who knows their own week, and it says it without the reader
        # having to find out whose day the engine had in mind. Scaled on the
        # magnitude and signed afterwards, as `_duration` is: subtraction
        # under `unit effort` produces negatives, and a rung against a
        # positive bound lets them through unformatted.
        sign = "-" if value < 0 else ""
        return f"{sign}{abs(value) / 3600.0:.1f}h"
    if unit == "moment":
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value / 1000.0, tz=UTC).date().isoformat()
    return f"{value:g}"


def _duration(seconds: float) -> str:
    """A span of seconds, in the largest unit that keeps it readable.

    **Scaled on the magnitude and signed afterwards.** Every rung here is a
    `<` against a positive bound, so a negative fell through the first one
    and printed raw: a rise of an hour read "1.0h" and a fall of two hours,
    in the same column, read "-7200s". Nothing produced a negative duration
    until `delta` did -- a change between buckets goes both ways -- which is
    why the ladder survived this long looking total.
    """
    size = abs(seconds)
    # The sign is dropped when the magnitude rounds away: a fall of a tenth of
    # a second is "0s", never "-0s". A signed nought reads as a direction
    # somebody measured, and this one is a rounding artefact -- reachable
    # because a duration delta is the difference of two float means.
    sign = "-" if seconds < 0 and round(size) != 0 else ""
    if size < 60:
        return f"{sign}{round(size)}s"
    if size < 3600:
        return f"{sign}{round(size / 60)}m"
    if size < 86_400:
        return f"{sign}{size / 3600:.1f}h"
    return f"{sign}{size / 86_400:.1f}d"


# ------------------------------------------------------------- summary --


@dataclass(frozen=True)
class Summary:
    values: dict[str, Value]
    units: dict[str, FigureUnit]
    flags: tuple[RenderedFlag, ...]


def summarise(
    plan: SummarisePlan,
    rows: Sequence[ProjectedRow],
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
            if when is None or holds(when, row.values, at_ms) is True:
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
            if when is not None and holds(when, row.values, at_ms) is not True:
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
        values[name] = _eval(expr, values, {}, at_ms)
        units[name] = unit

    flags = tuple(
        rendered
        for flag in plan.flags
        if (rendered := _flag(flag, values, units, at_ms)) is not None
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
