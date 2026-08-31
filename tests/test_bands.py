"""A band's threshold is a fact, never a control outside one.

A band used to compare a number against a **dial** -- a value the host set per
tenant, outside the fact stream, invisible to the evidence that explains every
other number on the screen. So the one part of a card that decides whether a
reader should worry was the one part nothing could cite.

What replaces it is a figure: the goal is computed from records like anything
else, carried forward across the buckets nobody moved it in, and cited the
same way. Two properties follow, and both are tested here:

- **The comparison is per coordinate.** A monthly number is judged against the
  goal in force *that month*, joined by bucket key rather than by position, so
  days compare against days and months against months.
- **An absent goal withholds the word.** A month before anybody set a target
  has no target, and a nought would sit comfortably under every threshold.
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
from uratori.results import BundleResult, Result

BANDS = Schema(
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
    status as text
    delivered_at as moment

# One change to one goal, for one courier. Sparse by nature: a record exists
# only when somebody moved the goal, so a month with no record is a month
# nobody touched it.
fact goal_change:
    courier_id as text
    setting as text
    value as number
    set_at as moment

group shop_order.carried_by from courier_id
group shop_order.dropped_by_month from (courier_id, delivered_at by month)
group shop_order.dropped_by_day from (courier_id, delivered_at by day)
filter shop_order.open where status != "delivered"

group goal_change.by_courier from courier_id
group goal_change.by_month from (courier_id, set_at by month)
group goal_change.by_day from (courier_id, set_at by day)
filter goal_change.drops where setting == "drops"
filter goal_change.hand where setting == "hand"

measure goal_change.amount = value in count

# The monthly delivery goal in force, carried across the months nobody moved
# it. This is the fact a band compares against.
figure shop_courier.goal_month bucketed:
    display "{shop_courier} monthly goal"
    unit count

    depends:
        sets = goal_change.by_month:{shop_courier} & goal_change.drops

    calculate:
        latest(goal_change.value over sets) carried forward

# Deliveries per courier per calendar month.
figure shop_courier.drops_month bucketed:
    display "{shop_courier} deliveries that month"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done)

# The most orders this courier should have in hand at once.
figure shop_courier.hand_limit:
    display "{shop_courier} hand limit"

    depends:
        sets = goal_change.by_courier:{shop_courier} & goal_change.hand

    calculate:
        sum(goal_change.amount over sets)
'''


def compile_world(extra: str = "") -> object:
    return compile_source(WORLD + extra, BANDS)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_world(extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


# The monthly number, banded against the monthly goal at the same coordinate.
BANDED_MONTH = '''
# Deliveries that month against the goal in force that month.
figure shop_courier.drops_vs_goal bucketed:
    display "{shop_courier} against goal"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done)

    band:
        when value < shop_courier.goal_month:{bucket} then "under"
        otherwise "met"
'''

# The count in hand, banded against the courier's own limit.
BANDED_HAND = '''
# Orders in hand against this courier's limit.
figure shop_courier.carrying:
    display "{value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value > shop_courier.hand_limit then "over"
        otherwise "ok"
'''

AT = 1_787_572_800_000.0  # 2026-08-24T12:00Z -- "now" for these tests

# Two orders delivered in June, four in July; the goal is 3 from February and
# 5 from July. So June meets its goal and July misses the raised one -- a
# scenario no single threshold can produce, which is the point.
DROPS = [
    ("d1", "2026-06-04T10:00:00Z"),
    ("d2", "2026-06-19T10:00:00Z"),
    ("d3", "2026-07-02T10:00:00Z"),
    ("d4", "2026-07-09T10:00:00Z"),
    ("d5", "2026-07-16T10:00:00Z"),
    ("d6", "2026-07-23T10:00:00Z"),
]

GOALS = [
    ("g1", "drops", 2.0, "2026-02-10T09:00:00Z"),
    ("g2", "drops", 5.0, "2026-07-01T09:00:00Z"),
]


async def _board(extra: str, *, goals=GOALS, hand: float | None = None):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(extra)
    engine = Uratori(schema=BANDS, library=library, store=store, facts=facts)

    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for key, when in DROPS:
        facts.put(
            "t1",
            "shop_order",
            key,
            {"ref": key.upper(), "courier_id": "c1", "status": "delivered",
             "delivered_at": when},
        )
    for key, setting, value, when in goals:
        facts.put(
            "t1",
            "goal_change",
            key,
            {"courier_id": "c1", "setting": setting, "value": value, "set_at": when},
        )
    if hand is not None:
        facts.put(
            "t1",
            "goal_change",
            "h1",
            {"courier_id": "c1", "setting": "hand", "value": hand,
             "set_at": "2026-02-10T09:00:00Z"},
        )
    await engine.run("t1", full=True, at_ms=AT)
    return engine, store, library, facts


def _served(results: tuple[Result | BundleResult, ...]) -> dict[str, Result]:
    return {r.name: r for r in results if isinstance(r, Result)}


async def _words(engine: Uratori, name: str) -> dict[str, str | None]:
    served = await engine.answer("t1", name)
    assert isinstance(served, Result), f"{name} answered nothing, so no band proves anything"
    return {s.id: s.level for s in served.subjects}


# ------------------------------------------- a figure banded by a figure --


async def test_a_bucketed_figure_bands_against_the_goal_at_the_same_coordinate() -> None:
    """The behaviour the language documented and the engine never had.

    `band: when value < shop_courier.goal_month:{bucket}` compiled, and then
    `_band_operand` met a `Coord`, answered `None`, and the comparison was
    unknown -- so every row on every board rendered *no word at all*, which
    reads as missing data rather than as a broken definition. The old test
    asserted the plan compiled and could not fail on any of that.

    June's two drops meet a goal of 2; July's four miss a goal of 5. One
    definition, two verdicts, because the goal moved between the months.
    """
    engine, _store, _library, _facts = await _board(BANDED_MONTH)
    words = await _words(engine, "shop_courier.drops_vs_goal")
    assert words.get("c1@2026-06") == "met", (
        f"June read {words.get('c1@2026-06')!r} against a goal of 2 it hit exactly"
    )
    assert words.get("c1@2026-07") == "under", (
        f"July read {words.get('c1@2026-07')!r} against the goal of 5 it missed"
    )


async def test_the_goal_is_read_at_each_coordinate_not_once_for_the_subject() -> None:
    """The control for the test above: a single lookup that ignored the
    coordinate would band both months against whichever goal it happened to
    find, so the two months would agree. They must not."""
    engine, _store, _library, _facts = await _board(BANDED_MONTH)
    words = await _words(engine, "shop_courier.drops_vs_goal")
    assert words.get("c1@2026-06") != words.get("c1@2026-07"), (
        "both months got the same word, so the goal was read once rather than "
        "per coordinate"
    )


async def test_a_coordinate_with_no_goal_withholds_the_word() -> None:
    """A month before anybody set a goal has no goal. The bottom rung would
    be the confident wrong answer: a nought sits comfortably under every
    threshold, so the row would read 'met' for a target that did not exist."""
    engine, _store, _library, _facts = await _board(
        BANDED_MONTH, goals=[("g2", "drops", 5.0, "2026-07-01T09:00:00Z")]
    )
    words = await _words(engine, "shop_courier.drops_vs_goal")
    # "unknown" is how a withheld word travels -- the wire says so rather than
    # dropping the field, so a screen can tell "no verdict" from "no row".
    assert words.get("c1@2026-06") == "unknown", (
        f"June read {words.get('c1@2026-06')!r} against a goal nobody had set"
    )
    assert words.get("c1@2026-07") == "under", "the month that has a goal lost its word too"


async def test_an_unbucketed_figure_bands_against_an_unbucketed_figure() -> None:
    """The same rule where there is no sequence: one value per courier judged
    against one limit per courier, joined by subject."""
    engine, _store, _library, _facts = await _board(BANDED_HAND, hand=1.0)
    words = await _words(engine, "shop_courier.carrying")
    assert words.get("c1") == "ok", (
        f"a courier holding no open orders read {words.get('c1')!r} against a limit of 1"
    )


async def test_moving_the_goal_re_bands_without_touching_the_stored_number() -> None:
    """A band is evaluated on read and stored nowhere, and that property has
    to survive the move from dials to facts: pushing a new goal must change
    the word while the metric's own stored value stays byte-identical.

    The old dial equivalent was `test_a_band_dial_move_re_serves...`; this is
    the fact-shaped version of the same guarantee.
    """
    engine, store, library, facts = await _board(BANDED_MONTH)
    plan = library.figure("shop_courier.drops_vs_goal")
    before = await store.value("t1", plan.name, plan.version, "c1@2026-06")
    assert (await _words(engine, "shop_courier.drops_vs_goal")).get("c1@2026-06") == "met"

    facts.put(
        "t1",
        "goal_change",
        "g3",
        {"courier_id": "c1", "setting": "drops", "value": 9.0,
         "set_at": "2026-06-01T09:00:00Z"},
    )
    await engine.run("t1", written={"goal_change": ["g3"]}, at_ms=AT)

    after = await store.value("t1", plan.name, plan.version, "c1@2026-06")
    assert after.value == before.value, "the metric was recomputed by a goal move"
    assert (await _words(engine, "shop_courier.drops_vs_goal")).get("c1@2026-06") == "under", (
        "the raised goal did not re-band the month"
    )


async def test_a_goal_move_re_serves_the_banded_figure() -> None:
    """The screen-keeps-lying case. The stored number did not move, so the
    change stream is silent; unless the pass notices that a figure's *band*
    source moved, every connected board keeps the old word until a reload.

    This is the fact-shaped replacement for
    `test_a_band_dial_move_re_serves_the_banded_figure_and_its_bundles`.
    """
    engine, _store, _library, facts = await _board(BANDED_HAND, hand=5.0)
    await engine.run("t1", at_ms=AT)

    facts.put(
        "t1",
        "goal_change",
        "h2",
        {"courier_id": "c1", "setting": "hand", "value": 1.0,
         "set_at": "2026-06-01T09:00:00Z"},
    )
    report = await engine.run("t1", written={"goal_change": ["h2"]}, at_ms=AT)

    assert "shop_courier.carrying" in _served(report.results), (
        "the limit moved and the figure banded against it was not re-served"
    )


# ------------------------------------------------- what a band may name --


def test_a_band_may_not_name_a_dial() -> None:
    """The whole point of the change: a threshold outside the fact stream is
    the one number on a card nothing can cite."""
    refuses(
        '''
# Orders in hand against a dial.
figure shop_courier.dialled:
    display "{value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value >= limits.carrying.over then "over"
        otherwise "ok"
''',
        "limits.carrying.over",
        "fact",
    )


def test_a_band_reference_must_share_the_grain() -> None:
    """Days against days, months against months. A monthly number judged
    against a daily goal is a comparison of two different populations, and
    the coordinates would never meet -- so every row would band unknown."""
    refuses(
        '''
# The daily goal.
figure shop_courier.goal_day bucketed:
    display "{shop_courier} daily goal"
    unit count

    depends:
        sets = goal_change.by_day:{shop_courier} & goal_change.drops

    calculate:
        latest(goal_change.value over sets) carried forward

# A month judged against a day.
figure shop_courier.mismatched bucketed:
    display "{shop_courier} mismatched"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done)

    band:
        when value < shop_courier.goal_day:{bucket} then "under"
        otherwise "met"
''',
        "month",
        "day",
    )


def test_a_band_reference_must_share_the_unit() -> None:
    """A duration compared against a count is 86,400 times wrong and throws
    nothing: both are numbers by the time the ladder sees them."""
    refuses(
        '''
# A share judged against a count.
figure shop_courier.share_month bucketed:
    display "{shop_courier} share"
    unit share

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done) / 4

    band:
        when value > shop_courier.goal_month:{bucket} then "over"
        otherwise "ok"
''',
        "share",
        "count",
    )


def test_an_unbucketed_figure_may_not_band_against_a_sequenced_one() -> None:
    """There is no coordinate to read the goal at, so the bare spelling would
    have to pick a bucket -- and whichever it picked would be a fabrication."""
    refuses(
        '''
# One value per courier, judged against a sequence.
figure shop_courier.flat:
    display "{value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value < shop_courier.goal_month then "under"
        otherwise "met"
''',
        "shop_courier.goal_month",
    )


# --------------------------------------------------- a reading's band --


BANDED_READING = '''
# Deliveries over the window, against the goal over the same window.
reading shop_courier.drops(range):
    display "{shop_courier} deliveries"

    band on sum:
        when value < shop_courier.goal_month then "under"
        otherwise "met"

    depends:
        months = shop_courier.drops_month in range

    calculate:
        sum(months)
'''


def test_a_reading_bands_with_a_ladder_rather_than_a_dial() -> None:
    lib = compile_world(BANDED_READING)
    plan = lib.reading("shop_courier.drops")
    assert plan is not None
    assert plan.band is not None, "the reading's ladder did not reach the plan"
    assert plan.band_on == "sum"
    assert plan.band_reads == ("shop_courier.goal_month",)


def test_the_old_dial_clause_is_refused_with_directions() -> None:
    """`band low against <dial>` compiled for the whole life of the language.
    Left accepted it would be a second banding path, and the one that cannot
    be cited."""
    with pytest.raises(SyntaxError_) as caught:
        compile_world(
            '''
# Deliveries over the window.
reading shop_courier.old(range):
    display "{shop_courier} deliveries"
    band low against limits.drops

    depends:
        months = shop_courier.drops_month in range

    calculate:
        sum(months)
'''
        )
    message = str(caught.value)
    assert "ladder" in message and "figure" in message, (
        f"the refusal must carry the rewrite, not just a syntax complaint: {message!r}"
    )


async def test_a_reading_band_reads_the_goal_over_the_same_window_and_statistic() -> None:
    """`on sum` bands the window's total, so the goal is totalled over the
    identical buckets -- six drops in June and July against goals of 2 and 5,
    which is 6 against 7 and therefore under.

    The tempting alternative is to compare the two-month total against a
    single month's goal, which reads 'met' and is the plausible wrong number
    this rule exists to refuse.
    """
    engine, _store, _library, _facts = await _board(BANDED_READING)
    served = await engine.answer("t1", "shop_courier.drops", trailing=["2-3"])
    assert isinstance(served, Result)
    [subject] = [s for s in served.subjects if s.id == "c1"]
    assert subject.windows is not None
    [window] = subject.windows
    assert window.level == "under", (
        f"the window read {window.level!r} totalling 6 drops against 7 of goal"
    )


def test_a_reading_band_reference_must_share_the_source_grain() -> None:
    refuses(
        '''
# The daily goal.
figure shop_courier.goal_day bucketed:
    display "{shop_courier} daily goal"
    unit count

    depends:
        sets = goal_change.by_day:{shop_courier} & goal_change.drops

    calculate:
        latest(goal_change.value over sets) carried forward

# A month window judged against a daily goal.
reading shop_courier.mismatched(range):
    display "{shop_courier} deliveries"

    band on sum:
        when value < shop_courier.goal_day then "under"
        otherwise "met"

    depends:
        months = shop_courier.drops_month in range

    calculate:
        sum(months)
''',
        "month",
        "day",
    )


def test_a_reading_band_must_name_a_statistic_it_calculates() -> None:
    """Unchanged from the dial era, and it has to survive the rewrite: a band
    over a statistic the reading does not compute colours nothing, and a
    permanently wordless row reads as missing data."""
    refuses(
        '''
# Deliveries over the window.
reading shop_courier.unbanded(range):
    display "{shop_courier} deliveries"

    band on mean:
        when value < shop_courier.goal_month then "under"
        otherwise "met"

    depends:
        months = shop_courier.drops_month in range

    calculate:
        sum(months)
''',
        "mean",
    )
