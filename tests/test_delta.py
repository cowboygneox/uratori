"""`delta`: the change between adjacent buckets, inside the range and nowhere
else.

The language's oldest "still missing" item was a trend, and it had been
punted to the server computing one over a reading's response -- which is
rule 2 with extra steps: a number on a screen that no definition claims and
no version cites. `delta` is that number, declared.

Two properties carry the whole construct, and each has a wrong version that
would look right on a chart:

**The range bounds it.** n buckets in range produce n-1 changes. The oldest
bucket has no predecessor *in range*, and the answer is that stated absence
-- never a quiet fetch of the bucket before the window to make the series
come out even. A window is the population, and reaching outside it for one
more value is the cheap path narrowing -- or here, widening -- what the
answer is computed over, with nothing on the response to show it happened.

**A hole is not a bridge.** A bucket with no value breaks the chain in both
directions. Differencing across it instead would report a change spanning
two buckets under a label claiming one.
"""

from __future__ import annotations

import pytest

from uratori.engine.read import Sample, delta_of
from uratori.engine.serve import serve_reading
from uratori.lang.check import CheckError
from uratori.lang.settings import fingerprint
from uratori.results import Ok
from uratori.store import MemoryEngineStore, Pointer

from .world import DEFAULTS, compile_source


def sample(*points: tuple[str, float | None]) -> Sample:
    return Sample(
        values=tuple(v for _, v in points if v is not None),
        points=points,
        buckets_covered=sum(1 for _, v in points if v is not None),
        buckets_requested=len(points),
    )


def test_the_oldest_bucket_in_range_has_no_predecessor_and_says_so() -> None:
    """A stated absence, not an omission. The response still describes n
    buckets, so a chart's x-axis is the window the caller asked for and not
    the window minus one."""
    got = delta_of(sample(("2026-01", 10.0), ("2026-02", 14.0), ("2026-03", 12.0)))
    assert got == [None, 4.0, -2.0]
    assert len(got) == 3, "n buckets in, n buckets out -- one of them absent"


def test_n_buckets_produce_n_minus_one_changes() -> None:
    got = delta_of(sample(*[(f"2026-{m:02d}", float(m)) for m in range(1, 7)]))
    assert sum(1 for v in got if v is not None) == 5


def test_a_single_bucket_window_is_one_stated_absence() -> None:
    """Not an empty list: the caller asked about one bucket and the honest
    answer is "there is nothing here to compare it against"."""
    assert delta_of(sample(("2026-01", 10.0))) == [None]


def test_an_empty_window_is_an_empty_answer() -> None:
    assert delta_of(sample()) == []


def test_a_hole_breaks_the_chain_in_both_directions() -> None:
    """February never happened. The change *into* February and the change
    *out of* it are both unknown.

    The wrong version bridges the hole -- differencing March against January
    -- which reports a two-month movement in a column headed "per month",
    and it does it most often exactly where collection was patchy.
    """
    got = delta_of(sample(("2026-01", 10.0), ("2026-02", None), ("2026-03", 20.0)))
    assert got == [None, None, None]


def test_a_hole_at_the_start_does_not_shift_the_answer() -> None:
    got = delta_of(sample(("2026-01", None), ("2026-02", 5.0), ("2026-03", 8.0)))
    assert got == [None, None, 3.0]


def test_a_flat_run_reads_nought_rather_than_absent() -> None:
    """A carried figure is flat by construction, and nought is a real change:
    "the goal did not move" is a finding. Absent would say the opposite --
    that nobody knows whether it moved."""
    got = delta_of(sample(("2026-01", 25.0), ("2026-02", 25.0), ("2026-03", 25.0)))
    assert got == [None, 0.0, 0.0]


def test_the_answer_lines_up_positionally_with_the_series() -> None:
    """Point i's delta is the change *into* bucket i, so a chart can draw the
    two against one x-axis. The other convention -- the change *out of*
    bucket i -- puts the last bucket's cell empty instead of the first, and a
    reader comparing the two columns would be off by one everywhere."""
    s = sample(("2026-01", 10.0), ("2026-02", 14.0))
    got = delta_of(s)
    assert len(got) == len(s.points)
    assert got[1] == 4.0, "bucket 2's cell is the change from bucket 1 into it"


def test_a_delta_cell_is_never_computed_from_a_value_outside_the_points() -> None:
    """The bounded-access claim at the unit level.

    Asserted by giving the sample values that make an out-of-range reach
    *visible*: if any cell were differenced against something other than its
    own neighbour, the arithmetic would not close. Every non-null cell must
    equal the difference of two adjacent points, and there must be exactly
    one fewer of them than there are points.

    (An earlier version of this test compared `sample.points` to itself
    before and after the call, on a frozen dataclass -- a tautology that
    could not fail under any implementation, including one that reached
    outside the window. The serving-level half of the claim is
    `test_no_fetch_reaches_outside_the_resolved_window`, below, in this
    file.)
    """
    points = (("2026-01", 3.0), ("2026-02", 11.0), ("2026-03", 2.0), ("2026-04", 40.0))
    got = delta_of(sample(*points))

    assert got[0] is None
    for index in range(1, len(points)):
        expected = points[index][1] - points[index - 1][1]  # type: ignore[operator]
        assert got[index] == expected, (
            f"cell {index} is {got[index]}, not the change from its own predecessor"
        )
    assert sum(1 for v in got if v is not None) == len(points) - 1


# --------------------------------------------------------- the grammar --

BASE = """
group code_change.merged_by_day from (authorAccountId through team_person.accounts.accountId, mergedAt by day in tenant.timezone)
group work_issue.delivered_by_day from (assigneeAccountId through team_person.accounts.accountId, completedAt by day in tenant.timezone)
group code_review_request.asked_of from reviewerAccountId through team_person.accounts.accountId
filter code_review_request.pending where pending == true
measure code_change.open_seconds = mergedAt - createdAt
measure code_review_request.waiting_seconds = now - requestedAt

# Time to merge.
figure team_person.time_to_merge bucketed:
    display "{team_person} to merge"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)

# Delivered per day.
figure team_person.delivered bucketed:
    display "{team_person} delivered"
    depends:
        done = work_issue.delivered_by_day:{team_person}
    calculate:
        count(done)
"""


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_source(BASE + extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


def test_delta_compiles_over_a_windowed_reading() -> None:
    lib = compile_source(
        BASE
        + """
# How the merge time moved.
reading team_person.merge_trend(range):
    display "{team_person} merge trend"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        median(t)
        delta(t)
"""
    )
    plan = lib.reading("team_person.merge_trend")
    assert plan is not None
    assert [s.fn for s in plan.calculate] == ["median", "delta"]


def test_delta_is_allowed_over_a_count_figure() -> None:
    """The distribution statistics are refused over daily counts because a
    mean of them is a mean per *day* wearing a per-record label. `delta` is
    not a distribution: the change between one day's count and the next is a
    claim about days, which is exactly what the buckets are."""
    lib = compile_source(
        BASE
        + """
# How delivery moved.
reading team_person.delivery_trend(range):
    display "{team_person} delivery trend"
    depends:
        d = team_person.delivered in range
    calculate:
        delta(d)
"""
    )
    assert lib.reading("team_person.delivery_trend") is not None


def test_a_live_reading_may_not_take_a_delta() -> None:
    """A live reading measures records as they stand and stores nothing, so
    there is no sequence of buckets to difference. Left to compile it would
    answer an empty list under a heading promising a trend."""
    refuses(
        """
# Pending, now.
reading team_person.pending():
    display "{team_person} pending"
    depends:
        waiting = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        delta(waiting)
""",
        "delta",
        "measures records as they stand",
    )


def test_two_deltas_are_refused_the_way_two_series_are() -> None:
    refuses(
        """
# Twice over.
reading team_person.twice(range):
    display "{team_person} twice"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        delta(t)
        delta(t)
""",
        "two deltas",
    )


def test_a_band_may_not_colour_a_delta() -> None:
    """A band colours one number. A delta is one cell per bucket, so there is
    no single value to compare against a threshold -- and a band that
    compiled here would leave `level_of` with nothing to read and every row
    permanently grey, which reads as missing data rather than as a broken
    definition. That is the same argument the "bands on the mean, which it
    does not calculate" refusal makes."""
    refuses(
        """
# Trend, banded.
reading team_person.banded(range):
    display "{team_person} banded"
    band low on delta against flow.leadTimeDays
    depends:
        t = team_person.time_to_merge in range
    calculate:
        delta(t)
""",
        "delta",
        "one cell per bucket",
    )


def test_delta_takes_no_grain() -> None:
    """No stride and no offset variants either -- each would be a second
    spelling of a question no definition has asked."""
    with pytest.raises(Exception) as caught:
        compile_source(
            BASE
            + """
# Grained.
reading team_person.grained(range):
    display "{team_person} grained"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        delta(t) by hour
"""
        )
    assert "series" in str(caught.value)


# ----------------------------------------------- the range really bounds --


DAILY = compile_source(
    BASE
    + """
# How the merge time moved, day to day.
reading team_person.merge_trend(range):
    display "{team_person} merge trend"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        median(t)
        delta(t)
"""
)


class SpyStore(MemoryEngineStore):
    """A store that remembers every way it was asked for stored values.

    The claim `delta` makes is that no fetch reaches outside the buckets the
    window resolved to. That is not something a value assertion can prove --
    a widened fetch that happened to be filtered afterwards would look
    identical on the wire -- so the proof has to be about the calls.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ranges: list[tuple[str, str]] = []
        self.unbounded: list[str] = []
        self._inside_range = 0

    async def values_in_range(self, tenant, name, version, frm, to):  # type: ignore[no-untyped-def]
        self.ranges.append((frm, to))
        # The in-memory store answers a bounded fetch by scanning `values` and
        # filtering -- a legitimate implementation detail (the Postgres store
        # issues a real bounded query), and not a fetch the *serving path*
        # made. Counting it would make this test assert the store's shape
        # instead of the reading's behaviour, and it would fail against one
        # store and pass against the other.
        self._inside_range += 1
        try:
            return await super().values_in_range(tenant, name, version, frm, to)
        finally:
            self._inside_range -= 1

    async def values(self, tenant, name, version):  # type: ignore[no-untyped-def]
        if not self._inside_range:
            self.unbounded.append(f"values({name})")
        return await super().values(tenant, name, version)

    async def values_under(self, tenant, name, version, prefix):  # type: ignore[no-untyped-def]
        if not self._inside_range:
            self.unbounded.append(f"values_under({name})")
        return await super().values_under(tenant, name, version, prefix)


async def _seeded() -> tuple[SpyStore, float]:
    store = SpyStore()
    figure = DAILY.figure("team_person.time_to_merge")
    assert figure is not None
    tenant = "t1"
    await store.set_pointer(
        tenant,
        figure.name,
        Pointer(
            version=figure.version,
            settings_fingerprint=fingerprint(dict(DEFAULTS), list(figure.settings)),
        ),
    )
    await store.set_buckets(tenant, "code_change.merged_by_day", "c1", ["p1@2026-03-10"])
    for day, value in (
        ("2026-03-01", [100.0]),  # well before the window -- must never be read
        ("2026-03-07", [900.0]),  # the day *before* the window opens
        ("2026-03-08", [100.0]),  # the window's oldest day
        ("2026-03-09", [140.0]),
        ("2026-03-10", [120.0]),  # the anchor day
    ):
        await store.save(
            tenant, figure.name, figure.version, f"p1@{day}", value, (), "P One"
        )
    return store, 1_773_172_800_000.0  # 2026-03-10T20:00Z, midday in Los Angeles


async def test_the_oldest_bucket_is_absent_rather_than_reaching_one_day_back() -> None:
    """The day before the window holds a value, and the answer must not use
    it.

    This is the whole discipline in one assertion. Differencing 2026-03-08
    against 2026-03-07 would produce a complete-looking column with no empty
    cell -- and a number computed from a bucket the response does not
    contain, which nobody reading the response could check.
    """
    store, at = await _seeded()
    reading = DAILY.reading("team_person.merge_trend")
    assert reading is not None
    result = await serve_reading(store, DAILY, "t1", reading, DEFAULTS, [3], at_ms=at)

    assert isinstance(result.state, Ok)
    window = result.subjects[0].windows[0]
    assert (window.frm, window.to) == ("2026-03-08", "2026-03-10")
    assert window.delta == [None, 40.0, -20.0], (
        "the oldest bucket in range has no predecessor in range, and 900.0 "
        "from the day before must not have been reached for"
    )


async def test_no_fetch_reaches_outside_the_resolved_window() -> None:
    """The call-level half: every bounded fetch sits inside the window, and
    no unbounded fetch of the source happens at all.

    Asserted on the calls rather than on the values because a widened fetch
    filtered afterwards is invisible on the wire -- and it is the fetch, not
    the filter, that the cheap-path rule is about.
    """
    store, at = await _seeded()
    reading = DAILY.reading("team_person.merge_trend")
    assert reading is not None
    await serve_reading(store, DAILY, "t1", reading, DEFAULTS, [3], at_ms=at)

    assert store.ranges, "the window is served from a bounded fetch"
    for frm, to in store.ranges:
        assert frm[:10] >= "2026-03-08", f"fetch opened at {frm}, before the window"
        assert to[:10] <= "2026-03-10", f"fetch ran to {to}, past the window"
    assert store.unbounded == [], (
        "a whole-figure read would drag every bucket of every subject back and "
        "make the range a filter rather than a bound: " + repr(store.unbounded)
    )


async def test_two_spans_fetch_across_their_union_and_neither_borrows_the_others_edge() -> None:
    """One request, two spans, one fetch -- and the older span's newest
    bucket must not become the newer span's predecessor.

    The join is per span: each resolves its own labels and each answers its
    own stated absence. Sharing one fetch is an efficiency; sharing an edge
    would be two windows quietly reporting a change between them.
    """
    store, at = await _seeded()
    reading = DAILY.reading("team_person.merge_trend")
    assert reading is not None
    result = await serve_reading(
        store, DAILY, "t1", reading, DEFAULTS, ["1-2", "3-4"], at_ms=at
    )
    newer, older = result.subjects[0].windows
    assert newer.delta is not None and newer.delta[0] is None
    assert older.delta is not None and older.delta[0] is None, (
        "each span states its own missing predecessor; neither borrows the other's"
    )


async def test_a_failed_floor_withholds_the_delta_with_every_other_statistic() -> None:
    """Every statistic is withheld together, and a delta is a statistic.

    A trend published beside a suppressed median would be the outlier's
    shape under a heading saying the sample was too thin to say anything --
    the "three statistics or none" bargain broken by the one statistic that
    draws a picture.
    """
    store, at = await _seeded()
    lib = compile_source(
        BASE
        + """
# How the merge time moved, if there is enough of it.
reading team_person.fussy(range):
    display "{team_person} fussy"
    depends:
        t = team_person.time_to_merge in range
    requires:
        at least 50 values in t
    calculate:
        median(t)
        delta(t)
"""
    )
    reading = lib.reading("team_person.fussy")
    figure = lib.figure("team_person.time_to_merge")
    assert reading is not None and figure is not None
    result = await serve_reading(store, lib, "t1", reading, DEFAULTS, [3], at_ms=at)
    window = result.subjects[0].windows[0]
    assert window.unmet, "the floor should have fallen short"
    assert window.delta is None, "a withheld reading may not still publish its trend"
    assert window.delta_display is None


async def test_the_delta_cells_are_served_rendered_and_signed() -> None:
    """The page prints these strings and computes nothing, so they are the
    whole of what a reader sees.

    A fall must read as a fall in the reading's own unit. Rendering the rise
    as `1.0h` and the fall beside it as `-3600s` is the same column speaking
    two languages, and it is what happens when a unit ladder is written as a
    series of `<` tests against positive bounds.
    """
    store, at = await _seeded()
    reading = DAILY.reading("team_person.merge_trend")
    assert reading is not None
    result = await serve_reading(store, DAILY, "t1", reading, DEFAULTS, [3], at_ms=at)
    window = result.subjects[0].windows[0]

    assert window.delta == [None, 40.0, -20.0]
    assert window.delta_display == [None, "40s", "-20s"], (
        "one cell per bucket, rendered, signed, and null exactly where delta is"
    )
    assert window.delta is not None and window.delta_display is not None
    assert len(window.delta_display) == len(window.delta)


def test_a_negative_duration_renders_in_the_same_unit_as_a_positive_one() -> None:
    """`delta` is the first statistic that can be negative, and it found the
    unit ladder testing `seconds < 60` -- which every negative satisfies."""
    from uratori.engine.project import format_value

    settings = {"tenant": {"hoursPerDay": 8}}
    assert format_value(3600.0, "duration", settings) == "1.0h"
    assert format_value(-3600.0, "duration", settings) == "-1.0h"
    assert format_value(-7200.0, "duration", settings) == "-2.0h"
    assert format_value(-30.0, "duration", settings) == "-30s"
    assert format_value(-172800.0, "duration", settings) == "-2.0d"
    # A fall too small to render is not a fall in the wrong direction. A
    # delta is the difference of two float means, so a tenth of a second is
    # an ordinary cell, and "-0s" reads as a direction somebody measured
    # where the truth is that it rounded away.
    assert format_value(-0.1, "duration", settings) == "0s"
    assert format_value(-0.0, "duration", settings) == "0s"
    # The same ladder shape, one unit along: a subtraction under `unit effort`
    # can go negative too, and printed a fortnight of work as "-240.0h".
    assert format_value(-864000.0, "effort", settings) == "-30.0d"


def test_a_delta_over_a_sub_day_figure_is_refused_rather_than_served_empty() -> None:
    """A delta's cells are its source's own buckets, and it has no grain to
    group them to the way a series has.

    Left to compile it did not fail -- it answered `[]`, with no reason and
    no unmet requirement, which is an empty list under a heading promising a
    trend: exactly what the live-reading refusal exists to prevent, reached
    by a different road.
    """
    sub_day = """
group work_issue.by_quarter from (assigneeAccountId through team_person.accounts.accountId, completedAt by 15 minutes in tenant.timezone)

# Delivered per quarter-hour.
figure team_person.quarter_volume bucketed:
    display "{team_person} per quarter"
    depends:
        done = work_issue.by_quarter:{team_person}
    calculate:
        count(done)
"""
    refuses(
        sub_day
        + """
# Trend, too finely.
reading team_person.quarter_trend(range):
    display "{team_person} quarter trend"
    depends:
        q = team_person.quarter_volume in range
    calculate:
        delta(q)
""",
        "15 minutes",
        "the raw collection the payload exists to withhold",
    )

    # The control: the same delta over a day-keyed figure compiles.
    assert compile_source(
        BASE
        + """
# Trend, at the day.
reading team_person.day_trend(range):
    display "{team_person} day trend"
    depends:
        d = team_person.delivered in range
    calculate:
        delta(d)
"""
    ).reading("team_person.day_trend") is not None


def test_a_band_may_not_colour_a_series_either() -> None:
    """The same refusal, and it closes a hole that predates the delta: a
    band on a series compiled, `level_of` found no scalar to read, and every
    row banded unknown for ever."""
    refuses(
        """
# Banded series.
reading team_person.banded_series(range):
    display "{team_person} banded series"
    band low on series against flow.leadTimeDays
    depends:
        t = team_person.time_to_merge in range
    calculate:
        series(t)
""",
        "series",
        "one cell per bucket",
    )

    # The control: banding a scalar the reading does calculate is fine.
    assert compile_source(
        BASE
        + """
# Banded median.
reading team_person.banded_median(range):
    display "{team_person} banded median"
    band low on median against flow.leadTimeDays
    depends:
        t = team_person.time_to_merge in range
    calculate:
        median(t)
        delta(t)
"""
    ).reading("team_person.banded_median") is not None


def test_adding_a_delta_moves_the_readings_version_and_nothing_elses() -> None:
    """A reading that starts answering a trend is a different definition, so
    its hash moves. Every other definition in the file is untouched, because
    a new optional construct nobody used must leave existing versions
    exactly where they were -- that is what lets a construct be added
    without rebuilding a tenant's history."""
    without = compile_source(
        BASE
        + """
# Merge pace.
reading team_person.pace(range):
    display "{team_person} pace"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        median(t)
"""
    )
    with_delta = compile_source(
        BASE
        + """
# Merge pace.
reading team_person.pace(range):
    display "{team_person} pace"
    depends:
        t = team_person.time_to_merge in range
    calculate:
        median(t)
        delta(t)
"""
    )
    before = without.reading("team_person.pace")
    after = with_delta.reading("team_person.pace")
    assert before is not None and after is not None
    assert before.version != after.version

    assert [f.version for f in without.figures] == [f.version for f in with_delta.figures], (
        "the figures underneath did not change, so their stored values must be reused"
    )
