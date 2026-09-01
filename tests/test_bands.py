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
# Somebody who carries orders, and the allowance written on their record.
fact shop_courier:
    name name
    name as text
    max_orders as number

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

# The same shape as BANDED_MONTH, but the threshold is read straight off the
# courier's record rather than computed. This is the shortcut the release was
# about, on the sequenced figure it was never tried on.
BANDED_MONTH_FIELD = '''
# Deliveries that month against the allowance on the courier's record.
figure shop_courier.drops_vs_allowance bucketed:
    display "{shop_courier} against allowance"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done)

    band:
        when value > shop_courier.max_orders then "over"
        otherwise "ok"
'''

# The unbucketed version, for the invalidation edge.
BANDED_FIELD = '''
# Orders in hand against the allowance on the courier's record.
figure shop_courier.holding:
    display "{value} in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value > shop_courier.max_orders then "over"
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


async def _board(
    extra: str, *, goals=GOALS, hand: float | None = None, allowance: float = 3.0
):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(extra)
    engine = Uratori(schema=BANDS, library=library, store=store, facts=facts)

    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": allowance})
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
    report = await engine.run("t1", written={"goal_change": ["g3"]}, at_ms=AT)

    after = await store.value("t1", plan.name, plan.version, "c1@2026-06")
    assert after.value == before.value, "the metric was recomputed by a goal move"
    assert "shop_courier.drops_vs_goal" not in report.outcome.rebuilt, (
        "the goal move rebuilt the metric. Equal values cannot show this -- a "
        "recompute lands on the same number -- so the pass has to say it did not "
        "run, or the cheap path is only cheap by accident"
    )
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


async def test_a_sequenced_figure_bands_against_a_field_on_the_subjects_record() -> None:
    """A courier has one allowance, not one per month, so the record is
    fetched once and joined by the subject the row is *about* -- a row keyed
    `c1@2026-07` is about `c1`.

    Keyed by the coordinate instead, every lookup missed and every row on
    every sequenced figure banded this way read no word at all. The shortcut
    worked only on the unbucketed figure it was demonstrated on.
    """
    engine, _store, _library, _facts = await _board(BANDED_MONTH_FIELD, allowance=3.0)
    words = await _words(engine, "shop_courier.drops_vs_allowance")
    assert words.get("c1@2026-06") == "ok", (
        f"June's two drops read {words.get('c1@2026-06')!r} against an allowance of 3"
    )
    assert words.get("c1@2026-07") == "over", (
        f"July's four drops read {words.get('c1@2026-07')!r} against an allowance of 3"
    )


async def test_moving_a_field_threshold_re_serves_the_figure_banded_by_it() -> None:
    """The same screen-keeps-lying case as the goal-figure move, on the
    threshold shape that replaced the dial for the common case.

    Nothing about the orders changed, so the stored number does not move and
    the change stream is silent. The pass has to notice that a *band* source
    moved -- and a band source is a record here, not a figure, which is the
    edge the figure-shaped version of this rule did not cover.
    """
    engine, _store, _library, facts = await _board(BANDED_FIELD, allowance=5.0)
    assert (await _words(engine, "shop_courier.holding")).get("c1") == "ok"

    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 1.0})
    report = await engine.run("t1", written={"shop_courier": ["c1"]}, at_ms=AT)

    assert "shop_courier.holding" in _served(report.results), (
        "the allowance on the record moved and the figure banded against it was "
        "not re-served, so every connected board keeps the old word"
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
    served = await engine.answer("t1", "shop_courier.drops", trailing=["3-4"])
    assert isinstance(served, Result)
    [subject] = [s for s in served.subjects if s.id == "c1"]
    assert subject.windows is not None
    [window] = subject.windows
    # Pinned, because the span this reads was wrong and the assertion below
    # could not tell: over July and August it totals 4 against 10 and reads
    # "under" for reasons the docstring does not describe.
    assert (window.frm, window.to) == ("2026-06", "2026-07"), (
        f"the window was not June and July: {window.frm}..{window.to}"
    )
    assert window.total == 6.0, f"the window did not total the two months: {window}"
    assert window.level == "under", (
        f"the window read {window.level!r} totalling 6 drops against 7 of goal"
    )


async def test_a_reading_band_totals_the_goal_over_the_window_and_no_further() -> None:
    """The control for the test above, which the fixture could not provide:
    with every goal inside the window, summing the goal over *every* stored
    bucket and summing it over the window's give the same answer, so the rule
    it names is unasserted.

    Here the window is June alone -- two drops against June's goal of 2, which
    it meets exactly. The goal was set in February and carried forward, so
    seven monthly goal buckets exist totalling 20; summed over all of them
    instead of over the window, two drops read 'under'. One month of value
    against seven months of goal, wrong by the length of the sequence.
    """
    engine, _store, _library, _facts = await _board(BANDED_READING)
    served = await engine.answer("t1", "shop_courier.drops", trailing=["4-4"])
    assert isinstance(served, Result)
    [subject] = [s for s in served.subjects if s.id == "c1"]
    assert subject.windows is not None
    [window] = subject.windows
    assert window.frm == "2026-06" and window.to == "2026-06", (
        f"the window was not June alone: {window.frm}..{window.to}"
    )
    assert window.total == 2.0, f"the window did not total June's drops: {window}"
    assert window.level == "met", (
        f"June read {window.level!r}: two drops meet June's goal of 2 exactly. "
        "Reading 'under' means the goal was totalled over months the window "
        "never covered"
    )


async def test_a_subject_with_no_goal_at_all_gets_no_word() -> None:
    """The absence rule, on the reading path. `sum` of no buckets is 0.0 --
    right for the reading's own value, and catastrophic for the threshold it
    is judged against, because the bottom rung of a band is reliably the
    comfortable one. A courier nobody has ever set a goal for is not meeting
    it."""
    engine, _store, _library, facts = await _board(BANDED_READING)
    facts.put("t1", "shop_courier", "c2", {"name": "Bo", "max_orders": 3.0})
    for key, when in DROPS:
        facts.put(
            "t1",
            "shop_order",
            f"{key}-c2",
            {"ref": key.upper(), "courier_id": "c2", "status": "delivered",
             "delivered_at": when},
        )
    await engine.run("t1", full=True, at_ms=AT)

    served = await engine.answer("t1", "shop_courier.drops", trailing=["2-3"])
    assert isinstance(served, Result)
    words = {
        s.id: (s.windows[0].level if s.windows else None) for s in served.subjects
    }
    assert words.get("c1") == "under", f"the courier with a goal lost their word: {words}"
    assert words.get("c2") == "unknown", (
        f"a courier nobody set a goal for read {words.get('c2')!r}. An absent goal "
        "summed to nought, and every total in the world clears nought"
    )


def test_a_reading_band_reference_must_share_the_unit() -> None:
    """The figure path refuses this and the reading path never did, so a count
    of deliveries could be judged against a share -- or, before the threshold
    unit clause was retired on the argument that the figure carries its own,
    against a duration in seconds, which bands every window comfortable for
    ever."""
    refuses(
        '''
# What share of the month's goal was delivered.
figure shop_courier.goal_share bucketed:
    display "{shop_courier} share"
    unit share

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        count(done) / shop_courier.goal_month:{bucket}

# A count of deliveries judged against a share.
reading shop_courier.crossed(range):
    display "{shop_courier} deliveries"

    band on sum:
        when value < shop_courier.goal_share then "under"
        otherwise "met"

    depends:
        months = shop_courier.drops_month in range

    calculate:
        sum(months)
''',
        "share",
        "count",
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


# ------------------------------------- a projection's `band of` column --


async def _card(engine: Uratori):
    served = await engine.answer("t1", "shop_courier.card")
    assert isinstance(served, Result), "the projection answered nothing"
    return [s.row for s in served.subjects if s.row is not None]


BANDED_CARD = '''
# One row per courier: what they are holding, and the word beside it.
projection shop_courier.card:
    field:
        who = name as text

    read:
        held = shop_courier.holding
        held_band = band of shop_courier.holding
'''


async def test_a_projections_band_column_reads_the_figures_own_thresholds() -> None:
    """`band of F` is the figure's own word, so it is derived from the same
    value against the same thresholds -- and where those thresholds are facts,
    the projection has to resolve them too.

    Every `band of` in the suite banded against a literal, so the whole
    threshold-resolution path on this route was dead code as far as any test
    was concerned: replacing it with an empty map broke nothing.
    """
    engine, _store, _library, facts = await _board(
        BANDED_FIELD + BANDED_CARD, allowance=5.0
    )
    facts.put(
        "t1",
        "shop_order",
        "open1",
        {"ref": "OPEN-1", "courier_id": "c1", "status": "riding",
         "delivered_at": "2026-08-01T10:00:00Z"},
    )
    await engine.run("t1", full=True, at_ms=AT)

    [row] = await _card(engine)
    assert row.values["held"] == 1.0
    assert row.values["held_band"] == "ok", (
        f"one order in hand against an allowance of five read {row.values['held_band']!r}"
    )

    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "max_orders": 0.0})
    await engine.run("t1", written={"shop_courier": ["c1"]}, at_ms=AT)

    [row] = await _card(engine)
    assert row.values["held"] == 1.0, "the figure's own value must not have moved"
    assert row.values["held_band"] == "over", (
        "the allowance dropped to nought and the column beside the unchanged "
        f"number kept saying {row.values['held_band']!r}"
    )


def test_a_band_reference_must_share_the_scope() -> None:
    """Different scopes are different id spaces, so the join is not a near
    miss -- `c1` against `o1` never meets, and every row bands unknown, which
    reads as missing data rather than as a wrong definition."""
    refuses(
        '''
# What each order weighs, so there is a figure over the wrong kind.
figure shop_order.weight:
    display "{shop_order} weight"

    depends:
        mine = shop_order.carried_by:{shop_order}

    calculate:
        count(mine)

# Deliveries judged against a figure about orders.
figure shop_courier.crossed:
    display "x"

    depends:
        mine = shop_order.carried_by:{shop_courier}

    calculate:
        count(mine)

    band:
        when value > shop_order.weight then "over"
        otherwise "ok"
''',
        "shop_order.weight",
        "shop_order",
    )


def test_a_band_reference_must_share_the_dimension() -> None:
    """A figure split across a dimension is keyed `c1@gitlab`, and an
    unsplit one `c1`. The two never meet either."""
    refuses(
        '''
# Deliveries split by status.
figure shop_courier.by_status across shop_order:
    display "x"

    depends:
        mine = shop_order.carried_by:{shop_courier}

    calculate:
        count(mine)

# An unsplit count judged against a split one.
figure shop_courier.mixed:
    display "x"

    depends:
        mine = shop_order.carried_by:{shop_courier}

    calculate:
        count(mine)

    band:
        when value > shop_courier.by_status then "over"
        otherwise "ok"
''',
        "shop_courier.by_status",
    )


# ------------------------------------- a threshold about a span of time --
#
# The retired clause was `band low against <dial> in minutes`, and the unit
# went with it on the argument that "a figure carries its own unit and the
# checker compares the two, so the mistake is unwritable rather than refused".
#
# True of a figure and false of everything else a threshold can now be. A fact
# field is structural by design -- `max_minutes as number` claims a shape and
# never a meaning -- and a literal claims nothing at all. So on the two roads
# the guide actually recommends, a duration in seconds could be judged against
# a number meaning minutes, and be wrong by sixty for ever. That is the exact
# failure the retired clause let an author avoid.

TIMED = '''
# How long this courier's deliveries take, and the line they may not cross.
measure shop_order.ride = delivered_at - delivered_at

# The typical ride, per courier.
figure shop_courier.ride bucketed:
    display "{shop_courier} typical ride"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        mean(shop_order.ride over done)
'''


def test_a_duration_band_says_what_scale_its_number_is_in() -> None:
    """`561600` is six and a half days, and nobody reads it as one. The
    showcase's only duration band was written exactly that way, in a language
    whose stated purpose is that somebody who does not write code can say
    whether a definition is right."""
    refuses(
        TIMED + '''
# Rides against a line nobody can read.
figure shop_courier.ride_band bucketed:
    display "x"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        mean(shop_order.ride over done)

    band:
        when value >= 561600 then "slow"
        otherwise "ok"
''',
        "561600",
        "duration",
    )


async def test_a_duration_band_reads_a_literal_with_its_scale() -> None:
    """And the number is written the way it is said."""
    engine, _store, library, _facts = await _board(
        TIMED + '''
# Rides against six and a half days.
figure shop_courier.ride_band bucketed:
    display "x"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        mean(shop_order.ride over done)

    band:
        when value >= 6.5 days then "slow"
        otherwise "ok"
'''
    )
    plan = library.figure("shop_courier.ride_band")
    assert plan is not None and plan.unit == "duration"
    words = await _words(engine, "shop_courier.ride_band")
    assert set(words.values()) == {"ok"}, (
        f"every ride is instantaneous in this fixture, so nothing is slow: {words}"
    )


def test_a_count_band_may_not_claim_a_scale_it_has_no_use_for() -> None:
    """The rule stays "declare what cannot be derived". A count of deliveries
    is a tally, and `3 days` beside it is a second claim about what the number
    measures, disagreeing with the calculation."""
    refuses(
        BANDED_MONTH.replace("value < shop_courier.goal_month:{bucket}", "value < 3 days"),
        "days",
        "count",
    )


def test_a_duration_band_reading_a_record_says_what_scale_the_field_is_in() -> None:
    """The road the guide recommends for a threshold somebody typed. A fact
    field is structural by design -- `max_minutes as number` claims a shape
    and never a meaning -- so nothing carries the scale but the definition."""
    refuses(
        TIMED + '''
# Rides against whatever is on the courier's record.
figure shop_courier.ride_vs_limit bucketed:
    display "x"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        mean(shop_order.ride over done)

    band:
        when value >= shop_courier.max_orders then "slow"
        otherwise "ok"
''',
        "shop_courier.max_orders",
        "scale",
    )


async def test_a_duration_band_reads_a_record_with_its_scale() -> None:
    engine, _store, _library, _facts = await _board(
        TIMED + '''
# Rides against the courier's own limit, in minutes.
figure shop_courier.ride_vs_limit bucketed:
    display "x"

    depends:
        done = shop_order.dropped_by_month:{shop_courier}

    calculate:
        mean(shop_order.ride over done)

    band:
        when value >= shop_courier.max_orders minutes then "slow"
        otherwise "ok"
''',
        allowance=30.0,
    )
    words = await _words(engine, "shop_courier.ride_vs_limit")
    assert set(words.values()) == {"ok"}, (
        f"an instantaneous ride is under thirty minutes: {words}"
    )
