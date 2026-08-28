"""Window specs: a span of positions in an ordered bucket sequence.

The spec grammar is shared by the HTTP window parameter and a bundle member's
`over` list, so its rules are tested here once, against the one implementation
both doors call. The semantics under test are the pinned ones:

- bucket 1 is the bucket the anchor falls in, both ends inclusive;
- a bare number N is exactly buckets 1-N -- the trailing behaviour that
  predates spans, unchanged;
- a span is integers and nothing else: what one bucket *is* lives in the
  declarations, hashed, because an argument may never change what a number
  means. The unit-suffixed tokens (`1-48h`, `90m`, `30d`) are refused with a
  message that points at the declarations;
- `each:a-b` expands to the one-bucket windows `a-a ... b-b`, so a
  per-bucket comparison does not have to be enumerated;
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
    expand_window_arg,
    reach_days,
    refuse_reach,
    window_token,
)


def test_a_bare_number_is_exactly_the_trailing_span() -> None:
    """`30` and `1-30` are one question, canonically spelled: the last 30
    buckets, bucket 1 being the anchor bucket. Two spellings compiling to
    two values would let two identical tiles hash differently."""
    assert as_window_spec(30) == WindowSpec(first=1, last=30)
    assert as_window_spec("30") == WindowSpec(first=1, last=30)
    assert as_window_spec("1-30") == WindowSpec(first=1, last=30)
    assert window_token(as_window_spec("1-30")) == "30"


def test_an_offset_span_names_both_bounds() -> None:
    assert as_window_spec("31-60") == WindowSpec(first=31, last=60)
    assert window_token(WindowSpec(first=31, last=60)) == "31-60"


def test_unit_suffixes_are_refused_toward_the_declarations() -> None:
    """The v0.12 unit tokens are retired, not silently reinterpreted: `48h`
    parsing as 48 buckets would quietly re-scale a bookmarked sub-day span
    by 60x. The refusal names where the unit now lives."""
    for wrong in ("1-48h", "48h", "1-90m", "90m", "30d", "1-30d"):
        with pytest.raises(WindowError) as caught:
            as_window_spec(wrong)
        message = str(caught.value)
        assert "retired" in message, wrong
        assert "group clause" in message, wrong


def test_a_zero_bound_is_refused_toward_bucket_one() -> None:
    """The instinctive spelling is `0-30, 31-60`, and honouring it would make
    the first bucket silently one wider. The refusal must teach the
    convention: bucket 1 is the anchor bucket."""
    for wrong in ("0", "0-30"):
        with pytest.raises(WindowError) as caught:
            as_window_spec(wrong)
        message = str(caught.value)
        assert "anchor" in message, wrong
        assert "1-30, 31-60" in message, (
            "the refusal must spell out the convention to write instead"
        )


def test_a_reversed_span_is_refused() -> None:
    with pytest.raises(WindowError) as caught:
        as_window_spec("60-31")
    assert "31-60" in str(caught.value), "the refusal should say what to write"


def test_the_reach_ceiling_converts_through_the_bucket_rule() -> None:
    """One bound, expressed as reach in days whatever the rule, so a coarser
    sequence cannot smuggle a longer walk in under a smaller-looking span:
    `1-120` is fine over days and ten years over quarters."""
    assert refuse_reach(WindowSpec(1, MAX_REACH_DAYS), "day") is None
    assert refuse_reach(WindowSpec(1, MAX_REACH_DAYS + 1), "day") is not None
    assert refuse_reach(WindowSpec(1, 118), "month") is None
    assert refuse_reach(WindowSpec(1, 121), "month") is not None
    assert refuse_reach(WindowSpec(1, 39), "quarter") is None
    assert refuse_reach(WindowSpec(1, 41), "quarter") is not None
    # A sub-day rule reaches by its own width: 96 quarter-hours is one day.
    assert reach_days(WindowSpec(1, 96), "15 minutes") == 1
    # An ordinal weekday-of-month rule is at most one bucket per month, so
    # it converts at the month's width.
    assert refuse_reach(WindowSpec(1, 118), "first monday of month") is None
    assert refuse_reach(WindowSpec(1, 121), "first monday of month") is not None
    message = refuse_reach(WindowSpec(1, 121), "month")
    assert message is not None and str(MAX_REACH_DAYS) in message


def test_malformed_tokens_are_refused_never_coerced() -> None:
    for wrong in ("", "h", "30-", "-30", "1-2-3", "7.5", "30x", "1 - 30", "30 h"):
        with pytest.raises(WindowError):
            as_window_spec(wrong)


def test_a_spec_passes_through_unchanged() -> None:
    spec = WindowSpec(first=2, last=5)
    assert as_window_spec(spec) == spec


def test_each_expands_to_one_bucket_windows_in_order() -> None:
    """`each:1-4` is sugar for `1-1, 2-2, 3-3, 4-4` -- one window per bucket,
    nearest first, indistinguishable downstream from the enumerated
    spelling. Order is substantive: a screen binds to positions."""
    assert expand_window_arg("each:1-4") == (
        WindowSpec(1, 1),
        WindowSpec(2, 2),
        WindowSpec(3, 3),
        WindowSpec(4, 4),
    )
    assert expand_window_arg("each:3-5") == (
        WindowSpec(3, 3),
        WindowSpec(4, 4),
        WindowSpec(5, 5),
    )
    # The expansion is exactly the enumerated windows: same tokens, so the
    # duplicate check and the bundle hash cannot tell the spellings apart.
    assert [window_token(s) for s in expand_window_arg("each:2-3")] == ["2-2", "3-3"]


def test_each_carries_the_span_rules() -> None:
    for wrong in ("each:0-3", "each:5-2"):
        with pytest.raises(WindowError):
            expand_window_arg(wrong)
    # A plain token expands to itself, so every door can expand uniformly.
    assert expand_window_arg("31-60") == (WindowSpec(31, 60),)
    assert expand_window_arg(30) == (WindowSpec(1, 30),)


def test_a_one_bucket_offset_span_is_legal_and_keeps_both_bounds() -> None:
    """`5-5` is one bucket, five back -- a real question, and its token keeps
    both bounds so it cannot be mistaken for the trailing `5`."""
    spec = as_window_spec("5-5")
    assert spec == WindowSpec(first=5, last=5)
    assert window_token(spec) == "5-5"


def test_unicode_digits_are_not_windows() -> None:
    r"""`\d` matches every Unicode digit class, and "٣٠" quietly becoming 30
    would be a coercion -- a plausible window nobody spelled."""
    fullwidth_twelve = "\uff11\uff12"  # fullwidth 12, spelled as escapes for the linter
    for wrong in ("٣٠", fullwidth_twelve, "1-٣٠"):
        with pytest.raises(WindowError):
            as_window_spec(wrong)
