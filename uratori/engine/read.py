"""Readings: evaluated, never stored.

A figure is stored; a reading is evaluated. That is the whole distinction, and
it is what lets the clock back in without letting it into the store -- a live
reading measures records against `now` and keeps nothing, so the question of a
stale value never arises.

The rule that keeps a reading a definition rather than a function: **an argument
may narrow the population and may never change the calculation.** A range picks
which stored values take part. The statistic, the minimum sample and the band
decide what the number *means*, so they are written in the definition and hashed
into its version.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..lang.plan import ReadingPlan
from ..lang.settings import band_value, seconds_per


@dataclass(frozen=True)
class Sample:
    """The values a reading is about, and how much of the range they cover.

    `days_covered` is a separate claim from the sample size and is reported
    separately: "the queue took no tickets" and "we were not collecting" are
    opposite findings that produce the same empty list. It stays a claim about
    *days* whatever the stored grain, because "how much of the window has
    evidence" is a question about the calendar the reader sees, not about how
    finely the store slices it.

    `points` is the series: one labelled value per bucket of the served grain,
    holes included.
    """

    values: tuple[float, ...]
    points: tuple[tuple[str, float | None], ...]
    days_covered: int
    days_requested: int


def statistics_of(plan: ReadingPlan, sample: Sample) -> dict[str, float | None]:
    """Only the statistics the definition asked for.

    Computing the others "in case" would put numbers on the wire that no
    definition claims, and the first screen to render one would be showing an
    unversioned figure.
    """
    out: dict[str, float | None] = {}
    values = sample.values
    for stat in plan.calculate:
        if stat.fn == "mean":
            out["mean"] = statistics.fmean(values) if values else None
        elif stat.fn == "median":
            out["median"] = statistics.median(values) if values else None
        elif stat.fn == "worst":
            # The worst case is the largest for a duration and, for anything
            # where lower is worse, still the largest -- every reading here is
            # "how long did this take", so worst means most.
            out["worst"] = max(values) if values else None
        elif stat.fn == "sum":
            # **A sum of nothing is nought and a mean of nothing is unknown.**
            # Deliberate asymmetry: a queue that took no tickets took no
            # tickets, where an average of no values is a claim nobody can make.
            out["total"] = float(sum(values))
        elif stat.fn == "count":
            out["count"] = float(len(values))
        elif stat.fn == "series":
            pass
    return out


def series_of(sample: Sample) -> list[float | None]:
    """One value per point of the range, holes included.

    Holes are `None` rather than nought: a day nobody merged on is not a day
    somebody merged nothing in zero seconds, and a sparkline that drew it as a
    floor would show a trough that never happened. The same lie is refused per
    hour and per quarter when the points are grouped finer than a day.
    """
    return [value for _, value in sample.points]


def delta_of(sample: Sample) -> list[float | None]:
    """The change into each bucket, one cell per bucket of the range.

    n buckets produce n-1 changes, and the cell that has none is the oldest
    one: it has no predecessor **in range**. That absence is stated rather
    than omitted, so the answer still describes the n buckets the caller
    asked about and a chart's axis is the window rather than the window
    minus one.

    **The range is the population, and nothing here reaches outside it.**
    The tempting fix for the empty first cell is to fetch the bucket before
    the window and difference against that. It would produce a fuller chart
    and a wrong one: the response could no longer be audited from its own
    contents, because one of its numbers would be about a bucket the
    response does not contain. That is the same refusal `:{bucket - 1}` got,
    at the reading layer.

    A hole breaks the chain in **both** directions -- the change into a
    missing bucket and the change out of it are equally unknowable.
    Differencing across the gap instead would report a two-bucket movement
    in a column headed per-bucket, and it would do it exactly where
    collection was patchy.

    The cell is the change *into* its bucket rather than out of it, so the
    deltas line up positionally with the series and one x-axis carries both.
    """
    out: list[float | None] = []
    previous: float | None = None
    for index, (_, value) in enumerate(sample.points):
        if index == 0 or previous is None or value is None:
            out.append(None)
        else:
            out.append(value - previous)
        previous = value
    return out


def unmet_of(plan: ReadingPlan, sample: Sample) -> list[str]:
    """Which requirements fell short, in words a reader can act on.

    Named rather than counted, because "we cannot say" and "we could say if you
    shipped one more" are different messages and the second is useful.
    """
    out: list[str] = []
    for requirement in plan.requires:
        if len(sample.values) < requirement.count:
            out.append(
                f"needs at least {requirement.count} "
                f"{'value' if requirement.count == 1 else 'values'}; "
                f"there {'is' if len(sample.values) == 1 else 'are'} {len(sample.values)}"
            )
    return out


def level_of(
    plan: ReadingPlan, stats: Mapping[str, float | None], settings: Mapping[str, Any]
) -> str:
    """Which band this reading falls in, decided here and nowhere else.

    Two things this must not get wrong, both of which v1 recorded the hard way:

    A threshold is written in a unit. A healthy acknowledgement is single digits
    of *minutes*; in days the tightest number anybody would type is 1, so every
    ack on every board bands good and the row is decoration.

    **A count has no time in it.** Left to the duration path a count of 3 becomes
    3/86400 against a threshold of 3 and every queue on every board bands good
    for ever. So a band on a count compares the raw number.
    """
    if plan.band is None:
        return "unknown"
    which = plan.band.on or "mean"
    key = {"sum": "total"}.get(which, which)
    value = stats.get(key)
    if value is None:
        return "unknown"

    good, poor = band_value(dict(settings), plan.band.setting)
    if which != "count":
        good *= seconds_per(plan.band.unit or "days", dict(settings))
        poor *= seconds_per(plan.band.unit or "days", dict(settings))

    if plan.band.direction == "low":
        if value <= good:
            return "ok"
        return "over" if value >= poor else "warn"
    if value >= good:
        return "ok"
    return "over" if value <= poor else "warn"


def sample_from_days(
    days: Sequence[tuple[str, float | list[float | None] | None]],
    frm: str,
    to: str,
) -> Sample:
    """Turn stored day values into a sample.

    Reads **both** stored shapes. A `list` figure keeps every value for the day
    and a `count` figure keeps one scalar, and v1's serve path silently dropped
    the second -- so every volume figure was stored, versioned and unreadable,
    with the checker and the reader agreeing so no request ever came back wrong.
    """
    values: list[float] = []
    per_day: dict[str, float | None] = {}
    covered = 0
    for day, stored in days:
        if isinstance(stored, list):
            got = [v for v in stored if v is not None]
            values.extend(got)
            per_day[day] = statistics.fmean(got) if got else None
            if got:
                covered += 1
        elif isinstance(stored, (int, float)):
            values.append(float(stored))
            per_day[day] = float(stored)
            covered += 1
        else:
            per_day[day] = None

    requested = _days_between(frm, to)
    ordered = _fill(frm, to, per_day)
    return Sample(
        values=tuple(values),
        points=tuple(ordered),
        days_covered=covered,
        days_requested=requested,
    )


def sample_from_buckets(
    buckets: Sequence[tuple[str, float | list[float | None] | None]],
    frm: str,
    to: str,
    by: str | None,
    series_to: str | None = None,
) -> Sample:
    """Turn sub-day bucket values into a sample, with the series grouped to `by`.

    Grouping touches **only the series**. The scalar statistics run over the
    raw stored values whatever the grain is -- a mean of the group means would
    weight each hour equally instead of each record, which is the mean-of-means
    trap wearing a new grain.

    A group's point follows the day series' own rule at the new grain: the
    mean of a `list` figure's records, the sum of a `count` figure's buckets --
    the number a `by day` index would have stored, so the two grains cannot be
    two answers to one question. A group whose buckets are all absent is a
    hole, never a nought.

    Labels are local time, so a group is a label prefix: the calendar was
    decided when the bucket was written.

    `series_to` is the last series label of a **sub-day-unit window**: such a
    window's `to` names its newest bucket at the window's own unit (an hour),
    while the series may be grouped finer (quarter-hours), so the series must
    run through the end of that bucket rather than stopping at its first
    label. Absent, the series ends at `to` -- the day-window behaviour,
    unchanged.
    """
    values: list[float] = []
    listed: dict[str, list[float]] = {}
    counted: dict[str, float] = {}
    days: set[str] = set()

    for label, stored in buckets:
        if isinstance(stored, list):
            got = [v for v in stored if v is not None]
            if got:
                values.extend(got)
                days.add(label[:10])
                if by is not None:
                    listed.setdefault(_group_of(label, by), []).extend(got)
        elif isinstance(stored, (int, float)):
            values.append(float(stored))
            days.add(label[:10])
            if by is not None:
                group = _group_of(label, by)
                counted[group] = counted.get(group, 0.0) + float(stored)

    points: list[tuple[str, float | None]] = []
    if by is not None:
        for label in _labels_between(frm, series_to if series_to is not None else to, by):
            if label in listed:
                points.append((label, statistics.fmean(listed[label])))
            elif label in counted:
                points.append((label, counted[label]))
            else:
                points.append((label, None))

    return Sample(
        values=tuple(values),
        points=tuple(points),
        days_covered=len(days),
        days_requested=_days_between(frm, to),
    )


def _group_of(label: str, by: str) -> str:
    """Which group a bucket label belongs to: prefix truncation, no clocks."""
    if by == "day":
        return label[:10]
    if by == "hour":
        return label[:13] + ":00"
    if by == "15 minutes":
        minute = int(label[14:16])
        return f"{label[:14]}{minute - minute % 15:02d}"
    return label


def _labels_between(frm: str, to: str, by: str) -> list[str]:
    """Every group label of the range, in order, so holes are visible.

    Every local day carries the same label set whatever the clocks did: the
    fall-back day's repeated labels merged at write time, and the
    spring-forward day's missing hour simply stays a run of holes -- which is
    true, since those wall-clock minutes never happened.

    Sub-day *bounds* -- a bucket-span window in hours or minutes -- step
    through the same label space from the first label to the last, rather
    than expanding to whole days: the window's series covers exactly the
    window's buckets. A `by day` grain never meets sub-day bounds, because a
    day point does not fit inside a sub-day window and the request is
    refused before sampling.
    """
    from datetime import date, datetime, timedelta

    steps = {"hour": 60, "15 minutes": 15, "minute": 1}
    out: list[str] = []
    if "T" in frm:
        step = steps[by]
        moment = datetime.fromisoformat(frm)
        end_moment = datetime.fromisoformat(to)
        while moment <= end_moment:
            out.append(f"{moment.date().isoformat()}T{moment.hour:02d}:{moment.minute:02d}")
            if moment > datetime.max - timedelta(minutes=step):
                # The calendar's own last labels: the step beyond them that
                # the loop condition would catch overflows before the
                # condition ever runs -- `_fill`'s guard, at the finer grain.
                break
            moment += timedelta(minutes=step)
        return out
    day = date.fromisoformat(frm)
    end = date.fromisoformat(to)
    while day <= end:
        if by == "day":
            out.append(day.isoformat())
        else:
            step = steps[by]
            for minutes in range(0, 1440, step):
                out.append(f"{day.isoformat()}T{minutes // 60:02d}:{minutes % 60:02d}")
        if day == date.max:
            # A range may legitimately end on the calendar's last day -- an
            # anchored request at 9999-12-31 -- and the step beyond it
            # overflows before the loop condition runs. `_fill` has carried
            # this guard since the anchor landed; this loop needed it too,
            # which is where the fixed traceback moved to.
            break
        day += timedelta(days=1)
    return out


def _days_between(frm: str, to: str) -> int:
    """Calendar days the bounds touch, whether they are days or labels."""
    from datetime import date

    start = date.fromisoformat(frm[:10])
    end = date.fromisoformat(to[:10])
    return (end - start).days + 1


def _fill(frm: str, to: str, per_day: Mapping[str, float | None]) -> list[tuple[str, float | None]]:
    from datetime import date, timedelta

    out: list[tuple[str, float | None]] = []
    day = date.fromisoformat(frm)
    end = date.fromisoformat(to)
    while day <= end:
        key = day.isoformat()
        out.append((key, per_day.get(key)))
        if day == date.max:
            # A window may legitimately end on the calendar's last day --
            # 9999-12-31 is a well-formed anchor -- and the step beyond it
            # that this loop's condition would catch overflows before the
            # condition ever runs.
            break
        day += timedelta(days=1)
    return out
