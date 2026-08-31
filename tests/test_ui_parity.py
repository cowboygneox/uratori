"""The investigation UI's parity contract: everything a definition carries
renders, on every surface that shows the definition.

These tests exist because language expansions kept landing with UI gaps
found by a person clicking around instead of by the build: bundles reached
the catalogue two releases after the language learned them, a reading's
`series` travelled on the wire with no surface rendering it, and a record's
citation list capped at sixty with no door to the rest. Each section here
pins one of those classes shut:

- **The wire names what a reading calculates.** A page drawing one column
  per statistic needs the declared list, stable across windows -- deriving
  columns from whichever values happen to be present would make a withheld
  window silently narrow the table.
- **A record's page walks up through every kind** -- readings and the
  bundles ("tiles") included, each member rendered by the same serving code
  it gets standalone, narrowed to the record as a fetch scope.
- **Capped lists have totals and doors.** The about page's per-entry cap
  states the true count, and paged routes serve the rest in a
  server-decided order that neither drops nor doubles a row at a boundary.
- **Dials are visible where they are read.** Every declaration page can
  say which tenant settings can move it AND what they currently hold.
- **The structural guard.** Wire fields, declaration kinds and result
  kinds are enumerated, and a construct the page does not handle is a red
  test naming it -- stated somewhere, enforced here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import httpx
from pydantic import BaseModel

from uratori.server.ui import DeclarationKind

from .test_ui import serve

STATIC = Path(__file__).parent.parent / "uratori" / "server" / "static"


# ------------------------------------------------------------- fixtures --

PARITY_SCHEMA = {
    "bucket_settings": ["tenant.timezone"],
    "figure_settings": ["limits.carrying.over"],
    "reading_settings": ["limits.ride"],
    "defaults": {
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {"carrying": {"over": 3}, "ride": {"good": 3600, "poor": 7200}},
    },
}

PARITY_SOURCE = """
# A courier on the road.
fact shop_courier:
    name name
    name as text
    timezone as text

# One order, from pickup to the door.
fact shop_order:
    name ref
    ref as text
    courier_id as text
    status as text
    picked_up_at as moment
    delivered_at as moment
    many tag_ids:
        tag as text

# A handling tag an order can wear.
fact shop_tag:
    name label
    label as text

# What a courier is cleared for: how many orders in hand, and how long a
# round should take. One record per courier, moved when somebody moves it.
fact shop_limit:
    courier_id as text
    orders as number
    seconds as number
    set_at as moment

group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
group shop_order.delivered_by_day from (courier_id, delivered_at by day in shop_courier.timezone)
group shop_order.by_tag from tag_ids.tag
group shop_limit.set_for from courier_id
group shop_limit.set_by_day from (courier_id, set_at by day)

measure shop_order.riding_seconds = delivered_at - picked_up_at
measure shop_limit.orders_allowed = orders in count

# How many orders this courier is cleared to hold at once.
figure shop_courier.hand_limit:
    display "{shop_courier} may hold {value}"
    depends:
        set = shop_limit.set_for:{shop_courier}
    calculate:
        sum(shop_limit.orders_allowed over set)

# How long a round is meant to take, as last set -- carried across the days
# nobody moved it.
figure shop_courier.ride_budget bucketed:
    display "{shop_courier} ride budget"
    unit duration
    depends:
        set = shop_limit.set_by_day:{shop_courier}
    calculate:
        latest(shop_limit.seconds over set) carried forward

# Orders in hand right now.
figure shop_courier.carrying:
    display "{shop_courier} has {value} in hand"
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

# How many orders wear this tag.
figure shop_tag.orders:
    display "{shop_tag} tagged {value}"
    depends:
        tagged = shop_order.by_tag:{shop_tag}
    calculate:
        count(tagged)

# The typical ride, over a window.
reading shop_courier.typical_ride(range):
    display "{shop_courier} typical ride"
    band on worst:
        when value > shop_courier.ride_budget then "over"
        otherwise "ok"
    depends:
        rides = shop_courier.ride_times in range
    requires:
        at least 2 values in rides
    calculate:
        mean(rides)
        worst(rides)

# One row per order, alphabetically.
projection shop_order.board:
    sort by ref ascending

    field:
        ref = ref as text
        status = status as text

    flag riding when status == "riding":
        label "Riding"
        detail "{ref} is out for delivery."
        severity info

# The whole book of orders, one row.
summarise shop_order.book over shop_order.board:
    count orders

# The courier tile.
bundle shop_courier.card:
    typical = reading shop_courier.typical_ride over 30, 7
    carrying = figure shop_courier.carrying
    board = projection shop_order.board
    book = summarise shop_order.book
"""


async def _teach(http: httpx.AsyncClient) -> None:
    put = await http.put("/schema", json=PARITY_SCHEMA)
    assert put.status_code == 200, put.text
    put = await http.put("/definitions", json={"source": PARITY_SOURCE})
    assert put.status_code == 200, put.text


def _rides(
    n: int, courier: str = "c1", tags: list[str] | None = None, prefix: str = "r"
) -> dict[str, dict[str, Any]]:
    """n delivered orders, one per day counting back from yesterday, so a
    by-day figure holds n rows for the one courier. Counted back from *now*
    rather than a fixed date, because the reading tests window against the
    clock: a fixed anchor is a test that starts failing the month the
    windows drift past it."""
    from datetime import UTC, datetime, timedelta

    end = datetime.now(tz=UTC) - timedelta(days=1)
    out = {}
    for i in range(n):
        at = end - timedelta(days=i)
        out[f"{prefix}{i}"] = {
            "ref": f"{prefix.upper()}-{i:03}",
            "courier_id": courier,
            "status": "delivered",
            "picked_up_at": (at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delivered_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tag_ids": [{"tag": t} for t in (tags or [])],
        }
    return out


async def _push(http: httpx.AsyncClient, writes: dict[str, Any]) -> None:
    push = await http.post("/tenants/t1/facts", json={"writes": writes})
    assert push.status_code == 200, push.text


async def _about(http: httpx.AsyncClient, kind: str, key: str) -> dict[str, Any]:
    got = await http.get(f"/ui/api/tenants/t1/about/{kind}/{key}")
    assert got.status_code == 200, got.text
    return got.json()


async def _walk(
    http: httpx.AsyncClient,
    path: str,
    *,
    limit: int,
    rows_of: Any,
    total: int,
) -> list[str]:
    """Walk a keyset-paged route to exhaustion, asserting the shared page
    contract as it goes: the true total on every page, and termination."""
    walked: list[str] = []
    after = ""
    for _ in range(total + 2):
        page = await http.get(
            path, params={"limit": limit, **({"after": after} if after else {})}
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["total"] == total, "the total holds on every page"
        ids = rows_of(body)
        walked += ids
        if not body["more"]:
            break
        after = ids[-1]
    return walked


# ---------------------------------------------- the statistics a reading --


async def test_a_reading_names_its_statistics_and_the_one_the_band_judges(
    pg_dsn: str,
) -> None:
    """The matrix's column set comes from the declaration, not from whichever
    values a window happens to hold: a withheld window has empty `display`,
    and a table that unioned present keys would silently narrow itself the
    day every window fell short. `banded_on` names the statistic the band
    judges, because the band word is a verdict on ONE number and a column
    that coloured its neighbours would band statistics the definition never
    banded."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}, "shop_order": _rides(3)})

        got = await http.get("/ui/api/tenants/t1/results/shop_courier.typical_ride")
        assert got.status_code == 200, got.text
        result = got.json()
        assert result["statistics"] == ["mean", "worst"], (
            "the declared list, in declaration order -- the stable column set"
        )
        assert result["banded_on"] == "worst", "the band names worst, not the default mean"

        # The load-bearing half of "declared, not derived": a window whose
        # every statistic is withheld still travels the full declared list.
        # A one-day window holds at most one ride against a floor of two, so
        # every display map here is empty -- and a column set unioned from
        # present keys would collapse to nothing.
        thin = (
            await http.get(
                "/ui/api/tenants/t1/results/shop_courier.typical_ride?trailing=1"
            )
        ).json()
        assert thin["statistics"] == ["mean", "worst"]
        for subject in thin["subjects"]:
            for window in subject["windows"]:
                assert window["unmet"], "the one-day window must fall short"
                assert window["display"] == {}, "withheld means every statistic"

        # A figure's answer carries neither: statistics are a reading's
        # vocabulary, and a null here is 'does not apply', never 'empty'.
        figure = (await http.get("/ui/api/tenants/t1/results/shop_courier.carrying")).json()
        assert figure["statistics"] is None
        assert figure["banded_on"] is None


async def test_a_sum_statistic_travels_under_its_wire_name(pg_dsn: str) -> None:
    """`sum` renders on the wire as `total` (the Window field's name), and the
    declared list must speak the wire's language -- a column headed by a key
    the display map never uses would dash every row."""
    async with serve(pg_dsn) as http:
        source = PARITY_SOURCE + """
# All riding time, totalled, banded on the total.
reading shop_courier.ride_total(range):
    display "{shop_courier} total riding"
    band on sum:
        when value > 36000 then "over"
        otherwise "ok"
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        sum(rides)

# The band's unwritten default: it judges the mean.
reading shop_courier.usual_ride(range):
    display "{shop_courier} usual ride"
    band:
        when value > 3600 then "over"
        otherwise "ok"
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)
"""
        put = await http.put("/schema", json=PARITY_SCHEMA)
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": source})
        assert put.status_code == 200, put.text
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}, "shop_order": _rides(2)})

        result = (await http.get("/ui/api/tenants/t1/results/shop_courier.ride_total")).json()
        assert result["statistics"] == ["total"]
        assert result["banded_on"] == "total", (
            "a band written `on sum` names the wire's `total` column -- the "
            "language's word would head a column the display map never fills"
        )
        held = [
            window["display"]
            for subject in result["subjects"]
            for window in subject["windows"]
            if window["display"]
        ]
        assert held, "at least one window must actually carry a total"
        for display in held:
            assert set(display) == {"total"}

        usual = (await http.get("/ui/api/tenants/t1/results/shop_courier.usual_ride")).json()
        assert usual["banded_on"] == "mean", "the unwritten band target is the mean"


# ------------------------------------------------- a record's tiles etc. --


async def test_a_record_serves_the_readings_scoped_to_its_kind(pg_dsn: str) -> None:
    """The record page walks up through EVERY kind. Readings were the silent
    one: scoped to a kind, computed per subject, and absent from the about
    payload -- so a courier's page said what the figures made of it while the
    reading over those same figures was invisible. Narrowed to the record as
    a fetch scope: the same evaluation, this subject's rows picked out."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(
            http,
            {
                "shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}, "c2": {"name": "Bo", "timezone": "UTC"}},
                "shop_order": {**_rides(3), **_rides(2, "c2", prefix="b")},
            },
        )

        about = await _about(http, "shop_courier", "c1")
        readings = {r["result"]["name"]: r for r in about["readings"]}
        assert set(readings) == {"shop_courier.typical_ride"}
        narrowed = readings["shop_courier.typical_ride"]["result"]
        assert narrowed["state"]["ok"] is True
        assert [s["id"] for s in narrowed["subjects"]] == ["c1"], (
            "this record's rows only -- the neighbour courier stays on the "
            "reading's own page"
        )
        assert narrowed["subjects"][0]["windows"], "the windows travel"
        assert narrowed["statistics"] == ["mean", "worst"], (
            "the narrowed result is the same shape the reading serves alone, "
            "declared statistics included"
        )

        # An order's page lists no reading -- none is scoped to shop_order --
        # and the section says so by existing empty, not by vanishing.
        order = await _about(http, "shop_order", "r0")
        assert order["readings"] == []


async def test_a_record_serves_the_tiles_its_kind_appears_on(pg_dsn: str) -> None:
    """Bundles reach the facts side: a record's page lists every tile with a
    member that concerns its kind, each such member rendered by composition
    -- the member's ordinary Result, narrowed to this record -- under its
    slot, with the member's own name and version. Members about other
    records state that instead of shipping another kind's data, and the
    page-level summarise states that it is not a per-record number."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        # TWO couriers, so a narrowing that quietly served everybody's rows
        # would fail here rather than pass by there being nobody else; and
        # one order still riding, so the board's flag machinery is exercised
        # by a row that actually earned its sentence.
        await _push(
            http,
            {
                "shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}, "c2": {"name": "Bo", "timezone": "UTC"}},
                "shop_order": {
                    **_rides(3),
                    **_rides(2, "c2", prefix="b"),
                    "o0": {"ref": "A-0", "courier_id": "c1", "status": "riding"},
                },
            },
        )

        about = await _about(http, "shop_courier", "c1")
        tiles = {t["bundle"]: t for t in about["tiles"]}
        assert set(tiles) == {"shop_courier.card"}
        card = tiles["shop_courier.card"]
        assert card["version"], "the review hash travels, stated as review-only"

        members = {m["slot"]: m for m in card["members"]}
        assert list(members) == ["typical", "carrying", "board", "book"], (
            "declaration order, like the tile itself"
        )

        typical = members["typical"]
        assert typical["kind"] == "reading"
        assert typical["result"]["name"] == "shop_courier.typical_ride"
        assert typical["result"]["version"], "the member's own citation"
        assert [s["id"] for s in typical["result"]["subjects"]] == ["c1"], (
            "this courier's windows alone -- Bo's stay on the tile's own page"
        )
        spans = [w["span"] for w in typical["result"]["subjects"][0]["windows"]]
        assert spans == ["30", "7"], (
            "the tile's own declared windows, not the serving default"
        )

        carrying = members["carrying"]
        assert [s["id"] for s in carrying["result"]["subjects"]] == ["c1"], (
            "this courier's row alone -- the figure holds Bo's too"
        )

        board = members["board"]
        assert board["result"] is None and board["note"], (
            "the board's rows are shop_order records; a courier's page states "
            "that instead of shipping another kind's rows under this record"
        )
        book = members["book"]
        assert book["result"] is None and book["note"], (
            "a summarise is about the whole page, never one record"
        )

        # The order's page sees the same tile from the other side: the board
        # member narrows to this record's row -- flags intact -- and the
        # courier members state whose records they are about.
        order = await _about(http, "shop_order", "o0")
        card = {t["bundle"]: t for t in order["tiles"]}["shop_courier.card"]
        members = {m["slot"]: m for m in card["members"]}
        assert members["board"]["result"] is not None
        rows = members["board"]["result"]["subjects"]
        assert [s["id"] for s in rows] == ["o0"], "this record's row alone"
        assert [f["label"] for f in rows[0]["row"]["flags"]] == ["Riding"], (
            "the row keeps the sentence it earned on the page"
        )
        assert members["board"]["result"]["summary"] is None, (
            "the page-level summary row stays home: under one record's row "
            "it would read as this record's contribution, which it is not"
        )
        assert members["board"]["note"], (
            "and staying home is stated, never silent -- the note says where "
            "the summary row lives"
        )
        assert members["typical"]["result"] is None and members["typical"]["note"]
        assert members["carrying"]["result"] is None and members["carrying"]["note"]

        # A tag record is on no tile, and the section says so by being empty.
        await _push(http, {"shop_tag": {"t-x": {"label": "fragile"}}})
        tag = await _about(http, "shop_tag", "t-x")
        assert tag["tiles"] == []


# ------------------------------------------------ every value, paginated --


async def test_about_entries_state_their_true_totals(pg_dsn: str) -> None:
    """A capped entry says how many rows exist, not just that more do: 'more
    than sixty' is not a number a reader can reconcile against anything."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(
            http,
            {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}, "shop_order": _rides(65)},
        )

        about = await _about(http, "shop_courier", "c1")
        rides = {
            f["result"]["name"]: f for f in about["figures"]
        }["shop_courier.ride_times"]
        assert rides["more"] is True
        assert rides["total"] == 65, "the true count beside the capped sample"
        assert len(rides["result"]["subjects"]) == 60


async def test_computed_rows_page_in_row_order_without_drop_or_double(
    pg_dsn: str,
) -> None:
    """The paged door behind 'show all N rows': the figure's rows for one
    record, in the figure's own serving order (day rows chronological),
    keyset-paged. Walking the pages reassembles exactly the full set -- a
    boundary that dropped or doubled a row is the bug this test exists to
    catch -- and every page carries the same true total."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        # A second courier, so a route that dropped its `subject=` narrowing
        # would return somebody else's rows here instead of passing because
        # nobody else exists.
        await _push(
            http,
            {
                "shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}, "c2": {"name": "Bo", "timezone": "UTC"}},
                "shop_order": {**_rides(7), **_rides(4, "c2", prefix="b")},
            },
        )

        full = (await http.get("/ui/api/tenants/t1/results/shop_courier.ride_times")).json()
        expected = [s["id"] for s in full["subjects"] if s["id"].split("@", 1)[0] == "c1"]
        assert len(expected) == 7

        walked = await _walk(
            http,
            "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
            limit=3,
            rows_of=lambda body: [s["id"] for s in body["result"]["subjects"]],
            total=7,
        )
        assert walked == expected, "no page boundary drops or doubles a row"

        # The exact-boundary case: a limit that divides the total must end
        # cleanly, not offer one more page holding nothing.
        last = await http.get(
            "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
            params={"limit": 7},
        )
        body = last.json()
        assert len(body["result"]["subjects"]) == 7 and body["more"] is False, (
            "a page exactly at the end offers no empty next page"
        )
        past = await http.get(
            "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
            params={"limit": 3, "after": expected[-1]},
        )
        assert past.json()["result"]["subjects"] == [] and past.json()["more"] is False

        # Rendering identical to the figure's own page, row for row -- and
        # the page states whose order the rows are in.
        by_id = {s["id"]: s for s in full["subjects"]}
        page_one = (
            await http.get(
                "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
                params={"limit": 3},
            )
        ).json()
        assert page_one["order"], "the order is the server's to state, in words"
        for subject in page_one["result"]["subjects"]:
            assert subject == by_id[subject["id"]]


async def test_the_paged_doors_refuse_what_the_overview_would_have(
    pg_dsn: str,
) -> None:
    """The paged routes carry the overview's own honesty: an unknown figure
    or a wrong kind is a 404 with a sentence, and a figure whose stored
    values may not honestly serve answers 409 with the reason -- never a
    200 whose empty page reads as 'zero rows', which is the confident
    absence the whole about surface exists to prevent."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}},
                "defer": True,
            },
        )
        assert push.status_code == 200, push.text

        base = "/ui/api/tenants/t1/computed"
        missing = await http.get(f"{base}/no.such/shop_courier/c1")
        assert missing.status_code == 404
        wrong = await http.get(f"{base}/shop_courier.ride_times/shop_order/c1")
        assert wrong.status_code == 404
        assert "scoped to" in wrong.json()["detail"]

        # The deferred pass left this tenant never bucketed: the figure's
        # own page says never-computed, and the paged door must not answer
        # a confident empty 200 where the overview would state the absence.
        refused = await http.get(f"{base}/shop_courier.ride_times/shop_courier/c1")
        assert refused.status_code == 409, refused.text

        cited = await http.get("/ui/api/tenants/t1/cited/no.such/shop_order/c1")
        assert cited.status_code == 404
        never = await http.get(
            "/ui/api/tenants/t1/cited/shop_tag.orders/shop_courier/c1"
        )
        assert never.status_code == 404, "shop_tag.orders never cites couriers"


async def test_cited_rows_page_in_subject_order_without_drop_or_double(
    pg_dsn: str,
) -> None:
    """The same walk for the citation side: every stored row of one figure
    that counted this record, in subject order, keyset-paged with a true
    total -- the section used to dead-end at '… and more citations than this
    page shows'."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        # Scrambled on purpose: pushed in an order that is NOT subject order,
        # so a route that skipped the sort and leaned on arrival order would
        # fail here instead of passing because the fixture was pre-sorted.
        tags = ["t3", "t0", "t4", "t1", "t2"]
        await _push(
            http,
            {
                "shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}},
                "shop_tag": {t: {"label": t} for t in tags},
                "shop_order": _rides(1, tags=tags),
            },
        )

        walked = await _walk(
            http,
            "/ui/api/tenants/t1/cited/shop_tag.orders/shop_order/r0",
            limit=2,
            rows_of=lambda body: [row["id"] for row in body["rows"]],
            total=5,
        )
        assert walked == sorted(tags), (
            "every tag's row counted this order exactly once, in subject order"
        )

        about = await _about(http, "shop_order", "r0")
        cited = {c["figure"]: c for c in about["cited"]}["shop_tag.orders"]
        assert cited["total"] == 5, "the overview entry states the same total"

        # The walk's default first page IS the overview's capped sample:
        # same order, same head -- so "browse all" continues the list the
        # reader was looking at rather than reshuffling it.
        page_one = (
            await http.get("/ui/api/tenants/t1/cited/shop_tag.orders/shop_order/r0")
        ).json()
        assert [r["id"] for r in page_one["rows"]][: len(cited["rows"])] == [
            r["id"] for r in cited["rows"]
        ]


async def test_the_activity_log_pages_back_to_the_first_kept_run(pg_dsn: str) -> None:
    """'Showing the newest 50 of N' with no door to the other N-50 was the
    one remaining stated-but-sealed cap. Keyset by run id, newest first."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        for i in range(5):
            await _push(http, {"shop_courier": {"c1": {"name": f"Aki v{i}", "timezone": "UTC"}}})
        # A do-nothing run, for the default view's paged path below.
        await _push(http, {"shop_courier": {"c1": {"name": "Aki v4", "timezone": "UTC"}}})

        async def pages(quiet: bool, limit: int, total: int) -> list[int]:
            walked: list[int] = []
            after = ""
            for _ in range(total + 2):
                page = await http.get(
                    "/ui/api/tenants/t1/activity",
                    params={
                        "limit": limit,
                        **({"quiet": "true"} if quiet else {}),
                        **({"after": after} if after else {}),
                    },
                )
                assert page.status_code == 200, page.text
                body = page.json()
                assert body["total"] == total
                walked += [run["id"] for run in body["runs"]]
                if not body["more"]:
                    break
                after = str(body["runs"][-1]["id"])
            return walked

        everything = await pages(quiet=True, limit=2, total=6)
        assert len(everything) == 6, (
            "every kept run exactly once -- a boundary that doubled a row "
            "would count seven, one that dropped a row five"
        )
        assert len(set(everything)) == 6
        assert everything == sorted(everything, reverse=True), "newest first"

        # The default view hides the quiet run on EVERY page, not just the
        # first: a cursor that forgot the loud filter would leak it back in
        # from page two onward.
        loud = await pages(quiet=False, limit=2, total=5)
        assert len(loud) == 5 and len(set(loud)) == 5
        assert set(everything) - set(loud), "the quiet run exists to be hidden"
        assert (set(everything) - set(loud)).isdisjoint(loud)


# ------------------------------------------------------- dials, valued --


async def test_the_dials_route_serves_every_declarable_setting_with_its_value(
    pg_dsn: str,
) -> None:
    """A definition names a dial by name; the page must also say what the
    tenant's copy of that dial holds right now, and whether that is the
    tenant's own setting or the schema's default -- the same two answers the
    engine's own settings_for merge gives, rendered once, server-side."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}})
        put = await http.put(
            "/tenants/t1/settings", json={"document": {"limits": {"carrying": {"over": 5}}}}
        )
        assert put.status_code == 200, put.text

        got = await http.get("/ui/api/tenants/t1/dials")
        assert got.status_code == 200, got.text
        dials = {d["name"]: d for d in got.json()["dials"]}

        over = dials["limits.carrying.over"]
        assert over["display"] == "5"
        assert over["source"] == "tenant", "the tenant turned this one"

        zone = dials["tenant.timezone"]
        assert zone["display"] == "UTC"
        assert zone["source"] == "default", "untouched, so the schema's default"

        ride = dials["limits.ride"]
        assert ride["source"] == "default"
        assert "3600" in ride["display"] and "7200" in ride["display"], (
            "a threshold pair renders whole -- both rungs are the dial"
        )

        # Every declarable dial answers -- the page joins declaration edges
        # to this list, and a dial missing here would render as a name with
        # no value beside it. There is no reserved one to add any more:
        # `tenant.hoursPerDay` went when an effort stopped being rendered
        # against a working day.
        assert "tenant.hoursPerDay" not in dials


async def test_a_dial_nobody_gave_a_value_is_a_stated_absence(pg_dsn: str) -> None:
    """Declarable, never defaulted, never set: the dial lists with no display
    and `source` saying nobody holds it -- not a fabricated rendering of a
    value that does not exist."""
    async with serve(pg_dsn) as http:
        schema = {
            "bucket_settings": ["tenant.timezone"],
            "figure_settings": ["limits.carrying.over"],
            "reading_settings": ["limits.ride"],
            "defaults": {
                "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
                "limits": {"ride": {"good": 3600, "poor": 7200}},
            },
        }
        put = await http.put("/schema", json=schema)
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": PARITY_SOURCE})
        assert put.status_code == 200, put.text
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}})

        got = await http.get("/ui/api/tenants/t1/dials")
        assert got.status_code == 200, got.text
        dials = {d["name"]: d for d in got.json()["dials"]}
        over = dials["limits.carrying.over"]
        assert over["display"] is None
        assert over["source"] == "unset"


async def test_the_dials_page_shows_what_the_engine_reads_not_the_raw_document(
    pg_dsn: str,
) -> None:
    """The dial's value is the MERGED one -- the same `settings_for` merge
    the engine computes with -- never a walk of the raw tenant document. A
    tenant that half-overrides a threshold pair ({good: 1} over
    {good: 2, poor: 5}) is served good 1 · poor 5, and a page that walked
    the raw document would show `good 1` alone: a dial that disagrees with
    the calculation it claims to explain."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}})
        put = await http.put(
            "/tenants/t1/settings", json={"document": {"limits": {"ride": {"good": 1}}}}
        )
        assert put.status_code == 200, put.text

        dials = {d["name"]: d for d in (await http.get("/ui/api/tenants/t1/dials")).json()["dials"]}
        ride = dials["limits.ride"]
        assert "1" in ride["display"] and "7200" in ride["display"], (
            "the merged pair, both rungs -- the engine bands against poor "
            "7200 whatever the tenant wrote beside good"
        )
        assert ride["source"] == "tenant"


async def test_a_null_settings_leaf_is_an_absence_not_the_word_none(
    pg_dsn: str,
) -> None:
    """A tenant document may carry an explicit null (the settings body is
    untyped JSON). The engine's merge treats it as holding nothing, and the
    dials page must agree -- rendering the Python repr `None` as if it were
    a chosen value would be an absence dressed as data."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}})
        put = await http.put(
            "/tenants/t1/settings",
            json={"document": {"limits": {"carrying": {"over": None}}}},
        )
        assert put.status_code == 200, put.text

        dials = {d["name"]: d for d in (await http.get("/ui/api/tenants/t1/dials")).json()["dials"]}
        over = dials["limits.carrying.over"]
        assert over["display"] != "None", "never the repr of an absence"
        # What the engine actually reads through the merge is what the page
        # must claim -- computed here from the engine's own merge, so the
        # test cannot drift from it.
        from uratori.schema import Schema

        merged = Schema.from_document(PARITY_SCHEMA).settings_for(
            {"limits": {"carrying": {"over": None}}}
        )
        engine_reads = merged["limits"]["carrying"]["over"]
        if engine_reads is None:
            assert over["display"] is None and over["source"] == "unset"
        else:
            assert over["display"] == str(engine_reads)
            assert over["source"] in ("tenant", "default")


# ------------------------------------------------------ the unservable --


LIVE_TAIL = """
# Orders in hand right now, against the clock.
reading shop_courier.queue():
    display "{shop_courier} queue"
    depends:
        waiting = shop_order.riding_seconds over (shop_order.carried_by:{shop_courier} & shop_order.open)
    calculate:
        count(waiting)

# A tile with a live member.
bundle shop_courier.live_card:
    queue = reading shop_courier.queue
    carrying = figure shop_courier.carrying
"""


async def test_an_unservable_reading_still_has_a_name_on_the_record_page(
    pg_dsn: str,
) -> None:
    """A live reading cannot be served yet; its entry must still carry the
    address -- name and version -- beside the sentence. Two live readings
    answering two identical anonymous sentences would leave the reader
    unable to say WHICH reading is missing, which is the silence this
    section exists to end."""
    async with serve(pg_dsn) as http:
        put = await http.put("/schema", json=PARITY_SCHEMA)
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": PARITY_SOURCE + LIVE_TAIL})
        assert put.status_code == 200, put.text
        await _push(http, {"shop_courier": {"c1": {"name": "Aki", "timezone": "UTC"}}, "shop_order": _rides(3)})

        about = await _about(http, "shop_courier", "c1")
        by_name = {r["name"]: r for r in about["readings"]}
        assert set(by_name) == {"shop_courier.typical_ride", "shop_courier.queue"}
        live = by_name["shop_courier.queue"]
        assert live["result"] is None
        assert live["note"], "the route's own sentence travels"
        assert live["version"], "the address is whole even with no rows"

        # The tile with the live member lists too, members and versions
        # intact, with the sentence on the tile -- absent from the section
        # it would read as "this record is on no such tile".
        tiles = {t["bundle"]: t for t in about["tiles"]}
        assert "shop_courier.live_card" in tiles
        card = tiles["shop_courier.live_card"]
        assert card["note"], "why the tile has no rows, said out loud"
        slots = {m["slot"]: m for m in card["members"]}
        assert set(slots) == {"queue", "carrying"}
        assert all(m["version"] for m in card["members"])
        assert card["at"] is None, (
            "nothing was evaluated, so there is no instant to stamp -- a "
            "fresh timestamp on a non-answer would be a fabricated clock"
        )


# ---------------------------------------------------- the structural guard --


def _app_source_sans_comments() -> str:
    """app.js with its comments stripped, so a field 'handled' only in prose
    does not count as handled."""
    text = (STATIC / "app.js").read_text()
    out = []
    for line in text.split("\n"):
        # Single-line /* */ first, then // to end of line. String literals
        # never contain either marker -- asserted below so the day one does,
        # this helper fails loudly instead of silently truncating the code
        # it guards.
        line = re.sub(r"/\*.*?\*/", "", line)
        cut = line.find("//")
        out.append(line[:cut] if cut != -1 else line)
    stripped = "\n".join(out)
    assert "/*" not in stripped, "a multi-line comment block this helper cannot strip"
    return stripped


def _referenced(source: str, field: str) -> bool:
    """Whether app.js reads a property of this name -- `.field`, never the
    bare word, so a field named in the editor's keyword lists or a comment
    does not count as handled. (That looseness is exactly how the first
    draft of this guard blessed `series`: the word sat in FIG_WORDS while
    nothing drew the data.)"""
    return re.search(rf"\.{re.escape(field)}\b", source) is not None


# What the page deliberately does not render, each with the reason. A field
# added to the wire lands red here until it is rendered or its absence is
# argued in a sentence -- the enforcement the docstring promises.
UNRENDERED: dict[str, str] = {
    "WorldOut.name_fields": "records arrive already named by the server; the map is for API clients",
    "Subject.value": "positional only, and the page draws series bars from the served scale, not this scalar",
    "Window.series": "the page draws the served series_scale; the raw values are for clients with axes of their own",
    "Window.delta": (
        "the page prints the served delta_display cells; the raw signed numbers are "
        "for clients with axes of their own, and a page reading them would be one "
        "step from formatting a duration, which is a division"
    ),
    "Window.trailing": "span+bucket state the window; trailing exists for typed API clients",
    "Window.mean": "rendered through the display map, never the raw scalar",
    "Window.median": "rendered through the display map, never the raw scalar",
    "Window.worst": "rendered through the display map, never the raw scalar",
    "Window.total": "rendered through the display map, never the raw scalar",
    "Window.count": "rendered through the display map, never the raw scalar",
    "Row.values": "positional only -- the page renders display",
    "Row.units": "display strings arrive rendered; units exist for clients that draw",
    "Flag.name": "the stable id; the page shows label and detail",
    "Flag.action": "no surface offers actions; the fields shown are label and detail",
    "Evidence.subject": "the row clicked is the subject; repeating it under itself adds nothing",
    "Evidence.display": "the value is on the row the panel expands under",
    "Evidence.version": "the row's result already cites it",
    "Evidence.source": "the members already name their figure per row",
    "CitedPageOut.figure": "the opener already names the figure; the echo is for API readers of the page",
    "TenantOut.facts": "the switcher is an address list; per-kind counts live on the Facts tab it switches",
    "SourceOut.refusal": "the editor shows the refusal the check route serves; the boot copy is the same text",
    "CheckOut.ok": "the editor branches on the refusal being present, the same fact",
}

_REQUEST_MODELS = {"CheckIn", "SaveIn", "EditRunIn"}
"""Bodies the page composes rather than reads: their fields appear in app.js
as object-literal keys or shorthand, which no property-access match can see,
and a field the server never reads already fails the server's own tests."""


def _wire_models() -> list[type]:
    """Every response model the /ui page is served: the results-module wire
    shapes, plus EVERY pydantic model ui.py declares -- enumerated from the
    module, not hand-listed, so a new route's new model is guarded the
    moment it exists rather than when somebody remembers this list."""
    import inspect

    from uratori import results
    from uratori.server import ui

    served = [
        results.Result,
        results.BundleResult,
        results.BundleMemberResult,
        results.Subject,
        results.Window,
        results.Row,
        results.Flag,
        results.Evidence,
        results.EvidenceMember,
        results.Ok,
        results.Unavailable,
    ]
    for _name, model in inspect.getmembers(ui, inspect.isclass):
        if (
            issubclass(model, BaseModel)
            and model.__module__ == ui.__name__
            and model.__name__ not in _REQUEST_MODELS
        ):
            served.append(model)
    return served


def test_every_wire_field_the_ui_is_served_is_rendered_or_argued() -> None:
    """The guard that makes a silent gap a red build: every field on every
    model the /ui page is served must be read somewhere in app.js (as a
    property access), or carry a written reason in UNRENDERED. This is how
    `series` went unrendered for two releases -- the wire grew, the page did
    not, and nothing went red.

    A tripwire, not a proof: a field whose name collides with another
    model's rendered field (or a DOM property) slips through, so a truly
    novel name is always caught and a shared one sometimes is not. That
    asymmetry is accepted -- the alternative is a JS parser in the test."""
    source = _app_source_sans_comments()
    missing: list[str] = []
    for model in _wire_models():
        for field in model.model_fields:
            qualified = f"{model.__name__}.{field}"
            if qualified in UNRENDERED:
                continue
            if not _referenced(source, field):
                missing.append(qualified)
    assert not missing, (
        "these wire fields reach the page and nothing renders them -- render "
        f"each, or state why in UNRENDERED: {sorted(missing)}"
    )
    # Every allowlist entry must name a real field, or the list rots into
    # reasons about models that no longer exist.
    fields = {
        f"{model.__name__}.{field}"
        for model in _wire_models()
        for field in model.model_fields
    }
    orphaned = set(UNRENDERED) - fields
    assert not orphaned, f"UNRENDERED excuses fields that do not exist: {sorted(orphaned)}"


def test_every_declaration_kind_has_a_home_on_the_page() -> None:
    """A new declaration kind must land in the roster's ordering and the
    editor's vocabulary the release it lands in the language -- bundles
    reached the language two releases before the page learned the word."""
    source = _app_source_sans_comments()
    kind_order = re.search(r"const KIND_ORDER = \[([^\]]*)\]", source)
    declared = re.search(r"const FIG_DECLS = \[([^\]]*)\]", source)
    assert kind_order and declared, "the page's two kind vocabularies"
    in_roster = set(re.findall(r"'([a-z]+)'", kind_order.group(1)))
    in_editor = set(re.findall(r"'([a-z]+)'", declared.group(1)))
    for kind in get_args(DeclarationKind):
        assert kind in in_roster, f"KIND_ORDER does not order {kind}"
        # The editor speaks the language's keywords: `summarise` declares
        # what the catalogue calls a summary.
        keyword = "summarise" if kind == "summary" else kind
        assert keyword in in_editor, f"FIG_DECLS does not know {keyword}"


def test_every_result_kind_has_a_renderer() -> None:
    """resultBlocks branches on the result kinds; a kind it never names
    falls to the figure table, which renders another shape's answer as an
    empty table instead of the shape's own blocks."""
    from uratori.results import BundleResult, Result

    source = _app_source_sans_comments()
    kinds = set(get_args(Result.model_fields["kind"].annotation))
    kinds |= set(get_args(BundleResult.model_fields["kind"].annotation))
    for kind in sorted(kinds):
        assert re.search(rf"kind === '{kind}'", source), (
            f"no renderer branches on result kind {kind!r}"
        )


async def test_every_threshold_entry_point_reaches_the_page_as_an_edge(
    pg_dsn: str,
) -> None:
    """Settings enter the language in several places -- day-bucketing zones,
    band thresholds on figures and on readings. Each entry point must
    surface as a `setting` dependency edge in the world payload, because
    the page's dial rendering hangs off those edges: a construct that
    learns to read a dial without emitting the edge renders the definition
    with its dial invisible. Behavioural, over the compiled world -- a
    field-name check on the plans would pass while the edge emission
    quietly rotted."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        world = (await http.get("/ui/api/world")).json()
        edges = {
            d["name"]: {(e["type"], e["name"]) for e in d["rests_on"]}
            for d in world["declarations"]
        }
        assert ("figure", "shop_courier.hand_limit") in edges["shop_courier.carrying"], (
            "a figure's band threshold -- the goal it is judged against"
        )
        assert ("figure", "shop_courier.ride_budget") in edges[
            "shop_courier.typical_ride"
        ], "a reading's band threshold"
        assert ("fact", "shop_courier") in edges["shop_order.delivered_by_day"], (
            "a group's calendar, which is a field on the subject's record"
        )
        # And the closure carries them to the tile, whose page shows the same
        # lines through the same moved_by rendering. `moved_by` is leaves
        # only, so what has to arrive there is the *fact* the goals are
        # computed from -- and `shop_limit` enters this world through nothing
        # else, so its presence is the band edge and only the band edge. A
        # band threshold used to be a dial and this was a `setting` leaf; the
        # claim is the same one either way, and it is the point of the change:
        # what decides the word beside a number is now a record a reader can
        # go and look at.
        card = next(
            d for d in world["declarations"] if d["name"] == "shop_courier.card"
        )
        moved = {(e["type"], e["name"]) for e in card["moved_by"]}
        assert ("fact", "shop_limit") in moved, (
            "the tile's page never names the records that decide its colours"
        )

    # The page joins those edges to the tenant's current values through the
    # dials payload.
    source = _app_source_sans_comments()
    assert "/dials" in source, (
        "the page never fetches the dials payload, so a definition's setting "
        "edges render as names with no values beside them"
    )


def test_the_delta_cells_render_an_absence_as_an_absence_not_as_a_nought() -> None:
    """A null delta cell printed as `0` would say "no change" where the
    server said "not computed".

    The whole payload is built to keep those apart, and the page would be
    drawing a flat line through exactly the buckets nothing was collected
    for, with no way for a reader to tell which was which.

    Guarded on the source because the suite runs no JavaScript, so the reach
    is honest but limited: it catches a null rendered as any numeral, and
    not a null rendered as an empty string. The field-presence guard above
    cannot catch either -- it only asks whether `delta_display` is mentioned,
    and a mutant that renders every hole as `0` mentions it just as much.
    """
    source = _app_source_sans_comments()
    found = re.search(r"function deltaCells\(window\)\s*\{(.*?)\n\}", source, re.S)
    assert found is not None, "deltaCells has been renamed; this guard names it"
    body = found.group(1)

    for literal in re.findall(r"'([^']*)'", body):
        assert not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", literal), (
            f"deltaCells renders the literal {literal!r}. A cell the server said "
            "nothing about must not print as a number"
        )
    assert body.count("'—'") >= 2, (
        "both absences -- a column with no cells at all, and a hole inside one "
        "-- print the absence glyph rather than a value"
    )


def test_the_editor_knows_every_word_a_figure_header_can_carry() -> None:
    """A construct the editor does not know renders as plain text beside the
    keywords around it, which reads as a typo in the definition.

    `series` sat unhighlighted for two releases and nothing went red, so the
    words are enumerated here rather than trusted to whoever adds one. Held
    against the *language's* own vocabulary: the source of truth is the
    parser, so a keyword added there without a colour is caught by this
    listing rather than by somebody noticing.
    """
    source = _app_source_sans_comments()
    for word in ("bucketed", "carried", "forward", "median", "worst", "mean"):
        assert f"'{word}'" in source, (
            f"the editor does not know the word {word!r}, so a definition using it "
            "renders it as plain text beside the keywords around it"
        )


def test_a_carried_figures_declaration_reaches_the_page_whole() -> None:
    """The definition pane shows a figure exactly as written, and the header
    pattern that finds the block has to admit every optional word.

    It fails quietly when it does not -- the block is not found and the pane
    serves a *blank* formula, which is a figure nobody can read on the very
    surface that exists so they can. Both new header words are covered
    together with the calculation's suffix, because all three are new places
    the pattern could miss.
    """
    from tests.test_onchange import CARRIED, compile_world
    from uratori.lang.source import declaration_source

    lib = compile_world(CARRIED)
    text = declaration_source(lib, "site.target_month")
    assert text, "a carried figure served no formula at all"
    assert "bucketed" in text
    assert "carried forward" in text, (
        "the suffix that says what the buckets between changes mean was cut "
        "from the text a reader checks the number against"
    )
    assert "latest(setting_change.value over sets)" in text
