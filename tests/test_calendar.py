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
    assert "c1@2026-06-25" not in rows, (
        "the old day survived the move, so the same delivery is filed twice. "
        "A nought is not acceptable here either: a day with nothing in it is "
        f"absent, never a measured none-of-it. {rows}"
    )


async def test_a_corrected_timestamp_leaves_no_measured_nought_behind() -> None:
    """The headline case for a moved bucket, on the warm path a host actually
    uses. An order's `delivered_at` is corrected from the 25th to the 27th, so
    the 25th has nothing in it any more.

    The row for the 25th was recomputed rather than removed, and `count()` of
    nothing is the scalar `0.0` -- so the day held a *measured nought*. Only
    an empty list was treated as an emptied bucket, and a count never produces
    one. The sweep that would have caught it runs on a full pass, and a
    corrected timestamp arrives as an ordinary write.

    Every reader believes the row: the figure is stored, versioned and
    current. A window counts the day as covered, which drags the mean down and
    overstates how much of the range was measured, and the change stream
    pushes 1 -> 0 to every screen.
    """
    engine, store, library, facts = await _board(
        {"c1": "Europe/London"},
        {"o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT}},
    )
    assert await _rows(store, library) == {"c1@2026-06-25": 1.0}

    facts.put(
        "t1",
        "shop_order",
        "o1",
        {"ref": "O-1", "courier_id": "c1", "delivered_at": "2026-06-27T14:00:00Z"},
    )
    await engine.run("t1", written={"shop_order": ["o1"]})

    rows = await _rows(store, library)
    assert rows == {"c1@2026-06-27": 1.0}, (
        f"the day the order left is still reporting a number: {rows}"
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


async def test_a_courier_whose_calendar_is_not_a_calendar_is_in_no_bucket() -> None:
    """`"PST"` is not a zone name, and neither is a typo, an empty template or
    whatever a provider put in that column. As a dial this was one value with
    one write door to validate at; as a fact it is uncontrolled data on every
    record of a roster kind, and `timezone as text` accepts anything.

    Unguarded, the lookup raised out of the bucketing and took the *whole
    tenant's pass* with it -- every figure for everybody, on the strength of
    one bad string on one record. The design already has the right answer for
    a subject whose calendar it does not know: they are in no bucket. An
    unusable value is not knowing it.
    """
    _engine, store, library, _facts = await _board(
        {"c1": "Europe/London", "c2": "PST"},
        {
            "o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT},
            "o2": {"ref": "O-2", "courier_id": "c2", "delivered_at": ACROSS_MIDNIGHT},
        },
    )
    rows = await _rows(store, library)
    assert rows == {"c1@2026-06-25": 1.0}, (
        f"one unusable zone string took the whole board's pass with it: {rows}"
    )


async def test_an_unusable_calendar_on_a_courier_with_nothing_still_serves() -> None:
    """The same value reaches the serving path by a different route: the
    windows to resolve are drawn from every distinct zone in the tenant,
    whether or not any stored row was cut on one. So a courier with a typo and
    no orders at all made the reading endpoint raise for *everybody*."""
    engine, _store, _library, _facts = await _board(
        {"c1": "Europe/London", "c2": "Not/AZone"},
        {"o1": {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT}},
    )
    served = await engine.answer("t1", "shop_courier.recent", trailing=[7], at="2026-06-26")
    assert isinstance(served, Result)
    assert [s.id for s in served.subjects] == ["c1"]


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


def test_the_calendar_must_be_the_subjects_kind_without_a_hop_too() -> None:
    """The hop was the only shape checked, and it is the rarer one. With a
    plain key field -- which is how nearly every group is written -- any kind
    at all was accepted, its records keyed by ids belonging to somebody else,
    and every lookup missed. The shipped NFL example did exactly this and its
    figure had been serving nothing.

    The group alone cannot always tell: `courier_id` says nothing about which
    kind its values key into. The figure does, because it names the scope the
    group fans out by, so that is where the two are compared.
    """
    with pytest.raises(CheckError) as caught:
        compile_world(
            '''
# Orders by courier, cut on somebody else's calendar.
group shop_order.by_wrong_day from (courier_id, delivered_at by day in shop_order.ref)

# Deliveries a day, against a calendar no courier carries.
figure shop_courier.wrong bucketed:
    display "x"

    depends:
        done = shop_order.by_wrong_day:{shop_courier}

    calculate:
        count(done)
'''
        )
    assert "shop_courier" in caught.value.message
    assert "shop_order.ref" in caught.value.message


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


# --------------------------------------------- one calendar, written down --
#
# A board where everybody shares a calendar is the common case, and reaching
# it through a field meant copying one value onto every record of a roster
# kind. The showcase did exactly that -- thirty-two franchises each carrying
# `"America/New_York"`, with a comment saying the league keeps one clock -- so
# a team missing it fell into no bucket and a typo on one of them was a
# one-record way to lose a figure.
#
# The argument for keeping a literal threshold applies to a calendar word for
# word: a value written in the definition is not a control outside it. The
# reader can see it, and moving it forks the version.

LEAGUE = SOURCE.replace(
    "delivered_at by day in shop_courier.timezone",
    'delivered_at by day in "Pacific/Auckland"',
)


async def test_a_calendar_may_be_written_in_the_definition() -> None:
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_source(LEAGUE, WORLD)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    # No `timezone` on either record, and it is not needed.
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "timezone": ""})
    facts.put("t1", "shop_courier", "c2", {"name": "Bo", "timezone": ""})
    for key, courier in (("o1", "c1"), ("o2", "c2")):
        facts.put(
            "t1",
            "shop_order",
            key,
            {"ref": key.upper(), "courier_id": courier, "delivered_at": ACROSS_MIDNIGHT},
        )
    await engine.run("t1", full=True)

    rows = await _rows(store, library)
    assert rows == {"c1@2026-06-26": 1.0, "c2@2026-06-26": 1.0}, (
        f"14:00Z on the 25th is already the 26th in Auckland, for everybody: {rows}"
    )


async def test_a_written_calendar_is_the_answer_a_window_reports() -> None:
    """One calendar for the board, so the response says it once rather than
    each row carrying the same word."""
    facts = MemoryFactStore()
    library = compile_source(LEAGUE, WORLD)
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "timezone": ""})
    facts.put(
        "t1",
        "shop_order",
        "o1",
        {"ref": "O-1", "courier_id": "c1", "delivered_at": ACROSS_MIDNIGHT},
    )
    await engine.run("t1", full=True)

    served = await engine.answer("t1", "shop_courier.recent", trailing=[7], at="2026-06-26")
    assert isinstance(served, Result)
    assert served.zone == "Pacific/Auckland", (
        f"the board's one calendar was not reported at the top: {served.zone}"
    )
    [subject] = [s for s in served.subjects if s.id == "c1"]
    assert subject.windows is not None
    assert subject.windows[0].zone == "Pacific/Auckland", (
        "the heading said Auckland and the window's own bounds were cut "
        f"somewhere else: {subject.windows[0].zone}"
    )
    assert subject.windows[0].to == "2026-06-26"


def test_a_written_calendar_must_name_a_real_one() -> None:
    """Written in the definition, so it is refused at compile time rather than
    treated as absent -- unlike a record's, where an unusable value is a fact
    about one subject and the board goes on."""
    with pytest.raises(CheckError) as caught:
        compile_source(
            SOURCE.replace(
                "delivered_at by day in shop_courier.timezone",
                'delivered_at by day in "PST"',
            ),
            WORLD,
        )
    assert "PST" in caught.value.message


def test_a_written_calendar_is_part_of_the_version() -> None:
    one = compile_source(LEAGUE, WORLD).figure("shop_courier.drops")
    other = compile_source(
        LEAGUE.replace('"Pacific/Auckland"', '"Europe/London"'), WORLD
    ).figure("shop_courier.drops")
    assert one is not None and other is not None
    assert one.version != other.version, (
        "moving the calendar changed which day every record is filed under and "
        "the version did not move with it"
    )
