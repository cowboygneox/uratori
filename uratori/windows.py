"""A window spec: a span of stored buckets, counted back from the anchor.

One implementation, called from every door -- the HTTP window parameter, a
bundle member's `over` list, and the facade's own arguments -- because the
spec's semantics are the kind of thing two parsers would quietly disagree
about at exactly one boundary.

The pinned convention: **bucket 1 is the bucket the anchor falls in** (day 1
is the anchor day), both ends inclusive, so `1-30` is the last thirty days
and `31-60` is the thirty before them, with no overlap and no gap. A bare
number `N` is exactly `1-N` in days -- the trailing behaviour that predates
spans, unchanged, and days *whatever the source figure's grain is*: bare
numbers denominated in the grain were considered and rejected, because
regrading a figure from days to quarter-hours would silently re-scale every
bookmarked URL and every bundle by 96x. Anything finer than a day says so,
explicitly.

A spec is an **argument**: it narrows which stored buckets take part and may
never change the calculation. That is why none of this touches any version
hash -- the statistic, the sample floor and the band stay in the definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

WindowUnit = Literal["day", "hour", "minute"]

_UNIT_MINUTES: dict[str, int] = {"day": 1440, "hour": 60, "minute": 1}

_UNIT_SUFFIX: dict[str, str] = {"day": "", "hour": "h", "minute": "m"}

MAX_REACH_DAYS = 3660
"""How far back, in days, any span may reach -- ten years of daily buckets.

The bound exists because the reach is the cost: every bucket of the span is a
stored point the server may walk per subject, and `?trailing=1000000` used to
walk ~739k per-day points per subject before answering an empty window. Ten
years is far beyond any dashboard's horizon; a board that genuinely wants a
decade of history is a definition/product conversation, not a request
parameter. One bound, expressed as reach in *days* whatever the unit, so a
finer unit cannot smuggle the same walk in under a smaller-looking number.
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
    `last` the farthest, both inclusive, each bucket one `unit` wide."""

    first: int
    last: int
    unit: WindowUnit = "day"


def span_text(spec: WindowSpec) -> str:
    """The span half of the canonical spelling, without the unit: `30`,
    `31-60`. What travels as `Window.span` on the wire, beside `bucket`
    carrying the unit -- one implementation with `window_token`, so the
    hash's spelling and the wire's cannot disagree about a span."""
    return str(spec.last) if spec.first == 1 else f"{spec.first}-{spec.last}"


def window_token(spec: WindowSpec) -> str:
    """The canonical spelling: `30`, `31-60`, `48h`, `49-96h`.

    Canonical because it feeds the bundle hash and the duplicate check --
    `over 1-30` and `over 30` are one question, and two spellings hashing
    differently would make two identical tiles differ by punctuation.
    """
    return span_text(spec) + _UNIT_SUFFIX[spec.unit]


def reach_days(spec: WindowSpec) -> int:
    """How many whole days back the span's far edge reaches, rounded up."""
    minutes = spec.last * _UNIT_MINUTES[spec.unit]
    return -(-minutes // 1440)


# ASCII digits only, deliberately: `\d` matches every Unicode digit class,
# and "٣٠" quietly becoming 30 is a coercion -- a plausible window nobody
# spelled. The `d` suffix is the explicit-days form, documented beside `h`
# and `m`, mirroring the bundle grammar's `in days`.
_TOKEN = re.compile(r"([0-9]+)(?:-([0-9]+))?(h|m|d)?\Z")

_UNIT_OF_SUFFIX: dict[str, WindowUnit] = {"d": "day", "h": "hour", "m": "minute"}

_UNIT_WORDS: dict[str, str] = {"day": "days", "hour": "hours", "minute": "minutes"}


def make_window_spec(first: int, last: int, unit: WindowUnit) -> WindowSpec:
    """Build a spec or refuse it -- the one place the bounds rules live."""
    if first == 0 or last == 0:
        raise WindowError(
            "a bucket span counts from 1: bucket 1 is the one the anchor falls in "
            "(day 1 is the anchor day), so \"0-30, 31-60\" would silently make the "
            'first bucket 31 days wide. Write "1-30, 31-60".'
        )
    if first < 0 or last < 0:
        raise WindowError("a bucket span counts back from the anchor; it has no negative buckets.")
    if first > last:
        raise WindowError(
            f'"{first}-{last}" runs backwards: a span is written nearest-first, and '
            f"bucket {last} is further from the anchor than bucket {first}. "
            f'Write "{last}-{first}".'
        )
    spec = WindowSpec(first=first, last=last, unit=unit)
    if reach_days(spec) > MAX_REACH_DAYS:
        raise WindowError(
            f"this span reaches {reach_days(spec)} days back; the ceiling is "
            f"{MAX_REACH_DAYS} days (ten years of daily buckets). Every bucket of a "
            "span is a stored point the server may walk per subject, and a board "
            "wanting more than a decade of history is a definition conversation, "
            "not a request parameter."
        )
    return spec


def as_window_spec(value: int | str | WindowSpec) -> WindowSpec:
    """One window argument, whatever door it arrived through.

    Integers are the pre-span trailing form and stay exactly what they were:
    the last N *days*. Strings are tokens -- `30`, `31-60`, `1-48h`, `90m` --
    and anything else is refused, never coerced: a coerced window is a
    plausible population nobody asked for.
    """
    if isinstance(value, WindowSpec):
        return value
    if isinstance(value, bool):
        raise WindowError(f"{value!r} is not a window.")
    if isinstance(value, int):
        return make_window_spec(1, value, "day")
    matched = _TOKEN.fullmatch(value.strip()) if isinstance(value, str) else None
    if matched is None:
        raise WindowError(
            f'"{value}" is not a window. A window is a span of buckets counted back '
            'from the anchor: "30" (the last 30 days), "31-60" (the 30 before '
            'them), or with a unit suffix "1-48h" (hours) / "1-90m" (minutes) -- '
            "days when unwritten, always."
        )
    first_text, last_text, suffix = matched.groups()
    unit = _UNIT_OF_SUFFIX.get(suffix or "d", "day")
    if last_text is None:
        return make_window_spec(1, int(first_text), unit)
    return make_window_spec(int(first_text), int(last_text), unit)


def refuse_unit_over_grain(spec: WindowSpec, grain: str, figure: str) -> str | None:
    """Why this spec cannot slice this figure's storage, or None.

    A window can only slice what is stored: an hour bucket over a figure
    stored by day has nothing to slice, and a minute bucket over quarter-hour
    storage would split stored buckets. Days work over every grain -- a day
    bucket covers whole finer buckets, which is what day windows have always
    done. The prose is built here so the compile-time door (a bundle member)
    and the request-time door (the HTTP parameter) refuse in the same words.
    """
    if spec.unit == "day":
        return None
    grain_minutes = {"day": 1440, "minute": 1, "15 minutes": 15}.get(grain, 1440)
    # Divisibility alone decides: a grain that divides the unit is never
    # coarser than it, so there is no separate ordering check to keep in step.
    if _UNIT_MINUTES[spec.unit] % grain_minutes == 0:
        return None
    return (
        f"a window in {_UNIT_WORDS[spec.unit]} cannot slice {figure}, which is "
        f"stored by {grain}: a bucket must cover whole stored buckets, never "
        "split one. Write the span in days"
        + (" or hours" if grain == "15 minutes" and spec.unit == "minute" else "")
        + ", or regrade the figure."
    )


def refuse_series_in_window(spec: WindowSpec, series_by: str | None, reading: str) -> str | None:
    """Why this reading's series cannot be served inside this window, or None.

    A series point must sit wholly inside the window: a `by day` point inside
    an hour span would claim a day the span does not cover -- a partially
    covered point wearing a whole point's label. Day spans align with every
    series grain by construction, so only sub-day spans can refuse.
    """
    if series_by is None or spec.unit == "day":
        return None
    series_minutes = {"15 minutes": 15, "hour": 60, "day": 1440}.get(series_by, 1440)
    if series_minutes <= _UNIT_MINUTES[spec.unit]:
        return None
    named = "15-minute" if series_by == "15 minutes" else series_by
    return (
        f"{reading} declares series(...) by {series_by}, and a {named} point "
        f"does not fit inside a window of {_UNIT_WORDS[spec.unit]}: the point would "
        f"claim time the window does not cover. Ask for this span in {named}-sized "
        "units or coarser, or serve the reading over day spans."
    )


__all__ = [
    "MAX_REACH_DAYS",
    "WindowError",
    "WindowSpec",
    "WindowUnit",
    "as_window_spec",
    "make_window_spec",
    "reach_days",
    "refuse_series_in_window",
    "refuse_unit_over_grain",
    "span_text",
    "window_token",
]
