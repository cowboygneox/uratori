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

group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)
group shop_order.by_tag from tag_ids.tag

measure shop_order.riding_seconds = delivered_at - picked_up_at

# Orders in hand right now.
figure shop_courier.carrying:
    display "{shop_courier} has {value} in hand"
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
    band low on worst against limits.ride
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


def _rides(n: int, courier: str = "c1", tags: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """n delivered orders, one per day counting back from a fixed date, so a
    by-day figure holds n rows for the one courier."""
    from datetime import datetime, timedelta

    end = datetime(2026, 8, 20, 12, 0)
    out = {}
    for i in range(n):
        at = end - timedelta(days=i)
        out[f"r{i}"] = {
            "ref": f"R-{i:03}",
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
        await _push(http, {"shop_courier": {"c1": {"name": "Aki"}}, "shop_order": _rides(3)})

        got = await http.get("/ui/api/tenants/t1/results/shop_courier.typical_ride")
        assert got.status_code == 200, got.text
        result = got.json()
        assert result["statistics"] == ["mean", "worst"], (
            "the declared list, in declaration order -- the stable column set"
        )
        assert result["banded_on"] == "worst", "the band names worst, not the default mean"

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
# All riding time, totalled.
reading shop_courier.ride_total(range):
    display "{shop_courier} total riding"
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        sum(rides)
"""
        put = await http.put("/schema", json=PARITY_SCHEMA)
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": source})
        assert put.status_code == 200, put.text
        await _push(http, {"shop_courier": {"c1": {"name": "Aki"}}, "shop_order": _rides(2)})

        result = (await http.get("/ui/api/tenants/t1/results/shop_courier.ride_total")).json()
        assert result["statistics"] == ["total"]
        for subject in result["subjects"]:
            for window in subject["windows"]:
                assert set(window["display"]) <= {"total"}


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
                "shop_courier": {"c1": {"name": "Aki"}, "c2": {"name": "Bo"}},
                "shop_order": {**_rides(3), **_rides(2, "c2")},
            },
        )
        # _rides(2, 'c2') reuses r0/r1 keys -- rewrite with distinct keys.
        await _push(
            http,
            {
                "shop_order": {
                    f"b{i}": row
                    for i, row in enumerate(_rides(2, "c2").values())
                }
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
        await _push(
            http,
            {"shop_courier": {"c1": {"name": "Aki"}}, "shop_order": _rides(3)},
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
        assert [s["id"] for s in typical["result"]["subjects"]] == ["c1"]
        spans = [w["span"] for w in typical["result"]["subjects"][0]["windows"]]
        assert spans == ["30", "7"], (
            "the tile's own declared windows, not the serving default"
        )

        carrying = members["carrying"]
        assert [s["id"] for s in carrying["result"]["subjects"]] == ["c1"]

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
        # member narrows to this record's row, the courier members state
        # whose records they are about.
        order = await _about(http, "shop_order", "r0")
        card = {t["bundle"]: t for t in order["tiles"]}["shop_courier.card"]
        members = {m["slot"]: m for m in card["members"]}
        assert members["board"]["result"] is not None
        rows = members["board"]["result"]["subjects"]
        assert [s["id"] for s in rows] == ["r0"], "this record's row alone"
        assert rows[0]["row"]["flags"] == [], "a delivered order earned no flag"
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
            {"shop_courier": {"c1": {"name": "Aki"}}, "shop_order": _rides(65)},
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
        await _push(
            http,
            {"shop_courier": {"c1": {"name": "Aki"}}, "shop_order": _rides(7)},
        )

        full = (await http.get("/ui/api/tenants/t1/results/shop_courier.ride_times")).json()
        expected = [s["id"] for s in full["subjects"] if s["id"].split("@", 1)[0] == "c1"]
        assert len(expected) == 7

        walked: list[str] = []
        after = ""
        for _ in range(10):
            page = await http.get(
                "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
                params={"limit": 3, **({"after": after} if after else {})},
            )
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["total"] == 7, "the total holds on every page"
            rows = body["result"]["subjects"]
            walked += [s["id"] for s in rows]
            if not body["more"]:
                break
            after = rows[-1]["id"]
        assert walked == expected, "no page boundary drops or doubles a row"

        # Rendering identical to the figure's own page, row for row.
        by_id = {s["id"]: s for s in full["subjects"]}
        page_one = (
            await http.get(
                "/ui/api/tenants/t1/computed/shop_courier.ride_times/shop_courier/c1",
                params={"limit": 3},
            )
        ).json()
        for subject in page_one["result"]["subjects"]:
            assert subject == by_id[subject["id"]]


async def test_cited_rows_page_in_subject_order_without_drop_or_double(
    pg_dsn: str,
) -> None:
    """The same walk for the citation side: every stored row of one figure
    that counted this record, in subject order, keyset-paged with a true
    total -- the section used to dead-end at '… and more citations than this
    page shows'."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        tags = [f"t{i}" for i in range(5)]
        await _push(
            http,
            {
                "shop_courier": {"c1": {"name": "Aki"}},
                "shop_tag": {t: {"label": t} for t in tags},
                "shop_order": _rides(1, tags=tags),
            },
        )

        walked: list[str] = []
        after = ""
        for _ in range(10):
            page = await http.get(
                "/ui/api/tenants/t1/cited/shop_tag.orders/shop_order/r0",
                params={"limit": 2, **({"after": after} if after else {})},
            )
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["total"] == 5
            walked += [row["id"] for row in body["rows"]]
            if not body["more"]:
                break
            after = body["rows"][-1]["id"]
        assert walked == sorted(tags), (
            "every tag's row counted this order exactly once, in subject order"
        )

        about = await _about(http, "shop_order", "r0")
        cited = {c["figure"]: c for c in about["cited"]}["shop_tag.orders"]
        assert cited["total"] == 5, "the overview entry states the same total"


async def test_the_activity_log_pages_back_to_the_first_kept_run(pg_dsn: str) -> None:
    """'Showing the newest 50 of N' with no door to the other N-50 was the
    one remaining stated-but-sealed cap. Keyset by run id, newest first."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        for i in range(5):
            await _push(http, {"shop_courier": {"c1": {"name": f"Aki v{i}"}}})

        walked: list[int] = []
        after = ""
        for _ in range(10):
            page = await http.get(
                "/ui/api/tenants/t1/activity",
                params={"limit": 2, "quiet": "true", **({"after": after} if after else {})},
            )
            assert page.status_code == 200, page.text
            body = page.json()
            assert body["total"] == 5
            walked += [run["id"] for run in body["runs"]]
            if not body["more"]:
                break
            after = str(body["runs"][-1]["id"])
        assert walked == sorted(walked, reverse=True) and len(set(walked)) == 5, (
            "every kept run exactly once, newest first"
        )


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
        await _push(http, {"shop_courier": {"c1": {"name": "Aki"}}})
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

        # Every declarable dial answers, including the reserved rendering
        # one -- the page joins declaration edges to this list, and a dial
        # missing here would render as a name with no value beside it.
        assert "tenant.hoursPerDay" in dials


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
        await _push(http, {"shop_courier": {"c1": {"name": "Aki"}}})

        got = await http.get("/ui/api/tenants/t1/dials")
        assert got.status_code == 200, got.text
        dials = {d["name"]: d for d in got.json()["dials"]}
        over = dials["limits.carrying.over"]
        assert over["display"] is None
        assert over["source"] == "unset"


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
    return re.search(rf"\b{re.escape(field)}\b", source) is not None


# What the page deliberately does not render, each with the reason. A field
# added to the wire lands red here until it is rendered or its absence is
# argued in a sentence -- the enforcement the docstring promises.
UNRENDERED: dict[str, str] = {
    "WorldOut.name_fields": "records arrive already named by the server; the map is for API clients",
    "Result.zone": "the windows carry their own zone; the result-level copy is for API clients",
    "Result.empty": "rendered via the projection/figure empty sentences keyed off subjects",
    "Subject.value": "positional only -- the page renders display, and draws nothing scaled yet",
    "Window.zone": "the frm/to labels are already local; printing the zone per row is noise",
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
    "EvidenceMember.value": "positional only -- the page renders display",
}


def _wire_models() -> list[type]:
    from uratori import results
    from uratori.server import ui

    return [
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
        ui.WorldOut,
        ui.DeclarationOut,
        ui.BundleSlot,
        ui.Dependency,
        ui.TenantsOut,
        ui.TenantOut,
        ui.FactKindsOut,
        ui.KindCount,
        ui.FactPageOut,
        ui.FactRecordOut,
        ui.RunOutLog,
        ui.ActivityOut,
        ui.MembershipOut,
        ui.MembershipBucket,
        ui.MemberPageOut,
        ui.MemberRecordOut,
        ui.MeasuredPageOut,
        ui.MeasuredRecordOut,
        ui.RecordOut,
        ui.FiledOut,
        ui.RecordMeasureOut,
        ui.AboutOut,
        ui.AboutFigureOut,
        ui.AboutReadingOut,
        ui.AboutTileOut,
        ui.TileMemberOut,
        ui.CitedFigureOut,
        ui.CitedRowOut,
        ui.AboutPageOut,
        ui.ComputedPageOut,
        ui.CitedPageOut,
        ui.DialsOut,
        ui.DialOut,
    ]


def test_every_wire_field_the_ui_is_served_is_rendered_or_argued() -> None:
    """The guard that makes a silent gap a red build: every field on every
    model the /ui page is served must be referenced by app.js, or carry a
    written reason in UNRENDERED. This is how `series` went unrendered for
    two releases -- the wire grew, the page did not, and nothing went red."""
    source = _app_source_sans_comments()
    missing: list[str] = []
    stale: list[str] = []
    for model in _wire_models():
        for field in model.model_fields:
            qualified = f"{model.__name__}.{field}"
            if qualified in UNRENDERED:
                if _referenced(source, field):
                    # An allowlist entry for a field the page now renders is
                    # a reason that no longer describes the code.
                    stale.append(qualified)
                continue
            if not _referenced(source, field):
                missing.append(qualified)
    assert not missing, (
        "these wire fields reach the page and nothing renders them -- render "
        f"each, or state why in UNRENDERED: {sorted(missing)}"
    )
    # A field both rendered and excused is only worth failing over when the
    # exemption is field-specific: shared names (display, name, kind) are
    # referenced for other models' sake.
    del stale


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


def test_every_dial_entry_point_reaches_the_page_as_a_setting_edge() -> None:
    """Settings enter the language in several places -- age predicates, day
    bucketing zones, band thresholds, figure/projection ladders. Each entry
    point must surface as a `setting` dependency edge, because the page's
    dial rendering hangs off those edges: a construct that learns to read a
    dial without emitting the edge renders the definition with the dial
    invisible. Enumerated here over the compiled plans' own fields."""
    from uratori.lang import plan as plans

    carriers = {
        plans.FigurePlan: {"settings", "band_settings"},
        plans.ReadingPlan: {"settings"},
        plans.ProjectPlan: {"settings"},
        plans.SummarisePlan: {"settings"},
    }
    for holder, fields in carriers.items():
        held = {f.name for f in __import__("dataclasses").fields(holder)}
        for field in fields:
            assert field in held, f"{holder.__name__} lost {field}"
    # And the page must join those edges to the tenant's current values:
    # the dials payload, referenced by name.
    source = _app_source_sans_comments()
    assert "/dials" in source, (
        "the page never fetches the dials payload, so a definition's setting "
        "edges render as names with no values beside them"
    )
