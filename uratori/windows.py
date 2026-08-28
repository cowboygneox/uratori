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
from collections.abc import Sequence
from dataclasses import dataclass

MAX_BUCKETS = 3660
"""How many buckets one span may cover -- ten years of daily buckets.

**The bound on the walk, and the only one that holds whatever the rule is.**
Every bucket of a span is a stored point the server may walk per subject, and
resolving a span now materialises its labels, so the count *is* the cost.
This is checked in `make_window_spec`, at construction, because that is the
one gate every door goes through -- and because the expansions downstream of
it (`each`, and the label list a span resolves to) are linear in exactly this
number.

`MAX_REACH_DAYS` below is a second, calendar-aware bound and not a substitute
for this one: expressed in days, it is *looser* for a fine sequence in
precisely the way it claims to be tighter. 5,270,400 minute-buckets convert
to 3,660 days and sail through it, having asked the server for five million
labels. A bound on the walk has to count the walk.
"""

MAX_REACH_DAYS = 3660
"""How far back, in days, any span may reach -- ten years of daily buckets.

Where `MAX_BUCKETS` bounds the *work*, this bounds the *horizon*, and a coarse
sequence is why it is still needed: 3,660 monthly buckets is three centuries,
well inside the bucket ceiling and far outside any board's question. Ten years
is far beyond any dashboard's horizon; a board that genuinely wants a decade of
history is a definition/product conversation, not a request parameter. Each
rule converts through its own widest bucket (`BUCKET_DAYS`).

Checked where the spec meets the rule rather than at construction, because
only the reading's declaration knows which sequence the positions walk.
"""

MAX_WINDOWS = 366
"""How many windows one request may ask for -- a year of daily buckets.

A separate bound because `each` turns one argument into one window *per
bucket*, and the cost is windows x subjects: `each 1-3660` is inside the
bucket ceiling as a span and yet asks for 3,660 answers per subject, which is
a different question from `1-3660`'s single pooled one. A per-bucket
comparison wider than a year -- this month beside each of the previous
eleven, the worked example -- is a chart a definition should declare, not a
request parameter.
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
judged as ten years the way `over 1-3660` over days is.
"""

FIFTH_BUCKET_DAYS = 92.0
"""How far one `fifth <weekday> of month` bucket reaches -- a quarter, not a
month.

The rest of the ordinal family is one bucket per month, so it converts at the
month's width. A *fifth* weekday is different in kind: it exists only in
months long enough to hold one, which is about four months in twelve, so its
buckets sit ~87 days apart on average. Converting it at a month's width made
the ceiling claim ten years and grant twenty-eight -- 118 fifth-Mondays is
over 10,000 days. A quarter's width is the honest conversion, and slightly
conservative against the mean.
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

    Rules not in `BUCKET_DAYS` are the ordinal weekday-of-month family: one
    bucket per month for `first` through `fourth`, and a quarter's width for
    `fifth`, which most months have none of (`FIFTH_BUCKET_DAYS`).
    """
    if rule not in BUCKET_DAYS and rule.startswith("fifth "):
        width = FIFTH_BUCKET_DAYS
    else:
        width = BUCKET_DAYS.get(rule, BUCKET_DAYS["month"])
    days = spec.last * width
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
    # Before anything downstream is sized by these numbers. `each` expands to
    # one window per bucket and a span resolves to one label per bucket, both
    # linear in `last`, so a ceiling applied after either has already paid for
    # the span it means to refuse.
    if last > MAX_BUCKETS:
        raise WindowError(
            f'"{span_text(WindowSpec(first=first, last=last))}" covers {last} buckets; the '
            f"ceiling is {MAX_BUCKETS} (ten years of daily buckets). Every bucket of a span "
            "is a stored point the server walks per subject, and a board wanting more than "
            "that is a definition conversation, not a request parameter. What one bucket is "
            "lives in the source figure's group clause: a coarser rule there asks the same "
            "question in fewer buckets."
        )
    return WindowSpec(first=first, last=last)


def refuse_window_count(count: int) -> str | None:
    """Why this many windows in one request is too many, or None."""
    if count <= MAX_WINDOWS:
        return None
    # "at least", because the list is counted as it grows and refused the
    # moment it passes: the true total may be far larger, and a precise
    # number here would have cost exactly what the ceiling exists to avoid.
    return (
        f"this request asks for at least {count} windows; the ceiling is {MAX_WINDOWS} (a year of "
        "daily buckets). `each a-b` is one window per bucket, and the server answers each "
        "one per subject, so the cost is windows times subjects. A per-bucket comparison "
        "wider than a year is a chart a definition should declare, not a request parameter."
    )


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
# Case-insensitive, and `hr`/`min` too: the point is to catch a bookmarked
# v0.12 span and send its author to the group clause. `1-48H` getting the
# generic "not a window" sent exactly the reader this message exists for
# hunting a typo instead.
_RETIRED_SUFFIX = re.compile(r"[0-9-]+(h|hr|hrs|m|min|mins|d)\Z", re.IGNORECASE)

# `each:12` as well as `each:1-12`. A bare bound means `1-N` here exactly as
# it does everywhere else, so `each:12` is the twelve one-bucket windows --
# not the single bucket 12, which is what `each:12-12` says. The two
# readings matter: a tile author writing the bare form wants twelve columns,
# and silently handing back one is a wrong answer with no error attached.
_EACH = re.compile(r"each:([0-9]+)(?:-([0-9]+))?\Z")


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
    enumerated `1, 2-2, 3-3` are indistinguishable everywhere downstream --
    same windows, same duplicate check, same answer.
    """
    if isinstance(value, str):
        matched = _EACH.fullmatch(value.strip())
        if matched is not None:
            if matched.group(2) is None:
                first, last = 1, int(matched.group(1))
            else:
                first, last = int(matched.group(1)), int(matched.group(2))
            # Validated before expanded: `make_window_spec` carries the bucket
            # ceiling, so a refused span is refused without first building the
            # millions of one-bucket windows it asked for.
            spec = make_window_spec(first, last)
            return tuple(WindowSpec(first=k, last=k) for k in range(spec.first, spec.last + 1))
    return (as_window_spec(value),)


def expand_window_args(
    values: Sequence[int | str | WindowSpec],
) -> tuple[WindowSpec, ...]:
    """A whole window list, expanded and bounded -- the door every request
    surface uses, so the HTTP route, the socket's subscribe entries and the
    UI's own route cannot disagree about what is too much to ask for.

    The count is checked as the list grows, not once at the end: a repeatable
    parameter is a second multiplier the ceiling has to see. Two thousand
    copies of a legal `each:1-3660` is a 40 KB query string, and building the
    whole list before counting it spent 15s and a gigabyte to answer 422 --
    the same shape of bug as expanding an oversized `each`, reached by
    repetition instead of by one big number. Refusing as soon as the running
    total passes the ceiling bounds the peak at one argument's worth.
    """
    out: list[WindowSpec] = []
    for value in values:
        # Each argument's own expansion is already bounded by the bucket
        # ceiling, so this can never hold more than `MAX_WINDOWS` plus one
        # argument's worth at any moment.
        expanded = expand_window_arg(value)
        refusal = refuse_window_count(len(out) + len(expanded))
        if refusal is not None:
            raise WindowError(refusal)
        out.extend(expanded)
    return tuple(out)


__all__ = [
    "BUCKET_DAYS",
    "MAX_BUCKETS",
    "MAX_REACH_DAYS",
    "MAX_WINDOWS",
    "WindowError",
    "WindowSpec",
    "as_window_spec",
    "expand_window_arg",
    "expand_window_args",
    "make_window_spec",
    "reach_days",
    "refuse_reach",
    "refuse_window_count",
    "span_text",
    "window_token",
]
