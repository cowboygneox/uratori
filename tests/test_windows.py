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
    MAX_BUCKETS,
    MAX_REACH_DAYS,
    MAX_WINDOWS,
    WindowError,
    WindowSpec,
    as_window_spec,
    expand_window_arg,
    expand_window_args,
    make_window_spec,
    reach_days,
    refuse_reach,
    refuse_window_count,
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
    for wrong in (
        "1-48h", "48h", "1-90m", "90m", "30d", "1-30d",
        # Case and the longer spellings too: the message exists to catch a
        # bookmarked v0.12 span and send its author to the group clause, and
        # `1-48H` falling through to the generic "not a window" sent exactly
        # that reader hunting for a typo instead.
        "1-48H", "48H", "90D", "1-90M", "48hr", "90min",
    ):
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


# ------------------------------------------------ the bounds on the walk --


def test_a_span_is_bounded_in_buckets_before_anything_is_sized_by_it() -> None:
    """The reach ceiling is expressed in days, which makes it *looser* for a
    fine sequence exactly where it claims to be tighter: 5,270,400 minute
    buckets divide down to 3,660 days and sail through `refuse_reach`, having
    asked the server for five million labels and -- under `each` -- five
    million windows.

    So the count is bounded at construction, the one gate every door goes
    through. Without this the refusal arrives only after the work it means to
    refuse has been done.
    """
    assert make_window_spec(1, MAX_BUCKETS).last == MAX_BUCKETS

    with pytest.raises(WindowError) as refused:
        make_window_spec(1, MAX_BUCKETS + 1)
    assert str(MAX_BUCKETS) in str(refused.value)
    assert "group clause" in str(refused.value), (
        "the refusal must point at the declaration that could ask the same "
        "question in fewer buckets, not merely say no"
    )

    # The span the days-based ceiling waves through: 5,270,400 minute buckets
    # divide down to 3,660 days. Asserted only as the bucket ceiling catching
    # it -- deliberately *not* as `refuse_reach` returning None, which would
    # pin the absence of a bound and turn red on a correct future tightening.
    with pytest.raises(WindowError):
        make_window_spec(1, 5_270_400)

    # An offset span is bounded by its far edge, not its width: `5000-5001`
    # is two buckets but reaches five thousand back, and resolving it walks
    # the sequence from the anchor to get there.
    with pytest.raises(WindowError):
        make_window_spec(MAX_BUCKETS + 1, MAX_BUCKETS + 2)


def test_each_refuses_an_oversized_span_without_first_expanding_it() -> None:
    """`each:1-20000000` used to build twenty million one-bucket windows and
    then let something downstream complain -- gigabytes of tuples per request,
    on a route anyone can call.

    Asserted as bounded *work*, not elapsed time: the check counts the objects
    the call is allowed to make, so it cannot flake on a slow runner the way a
    timing assertion would.
    """
    # Counted, not timed. `tests/test_server.py` already asserted a 422 for
    # `each:1-1000000` and passed while allocating 136 MB first, because a
    # status code cannot tell you what was spent reaching it. This counts the
    # specs the call is allowed to construct, so it is deterministic and
    # cannot flake on a slow runner.
    import uratori.windows as module

    built = 0
    real = module.WindowSpec

    class Counted(real):  # type: ignore[misc,valid-type]
        def __new__(cls, *args: object, **kwargs: object) -> Counted:
            nonlocal built
            built += 1
            return super().__new__(cls)  # type: ignore[misc]

    module.WindowSpec = Counted  # type: ignore[misc]
    try:
        with pytest.raises(WindowError):
            expand_window_arg("each:1-20000000")
        assert built <= 2, (
            f"refusing an oversized `each` built {built} window specs -- it must "
            "validate the span before expanding it, or the refusal costs exactly "
            "the work it exists to refuse"
        )
    finally:
        module.WindowSpec = real  # type: ignore[misc]

    # And the legal maximum expands to exactly its buckets, no more.
    assert len(expand_window_arg(f"each:1-{MAX_BUCKETS}")) == MAX_BUCKETS


def test_one_request_may_not_ask_for_more_windows_than_the_ceiling() -> None:
    """A span's bucket ceiling bounds one *window*; `each` turns one argument
    into one window per bucket, and the server answers every window for every
    subject. `each 1-3660` is inside the bucket ceiling as a span and yet asks
    for 3,660 answers per subject -- a different question from `1-3660`'s
    single pooled one, and the ceiling that bounds the first does not bound
    the second.
    """
    assert refuse_window_count(MAX_WINDOWS) is None
    refusal = refuse_window_count(MAX_WINDOWS + 1)
    assert refusal is not None and str(MAX_WINDOWS) in refusal

    # The list door refuses the product, not just each argument on its own:
    # every token below is individually legal.
    with pytest.raises(WindowError):
        expand_window_args([f"each:1-{MAX_WINDOWS}", "1-5"])

    assert len(expand_window_args([f"each:1-{MAX_WINDOWS}"])) == MAX_WINDOWS


def test_a_bare_bound_after_each_is_one_to_n_the_way_it_is_everywhere() -> None:
    """Two bugs met here. The HTTP regex demanded the dash, so `each:12` was
    refused at one door and accepted at the other; and the bundle clause
    read the bare form as the single bucket 12, when `over 12` means `1-12`
    everywhere else in the grammar.

    The second is the dangerous one: it is silent. An author writing the
    short spelling wants a column per bucket, and one column is a wrong
    answer with no error attached. A bare bound is `1-N` whether or not
    `each` precedes it; the single bucket 12 is `each 12-12`, which still
    says so.
    """
    assert expand_window_arg("each:3") == (
        WindowSpec(1, 1),
        WindowSpec(2, 2),
        WindowSpec(3, 3),
    )
    assert expand_window_arg("each:3") == expand_window_arg("each:1-3")
    # And the offset form still names one bucket when that is what is written.
    assert expand_window_arg("each:12-12") == (WindowSpec(12, 12),)


def test_a_fifth_weekday_bucket_reaches_a_quarter_not_a_month() -> None:
    """Most of the ordinal family is one bucket per month. A *fifth* weekday
    is not: it exists only in months long enough to hold one, about four
    months in twelve, so its buckets sit ~87 days apart.

    Converting it at a month's width made the ceiling claim ten years and
    grant twenty-eight -- 118 fifth-Mondays reach past 10,000 days. Measured
    against the calendar rather than asserted: the control is `first monday`,
    which really is monthly and must be unaffected.
    """
    from datetime import date

    from uratori.engine.buckets import ordinal_weekday_day

    def mean_gap(ordinal: int) -> float:
        days = [
            date.fromisoformat(found)
            for year in range(1900, 2100)
            for month in range(1, 13)
            if (found := ordinal_weekday_day(year, month, ordinal, 0)) is not None
        ]
        return (days[-1] - days[0]).days / (len(days) - 1)

    assert 30.0 < mean_gap(1) < 31.0, "a first Monday really is monthly"
    assert 85.0 < mean_gap(5) < 90.0, "a fifth Monday is nearly quarterly"

    # 118 buckets is the month-width ceiling's answer. Against the real
    # spacing it is 28 years, so the fifth-weekday rule must refuse it.
    assert refuse_reach(WindowSpec(1, 118), "first monday of month") is None
    assert refuse_reach(WindowSpec(1, 118), "fifth monday of month") is not None
    # And its own boundary holds: 3660 / 92 is 39 buckets.
    assert refuse_reach(WindowSpec(1, 39), "fifth monday of month") is None
    assert refuse_reach(WindowSpec(1, 41), "fifth monday of month") is not None


def test_the_window_ceiling_counts_as_the_list_grows_not_once_at_the_end() -> None:
    """The same bomb by repetition instead of by one big number. Two thousand
    copies of a perfectly legal `each:1-3660` is a 40 KB query string, and
    building the whole list before counting it spent 15 seconds and a
    gigabyte to arrive at a 422 -- the refusal costing exactly what it exists
    to prevent, which is the bug this ceiling was added to fix in the first
    place.

    Counted, not timed: the peak must be bounded by one argument's worth, so
    the assertion is on how many specs were ever constructed.
    """
    import uratori.windows as module

    built = 0
    real = module.WindowSpec

    class Counted(real):  # type: ignore[misc,valid-type]
        def __new__(cls, *args: object, **kwargs: object) -> Counted:
            nonlocal built
            built += 1
            return super().__new__(cls)  # type: ignore[misc]

    module.WindowSpec = Counted  # type: ignore[misc]
    try:
        with pytest.raises(WindowError) as refused:
            expand_window_args([f"each:1-{MAX_BUCKETS}"] * 2000)
        # One argument's expansion, and no more: the second copy is never
        # reached, because the first already passed the ceiling.
        assert built <= MAX_BUCKETS + MAX_WINDOWS, (
            f"refusing a repeated window argument built {built} specs -- the "
            "count must be checked as the list grows, or a repeatable "
            "parameter multiplies past any per-argument bound"
        )
        assert str(MAX_WINDOWS) in str(refused.value)
    finally:
        module.WindowSpec = real  # type: ignore[misc]

    # Many small arguments are bounded the same way, and the boundary holds.
    assert len(expand_window_args(["1"] * MAX_WINDOWS)) == MAX_WINDOWS
    with pytest.raises(WindowError):
        expand_window_args(["1"] * (MAX_WINDOWS + 1))
