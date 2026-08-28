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
