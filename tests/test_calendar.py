"""Whose midnight cuts a bucket.

A calendar used to be a tenant dial: one timezone for a whole board, so a
courier in Tokyo and one in London had their days cut on somebody else's
midnight and the number under "yesterday" was about a period neither of them
worked. It is a **field on the subject's record** now.

Subject-scoped rather than record-scoped, and that is the load-bearing choice.
Read off the record being bucketed, a subject's sequence could mix calendars --
some of a courier's days cut in Tokyo, some in Berlin, depending on where each
order came from -- and a reading walks that sequence counting back positions,
so its window would be a span of no particular calendar. Read off the subject,
every bucket in a subject's sequence is cut the same way.

The price is here in the first test: one record shared by two subjects lands
on two different dates. That is the honest answer to "which day was this, for
them", and it is not the engine's place to pick.
"""

from __future__ import annotations

import pytest

from uratori import (
    CheckError,
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    Uratori,
    compile_source,
)
from uratori.results import Result

WORLD = Schema(kinds=frozenset())

SOURCE = '''
# Somebody who carries orders, and the calendar their days are cut on.
fact shop_courier:
    name name
    name as text
    timezone as text

# One order, delivered at one instant, possibly by two people.
fact shop_order:
    name ref
    ref as text
    courier_id as text
    delivered_at as moment

group shop_order.dropped_by_day from (courier_id, delivered_at by day in shop_courier.timezone)

# Deliveries per courier per day, each courier's own day.
figure shop_courier.drops bucketed:
    display "{shop_courier} deliveries that day"

    depends:
        done = shop_order.dropped_by_day:{shop_courier}

    calculate:
        count(done)

# How many this courier delivered over the window.
reading shop_courier.recent(range):
    display "{shop_courier} deliveries lately"

    depends:
        days = shop_courier.drops in range

    calculate:
        sum(days)
'''

# 2026-06-25T14:00Z: the 25th in London, already the 26th in Auckland.
ACROSS_MIDNIGHT = "2026-06-25T14:00:00Z"
AT = 1_782_000_000_000.0  # 2026-06-21T05:20Z -- unused as an anchor, fixed for repeatability


def compile_world(extra: str = ""):
    return compile_source(SOURCE + extra, WORLD)


async def _board(couriers: dict[str, str], orders: dict[str, dict[str, str]]):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world()
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    for key, zone in couriers.items():
        facts.put("t1", "shop_courier", key, {"name": key.upper(), "timezone": zone})
    for key, body in orders.items():
        facts.put("t1", "shop_order", key, body)
    await engine.run("t1", full=True)
    return engine, store, library, facts


async def _rows(store, library) -> dict[str, float | None]:
    plan = library.figure("shop_courier.drops")
    return {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}


async def test_each_subject_gets_their_own_days() -> None:
    """Two couriers in different calendars, one instant each. The labels are
    each courier's own date, and under a single tenant dial one of the two was
    always filed under a day they did not work."""
    _engine, store, library, _facts = await _board(
        {"c1": "Europe/London", "c2": "Pacific/Auckland"},
        {
            "o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT},
            "o2": {"ref": "O-2", "courier_id": "c2", "delivered_at": ACROSS_MIDNIGHT},
        },
    )
    rows = await _rows(store, library)
    assert rows.get("c1@2026-06-25") == 1.0, f"London's courier is filed wrong: {rows}"
    assert rows.get("c2@2026-06-26") == 1.0, f"Auckland's courier is filed wrong: {rows}"


async def test_a_subject_with_no_calendar_is_in_no_bucket() -> None:
    """Never UTC as a fallback. A courier nobody has recorded a calendar for
    has no calendar, and defaulting one files their history under days they
    never worked -- with nothing on the board to say it happened."""
    _engine, store, library, _facts = await _board(
        {"c1": ""},
        {"o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT}},
    )
    assert await _rows(store, library) == {}, (
        "a courier with no calendar was filed against one anyway"
    )


async def test_moving_a_subjects_calendar_refiles_their_history() -> None:
    """The invalidation this design turns on. Nothing about the *orders*
    changed, so the change stream over them says nothing -- but the courier
    is a kind the grouping resolves through, and a write to one of those
    escalates the pass to a full one. Without that, the board keeps yesterday's
    filing for ever and nothing anywhere says so."""
    engine, store, library, facts = await _board(
        {"c1": "Europe/London"},
        {"o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT}},
    )
    assert "c1@2026-06-25" in await _rows(store, library)

    facts.put("t1", "shop_courier", "c1", {"name": "C1", "timezone": "Pacific/Auckland"})
    await engine.run("t1", written={"shop_courier": ["c1"]})

    rows = await _rows(store, library)
    assert "c1@2026-06-26" in rows, (
        f"the courier moved calendars and their history stayed where it was: {rows}"
    )
    assert rows.get("c1@2026-06-25") in (None, 0.0), (
        f"the old day survived the move, so the same delivery is filed twice: {rows}"
    )


async def test_a_window_covers_each_subjects_own_dates() -> None:
    """The consequence a board has to live with: "the last seven days" is a
    different seven dates for each subject, so each window reports its own
    bounds and the response carries no single calendar over the top."""
    engine, _store, _library, _facts = await _board(
        {"c1": "Europe/London", "c2": "Pacific/Auckland"},
        {
            "o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT},
            "o2": {"ref": "O-2", "courier_id": "c2", "delivered_at": ACROSS_MIDNIGHT},
        },
    )
    served = await engine.answer("t1", "shop_courier.recent", trailing=[7], at="2026-06-26")
    assert isinstance(served, Result)
    assert served.zone is None, (
        "one calendar was printed over subjects cut two different ways"
    )
    ends = {s.id: s.windows[0].to for s in served.subjects if s.windows}
    assert ends["c1"] == "2026-06-26" and ends["c2"] == "2026-06-26"
    zones = {s.id: s.windows[0].zone for s in served.subjects if s.windows}
    assert zones == {"c1": "Europe/London", "c2": "Pacific/Auckland"}, (
        "each window must say whose calendar it was cut on, or a reader "
        "comparing two rows cannot tell they cover different dates"
    )


# ----------------------------------------------------------- what is refused --


def test_a_calendar_must_be_a_fact_kind_and_field() -> None:
    with pytest.raises(Exception) as caught:
        compile_world(
            '''
group shop_order.dialled from (courier_id, delivered_at by day in tenant.timezone)
'''
        )
    assert "not a fact kind" in str(caught.value)


def test_a_bare_name_is_refused_with_the_rewrite() -> None:
    with pytest.raises(Exception) as caught:
        compile_world(
            '''
group shop_order.bare from (courier_id, delivered_at by day in timezone)
'''
        )
    assert "kind and a field" in str(caught.value)


def test_the_calendar_must_be_on_the_subjects_own_kind() -> None:
    """Named on anything else, every key would be looked up in the wrong
    table and every record would land in no bucket -- which reads as a board
    that has collected nothing rather than as a wrong declaration."""
    with pytest.raises(CheckError) as caught:
        compile_world(
            '''
group shop_order.hopped from (courier_id through shop_courier.name, delivered_at by day in shop_order.ref)
'''
        )
    assert "subject" in caught.value.message


def test_the_calendar_is_part_of_the_groups_spec() -> None:
    """Two groups cutting the same instant on two calendars file it under
    different labels, so they are different specs -- and a figure over one
    must not reuse values written under the other."""
    one = compile_world().figure("shop_courier.drops")
    other = compile_source(
        SOURCE.replace("by day in shop_courier.timezone", "by day"), WORLD
    ).figure("shop_courier.drops")
    assert one is not None and other is not None
    assert one.version != other.version
