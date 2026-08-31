"""What a pass pushes: the impacted answers, bundles included, dials included.

Two behaviours are pinned here, both of them "the screen keeps lying until a
reload" bugs when they regress:

**A bundle is impacted exactly when a member is.** A tile that never re-serves
freezes; a tile that re-serves on every pass makes every card flicker whether
or not anything on it moved. So the union is tested from all three directions
a member can move -- a figure touched, a reading whose source figure was
touched, a projection reached -- and the negative is tested beside each: a
bundle containing none of the moved members stays quiet.

**A dial move re-serves what renders under the dial.** A band threshold and a
reading's band dial move no stored value, so the change stream says nothing --
the serve stamps are what notice, and what they notice must be pushed. The
control is a dial nothing names: it must move nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from uratori import (
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    Uratori,
)
from uratori import compile_source as compile_against
from uratori.results import BundleResult, Result

WORLD = Schema(
    kinds=frozenset({"shop_order", "shop_courier", "shop_review", "shop_limit"}),
    name_fields={"shop_courier": "name", "shop_order": "ref", "shop_review": "ref"},
    bucket_settings=("tenant.timezone",),
    figure_settings=("limits.spare",),
    defaults={
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {"spare": 1},
    },
)

SOURCE = """
group shop_order.carried_by from courier_id
group shop_limit.set_for from courier_id
group shop_limit.set_by_day from (courier_id, set_at by day)
filter shop_order.open where status != "delivered"
group shop_order.delivered_by_day from (courier_id, delivered_at by day)
group shop_review.rated_by from courier_id

measure shop_order.riding_seconds = delivered_at - picked_up_at
measure shop_limit.orders = orders in count
measure shop_order.lug_seconds = lug_seconds in effort

# How many orders this courier is cleared to hold at once.
figure shop_courier.hand_limit:
    display "{value} allowed"
    depends:
        set = shop_limit.set_for:{shop_courier}
    calculate:
        sum(shop_limit.orders over set)

# How long a ride this courier's round is meant to take, as last set --
# carried across the days nobody changed it, so every day has a budget.
figure shop_courier.ride_budget bucketed:
    display "{value} budget"
    unit duration
    depends:
        set = shop_limit.set_by_day:{shop_courier}
    calculate:
        latest(shop_limit.seconds over set) carried forward

# Orders in hand right now.
figure shop_courier.carrying:
    display "{value} in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)
    band:
        when value >= shop_courier.hand_limit then "over"
        otherwise "ok"

# Every delivery's ride time, day by day.
figure shop_courier.ride_times bucketed:
    display "{shop_courier} rides"
    depends:
        done = shop_order.delivered_by_day:{shop_courier}
    calculate:
        list(shop_order.riding_seconds over done)

# The typical ride, over a window.
reading shop_courier.typical_ride(range):
    display "{value}"
    band:
        when value >= shop_courier.ride_budget then "over"
        otherwise "ok"
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)

# How many reviews this courier has collected.
figure shop_courier.reviews:
    display "{value} reviews"
    depends:
        mine = shop_review.rated_by:{shop_courier}
    calculate:
        count(mine)

# Working time in hand right now.
figure shop_courier.lugging:
    display "{value} in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        sum(shop_order.lug_seconds over mine)

# One row per courier, with their working time in hand.
projection shop_courier.desk:
    field:
        name = name as text

    read:
        lugging = shop_courier.lugging

# One row per order, alphabetically.
projection shop_order.board:
    sort by ref ascending

    field:
        ref = ref as text

# The whole book of orders, one row.
summarise shop_order.book over shop_order.board:
    count orders

# The courier tile: a banded count and a windowed reading.
bundle shop_courier.card:
    typical = reading shop_courier.typical_ride over 9, 31-60
    carrying = figure shop_courier.carrying

# The reviews tile: one figure nothing else here touches.
bundle shop_courier.reviews_card:
    reviews = figure shop_courier.reviews

# The board tile: rows and their headline.
bundle shop_order.board_card:
    board = projection shop_order.board
    book = summarise shop_order.book

# Orders being ridden right now, against the clock.
reading shop_courier.riding_now():
    display "{value}"
    depends:
        w = shop_order.riding_seconds over (shop_order.carried_by:{shop_courier} & shop_order.open)
    calculate:
        count(w)

# The live tile: a member that is not servable yet.
bundle shop_courier.live_card:
    now = reading shop_courier.riding_now
"""


def _engine() -> tuple[Uratori, MemoryFactStore]:
    library = compile_against(SOURCE, WORLD)
    facts = MemoryFactStore()
    engine = Uratori(
        schema=WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    return engine, facts


def _feed(facts: MemoryFactStore) -> None:
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for n in range(2):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"},
        )
    facts.put("t1", "shop_review", "v1", {"ref": "V-1", "courier_id": "c1"})

    def iso(at: datetime) -> str:
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")

    now = datetime.now(tz=UTC)
    facts.put(
        "t1",
        "shop_limit",
        "l1",
        {
            "courier_id": "c1",
            "orders": 3,
            "seconds": 7200,
            "set_at": iso(now - timedelta(days=2)),
        },
    )
    facts.put(
        "t1",
        "shop_order",
        "r1",
        {
            "ref": "R-1",
            "courier_id": "c1",
            "status": "delivered",
            "picked_up_at": iso(now - timedelta(hours=2)),
            "delivered_at": iso(now - timedelta(hours=1)),
        },
    )


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bundles(results: tuple[Result | BundleResult, ...]) -> dict[str, BundleResult]:
    return {r.name: r for r in results if isinstance(r, BundleResult)}


def _results(results: tuple[Result | BundleResult, ...]) -> dict[str, Result]:
    return {r.name: r for r in results if isinstance(r, Result)}


async def _settled(engine: Uratori, facts: MemoryFactStore) -> None:
    """A fed, fully built board whose serve stamps have settled -- the state
    every quiet-side assertion below has to start from, or the first pass
    would re-serve the world for the honest reason that nothing was stamped
    yet."""
    _feed(facts)
    await engine.run("t1", full=True)
    await engine.run("t1")


# ------------------------------------------------------- bundle impact --


async def test_a_pass_pushes_the_bundles_a_touched_figure_sits_in_and_no_other() -> None:
    """The precision half is the assertion that matters: `reviews_card`
    contains nothing this pass touched, and a hub that pushed it anyway is a
    hub that re-serves every tile on every pass -- the flicker that makes a
    live board unwatchable and the push surface unreviewable."""
    engine, facts = _engine()
    await _settled(engine, facts)

    facts.put(
        "t1", "shop_order", "o9", {"ref": "A-9", "courier_id": "c1", "status": "riding"}
    )
    report = await engine.run("t1", written={"shop_order": ["o9"]})

    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles, (
        "carrying moved and the tile containing it was not re-served"
    )
    assert "shop_courier.reviews_card" not in bundles, (
        "a bundle containing nothing this pass touched was pushed"
    )
    # A pass through the facts door is the sync moment: every projection
    # re-serves on it (the clock is one of its inputs), so the board tile
    # follows its projection member.
    assert "shop_order.board_card" in bundles

    assert "shop_courier.card" in report.moved
    assert "shop_courier.reviews_card" not in report.moved
    assert "shop_courier.reviews" not in report.moved


async def test_a_touched_source_reaches_the_bundle_through_its_reading_member() -> None:
    """The reading route into the union: a delivered order moves
    `ride_times`, the reading summarises it, and the tile holding the reading
    must re-serve -- the same source-in-touched test the bulk surface already
    applies to the reading itself."""
    engine, facts = _engine()
    await _settled(engine, facts)

    now = datetime.now(tz=UTC)
    facts.put(
        "t1",
        "shop_order",
        "r2",
        {
            "ref": "R-2",
            "courier_id": "c1",
            "status": "delivered",
            "picked_up_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    report = await engine.run("t1", written={"shop_order": ["r2"]})

    assert "shop_courier.typical_ride" in _results(report.results), (
        "the reading over the touched figure was not re-served"
    )
    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles
    assert "shop_courier.reviews_card" not in bundles


async def test_a_pushed_bundle_serves_its_declared_windows_not_the_defaults() -> None:
    """The tile's windows are part of its definition (`over 9, 31-60`), and a
    push path that evaluated members at DEFAULT_TRAILING would put a
    different tile on the socket than the one `answer` serves by name --
    same name, same version, different numbers."""
    engine, facts = _engine()
    await _settled(engine, facts)

    now = datetime.now(tz=UTC)
    facts.put(
        "t1",
        "shop_order",
        "r2",
        {
            "ref": "R-2",
            "courier_id": "c1",
            "status": "delivered",
            "picked_up_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    report = await engine.run("t1", written={"shop_order": ["r2"]})

    card = _bundles(report.results)["shop_courier.card"]
    [typical] = [m.result for m in card.results if m.slot == "typical"]
    [subject] = typical.subjects
    assert subject.windows is not None
    assert [w.span for w in subject.windows] == ["9", "31-60"], (
        "the bundle's reading member was evaluated at windows its definition "
        "never declared"
    )


async def test_first_paint_serves_every_bundle() -> None:
    """A client's first paint (`results` with nothing narrowed) must carry
    the tiles too, or every screen that binds one renders blank until the
    next pass happens to touch a member."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    everything = await engine.results("t1")
    assert set(_bundles(everything)) == {
        "shop_courier.card",
        "shop_courier.reviews_card",
        "shop_order.board_card",
    }


# ------------------------------------------------------- goal movement --
#
# These two were dial-movement tests: a band threshold was a tenant setting,
# so turning it moved no stored value and the change stream said nothing, and
# the serve stamps' dial fingerprint was what noticed. A band's threshold is a
# fact now, so the trigger is a *record*, and what has to notice is the same
# thing: the figure it bands is byte-identical, and every connected screen
# keeps the old word unless the pass follows the band's edge.


async def test_a_goal_move_re_serves_the_banded_figure_and_its_bundles() -> None:
    """The courier holds two open orders and is cleared for three, so the band
    reads "ok". Cutting the allowance to one re-bands the *same stored count*
    as "over" -- nothing about `carrying` moved, and the tile holding it must
    follow anyway."""
    engine, facts = _engine()
    await _settled(engine, facts)

    facts.put(
        "t1", "shop_limit", "l1", {"courier_id": "c1", "orders": 1, "seconds": 7200}
    )
    report = await engine.run("t1", written={"shop_limit": ["l1"]})

    served = _results(report.results)
    assert "shop_courier.carrying" in served, (
        "the goal moved the served band and nothing was re-served"
    )
    [subject] = served["shop_courier.carrying"].subjects
    assert subject.level == "over"

    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles
    [carrying] = [m.result for m in bundles["shop_courier.card"].results if m.slot == "carrying"]
    assert carrying.subjects[0].level == "over"
    assert "shop_courier.reviews_card" not in bundles, (
        "a tile whose members band against nothing that moved was pushed"
    )


async def test_a_goal_move_re_serves_the_reading_that_bands_against_it() -> None:
    engine, facts = _engine()
    await _settled(engine, facts)

    facts.put(
        "t1",
        "shop_limit",
        "l1",
        {
            "courier_id": "c1",
            "orders": 3,
            "seconds": 60,
            "set_at": _iso(datetime.now(tz=UTC) - timedelta(days=2)),
        },
    )
    report = await engine.run("t1", written={"shop_limit": ["l1"]})

    assert "shop_courier.typical_ride" in _results(report.results), (
        "the reading bands against this goal and was not re-served"
    )
    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles
    assert "shop_courier.reviews_card" not in bundles


async def test_a_dial_nothing_names_moves_nothing() -> None:
    """The control for both dial tests above: a serve-stamp discipline that
    fingerprinted the whole document instead of each definition's own dials
    would pass them and fail this one -- by re-serving the world on every
    settings save."""
    engine, facts = _engine()
    await _settled(engine, facts)

    report = await engine.run("t1", {"limits": {"spare": 2}})

    assert report.results == (), (
        "a dial no definition names re-served something"
    )
    assert report.moved == frozenset()


async def test_a_quiet_definition_only_pass_pushes_nothing() -> None:
    """The baseline the whole surface rests on: same settings, no facts, no
    edits -- nothing moves, nothing serves, no listener wakes."""
    engine, facts = _engine()
    await _settled(engine, facts)

    report = await engine.run("t1")
    assert report.results == ()
    assert report.moved == frozenset()


# ------------------------------------------------------------ serve=False --


async def test_serve_false_reports_what_moved_without_evaluating_answers() -> None:
    """The lazy half of the seam: a host that owns its own delivery (per-client
    subscriptions) asks the pass what moved and evaluates only what somebody
    watches. The report must still name everything impacted -- and the stamps
    must still settle, so the next pass does not re-report a dial move the
    caller was already told about."""
    engine, facts = _engine()
    await _settled(engine, facts)

    facts.put(
        "t1", "shop_order", "o9", {"ref": "A-9", "courier_id": "c1", "status": "riding"}
    )
    report = await engine.run("t1", written={"shop_order": ["o9"]}, serve=False)

    assert report.results == ()
    assert "shop_courier.carrying" in report.moved
    assert "shop_courier.card" in report.moved
    assert "shop_courier.reviews_card" not in report.moved

    follow_up = await engine.run("t1")
    assert follow_up.moved == frozenset(), (
        "a serve=False pass failed to settle its stamps, so the next pass "
        "re-reported the same movement"
    )

    # The definition-only variant is the assertion with teeth: a fact write
    # moves no serve stamp (they were settled before it), so only a pass that
    # moves a stamp can prove serve=False still settles them. Skipping the
    # settle would make the second run here re-report the same move for ever.
    #
    # It used to turn a dial -- a band threshold, then the effort rendering
    # one. Both are gone, so what is left with that shape is prose: outside
    # every version hash, on the wire, and noticed by the stamps alone.
    reworded = compile_against(
        SOURCE.replace("# Orders in hand right now.", "# Orders being carried."), WORLD
    )
    redeployed = Uratori(
        schema=WORLD, library=reworded, store=engine._store, facts=facts
    )
    edit = await redeployed.run("t1", serve=False)
    assert edit.results == ()
    assert "shop_courier.carrying" in edit.moved
    settled = await redeployed.run("t1")
    assert settled.moved == frozenset(), (
        "the edit was reported once and must not be reported again"
    )


async def test_a_settings_save_now_re_serves_nothing_at_all() -> None:
    """This was the effort dial's test: `format_value` divided an effort by
    `tenant.hoursPerDay` on the way to every `display`, so moving it re-worded
    the effort figure and the projection rendering the same efforts, while no
    stored value moved anywhere. It was the last dial with that shape.

    An effort renders in hours now, so there is nothing left for a settings
    document to reach -- and the assertion inverts. It is worth keeping in
    that form: the serve stamps still exist, and a stamp that started
    fingerprinting a document nothing reads would re-serve the world on every
    save, which is the fixed tail they were built to replace.
    """
    engine, facts = _engine()
    facts.put(
        "t1",
        "shop_order",
        "e1",
        {"ref": "E-1", "courier_id": "c1", "status": "riding", "lug_seconds": 28800},
    )
    await _settled(engine, facts)

    report = await engine.run("t1", {"tenant": {"hoursPerDay": 4}})

    assert _results(report.results) == {}, (
        "a settings save re-served something, so a definition or a renderer "
        "still reads a dial"
    )


async def test_a_prose_edit_re_serves_the_definition_and_its_tiles() -> None:
    """Prose is deliberately outside every version hash -- an explanation is
    not a calculation -- but it IS on the wire (`Result.doc`), so an editor
    save that re-words a figure must reach every connected screen. Before
    the serve stamps carried the prose, `moved` reported nothing and every
    screen kept the old sentence until a reload."""
    engine, facts = _engine()
    await _settled(engine, facts)

    edited = SOURCE.replace(
        "# Orders in hand right now.", "# Orders currently being carried."
    )
    library = compile_against(edited, WORLD)
    redeployed = Uratori(
        schema=WORLD, library=library, store=engine._store, facts=facts
    )
    report = await redeployed.run("t1")

    served = _results(report.results)
    assert "shop_courier.carrying" in served
    assert served["shop_courier.carrying"].doc == "Orders currently being carried."
    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles, (
        "the tile carries the member's doc and kept the old words"
    )
    assert "shop_courier.reviews_card" not in bundles
    assert "shop_courier.reviews" not in served


async def test_a_bundle_prose_edit_re_serves_the_tile_alone() -> None:
    """The tile's own sentence travels on `BundleResult.doc` and lives
    outside the bundle's hash (prose is not composition), so only the serve
    stamp can notice it moved -- and nothing else may move with it."""
    engine, facts = _engine()
    await _settled(engine, facts)

    edited = SOURCE.replace(
        "# The courier tile: a banded count and a windowed reading.",
        "# The courier tile, re-worded.",
    )
    library = compile_against(edited, WORLD)
    redeployed = Uratori(
        schema=WORLD, library=library, store=engine._store, facts=facts
    )
    report = await redeployed.run("t1")

    assert {r.name for r in report.results} == {"shop_courier.card"}
    [card] = report.results
    assert isinstance(card, BundleResult)
    assert card.doc == "The courier tile, re-worded."


async def test_a_live_member_keeps_its_tile_off_the_bulk_surface_not_the_by_name_one() -> None:
    """`shop_courier.live_card` composes a reading that is not servable yet.
    Serving the bulk surface anyway would 500 the entire first paint over
    one member's gap; dropping the member would serve a tile quietly shorter
    than its definition. So the whole tile stays off the bulk surface, and
    the by-name route still refuses it with the reason -- the absence is
    stated exactly where the tile is asked for."""
    import pytest

    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    everything = await engine.results("t1")
    assert "shop_courier.live_card" not in {r.name for r in everything}
    with pytest.raises(NotImplementedError):
        await engine.answer("t1", "shop_courier.live_card")
