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

import pytest

from uratori import MemoryEngineStore, MemoryFactStore, Schema, Uratori, compile_source
from uratori.results import BundleResult, Result
from uratori.windows import WindowError

WORLD = Schema(kinds=frozenset())

DEFS = """
# A place a case happens.
fact room:
    name name
    name as text

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

    facts.put("t1", "room", "r1", {"name": "OR 1"})
    facts.put("t1", "room", "r2", {"name": "OR 2"})
    facts.put("t1", "room", "r3", {"name": "OR 3"})

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
