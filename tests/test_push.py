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
    kinds=frozenset({"shop_order", "shop_courier", "shop_review"}),
    name_fields={"shop_courier": "name", "shop_order": "ref", "shop_review": "ref"},
    bucket_settings=("tenant.timezone",),
    figure_settings=("limits.carrying.over", "limits.spare"),
    reading_settings=("limits.rideHours",),
    defaults={
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {
            "carrying": {"over": 3},
            "rideHours": {"good": 2, "poor": 5},
            "spare": 1,
        },
    },
)

SOURCE = """
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)
group shop_review.rated_by from courier_id

measure shop_order.riding_seconds = delivered_at - picked_up_at

# Orders in hand right now.
figure shop_courier.carrying:
    display "{value} in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)
    band:
        when value >= limits.carrying.over then "over"
        otherwise "ok"

# Every delivery's ride time, day by day.
figure shop_courier.ride_times:
    display "{shop_courier} rides"
    depends:
        done = shop_order.delivered_by_day:{shop_courier}
    calculate:
        list(shop_order.riding_seconds over done)

# The typical ride, over a window.
reading shop_courier.typical_ride(range):
    display "{value}"
    band low against limits.rideHours
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


# ------------------------------------------------------- dial movement --


async def test_a_band_dial_move_re_serves_the_banded_figure_and_its_bundles() -> None:
    """A band dial lives outside the compute fingerprint on purpose (the
    stored values did not move), so the change stream is silent -- and before
    the serve stamps existed, so was the socket: the board kept the old
    colour on every connected screen until a reload. The re-served answer
    must carry the new word, and the tile holding the figure must follow."""
    engine, facts = _engine()
    await _settled(engine, facts)

    # Defaults band `carrying` (2 open orders) as "ok" (over at >= 3);
    # lowering the dial to 1 re-bands the same stored count as "over".
    report = await engine.run("t1", {"limits": {"carrying": {"over": 1}}})

    served = _results(report.results)
    assert "shop_courier.carrying" in served, (
        "the dial moved the served band and nothing was re-served"
    )
    [subject] = served["shop_courier.carrying"].subjects
    assert subject.level == "over"

    bundles = _bundles(report.results)
    assert "shop_courier.card" in bundles
    [carrying] = [m.result for m in bundles["shop_courier.card"].results if m.slot == "carrying"]
    assert carrying.subjects[0].level == "over"
    assert "shop_courier.reviews_card" not in bundles, (
        "a tile whose members name no moved dial was pushed"
    )


async def test_a_reading_dial_move_re_serves_the_reading_and_its_bundles() -> None:
    engine, facts = _engine()
    await _settled(engine, facts)

    report = await engine.run("t1", {"limits": {"rideHours": {"good": 1, "poor": 2}}})

    assert "shop_courier.typical_ride" in _results(report.results), (
        "the reading renders under this dial and was not re-served"
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
