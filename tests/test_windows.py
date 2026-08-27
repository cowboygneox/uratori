"""Window specs: a span of stored buckets, counted back from the anchor.

The spec grammar is shared by the HTTP window parameter and a bundle member's
`over` list, so its rules are tested here once, against the one implementation
both doors call. The semantics under test are the pinned ones:

- bucket 1 is the bucket the anchor falls in (day 1 is the anchor day), both
  ends inclusive;
- a bare number N is exactly buckets 1-N of days -- the trailing behaviour
  that predates spans, unchanged;
- bare numbers are days *always*, never the source figure's grain: a figure
  regraded from days to quarter-hours must not silently re-scale every
  bookmarked URL and every bundle by 96x;
- a bound of 0, a reversed span and a span past the reach ceiling are refused
  with messages that say what to write instead.
"""

from __future__ import annotations

import pytest

from uratori.windows import (
    MAX_REACH_DAYS,
    WindowError,
    WindowSpec,
    as_window_spec,
    window_token,
)


def test_a_bare_number_is_exactly_the_trailing_days_span() -> None:
    """`30`, `1-30` and `30 in days` are one question, canonically spelled:
    the last 30 days, day 1 being the anchor day. Three spellings compiling
    to two values would let two identical tiles hash differently."""
    assert as_window_spec(30) == WindowSpec(first=1, last=30, unit="day")
    assert as_window_spec("30") == WindowSpec(first=1, last=30, unit="day")
    assert as_window_spec("1-30") == WindowSpec(first=1, last=30, unit="day")
    assert window_token(as_window_spec("1-30")) == "30"


def test_an_offset_span_names_both_bounds() -> None:
    assert as_window_spec("31-60") == WindowSpec(first=31, last=60, unit="day")
    assert window_token(WindowSpec(first=31, last=60, unit="day")) == "31-60"


def test_sub_day_units_ride_as_a_suffix() -> None:
    assert as_window_spec("1-48h") == WindowSpec(first=1, last=48, unit="hour")
    assert as_window_spec("48h") == WindowSpec(first=1, last=48, unit="hour")
    assert as_window_spec("1-90m") == WindowSpec(first=1, last=90, unit="minute")
    assert window_token(WindowSpec(first=1, last=48, unit="hour")) == "48h"
    assert window_token(WindowSpec(first=49, last=96, unit="hour")) == "49-96h"


def test_a_zero_bound_is_refused_toward_day_one() -> None:
    """The instinctive spelling is `0-30, 31-60`, and honouring it would make
    the first bucket silently 31 days wide. The refusal must teach the
    convention: bucket 1 is the anchor bucket."""
    for wrong in ("0", "0-30", "0-30h"):
        with pytest.raises(WindowError) as caught:
            as_window_spec(wrong)
        message = str(caught.value)
        assert "1" in message and "anchor" in message, wrong
        assert "31" in message or "0-" in message or "counts from 1" in message


def test_a_reversed_or_empty_span_is_refused() -> None:
    with pytest.raises(WindowError) as caught:
        as_window_spec("60-31")
    assert "31-60" in str(caught.value), "the refusal should say what to write"


def test_the_reach_ceiling_is_in_days_whatever_the_unit() -> None:
    """`?trailing=1000000` used to walk ~739k stored day points per subject.
    The ceiling is one bound, expressed as reach in days, so an hour span
    cannot smuggle the same walk in under a finer unit."""
    with pytest.raises(WindowError) as caught:
        as_window_spec("1000000")
    assert str(MAX_REACH_DAYS) in str(caught.value)

    with pytest.raises(WindowError):
        as_window_spec(f"{MAX_REACH_DAYS * 24 + 1}h")
    with pytest.raises(WindowError):
        as_window_spec(f"{MAX_REACH_DAYS + 1}")

    # The boundary itself answers: the ceiling is a ceiling, not a fence
    # one short of it.
    assert as_window_spec(str(MAX_REACH_DAYS)).last == MAX_REACH_DAYS
    assert as_window_spec(f"{MAX_REACH_DAYS * 24}h").last == MAX_REACH_DAYS * 24


def test_malformed_tokens_are_refused_never_coerced() -> None:
    for wrong in ("", "h", "30-", "-30", "1-2-3", "7.5", "30x", "1 - 30", "30 h"):
        with pytest.raises(WindowError):
            as_window_spec(wrong)


def test_a_spec_passes_through_unchanged() -> None:
    spec = WindowSpec(first=2, last=5, unit="hour")
    assert as_window_spec(spec) is spec
