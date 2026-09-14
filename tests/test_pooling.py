"""`?subject=` pools several subjects' stored buckets into one row.

A reading answers one row per subject, and a screen that lets a reader pick
several -- three rooms, say -- has no honest way to show "the median turnover
across these three": the engine refuses client-side arithmetic everywhere
else, and a facility-level figure answers the wrong population (every room,
not the three asked for). The engine already holds every room's stored
buckets, so it pools them itself rather than making a client average three
medians or fetch-and-compute.

Every test here is against the in-memory engine, which is the same serving
path a real Postgres-backed server runs -- `serve_reading`/`answer_bundle`
know nothing about which store they were given.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from uratori import MemoryEngineStore, MemoryFactStore, Schema, Uratori, compile_source
from uratori.engine.serve import _pool_kind
from uratori.lang.ast import Extreme
from uratori.results import BundleResult, Result
from uratori.windows import WindowError

WORLD = Schema(kinds=frozenset())

DEFS = """
# A place a case happens.
fact room:
    name name
    name as text
    goal_seconds as number

# One case, start to finish.
fact turnover_record:
    name ref
    ref as text
    room_id as text
    started_at as moment
    completed_at as moment

# One case closing, counted rather than timed.
fact case_record:
    name ref
    ref as text
    room_id as text
    closed_at as moment

group turnover_record.by_day from (room_id, completed_at by day)
group case_record.by_day from (room_id, closed_at by day)

# How long one case took.
measure turnover_record.length = completed_at - started_at

# Every case's length, day by day.
figure room.turnover_day bucketed:
    display "{room} turnover"
    depends:
        t = turnover_record.by_day:{room}
    calculate:
        list(turnover_record.length over t)

# How many cases closed, day by day.
figure room.cases_day bucketed:
    display "{room} cases"
    depends:
        c = case_record.by_day:{room}
    calculate:
        count(c)

# The daily case goal, day by day -- a second count figure, of the same
# shape as room.cases_day, so a band test can pool a threshold too.
figure room.case_goal_day bucketed:
    display "{room} case goal"
    depends:
        c = case_record.by_day:{room}
    calculate:
        count(c)

# The median case length over the window.
reading room.turnover_median(range):
    display "{room} median turnover"
    depends:
        d = room.turnover_day in range
    calculate:
        median(d)

# Cases closed over the window.
reading room.cases_total(range):
    display "{room} cases total"
    depends:
        c = room.cases_day in range
    calculate:
        sum(c)

# Cases closed, banded against the goal figure over the same window.
reading room.cases_banded(range):
    display "{room} vs goal"
    band on sum:
        when value > room.case_goal_day then "over"
        otherwise "ok"
    depends:
        c = room.cases_day in range
    calculate:
        sum(c)

# The room's own turnover, reduced to one statistic per bucket already --
# a `median` figure, not a `list` or a `count`. Exists to prove pooling
# refuses it: re-pooling an already-reduced bucket by concatenating or
# summing it across subjects would be a median of medians.
figure room.turnover_typical bucketed:
    display "{room} typical turnover"
    depends:
        t = turnover_record.by_day:{room}
    calculate:
        median(turnover_record.length over t)

# The room's typical turnover, reduced from the median figure above.
reading room.typical(range):
    display "{room} typical"
    depends:
        m = room.turnover_typical in range
    calculate:
        median(m)

# The daily turnover goal, read straight off the room's own record -- the
# same value at every bucket and, in the fixture below, the same value on
# every room, so pooling several rooms must not move it.
figure room.goal_day bucketed:
    display "{room} goal"
    unit duration

    depends:
        t = turnover_record.by_day:{room}
    calculate:
        room.goal_seconds

# Turnover banded against the shared goal, on the median.
reading room.turnover_median_banded(range):
    display "{room} vs goal (median)"
    band on median:
        when value > room.goal_day then "over"
        otherwise "ok"
    depends:
        d = room.turnover_day in range
    calculate:
        median(d)

# Turnover banded against the shared goal, on the p90.
reading room.turnover_p90_banded(range):
    display "{room} vs goal (p90)"
    band on percentile:
        when value > room.goal_day then "over"
        otherwise "ok"
    depends:
        d = room.turnover_day in range
    calculate:
        percentile 90 of d

# A note left on a room, with no time dimension at all.
fact room_note:
    name ref
    ref as text
    room_id as text

group room_note.by_room from room_id

# How many notes a room has -- a bare, non-bucketed figure, for the bundle
# test that needs a member pooling refuses.
figure room.notes:
    display "{room} notes"
    depends:
        n = room_note.by_room:{room}
    calculate:
        count(n)
"""

AT = 1_787_572_800_000.0  # 2026-08-24T12:00Z


async def _engine():
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_source(DEFS, WORLD)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)

    facts.put("t1", "room", "r1", {"name": "OR 1", "goal_seconds": 1200.0})
    facts.put("t1", "room", "r2", {"name": "OR 2", "goal_seconds": 1200.0})
    facts.put("t1", "room", "r3", {"name": "OR 3", "goal_seconds": 1200.0})
    facts.put("t1", "room", "r5", {"name": "OR 5", "goal_seconds": 1200.0})

    # r1: two cases on 2026-08-20, lengths 10 and 30 minutes.
    facts.put(
        "t1", "turnover_record", "tv1",
        {"ref": "TV1", "room_id": "r1",
         "started_at": "2026-08-20T09:00:00Z", "completed_at": "2026-08-20T09:10:00Z"},
    )
    facts.put(
        "t1", "turnover_record", "tv2",
        {"ref": "TV2", "room_id": "r1",
         "started_at": "2026-08-20T10:00:00Z", "completed_at": "2026-08-20T10:30:00Z"},
    )
    # r2: one case on 2026-08-20, length 50 minutes.
    facts.put(
        "t1", "turnover_record", "tv3",
        {"ref": "TV3", "room_id": "r2",
         "started_at": "2026-08-20T09:00:00Z", "completed_at": "2026-08-20T09:50:00Z"},
    )
    # r3 (a room with nothing stored -- named in the pool, contributes nothing).

    # r5: one case on 2026-08-20, length 30 minutes -- a third contributor
    # to the goal-invariance tests, sharing r1 and r2's own goal value.
    facts.put(
        "t1", "turnover_record", "tv5",
        {"ref": "TV5", "room_id": "r5",
         "started_at": "2026-08-20T11:00:00Z", "completed_at": "2026-08-20T11:30:00Z"},
    )

    # Cases closed: r1 has 2 on the 20th, r2 has 3 on the 20th.
    for i in range(2):
        facts.put(
            "t1", "case_record", f"c1_{i}",
            {"ref": f"C1-{i}", "room_id": "r1", "closed_at": "2026-08-20T12:00:00Z"},
        )
    for i in range(3):
        facts.put(
            "t1", "case_record", f"c2_{i}",
            {"ref": f"C2-{i}", "room_id": "r2", "closed_at": "2026-08-20T12:00:00Z"},
        )

    await engine.run("t1", full=True, at_ms=AT)
    return engine, store, library, facts


def _windowed(result: Result | BundleResult | None) -> Result:
    assert isinstance(result, Result)
    return result


async def test_pooled_median_equals_the_median_of_the_concatenated_values() -> None:
    """r1 holds [10, 30] and r2 holds [50] minutes on the 20th; pooled, the
    sample is [10, 30, 50] and the median is 30 -- not 30 (mean of r1's
    median 20 and r2's median 50), which is the wrong-population arithmetic a
    client averaging two medians would produce."""
    engine, *_ = await _engine()
    result = _windowed(
        await engine.answer("t1", "room.turnover_median", trailing=[7], at="2026-08-24", subject=["r1", "r2"])
    )
    assert len(result.subjects) == 1
    row = result.subjects[0]
    assert row.id == "pool:r1,r2"
    assert row.name == "2 pooled"
    assert row.pooled == ["r1", "r2"]
    window = row.windows[0]
    assert window.median == pytest.approx(30 * 60.0)
    assert window.sample == 3


async def test_pooled_count_figure_sums_per_bucket() -> None:
    """r1 closed 2 cases and r2 closed 3 on the same day; pooled and summed
    over the window, the total is 5 -- the per-bucket sum a `count` figure's
    pooling promises, not a concatenation (which would answer a sample size
    of 2, not a total of 5)."""
    engine, *_ = await _engine()
    result = _windowed(
        await engine.answer("t1", "room.cases_total", trailing=[7], at="2026-08-24", subject=["r1", "r2"])
    )
    window = result.subjects[0].windows[0]
    assert window.total == pytest.approx(5.0)


async def test_subjects_with_nothing_stored_contribute_nothing() -> None:
    """r3 has no turnover recorded at all -- named in the pool, it must not
    change the value, and it must still appear in the id, because a room
    with nothing is a true zero contribution, not an error."""
    engine, *_ = await _engine()
    two = _windowed(
        await engine.answer("t1", "room.cases_total", trailing=[7], at="2026-08-24", subject=["r1", "r2"])
    )
    three = _windowed(
        await engine.answer(
            "t1", "room.cases_total", trailing=[7], at="2026-08-24", subject=["r1", "r2", "r3"]
        )
    )
    assert two.subjects[0].windows[0].total == three.subjects[0].windows[0].total
    assert three.subjects[0].id == "pool:r1,r2,r3"
    assert three.subjects[0].pooled == ["r1", "r2", "r3"]


async def test_a_band_threshold_is_pooled_over_the_same_subjects() -> None:
    """`room.case_goal_day` is pooled and reduced through the same `sum` the
    reading's own value goes through, so 5 cases against a pooled goal of 5
    reads "ok" rather than "over" from an unpooled (single-room) threshold."""
    engine, *_ = await _engine()
    result = _windowed(
        await engine.answer("t1", "room.cases_banded", trailing=[7], at="2026-08-24", subject=["r1", "r2"])
    )
    row = result.subjects[0]
    assert row.windows[0].total == pytest.approx(5.0)
    assert row.level == "ok"


async def test_a_reading_over_a_per_bucket_statistic_refuses_pooling() -> None:
    """`room.turnover_typical` is a `median` figure: each bucket is already a
    statistic taken over one room's own records. Pooling it by concatenating
    or summing across rooms would be a median of medians -- the exact
    arithmetic a reading exists to withhold -- so the request is refused
    before any data is even fetched."""
    engine, *_ = await _engine()
    with pytest.raises(WindowError, match="median of medians"):
        await engine.answer(
            "t1", "room.typical", trailing=[7], at="2026-08-24", subject=["r1", "r2"]
        )


async def test_a_carried_or_extreme_figure_is_not_a_poolable_kind() -> None:
    """`_pool_kind` is what `serve_reading` asks before it will pool a
    reading's source at all -- the same decision `room.turnover_typical`'s
    end-to-end refusal above exercises for a `median` figure. Checked
    directly here for the other two unpoolable shapes, since compiling a
    `latest(...)` figure needs a moment measure and produces a figure whose
    unit a *reading* refuses outright regardless of pooling, which would
    test the wrong refusal.

    A point value taken over one subject's own records -- the latest record,
    or a carried-forward value repeated across buckets nobody moved it in --
    is not an additive total and not a list: pooling it by summing or
    concatenating would answer a number no definition claims."""
    engine, store, library, facts = await _engine()
    turnover_day = library.figure("room.turnover_day")
    assert turnover_day is not None

    extreme = replace(turnover_day, calculate=Extreme(which="latest", measure="x", set="t"))
    assert _pool_kind(extreme) is None

    carried_count = replace(library.figure("room.cases_day"), carried=True)
    assert _pool_kind(carried_count) is None


async def test_list_and_count_figures_still_pool_after_the_kind_check() -> None:
    """The control for the two refusals above: the kind check must not have
    collaterally broken the two shapes that were always meant to pool."""
    engine, *_ = await _engine()
    median = _windowed(
        await engine.answer(
            "t1", "room.turnover_median", trailing=[7], at="2026-08-24", subject=["r1", "r2"]
        )
    )
    assert median.subjects[0].windows[0].median == pytest.approx(30 * 60.0)
    total = _windowed(
        await engine.answer(
            "t1", "room.cases_total", trailing=[7], at="2026-08-24", subject=["r1", "r2"]
        )
    )
    assert total.subjects[0].windows[0].total == pytest.approx(5.0)


async def test_a_band_threshold_shared_across_pooled_subjects_is_not_multiplied() -> None:
    """r1, r2 and r5 all carry the *same* goal, 1200 seconds. Pooled turnover
    is [600, 1800, 3000, 1800] (r1's two cases, r2's one, r5's one) -- median
    1800, p90 3000, both above the shared goal. Concatenating the goal
    (the correct pooling for a threshold) leaves it at 1200 regardless of how
    many rooms echo it, so both bands read "over"; summing it the way a
    `count` source pools would inflate it to 3600 and flip both to "ok" --
    which is exactly the bug this test would catch."""
    engine, *_ = await _engine()
    median_band = _windowed(
        await engine.answer(
            "t1", "room.turnover_median_banded", trailing=[7], at="2026-08-24",
            subject=["r1", "r2", "r5"],
        )
    )
    p90_band = _windowed(
        await engine.answer(
            "t1", "room.turnover_p90_banded", trailing=[7], at="2026-08-24",
            subject=["r1", "r2", "r5"],
        )
    )
    assert median_band.subjects[0].windows[0].median == pytest.approx(1800.0)
    assert median_band.subjects[0].level == "over"
    assert p90_band.subjects[0].windows[0].percentile == pytest.approx(3000.0)
    assert p90_band.subjects[0].level == "over"


async def test_a_duplicate_subject_is_refused() -> None:
    engine, *_ = await _engine()
    with pytest.raises(WindowError, match="twice"):
        await engine.answer("t1", "room.turnover_median", trailing=[7], at="2026-08-24", subject=["r1", "r1"])


async def test_an_empty_subject_value_is_refused() -> None:
    engine, *_ = await _engine()
    with pytest.raises(WindowError, match="empty"):
        await engine.answer("t1", "room.turnover_median", trailing=[7], at="2026-08-24", subject=["r1", ""])


async def test_a_figure_refuses_subject() -> None:
    engine, *_ = await _engine()
    with pytest.raises(WindowError, match="pooling is a reading"):
        await engine.answer("t1", "room.turnover_day", at="2026-08-24", subject=["r1", "r2"])


async def test_a_projection_refuses_subject() -> None:
    engine, store, library, facts = await _engine()
    extra = """
# One row per case.
projection case_record.item:
    field:
        ref = ref as text
"""
    library2 = compile_source(DEFS + extra, WORLD)
    engine2 = Uratori(schema=WORLD, library=library2, store=store, facts=facts)
    with pytest.raises(WindowError, match="pooling is a reading"):
        await engine2.answer("t1", "case_record.item", subject=["r1", "r2"])


async def test_a_summary_refuses_subject() -> None:
    engine, store, library, facts = await _engine()
    extra = """
# One row per case.
projection case_record.item:
    field:
        ref = ref as text

# How many cases are on the page.
summarise case_record.total over case_record.item:
    count items
"""
    library2 = compile_source(DEFS + extra, WORLD)
    engine2 = Uratori(schema=WORLD, library=library2, store=store, facts=facts)
    with pytest.raises(WindowError, match="pooling is a reading"):
        await engine2.answer("t1", "case_record.total", subject=["r1", "r2"])


async def test_a_bundle_with_a_figure_member_refuses_subject_with_400_shaped_message() -> None:
    """The engine-level refusal a bundle gives is a `ValueError` -- the HTTP
    door maps that to 400, the same status the existing anchor-on-a-bundle
    refusal uses -- rather than the 422 a bare figure gets, because the
    figure being wrong is one member of a request that named several things
    at once."""
    engine, store, library, facts = await _engine()
    extra = """
# A tile: cases total beside the raw daily figure.
bundle room.board:
    cases = reading room.cases_total
    daily = figure room.notes
"""
    library2 = compile_source(DEFS + extra, WORLD)
    engine2 = Uratori(schema=WORLD, library=library2, store=store, facts=facts)
    with pytest.raises(ValueError, match="Pooling is a reading"):
        await engine2.answer("t1", "room.board", subject=["r1", "r2"])


async def test_a_bundle_of_readings_pools_every_member() -> None:
    engine, store, library, facts = await _engine()
    extra = """
# A tile: two readings, both poolable.
bundle room.readings_board:
    cases = reading room.cases_total
    median = reading room.turnover_median
"""
    library2 = compile_source(DEFS + extra, WORLD)
    engine2 = Uratori(schema=WORLD, library=library2, store=store, facts=facts)
    result = await engine2.answer("t1", "room.readings_board", subject=["r1", "r2"])
    assert isinstance(result, BundleResult)
    for member in result.results:
        row = member.result.subjects[0]
        assert row.id == "pool:r1,r2"
        assert row.pooled == ["r1", "r2"]
