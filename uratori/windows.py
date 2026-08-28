"""A window spec: a span of stored buckets, counted back from the anchor.

One implementation, called from every door -- the HTTP window parameter, a
bundle member's `over` list, and the facade's own arguments -- because the
spec's semantics are the kind of thing two parsers would quietly disagree
about at exactly one boundary.

The pinned convention: **bucket 1 is the bucket the anchor falls in**, both
ends inclusive, so `1-30` is the last thirty buckets and `31-60` the thirty
before them, with no overlap and no gap. A bare number `N` is exactly `1-N`
-- the trailing behaviour that predates spans, unchanged.

A span is **positions in an ordered bucket sequence, and nothing else**.
What one bucket *is* -- a day, a month, a quarter, the first Monday of each
month -- lives in the source figure's group clause, hashed. It has to live
there, because the bucket rule changes what a number means, and the
language's own law is that an argument may narrow the population and may
never change the calculation. A unit riding on the argument (`1-48h`,
`over ... in hours` -- shipped briefly and retired) was that law broken
quietly: the same reading meant two different things under two spellings of
one request.

`each a-b` is the one piece of sugar: it expands to the one-bucket windows
`a-a, a+1-a+1, ..., b-b`, one window per bucket in order, so a per-bucket
comparison -- this month beside each of the eleven before it -- does not
have to be spelled as twelve enumerated spans. Still integers, still
positions; `a-b` without `each` stays a single pooled window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_REACH_DAYS = 3660
"""How far back, in days, any span may reach -- ten years of daily buckets.

The bound exists because the reach is the cost: every bucket of the span is a
stored point the server may walk per subject, and `?trailing=1000000` used to
walk ~739k per-day points per subject before answering an empty window. Ten
years is far beyond any dashboard's horizon; a board that genuinely wants a
decade of history is a definition/product conversation, not a request
parameter. One bound, expressed as reach in *days* whatever the bucket rule,
so a finer sequence cannot smuggle the same walk in under a smaller-looking
number -- each rule converts through its own widest bucket (`BUCKET_DAYS`).
"""

BUCKET_DAYS: dict[str, float] = {
    "minute": 1 / 1440,
    "15 minutes": 15 / 1440,
    "hour": 60 / 1440,
    "day": 1.0,
    "week": 7.0,
    "month": 31.0,
    "quarter": 92.0,
}
"""How many days one bucket of each calendar rule can span, at widest.

The reach ceiling multiplies through this, so `over 1-120` over months is
judged as ten years the way `over 1-3660` over days is. An ordinal
weekday-of-month rule (`first monday of month`) is one bucket per month at
most, so any rule not listed here converts at the month's width.
"""


class WindowError(ValueError):
    """A window spec the server refuses, in words the caller can act on.

    A subclass of ValueError so an embedding host that already catches
    ValueError keeps working; the HTTP routes catch this one first and answer
    422, because a malformed *argument* is the caller's to fix and a 400/500
    says the server misbehaved.
    """


@dataclass(frozen=True)
class WindowSpec:
    """A span of buckets: `first` is the nearest (1 = the anchor bucket),
    `last` the farthest, both inclusive. Which sequence the positions walk
    is the reading's declaration, never the spec's."""

    first: int
    last: int


def span_text(spec: WindowSpec) -> str:
    """The canonical spelling: `30`, `31-60`. What travels as `Window.span`
    on the wire and what feeds the bundle hash and every duplicate check --
    `over 1-30` and `over 30` are one question, and two spellings hashing
    differently would make two identical tiles differ by punctuation."""
    return str(spec.last) if spec.first == 1 else f"{spec.first}-{spec.last}"


def window_token(spec: WindowSpec) -> str:
    """`span_text` under the name the hash and duplicate checks import.
    One implementation, so the hash's spelling and the wire's cannot
    disagree about a span."""
    return span_text(spec)


def reach_days(spec: WindowSpec, rule: str = "day") -> int:
    """How many whole days back the span's far edge can reach, rounded up.

    Rules not in `BUCKET_DAYS` are the ordinal weekday-of-month family --
    at most one bucket per month, so they convert at the month's width.
    """
    days = spec.last * BUCKET_DAYS.get(rule, BUCKET_DAYS["month"])
    return -(-int(days * 1440) // 1440)


def make_window_spec(first: int, last: int) -> WindowSpec:
    """Build a spec or refuse it -- the one place the bounds rules live.

    The reach ceiling is deliberately not here: reach depends on the bucket
    rule, which only the reading's declaration knows, so it is checked where
    the spec meets the rule (`refuse_reach`) -- at the serving door and at a
    bundle member's compile.
    """
    if first == 0 or last == 0:
        raise WindowError(
            "a bucket span counts from 1: bucket 1 is the one the anchor falls in, "
            'so "0-30, 31-60" would silently make the '
            'first bucket 31 buckets wide. Write "1-30, 31-60".'
        )
    if first < 0 or last < 0:
        raise WindowError("a bucket span counts back from the anchor; it has no negative buckets.")
    if first > last:
        raise WindowError(
            f'"{first}-{last}" runs backwards: a span is written nearest-first, and '
            f"bucket {last} is further from the anchor than bucket {first}. "
            f'Write "{last}-{first}".'
        )
    return WindowSpec(first=first, last=last)


def refuse_reach(spec: WindowSpec, rule: str) -> str | None:
    """Why this span reaches too far under this bucket rule, or None."""
    reach = reach_days(spec, rule)
    if reach <= MAX_REACH_DAYS:
        return None
    return (
        f"this span reaches {reach} days back over {rule} buckets; the ceiling is "
        f"{MAX_REACH_DAYS} days (ten years of daily buckets). Every bucket of a "
        "span is a stored point the server may walk per subject, and a board "
        "wanting more than a decade of history is a definition conversation, "
        "not a request parameter."
    )


# ASCII digits only, deliberately: `\d` matches every Unicode digit class,
# and "٣٠" quietly becoming 30 is a coercion -- a plausible window nobody
# spelled.
_TOKEN = re.compile(r"([0-9]+)(?:-([0-9]+))?\Z")

# The retired unit suffixes, matched only to refuse them with directions:
# the unit moved into the declarations, and a message that just said
# "not a window" would send somebody looking for a typo.
_RETIRED_SUFFIX = re.compile(r"[0-9-]+(h|m|d)\Z")

_EACH = re.compile(r"each:([0-9]+)-([0-9]+)\Z")


def as_window_spec(value: int | str | WindowSpec) -> WindowSpec:
    """One window argument, whatever door it arrived through.

    Integers are the pre-span trailing form: the last N buckets. Strings are
    tokens -- `30`, `31-60` -- and anything else is refused, never coerced: a
    coerced window is a plausible population nobody asked for.
    """
    if isinstance(value, WindowSpec):
        return value
    if isinstance(value, bool):
        raise WindowError(f"{value!r} is not a window.")
    if isinstance(value, int):
        return make_window_spec(1, value)
    matched = _TOKEN.fullmatch(value.strip()) if isinstance(value, str) else None
    if matched is None:
        if isinstance(value, str) and _RETIRED_SUFFIX.fullmatch(value.strip()):
            raise WindowError(
                f'"{value}" carries a unit, and window units were retired: a window '
                "is a span of positions in the source figure's own bucket sequence, "
                "and what one bucket is -- a day, an hour, a month -- is declared in "
                "that figure's group clause (`by hour`, `by month`, ...), where it "
                'is hashed. Write the bare span, e.g. "30" or "31-60".'
            )
        raise WindowError(
            f'"{value}" is not a window. A window is a span of buckets counted back '
            'from the anchor: "30" (the last 30 buckets, bucket 1 being the one '
            'the anchor falls in), or "31-60" (the 30 before them). What one '
            "bucket is lives in the reading's declaration, never in the argument."
        )
    first_text, last_text = matched.groups()
    if last_text is None:
        return make_window_spec(1, int(first_text))
    return make_window_spec(int(first_text), int(last_text))


def expand_window_arg(value: int | str | WindowSpec) -> tuple[WindowSpec, ...]:
    """One window argument, expanded: `each:a-b` becomes the one-bucket
    windows `a-a` through `b-b`, in order; anything else is one spec.

    The expansion happens here, at the argument door, so `each:1-3` and the
    enumerated `1-1, 2-2, 3-3` are indistinguishable everywhere downstream --
    same windows, same duplicate check, same answer.
    """
    if isinstance(value, str):
        matched = _EACH.fullmatch(value.strip())
        if matched is not None:
            spec = make_window_spec(int(matched.group(1)), int(matched.group(2)))
            return tuple(WindowSpec(first=k, last=k) for k in range(spec.first, spec.last + 1))
    return (as_window_spec(value),)


__all__ = [
    "BUCKET_DAYS",
    "MAX_REACH_DAYS",
    "WindowError",
    "WindowSpec",
    "as_window_spec",
    "expand_window_arg",
    "make_window_spec",
    "reach_days",
    "refuse_reach",
    "span_text",
    "window_token",
]
