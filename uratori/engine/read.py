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

from ..lang.ast import StatisticFn
from ..lang.plan import ReadingPlan
from .evaluate import band_of


@dataclass(frozen=True)
class Sample:
    """The values a reading is about, and how much of the window they cover.

    `buckets_covered` is a separate claim from the sample size and is
    reported separately: "the queue took no tickets" and "we were not
    collecting" are opposite findings that produce the same empty list. It
    is a claim about *buckets of the figure's own sequence* -- days of a day
    figure, months of a month figure, first-Mondays of a selective one --
    because that sequence is what the window's positions walked.

    `points` is the series: one labelled value per covered bucket, holes
    included.
    """

    values: tuple[float, ...]
    points: tuple[tuple[str, float | None], ...]
    buckets_covered: int
    buckets_requested: int


def statistic_of(fn: StatisticFn, sample: Sample) -> float | None:
    """One statistic over one sample.

    Split out from `statistics_of` because a band's threshold figure is
    reduced over the same window by the same statistic, and two
    implementations of "the mean" are two chances for the number and the
    threshold judging it to be computed differently.
    """
    values = sample.values
    if fn == "mean":
        return statistics.fmean(values) if values else None
    if fn == "median":
        return statistics.median(values) if values else None
    if fn == "worst":
        # The worst case is the largest for a duration and, for anything
        # where lower is worse, still the largest -- every reading here is
        # "how long did this take", so worst means most.
        return max(values) if values else None
    if fn == "sum":
        # **A sum of nothing is nought and a mean of nothing is unknown.**
        # Deliberate asymmetry: a queue that took no tickets took no
        # tickets, where an average of no values is a claim nobody can make.
        return float(sum(values))
    if fn == "count":
        return float(len(values))
    # `series` and `delta` are one cell per bucket rather than a statistic;
    # the checker refuses a band on either, and no caller asks for one here.
    return None


def threshold_of(
    fn: StatisticFn, judged: Sample, goal: Sample
) -> float | None:
    """The goal a window's statistic is compared against, or None.

    `statistic_of` is right for a reading's own value and wrong for the
    threshold judging it, in the one place they differ: a sum of nothing is
    nought, and nought is the number every total in the world clears. A
    courier nobody has ever set a goal for would read "met", which is the
    bottom-rung-by-default failure a band exists to avoid, arriving through
    the threshold rather than through the value.

    The same argument covers partial cover. Two months of deliveries judged
    against one month of goal is wrong by roughly the length of the window,
    and it is wrong in the comfortable direction. So the goal has to be known
    for every bucket the value was counted over; short of that the comparison
    is not knowable, and the window says so.
    """
    if not goal.values:
        return None
    known = {label for label, value in goal.points if value is not None}
    wanted = {label for label, value in judged.points if value is not None}
    if not wanted <= known:
        return None
    return statistic_of(fn, goal)


def statistics_of(plan: ReadingPlan, sample: Sample) -> dict[str, float | None]:
    """Only the statistics the definition asked for.

    Computing the others "in case" would put numbers on the wire that no
    definition claims, and the first screen to render one would be showing an
    unversioned figure.
    """
    out: dict[str, float | None] = {}
    for stat in plan.calculate:
        # `series` and `delta` are per-bucket answers served in their own
        # fields; a key here holding None would put a statistic on the wire
        # the definition never asked for, dashed.
        if stat.fn in ("series", "delta"):
            continue
        out[{"sum": "total"}.get(stat.fn, stat.fn)] = statistic_of(stat.fn, sample)
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
    plan: ReadingPlan,
    stats: Mapping[str, float | None],
    thresholds: Mapping[str, float | None] = {},
) -> str:
    """Which band this reading falls in, decided here and nowhere else.

    The verdict is about **one** statistic -- `band on sum:` says which -- and
    the goals it compares against are reduced over the same window by the same
    statistic, so a window's total is judged against the total of the goal
    across those same buckets. Comparing a span against a point is the mistake
    this arrangement exists to make unwritable: six months of deliveries beside
    one month of target reads plausibly and is wrong by the length of the
    window.

    This used to resolve a dial and a unit. The unit existed because a dial is
    a bare number that does not know what it measures, and getting it wrong
    banded every row good for ever at 1,440 times the intended threshold. A
    figure knows its own unit and the checker has already refused a mismatch,
    so there is nothing left here to scale.

    `unknown` where the statistic is missing -- and where a *goal* is, because
    a window nobody set a target for has no verdict, and the comfortable rung
    is the confident wrong answer.
    """
    if plan.band is None:
        return "unknown"
    key = {"sum": "total"}.get(plan.band_on or "mean", plan.band_on or "mean")
    value = stats.get(key)
    if value is None:
        return "unknown"
    return band_of(plan.band, value, dict(thresholds)) or "unknown"


def sample_over(
    stored: Sequence[tuple[str, float | list[float | None] | None]],
    labels: Sequence[str],
) -> Sample:
    """Turn stored bucket values into a sample over the covered labels.

    `labels` is the span resolved: every bucket the window covers, oldest
    first, whether the sequence is days, months or a sparse run of first
    Mondays -- the store's own labels, because a stored bucket *is* a
    position in its figure's declared sequence and nothing is regrouped on
    the way out. `stored` is the rows whose labels the window covers.

    Reads **both** stored shapes. A `list` figure keeps every value for the
    bucket and a `count` figure keeps one scalar, and v1's serve path
    silently dropped the second -- so every volume figure was stored,
    versioned and unreadable, with the checker and the reader agreeing so no
    request ever came back wrong.

    A bucket's point follows the shape: a list bucket's point is the mean of
    its records (the one summary a per-bucket sparkline can carry), a scalar
    bucket's point is the scalar. A bucket that stored nothing -- or an
    empty list -- is a hole, never a nought: a month nobody merged in is not
    a month somebody merged nothing in zero seconds.
    """
    values: list[float] = []
    per_bucket: dict[str, float | None] = {}
    covered = 0
    for label, held in stored:
        if isinstance(held, list):
            got = [v for v in held if v is not None]
            values.extend(got)
            per_bucket[label] = statistics.fmean(got) if got else None
            if got:
                covered += 1
        elif isinstance(held, (int, float)):
            values.append(float(held))
            per_bucket[label] = float(held)
            covered += 1
        else:
            per_bucket[label] = None

    return Sample(
        values=tuple(values),
        points=tuple((label, per_bucket.get(label)) for label in labels),
        buckets_covered=covered,
        buckets_requested=len(labels),
    )
