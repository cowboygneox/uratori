"""A number a definition needs comes from a fact, or from the definition.

Dials were the third source: a value the host set per tenant, outside the
fact stream, which meant a figure could be moved by something no evidence
mentioned. [`test_bands.py`](test_bands.py) took them out of bands, where the
cost was highest -- the word deciding whether a reader should worry. This file
takes them out of the two remaining positions that decide a *number*: a
figure's `calculate`, and a projection's or summary's values and flags.

What replaces them is not a constant. A calculation may name another
**figure** outright, joined by subject (and by coordinate for a sequenced
one), which is the same primitive a band's threshold uses one stratum up. A
projection already had the route -- `read:` binds a figure per row -- so
nothing new is needed there.

A literal stays legal, and that is not a loophole: a number written in the
definition is visible to the reader, hashed into the version, and moving it
forks the version like any other semantic change. What a dial did that a
literal does not is vary invisibly.
"""

from __future__ import annotations

import pytest

from uratori import (
    CheckError,
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    SyntaxError_,
    Uratori,
    compile_source,
)
from uratori.results import Result

DIALLED = Schema(
    kinds=frozenset(),
)

WORLD = '''
# Somebody who carries orders.
fact shop_courier:
    name name
    name as text

# One order, from pickup to doorstep.
fact shop_order:
    name ref
    ref as text
    courier_id as text
    depot_id as text
    status as text
    placed_at as moment

# Where an order is dispatched from, and how long one of its orders may sit
# before it counts as stale. Each depot draws its own line.
fact shop_depot:
    name name
    name as text
    id as text
    stale_days as number

# What a courier is cleared for. One record per courier, moved when
# somebody moves it.
fact shop_limit:
    courier_id as text
    orders as number

group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
group shop_limit.set_for from courier_id
group shop_order.from_depot from depot_id

measure shop_limit.orders_allowed = orders in count

# How many orders this courier is cleared to hold at once.
figure shop_courier.hand_limit:
    display "{shop_courier} may hold {value}"

    depends:
        set = shop_limit.set_for:{shop_courier}

    calculate:
        sum(shop_limit.orders_allowed over set)

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{shop_courier} has {value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)
'''

# The shape the dial used to serve: a number computed against a threshold
# that varies per subject. `depends` gives the population, and the figure
# named outright gives the number to measure it against.
SPARE = '''
# How much room this courier has left before they are at their limit.
figure shop_courier.spare:
    display "{shop_courier} has room for {value} more"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.hand_limit - count(mine)
'''


def compile_world(extra: str = ""):
    return compile_source(WORLD + extra, DIALLED)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_world(extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


async def _board(extra: str, *, allowed: float = 5.0, orders: int = 2):
    facts = MemoryFactStore()
    library = compile_world(extra)
    engine = Uratori(
        schema=DIALLED, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for n in range(orders):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"},
        )
    facts.put("t1", "shop_limit", "l1", {"courier_id": "c1", "orders": allowed})
    await engine.run("t1", full=True)
    return engine, facts


async def _value(engine: Uratori, name: str) -> float | str | None:
    served = await engine.answer("t1", name)
    assert isinstance(served, Result), f"{name} answered nothing"
    [subject] = served.subjects
    return subject.value


# ------------------------------------- a figure named outright in a sum --


async def test_a_calculation_may_name_a_figure_outright() -> None:
    """Cleared for five, holding two, so there is room for three.

    Neither half of that is expressible alone: `depends` gives the population
    and cannot reach a stored value, `combine` gives the stored value and
    cannot count records. Naming the figure is not a third population -- it
    is one number, looked up under this subject's own key.
    """
    engine, _facts = await _board(SPARE)
    assert await _value(engine, "shop_courier.spare") == 3.0


async def test_the_named_figure_is_a_read_the_engine_rebuilds_through() -> None:
    """The difference from a band, and it is the whole reason the two are
    tracked apart: this figure's *stored value* is derived from the limit, so
    moving the limit must recompute it. A band only re-words."""
    engine, facts = await _board(SPARE)
    assert await _value(engine, "shop_courier.spare") == 3.0

    facts.put("t1", "shop_limit", "l1", {"courier_id": "c1", "orders": 9.0})
    await engine.run("t1", written={"shop_limit": ["l1"]})

    assert await _value(engine, "shop_courier.spare") == 7.0, (
        "the limit moved and the figure derived from it kept its old number"
    )


def test_the_named_figure_travels_as_a_read() -> None:
    plan = compile_world(SPARE).figure("shop_courier.spare")
    assert plan is not None
    assert plan.reads == ("shop_courier.hand_limit",), (
        "a figure read by the calculation must be a read, or nothing downstream "
        "knows to rebuild this one when it moves"
    )
    assert plan.depth == 1, "a figure built on another must sort after it"


def test_a_figure_named_outright_must_share_the_scope() -> None:
    refuses(
        '''
# How many orders this depot has out.
figure shop_depot.out:
    display "{shop_depot} has {value} out"

    depends:
        theirs = shop_order.from_depot:{shop_depot} & shop_order.open

    calculate:
        count(theirs)

# A courier's count against a depot's figure.
figure shop_courier.crossed:
    display "{value}"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_depot.out - count(mine)
''',
        "shop_depot",
        "id spaces",
    )


def test_a_figure_may_not_name_itself() -> None:
    """A cycle has no line number, and on a cold build the wrong order stores
    a nought and never revisits it."""
    refuses(
        '''
# Itself.
figure shop_courier.recursive:
    display "{value}"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.recursive - count(mine)
''',
        "shop_courier.recursive",
    )


# ----------------------------------------------------- dials are refused --


def test_a_calculation_may_not_name_a_dial() -> None:
    refuses(
        '''
# Room left, against a dial.
figure shop_courier.dialled:
    display "{value}"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        limits.carrying.over - count(mine)
''',
        "limits.carrying.over",
        "tenant dial",
    )


def test_a_projection_value_may_not_name_a_dial() -> None:
    """A projection already reads figures per row, so the fact route was
    always there -- what was missing was the refusal of the other one."""
    refuses(
        '''
# One row per order.
projection shop_order.board:
    field:
        ref = ref as text

    value:
        busy in count =
            when limits.busy > 1 then 1
            otherwise 0
''',
        "limits.busy",
        "tenant dial",
    )


def test_a_flag_condition_may_not_name_a_dial() -> None:
    refuses(
        '''
# One row per order.
projection shop_order.flagged:
    field:
        ref = ref as text

    value:
        weight in count = 1

    flag heavy when weight >= limits.busy:
        label "Heavy"
        detail "Carrying a lot."
        severity attention
''',
        "limits.busy",
        "tenant dial",
    )


def test_a_summary_may_not_name_a_dial() -> None:
    refuses(
        '''
# One row per order.
projection shop_order.rows:
    field:
        ref = ref as text

    value:
        weight in count = 1

# The book of orders.
summarise shop_order.book over shop_order.rows:
    count heavy where weight >= limits.busy
''',
        "limits.busy",
        "tenant dial",
    )


# ---------------------------------------------------- literals stay legal --


def test_a_literal_threshold_still_compiles() -> None:
    """The control. A number written in the definition is not a control
    outside it: the reader can see it and the version moves when it does."""
    lib = compile_world(
        '''
# Room left, against a number this definition claims.
figure shop_courier.spare_five:
    display "{value}"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        5 - count(mine)
'''
    )
    assert lib.figure("shop_courier.spare_five") is not None


def test_a_literal_and_a_figure_hash_differently() -> None:
    """`5` and a figure that happens to hold 5 are different definitions, and
    a hash that could not tell them apart would let one be swapped for the
    other under a version claiming nothing moved."""
    a = compile_world(SPARE).figure("shop_courier.spare")
    b = compile_world(
        SPARE.replace("shop_courier.hand_limit - count(mine)", "5 - count(mine)")
    ).figure("shop_courier.spare")
    assert a is not None and b is not None
    assert a.version != b.version


# ------------------------------------------------ an age filter's threshold --


AGED = '''
# Orders left sitting longer than the depot they came from allows.
filter shop_order.stale where placed_at older than stale_days from depot_id through shop_depot.id
'''


def test_an_age_filter_reads_its_threshold_off_the_records_owner() -> None:
    """The hardest position to take a dial out of, and the reason it needed
    its own answer: a filter runs over records *before* anything buckets them
    by subject, so there is no subject whose goal figure could be looked up.
    What there is instead is the record's own owner."""
    lib = compile_world(AGED)
    spec = lib.indexes["shop_order.stale"].spec
    assert spec.days is None
    assert spec.read == "stale_days"
    assert spec.local == "depot_id"
    assert spec.through is not None and spec.through.kind == "shop_depot"


def test_an_age_filter_may_not_name_a_dial() -> None:
    with pytest.raises(SyntaxError_) as caught:
        compile_world(
            '''
filter shop_order.dialled where placed_at older than limits.busy
'''
        )
    assert "tenant dial" in str(caught.value) and "owner" in str(caught.value)


def test_a_literal_age_threshold_still_compiles() -> None:
    lib = compile_world("\nfilter shop_order.old where placed_at older than 3 days\n")
    spec = lib.indexes["shop_order.old"].spec
    assert spec.days == 3.0 and spec.through is None


async def test_each_owner_draws_its_own_line() -> None:
    """Two depots, two staleness rules, one filter. Under a dial there was one
    number for the whole tenant and this was not expressible at all."""
    facts = MemoryFactStore()
    library = compile_world(AGED)
    store = MemoryEngineStore()
    engine = Uratori(schema=DIALLED, library=library, store=store, facts=facts)
    facts.put("t1", "shop_depot", "d1", {"name": "North", "id": "d1", "stale_days": 1})
    facts.put("t1", "shop_depot", "d2", {"name": "South", "id": "d2", "stale_days": 30})
    # Both orders were placed the same number of days ago.
    from datetime import UTC, datetime, timedelta

    placed = (datetime.now(tz=UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, depot in (("o1", "d1"), ("o2", "d2")):
        facts.put(
            "t1",
            "shop_order",
            key,
            {"ref": key, "courier_id": "c1", "depot_id": depot, "status": "riding",
             "placed_at": placed},
        )
    await engine.run("t1", full=True)

    held = sorted(await store.members("t1", "shop_order.stale", ""))
    assert held == ["o1"], (
        f"one depot's line is at a day and the other's at a month, and the filter "
        f"held {held}"
    )


async def test_an_order_whose_owner_is_unknown_is_in_no_filter() -> None:
    """Never a default and never the whole population: an owner nobody has
    collected is a staleness rule nobody has stated, and guessing one files
    records under a line the definition never drew."""
    facts = MemoryFactStore()
    library = compile_world(AGED)
    store = MemoryEngineStore()
    engine = Uratori(schema=DIALLED, library=library, store=store, facts=facts)
    from datetime import UTC, datetime, timedelta

    placed = (datetime.now(tz=UTC) - timedelta(days=500)).strftime("%Y-%m-%dT%H:%M:%SZ")
    facts.put(
        "t1",
        "shop_order",
        "o1",
        {"ref": "o1", "courier_id": "c1", "depot_id": "missing", "status": "riding",
         "placed_at": placed},
    )
    await engine.run("t1", full=True)

    assert await store.members("t1", "shop_order.stale", "") == frozenset(), (
        "an order five hundred days old was filed as stale against a threshold "
        "nobody has stated"
    )
