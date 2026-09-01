"""Reading a number off the subject's own record.

The complaint this answers: using a fact was four times harder than using the
dial it replaced. To compare a count against a limit sitting on the courier's
record you had to write a group pairing a record with itself, a measure
renaming one field, and a figure summing a set of one -- three declarations
of pure ceremony before the band could name anything.

Meanwhile a group had been reading the same record with no ceremony at all
(`by day in shop_courier.timezone`). The mechanism existed; nothing else
could reach it.

So `<subject kind>.<field>` is an expression leaf now, in a band rung and in
a calculation, resolved against the record the value is *about*. A figure is
still the route for a threshold that is computed, or that has history -- but
it is no longer the only route, and it is no longer the route for the common
case, which is a number somebody typed onto a record.
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
# Somebody who carries orders, and how many they are cleared for.
fact shop_courier:
    name name
    name as text
    max_orders as number

# One order.
fact shop_order:
    name ref
    ref as text
    courier_id as text
    status as text
    weight as number
    delivered_at as moment
    many parcels:
        grams as number

group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

# Orders in hand right now, against this courier's own limit.
figure shop_courier.carrying:
    display "{shop_courier} has {value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value > shop_courier.max_orders then "over"
        otherwise "ok"
'''

# The other half: a subject field in the calculation, not just the band.
# `depends` gives the population and cannot reach the record; this is the
# number the population is measured against.
ROOM = '''
# How much room this courier has left.
figure shop_courier.room:
    display "{shop_courier} has room for {value} more"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.max_orders - count(mine)
'''


def compile_world(extra: str = ""):
    return compile_source(SOURCE + extra, WORLD)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_world(extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


async def _board(extra: str = "", *, allowed: float = 3.0, orders: int = 2):
    facts = MemoryFactStore()
    library = compile_world(extra)
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": allowed})
    for n in range(orders):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"},
        )
    await engine.run("t1", full=True)
    return engine, facts


async def _one(engine: Uratori, name: str) -> Result:
    served = await engine.answer("t1", name)
    assert isinstance(served, Result), f"{name} answered nothing"
    return served


# ------------------------------------------------- the whole of the feature --


async def test_a_band_compares_against_a_field_on_the_subjects_record() -> None:
    """Two orders against a limit of three is fine; against a limit of one it
    is not. Nothing is declared for the threshold at all -- the number is on
    the courier, where somebody typed it."""
    engine, _facts = await _board(allowed=3.0)
    [under] = (await _one(engine, "shop_courier.carrying")).subjects
    assert under.level == "ok", f"two orders against a limit of three read {under.level!r}"

    engine, _facts = await _board(allowed=1.0)
    [over] = (await _one(engine, "shop_courier.carrying")).subjects
    assert over.level == "over", f"two orders against a limit of one read {over.level!r}"


async def test_the_threshold_costs_no_declarations() -> None:
    """The point of the change, pinned as a fact about the source: the whole
    world above is a group, a filter and the figure itself. A `measure`, a
    self-pairing `group` and a second `figure` were the price of this before.
    """
    lib = compile_world()
    assert [m for m in lib.measures] == [], "the threshold needed a measure"
    assert sorted(lib.indexes) == ["shop_order.carried_by", "shop_order.open"], (
        f"the threshold needed a grouping of its own: {sorted(lib.indexes)}"
    )
    assert [f.name for f in lib.figures] == ["shop_courier.carrying"], (
        "the threshold needed a figure of its own"
    )


async def test_a_calculation_may_read_the_subjects_field_too() -> None:
    """`depends` gives a population and cannot reach the record; the record
    holds the number the population is measured against. Cleared for three,
    holding two, so there is room for one."""
    engine, _facts = await _board(ROOM, allowed=3.0)
    [row] = (await _one(engine, "shop_courier.room")).subjects
    assert row.value == 1.0, f"room read {row.value}"


async def test_moving_the_record_moves_the_number() -> None:
    """A stored value derived from a field on the subject's record has to
    recompute when that record moves -- the change is on the *courier*, and
    nothing about the orders it counts has moved at all."""
    engine, facts = await _board(ROOM, allowed=3.0)
    assert (await _one(engine, "shop_courier.room")).subjects[0].value == 1.0

    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 9.0})
    await engine.run("t1", written={"shop_courier": ["c1"]})

    assert (await _one(engine, "shop_courier.room")).subjects[0].value == 7.0, (
        "the courier's limit moved and the figure derived from it did not"
    )


async def test_a_record_with_no_value_there_withholds_the_word() -> None:
    """The absence rule, unchanged by the shortcut: a courier nobody has set
    a limit for has no limit, and a nought would sit comfortably under every
    comparison."""
    facts = MemoryFactStore()
    library = compile_world()
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    facts.put(
        "t1", "shop_order", "o0", {"ref": "A-0", "courier_id": "c1", "status": "riding"}
    )
    await engine.run("t1", full=True)

    [row] = (await _one(engine, "shop_courier.carrying")).subjects
    assert row.value == 1.0, "the count itself must still answer"
    assert row.level == "unknown", (
        f"a courier with no limit was banded {row.level!r} against one"
    )


# ------------------------------------------------------------ what is refused --


SEQUENCED_ROOM = '''
# Orders by courier and day.
group shop_order.dropped_by_day from (courier_id, delivered_at by day)

# How much room this courier had left that day.
figure shop_courier.room_that_day bucketed:
    display "{shop_courier} had room for {value} more"
    unit count

    depends:
        done = shop_order.dropped_by_day:{shop_courier}

    calculate:
        shop_courier.max_orders - count(done)
'''


async def test_a_write_to_the_record_recomputes_the_buckets_and_invents_none() -> None:
    """The subject's own record is not a *member* of the grouping that fans
    the figure out, so a write to it has to reach the figure by subject rather
    than by bucket. On a sequenced figure that subject is a coordinate --
    `c1@2026-06-25` -- and the bare key is not a row it has.

    Touched anyway, the bare key was evaluated, found no bucket, and stored
    the arithmetic over an empty set: a row saying the courier had room for
    nine more on no day at all, pushed on the change stream, served with no
    dimension, and surviving every later warm pass.
    """
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(SEQUENCED_ROOM)
    engine = Uratori(
        schema=WORLD, library=library, store=store, facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 5.0})
    facts.put(
        "t1",
        "shop_order",
        "o1",
        {"ref": "A-1", "courier_id": "c1", "status": "riding",
         "delivered_at": "2026-06-25T10:00:00Z"},
    )
    await engine.run("t1", full=True)
    plan = library.figure("shop_courier.room_that_day")

    async def rows() -> dict[str, object]:
        return {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}

    assert await rows() == {"c1@2026-06-25": 4.0}

    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 9.0})
    report = await engine.run("t1", written={"shop_courier": ["c1"]})

    assert await rows() == {"c1@2026-06-25": 8.0}, (
        f"the allowance moved and the day did not follow it, or a row was "
        f"invented for no day: {await rows()}"
    )
    assert [c.subject for c in report.outcome.changes] == ["c1@2026-06-25"], (
        f"a change was reported about a subject with no bucket: {report.outcome.changes}"
    )


def test_a_field_of_another_kind_is_not_the_subjects() -> None:
    """The record is the one the value is *about*. Naming another kind would
    have to pick a record of it, and there is no key to pick by."""
    refuses(
        '''
# Against an order's field.
figure shop_courier.crossed:
    display "x"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_order.weight - count(mine)
''',
        "is not what this value is about",
    )


def test_a_field_that_does_not_exist_is_refused() -> None:
    refuses(
        '''
# Against a field nobody declared.
figure shop_courier.typo:
    display "x"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.max_order - count(mine)
''',
        "max_order",
    )


def test_a_word_field_is_refused_where_a_number_is_wanted() -> None:
    refuses(
        '''
# Against a name.
figure shop_courier.worded:
    display "x"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.name - count(mine)
''',
        "text",
    )


def test_a_figure_may_not_take_a_name_a_field_of_its_scope_already_has() -> None:
    """Both are `<kind>.<name>`, so one spelling would answer two things and
    a reader could not tell which. Refused where it is created rather than
    where it is read."""
    refuses(
        '''
# A figure wearing a field's name.
figure shop_courier.max_orders:
    display "x"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)
''',
        "max_orders",
        "field",
    )


def test_the_field_read_is_part_of_the_version() -> None:
    """Two figures banding against two different fields of one record are two
    definitions, and a version that could not tell them apart would let one
    become the other under a hash claiming nothing moved."""
    a = compile_world().figure("shop_courier.carrying")
    b = compile_source(
        SOURCE.replace("max_orders as number", "max_orders as number\n    spare as number")
        .replace("value > shop_courier.max_orders", "value > shop_courier.spare"),
        WORLD,
    ).figure("shop_courier.carrying")
    assert a is not None and b is not None
    assert a.version != b.version


# ------------------------------------------- a field summed without a measure --
#
# `latest(kind.field over set)` has always worked with no measure in the way,
# on the argument that a measure would be a second name for one field. `sum`
# demanded one anyway. The asymmetry had no reason: only `latest` needs an
# ordering, which is why only `latest` is confined to a bucketed figure.
#
# A `measure` is still the right thing where a quantity is genuinely computed
# (a duration between two moments, an instant, a wait against the clock) or
# where one meaning is shared by several figures. It is no longer the toll on
# reading a number that is simply written down.

TOTALLED = '''
# What this courier is carrying, in kilos.
figure shop_courier.load:
    display "{shop_courier} carrying {value}"
    unit count

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        sum(shop_order.weight over mine)
'''


async def test_a_field_is_summed_over_a_set_with_no_measure_declared() -> None:
    facts = MemoryFactStore()
    library = compile_world(TOTALLED)
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 3})
    for n, weight in enumerate((2.0, 5.0)):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding", "weight": weight},
        )
    await engine.run("t1", full=True)

    served = await engine.answer("t1", "shop_courier.load")
    assert isinstance(served, Result)
    assert served.subjects[0].value == 7.0
    assert [m for m in library.measures] == [], "the total needed a measure"


def test_a_summed_field_must_declare_the_figures_unit() -> None:
    """The unit rule in its usual form: a field read says a record carries a
    number and nothing about what the number is, so nothing can derive it."""
    refuses(
        TOTALLED.replace("    unit count\n", ""),
        "unit",
    )


def test_a_summed_field_must_belong_to_the_sets_kind() -> None:
    refuses(
        TOTALLED.replace("shop_order.weight", "shop_courier.max_orders"),
        "shop_courier",
    )


def test_a_summed_field_must_be_a_number() -> None:
    """`sum` was given the same shortcut `latest` had and none of its guards.
    A text field reaches `read_number`, which answers None for every record,
    and the total of nothing at all is 0.0 -- a confident nought about a
    column of words, on every subject, for ever."""
    refuses(
        TOTALLED.replace("shop_order.weight", "shop_order.status"),
        "status",
        "text",
    )


def test_a_summed_field_may_not_cross_a_list() -> None:
    """The failure is worse here than under `latest`, because it is quiet
    rather than total: a record holding two parcels contributes nothing while
    a record holding one contributes normally, so the total is a real-looking
    number computed over the subset of records that happened to hold exactly
    one. The evidence cites only those, and reads as consistent."""
    refuses(
        TOTALLED.replace("shop_order.weight", "shop_order.parcels.grams"),
        "parcels.grams",
        "list",
    )


def test_a_measure_may_not_take_a_name_a_field_of_its_kind_already_has() -> None:
    """A measure wins where both could match, so declaring one silently
    changes what an already-written `sum(kind.field over set)` computes --
    the exact thing the resolution order was documented as preventing. The
    figure rule refuses the mirror-image collision; this is the same rule
    from the measure's side."""
    refuses(
        '''
# The surcharge, under a name an order's own field already has.
measure shop_order.weight = shop_order.status in count
''',
        "shop_order.weight",
        "already has as a field",
    )


def test_a_bare_subject_field_names_the_unit_it_cannot_derive() -> None:
    """Every other field read is on the must-declare list because a record
    says a number is there and nothing about what it measures. Reading one
    bare was left off, so a budget in seconds is silently a count and prints
    as 144000."""
    refuses(
        '''
# The budget on the courier's own record, read straight off it.
figure shop_courier.budget:
    display "{shop_courier} budget"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        shop_courier.max_orders
''',
        "unit",
    )


def test_a_field_read_names_the_field_once() -> None:
    """The refusal is what a definition's author reads to find their typo, so
    a doubled clause in the middle of it is a real cost: `reads
    shop_courier.max_order reads "max_order"` reads as two different names."""
    message = refuses(
        TOTALLED.replace("shop_order.weight", "shop_order.nope"),
        "nope",
    )
    assert message.count("reads") <= 1, message


def test_latest_still_needs_a_sequence_because_it_needs_an_ordering() -> None:
    """The restriction that survives, and the reason it does: `latest` has to
    know which record came last, and that ordering is the group's own time
    part. A sum needs no ordering, which is why it never needed the rule."""
    refuses(
        TOTALLED.replace("sum(shop_order.weight over mine)", "latest(shop_order.weight over mine)"),
        "sequence of buckets",
    )


# --------------------------------------------- combine, shrunk to its one job --


def test_a_plain_combine_is_refused_with_the_calculation_it_becomes() -> None:
    """`combine` had two jobs and only one of them was its own. Binding a
    figure's value was an alias for a name, declared above the single line
    that used it; a calculation names the figure. What is left is the rollup,
    which is a genuinely different operation."""
    refuses(
        '''
# The same count, via a binding.
figure shop_courier.aliased:
    display "x"
    unit count

    combine:
        held = shop_courier.carrying

    calculate:
        held - 1
''',
        "combine adds up the parts",
        "name it in the calculation",
    )


def test_a_figure_reading_another_needs_no_group_of_its_own() -> None:
    """Its subjects come from the figure it reads, exactly as a rollup's
    always have -- so dropping the block must not cost it its roster."""
    lib = compile_world(
        '''
# Whether this courier is over.
figure shop_courier.state:
    display "x"

    calculate:
        when shop_courier.carrying > shop_courier.max_orders then "over"
        otherwise "ok"
'''
    )
    plan = lib.figure("shop_courier.state")
    assert plan is not None
    assert plan.scope_index is None, "a figure over a figure has no group of its own"
    assert plan.reads == ("shop_courier.carrying",)


# ------------------------------------------------------- the docs' own example --


def test_the_language_guides_first_example_compiles_as_written() -> None:
    """The first thing a reader meets, held to the compiler.

    A guide's opening example is the one piece of source most people will ever
    copy, and it is the piece most likely to rot: it is prose to everybody
    editing the language and code to everybody reading it. This is the cheapest
    possible guard -- the block is lifted from the file and compiled.
    """
    import pathlib

    guide = pathlib.Path(__file__).parent.parent / "docs" / "language.md"
    # The third fenced block: the two before it are the records, as JSON.
    block = guide.read_text().split("```", 3)[3].split("```")[0]
    lib = compile_source(block, Schema(kinds=frozenset()))
    assert [f.name for f in lib.figures] == ["shop_courier.carrying"]
    plan = lib.figure("shop_courier.carrying")
    assert plan is not None and plan.band is not None
