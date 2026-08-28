"""The built-in investigation UI: every declaration, every fact, every cascade.

The UI exists so a developer can stand behind the firewall and ask "what does
this deployment know, and why does this number say what it says" without
checking a repository out. These tests pin the three claims that make it an
investigation tool rather than a status page:

- **The world payload is complete.** Every declaration of every kind --
  groups, filters and measures included, though they have no version of their
  own -- with its source text and its dependencies, typed all the way down to
  the fact kinds, so a reader can walk from any definition to the records it
  stands on.
- **The activity log is a cascade record.** A pushed fact leaves a persisted
  run whose movements say which figures moved and to what, frozen at the
  moment it happened.
- **The security posture is deliberate.** Unauthenticated by design (a
  firewall is the door), which is exactly why it must be OFF by default the
  moment the API itself is token-protected -- a token plus a silently open UI
  would leak everything the token guards.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
import httpx
import pytest

from uratori import compile_source
from uratori.server import create_app
from uratori.server import db as server_db

from .test_schema import COURIER_SOURCE, COURIER_WORLD


@asynccontextmanager
async def serve(pg_dsn: str, **kwargs: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A fresh service on a schema of its own, like test_server's fixture but
    parameterisable: the UI's whole security story is what `token` and `ui`
    do together, and a fixture with one fixed configuration cannot test it."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()

    app = create_app(dsn=pg_dsn, pg_schema=name, version="test", **kwargs)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                yield http
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def _teach(http: httpx.AsyncClient, headers: dict[str, str] | None = None) -> None:
    put = await http.put("/schema", json=COURIER_WORLD.to_document(), headers=headers)
    assert put.status_code == 200, put.text
    put = await http.put(
        "/definitions", json={"source": COURIER_SOURCE}, headers=headers
    )
    assert put.status_code == 200, put.text


def _orders(n: int) -> dict[str, dict[str, Any]]:
    return {
        f"o{i}": {"ref": f"A-{i}", "courier_id": "c1", "status": "riding"}
        for i in range(n)
    }


COURIER = {"c1": {"name": "Aki"}}


# ---------------------------------------------------------------- world --


async def test_the_world_payload_lists_every_declaration_with_its_dependencies(
    pg_dsn: str,
) -> None:
    """Groups, filters and measures included: they are the declarations the
    API's own LibraryOut reduces to counts, and the ones an investigator most
    needs -- they are where a definition touches the facts."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        world = (await http.get("/ui/api/world")).json()

        by_name = {d["name"]: d for d in world["declarations"]}
        assert set(by_name) == {
            "shop_order.carried_by",
            "shop_order.open",
            "shop_courier.carrying",
            "shop_courier.load_band",
        }
        assert sorted(world["kinds"]) == ["shop_courier", "shop_order"]

        carried_by = by_name["shop_order.carried_by"]
        assert carried_by["kind"] == "group"
        assert carried_by["version"] is None, (
            "a group has no version of its own -- its text is hashed into "
            "every figure that reads it, and the payload must say so rather "
            "than invent one"
        )
        assert {"type": "fact", "name": "shop_order"} in carried_by["rests_on"]
        assert "from courier_id" in carried_by["source"]

        carrying = by_name["shop_courier.carrying"]
        local = compile_source(COURIER_SOURCE, COURIER_WORLD)
        plan = local.figure("shop_courier.carrying")
        assert plan is not None
        assert carrying["version"] == plan.version, (
            "the version served is the citation every value carries; a payload "
            "hash that drifted from the compiler's would break verification"
        )
        rests = {(d["type"], d["name"]) for d in carrying["rests_on"]}
        assert ("group", "shop_order.carried_by") in rests
        assert ("filter", "shop_order.open") in rests
        assert ("fact", "shop_courier") in rests, (
            "the scope kind is a dependency too: the subjects, the roster the "
            "backfill writes noughts over, and the labels all come from its "
            "records"
        )
        assert "count(mine)" in carrying["source"]
        assert carrying["doc"], "the prose above the declaration travels with it"

        load_band = by_name["shop_courier.load_band"]
        rests = {(d["type"], d["name"]) for d in load_band["rests_on"]}
        assert ("figure", "shop_courier.carrying") in rests
        assert ("setting", "limits.carrying.over") in rests


async def test_dependencies_walk_all_the_way_to_the_original_fact(pg_dsn: str) -> None:
    """The trace the UI draws: from any definition, following rests_on edges
    through the payload alone must reach a fact kind. A payload needing a
    second request per hop, or one whose edges dead-end at a group, would
    make the trace a feature of the server rather than of the data."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        world = (await http.get("/ui/api/world")).json()
        by_name = {d["name"]: d for d in world["declarations"]}

        reached: set[str] = set()
        frontier = ["shop_courier.load_band"]
        walked: set[str] = set()
        while frontier:
            name = frontier.pop()
            if name in walked:
                continue
            walked.add(name)
            for edge in by_name[name]["rests_on"]:
                if edge["type"] == "fact":
                    reached.add(edge["name"])
                elif edge["type"] != "setting":
                    frontier.append(edge["name"])
        assert reached == {"shop_order", "shop_courier"}, (
            "the walk must find both the records the sets read and the kind "
            "the figure is about"
        )


async def test_an_untaught_world_is_a_409_that_names_the_gap(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        refused = await http.get("/ui/api/world")
        assert refused.status_code == 409
        assert "schema" in refused.json()["detail"].lower()


# ---------------------------------------------------------------- facts --


async def test_facts_are_browsable_with_paging_search_and_honest_counts(
    pg_dsn: str,
) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        pushed = await http.post(
            "/tenants/t1/facts", json={"writes": {"shop_order": _orders(3)}}
        )
        assert pushed.status_code == 200, pushed.text

        kinds = (await http.get("/ui/api/tenants/t1/facts")).json()["kinds"]
        counts = {k["kind"]: k["records"] for k in kinds}
        assert counts == {"shop_order": 3, "shop_courier": 0}, (
            "a schema kind nobody has pushed still appears at zero -- absence "
            "of records is a finding, not a missing row"
        )

        page = (await http.get("/ui/api/tenants/t1/facts/shop_order?limit=2")).json()
        assert [r["key"] for r in page["records"]] == ["o0", "o1"]
        assert page["more"] is True
        assert page["total"] == 3
        assert page["records"][0]["name"] == "A-0", (
            "the schema's name_field is applied by the server, not guessed "
            "by the browser"
        )
        assert page["records"][0]["value"]["status"] == "riding"

        rest = (
            await http.get("/ui/api/tenants/t1/facts/shop_order?limit=2&after=o1")
        ).json()
        assert [r["key"] for r in rest["records"]] == ["o2"]
        assert rest["more"] is False
        assert rest["total"] == 3, (
            "the total is the whole match, not the rest of it -- a cursor "
            "must never narrow the population the count describes"
        )

        found = (await http.get("/ui/api/tenants/t1/facts/shop_order?q=A-2")).json()
        assert [r["key"] for r in found["records"]] == ["o2"]
        assert found["total"] == 1

        by_key = (await http.get("/ui/api/tenants/t1/facts/shop_order?q=o1")).json()
        assert [r["key"] for r in by_key["records"]] == ["o1"], (
            "search covers the key as well as the record text"
        )

        # ILIKE's own operators arrive disarmed: A_2 means those three
        # characters, and a lone % is a character nobody stored, not
        # match-everything.
        assert (await http.get("/ui/api/tenants/t1/facts/shop_order?q=A_2")).json()[
            "total"
        ] == 0
        assert (await http.get("/ui/api/tenants/t1/facts/shop_order?q=%25")).json()[
            "total"
        ] == 0

        assert (
            await http.get("/ui/api/tenants/t1/facts/shop_order?limit=0")
        ).status_code == 422
        assert (
            await http.get("/ui/api/tenants/t1/facts/shop_order?limit=300")
        ).status_code == 422


async def test_a_record_without_its_name_field_is_named_nothing(pg_dsn: str) -> None:
    """None, not a KeyError: a record that does not carry the field has no
    name to claim. A record that carries it as a *number* is named by the
    engine's own resolver ("42"), because evidence titles already resolve
    names that way and one record must be named identically on every
    surface -- a list that dashes what the evidence pane titles is two
    answers to one question."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "bare": {"courier_id": "c1", "status": "riding"},
                        "odd": {"ref": 42, "courier_id": "c1", "status": "riding"},
                    }
                }
            },
        )
        page = (await http.get("/ui/api/tenants/t1/facts/shop_order")).json()
        names = {r["key"]: r["name"] for r in page["records"]}
        assert names == {"bare": None, "odd": "42"}


async def test_tenants_are_listed_with_their_fact_counts(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post("/tenants/t1/facts", json={"writes": {"shop_order": _orders(2)}})
        await http.post(
            "/tenants/t2/facts", json={"writes": {"shop_courier": COURIER}}
        )
        # A tenant taught settings but never fed is exactly the
        # misconfiguration an investigator comes looking for.
        await http.put("/tenants/t3/settings", json={"document": {}})
        tenants = (await http.get("/ui/api/tenants")).json()["tenants"]
        assert {(t["tenant"], t["facts"]) for t in tenants} == {
            ("t1", 2),
            ("t2", 1),
            ("t3", 0),
        }


# ------------------------------------------------------------- activity --


async def test_a_new_fact_shows_its_cascade_in_the_activity_log(pg_dsn: str) -> None:
    """The user's question, verbatim: send a new fact, see what it cascaded
    to. The third order crosses the carrying limit, so the log's newest run
    must show BOTH figures moving -- the count it feeds directly and the band
    built on the count."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        first = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
        )
        assert first.status_code == 200, first.text

        third = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"}
                    }
                }
            },
        )
        assert third.status_code == 200, third.text

        page = (await http.get("/ui/api/tenants/t1/activity")).json()
        assert len(page["runs"]) == 2, "one persisted run per pass, newest first"
        newest = page["runs"][0]
        assert newest["trigger"] == "facts"
        assert newest["written"] == 1
        moved = {c["figure"]: c for c in newest["shown"]}
        assert moved["shop_courier.carrying"]["after_display"] == "3"
        assert moved["shop_courier.load_band"]["after_display"] == "over", (
            "the cascade is the point: the fact moved a count, and the band "
            "resting on the count moved with it"
        )
        assert newest["changed"] == len(newest["shown"]), (
            "nothing was evicted here, so the true count and the sample agree"
        )
        assert newest["not_shown"] == 0, (
            "the page states what the sample is missing from a served number, "
            "never from its own subtraction"
        )
        assert page["total"] == 2, "the honest count beside a limit-capped list"

        one = (await http.get("/ui/api/tenants/t1/activity?limit=1")).json()
        assert len(one["runs"]) == 1 and one["total"] == 2, (
            "a smaller page must not shrink the total"
        )

        datetime.fromisoformat(newest["at"])  # a real moment, not just truthy text


async def test_a_deferred_batch_is_listed_rather_than_breaking_the_log(pg_dsn: str) -> None:
    """A bulk import lands its batches with `defer`, and each landing is a
    pass that happened, recorded under its own trigger. The response model
    once admitted only "facts" and "run", so a single deferred batch 500'd
    the whole activity page -- the log of everything, taken down by exactly
    the door big imports come through."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        landed = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER}, "defer": True},
        )
        assert landed.status_code == 200, landed.text

        # quiet=1 because a deferred landing computes nothing by design.
        page = await http.get("/ui/api/tenants/t1/activity?quiet=1")
        assert page.status_code == 200, page.text
        triggers = [run["trigger"] for run in page.json()["runs"]]
        assert "facts-deferred" in triggers


async def test_a_run_that_moved_nothing_is_quiet_but_not_erased(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post("/tenants/t1/facts", json={"writes": {"shop_courier": COURIER}})
        rerun = await http.post("/tenants/t1/runs", json={})
        assert rerun.status_code == 200
        assert rerun.json()["changed"] == 0

        page = (await http.get("/ui/api/tenants/t1/activity")).json()
        assert len(page["runs"]) == 1, "the do-nothing run is hidden by default"
        assert page["quiet_hidden"] == 1, (
            "hidden must be stated -- a filtered list that does not say what "
            "it filtered reads as complete"
        )

        loud_and_quiet = (
            await http.get("/ui/api/tenants/t1/activity?quiet=1")
        ).json()
        assert len(loud_and_quiet["runs"]) == 2
        assert loud_and_quiet["runs"][0]["trigger"] == "run"


async def test_the_run_log_keeps_only_the_newest_rows(
    pg_pool: asyncpg.Pool[Any],
) -> None:
    """Retention is per tenant and per insert, so the table cannot grow without
    bound on a busy deployment. Exercised at keep=2 directly against the db
    module -- driving hundreds of HTTP passes to overflow the real cap would
    test patience, not retention."""
    tenant = f"keep_{os.urandom(4).hex()}"
    bystander = f"{tenant}_bystander"
    await server_db.record_run(
        pg_pool,
        bystander,
        "facts",
        full=False,
        written=9,
        deleted=0,
        changed=0,
        rebuilt=[],
        covered=[],
        shown=[],
        keep=100,
    )
    for n in range(3):
        await server_db.record_run(
            pg_pool,
            tenant,
            "facts",
            full=False,
            written=n,
            deleted=0,
            changed=0,
            rebuilt=[],
            covered=[],
            shown=[],
            keep=2,
        )
    runs, _hidden, total = await server_db.page_runs(
        pg_pool, tenant, limit=10, quiet=True
    )
    assert [r["written"] for r in runs] == [2, 1]
    assert total == 2

    survived, _hidden, _total = await server_db.page_runs(
        pg_pool, bystander, limit=10, quiet=True
    )
    assert [r["written"] for r in survived] == [9], (
        "retention is per tenant: one tenant's busy sync must never reap "
        "another's history"
    )


# ------------------------------------------------------ results/evidence --


async def test_results_and_evidence_are_readable_through_the_ui(pg_dsn: str) -> None:
    """The definition page shows the current answer beside the source, and the
    evidence route says which records a value stands on -- the last hop of the
    trace, from figure to the facts themselves."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
        )

        result = (
            await http.get("/ui/api/tenants/t1/results/shop_courier.carrying")
        ).json()
        assert result["state"]["ok"] is True
        subjects = {s["id"]: s for s in result["subjects"]}
        assert subjects["c1"]["display"] == "2", (
            "display is the server-rendered value; a raw number would invite "
            "the browser to format"
        )

        evidence = (
            await http.get(
                "/ui/api/tenants/t1/evidence/shop_courier.carrying?subject=c1"
            )
        ).json()
        assert {m["key"] for m in evidence["members"]} == {"o0", "o1"}

        missing = await http.get("/ui/api/tenants/t1/results/no.such.figure")
        assert missing.status_code == 404


async def test_the_ui_results_route_speaks_the_same_window_spans_as_the_api(
    pg_dsn: str,
) -> None:
    """The route's own claim is refusal semantics identical to the
    authenticated door. An int-only parameter here would refuse `31-60`
    with a framework validation error the API answers, and wear the reach
    ceiling's refusal as a 400 -- the UI showing a number, or an error, the
    API would not."""
    windowed = COURIER_SOURCE + """
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)

measure shop_order.riding_seconds = delivered_at - picked_up_at

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
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)
"""
    world = COURIER_WORLD.to_document()
    world["bucket_settings"] = ["tenant.timezone"]
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=world)).status_code == 200
        put = await http.put("/definitions", json={"source": windowed})
        assert put.status_code == 200, put.text

        spanned = await http.get(
            "/ui/api/tenants/t1/results/shop_courier.typical_ride",
            params={"trailing": "31-60"},
        )
        assert spanned.status_code == 200, spanned.text
        [window] = spanned.json()["empty"]["windows"]
        assert (window["span"], window["trailing"]) == ("31-60", None)

        for wrong, fragment in (("0-30", "anchor"), ("1000000", "3660"), ("30x", "span")):
            refused = await http.get(
                "/ui/api/tenants/t1/results/shop_courier.typical_ride",
                params={"trailing": wrong},
            )
            assert refused.status_code == 422, f"{wrong!r}: {refused.status_code}"
            assert fragment in refused.text, refused.text


# ------------------------------------------------------------- posture --


async def test_the_ui_is_off_by_default_when_a_token_is_set(pg_dsn: str) -> None:
    """The UI is unauthenticated by design; the API's token is the operator
    saying this deployment is NOT open. Mounting the UI anyway would hand
    every fact and figure to anyone who can reach the port, so the default
    must flip with the token and only an explicit `ui` turn it back on."""
    async with serve(pg_dsn, token="secret") as http:
        assert (await http.get("/ui/")).status_code == 404
        assert (await http.get("/ui/api/world")).status_code == 404


async def test_the_ui_can_be_enabled_beside_a_token_and_needs_no_auth(
    pg_dsn: str,
) -> None:
    async with serve(pg_dsn, token="secret", ui=True) as http:
        bearer = {"Authorization": "Bearer secret"}
        await _teach(http, headers=bearer)

        world = await http.get("/ui/api/world")
        assert world.status_code == 200, (
            "the UI's door is the firewall, not the token -- an operator who "
            "enabled it beside a token chose that split deliberately"
        )
        assert (await http.get("/ui/")).status_code == 200

        refused = await http.get("/definitions")
        assert refused.status_code == 401, (
            "enabling the UI must not have unlocked the API itself"
        )


async def test_the_ui_serves_its_page_with_the_frame_ancestors_it_was_given(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Embedding is a permission the operator grants per deployment: default
    'self' (nobody may iframe it but itself), overridable with the embedding
    host's origin. The header rides the HTML because frame-ancestors governs
    the document being framed."""
    async with serve(pg_dsn) as http:
        page = await http.get("/ui/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert (
            page.headers["content-security-policy"] == "frame-ancestors 'self'"
        )

        script = await http.get("/ui/app.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        assert (await http.get("/ui/style.css")).status_code == 200
        assert (await http.get("/ui/no-such-file.js")).status_code == 404
        assert (await http.get("/ui/index.html")).status_code == 404, (
            "an allowlist, not a directory mount: index.html exists on disk "
            "beside the assets, and only /ui/ may serve it (with its CSP); a "
            "directory mount would serve whatever lands in the folder"
        )

        bare = await http.get("/ui", follow_redirects=False)
        assert bare.status_code in (301, 307, 308)
        # Relative on purpose: an absolute /ui/ would break the page the
        # moment a reverse proxy serves it under a sub-path.
        assert bare.headers["location"] == "ui/"

    async with serve(pg_dsn, frame_ancestors="https://urazuke.com") as http:
        page = await http.get("/ui/")
        assert (
            page.headers["content-security-policy"]
            == "frame-ancestors https://urazuke.com"
        )

    # The environment path, which the kwarg above short-circuits.
    monkeypatch.setenv("URATORI_UI_FRAME_ANCESTORS", "https://env.example")
    async with serve(pg_dsn) as http:
        page = await http.get("/ui/")
        assert (
            page.headers["content-security-policy"]
            == "frame-ancestors https://env.example"
        )


# ------------------------------------------------------ the full library --

# Every declaration kind and every grouping spec the language has, so the
# world payload's dependency edges are pinned where they actually vary: a
# composite group hopping `through` another kind and bucketing `by day` in a
# zone dial, an age filter reading a threshold, measures, a windowed and a
# live reading, a projection and its summary. The courier corpus alone cannot
# do this -- it holds two groupings and two figures, and a payload whose
# contract is "every declaration of every kind" needs a corpus that has them.
FULL_SOURCE = """
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
group code_review_request.asked_of from reviewer_account_id through team_person.accounts.account_id
filter code_review_request.pending where pending == true
filter code_change.stale where updated_at older than thresholds.staleChangeDays

measure code_change.open_seconds = merged_at - created_at
measure code_review_request.waiting_seconds = now - requested_at

# Every merge's duration.
figure team_person.time_to_merge:
    display "{team_person} time to merge"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)

# The typical merge, over a window.
reading team_person.lead_time(range):
    display "{value}"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

# What is waiting right now.
reading team_person.queue():
    display "{value}"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)

# One row per issue.
projection work_issue.card:
    field:
        key = title as text

# The backlog, in one row.
summarise work_issue.backlog over work_issue.card:
    count items
"""


async def test_every_declaration_kind_travels_with_its_own_edges(pg_dsn: str) -> None:
    from .world import WORLD

    async with serve(pg_dsn) as http:
        put = await http.put("/schema", json=WORLD.to_document())
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": FULL_SOURCE})
        assert put.status_code == 200, put.text

        world = (await http.get("/ui/api/world")).json()
        by_name = {d["name"]: d for d in world["declarations"]}
        kinds = {d["name"]: d["kind"] for d in world["declarations"]}
        assert kinds == {
            "code_change.merged_by_day": "group",
            "code_review_request.asked_of": "group",
            "code_review_request.pending": "filter",
            "code_change.stale": "filter",
            "code_change.open_seconds": "measure",
            "code_review_request.waiting_seconds": "measure",
            "team_person.time_to_merge": "figure",
            "team_person.lead_time": "reading",
            "team_person.queue": "reading",
            "work_issue.card": "projection",
            "work_issue.backlog": "summary",
        }

        def rests(name: str) -> set[tuple[str, str]]:
            return {(d["type"], d["name"]) for d in by_name[name]["rests_on"]}

        # A composite group: its own kind, the kind it hops through, and the
        # zone dial that decides which day a bucket is.
        assert rests("code_change.merged_by_day") == {
            ("fact", "code_change"),
            ("fact", "team_person"),
            ("setting", "tenant.timezone"),
        }
        # An age filter reads a threshold dial.
        assert rests("code_change.stale") == {
            ("fact", "code_change"),
            ("setting", "thresholds.staleChangeDays"),
        }
        # A measure rests on the records it measures, and nothing else.
        assert rests("code_change.open_seconds") == {("fact", "code_change")}
        assert by_name["code_change.open_seconds"]["version"] is None

        # A windowed reading: its scope kind, and the figure whose days it
        # summarises.
        assert ("figure", "team_person.time_to_merge") in rests("team_person.lead_time")
        assert ("fact", "team_person") in rests("team_person.lead_time")
        assert by_name["team_person.lead_time"]["mode"] == "window"

        # A live reading: the measure it reads, and the group and filter
        # that scope it -- each edge typed by its own declaration keyword.
        live = rests("team_person.queue")
        assert ("measure", "code_review_request.waiting_seconds") in live
        assert ("group", "code_review_request.asked_of") in live
        assert ("filter", "code_review_request.pending") in live
        assert by_name["team_person.queue"]["mode"] == "live"

        # A projection rests on the kind whose records are its rows; its
        # summary rests on the projection.
        assert ("fact", "work_issue") in rests("work_issue.card")
        assert rests("work_issue.backlog") == {("projection", "work_issue.card")}

        # And the walk from the deepest declaration still bottoms out on
        # facts alone, through every hop.
        reached: set[str] = set()
        frontier, walked = ["team_person.lead_time"], set()
        while frontier:
            name = frontier.pop()
            if name in walked:
                continue
            walked.add(name)
            for edge in by_name[name]["rests_on"]:
                if edge["type"] == "fact":
                    reached.add(edge["name"])
                elif edge["type"] != "setting":
                    frontier.append(edge["name"])
        assert reached == {"code_change", "team_person"}


# ------------------------------------------------------------- moved by --


async def test_moved_by_is_the_closure_to_leaves(pg_dsn: str) -> None:
    """The question a reader brings to a definition page is "if data changes,
    does this number move?" -- and the recursive rests_on tree answers it only
    after a walk the reader has to do themselves. `moved_by` is that walk done
    by the server: the transitive closure of every declaration's dependencies,
    reduced to the leaves (fact kinds and settings). Nothing else can move the
    number, and the payload must be entitled to say so."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        world = (await http.get("/ui/api/world")).json()
        by_name = {d["name"]: d for d in world["declarations"]}

        def moved(name: str) -> set[tuple[str, str]]:
            return {(d["type"], d["name"]) for d in by_name[name]["moved_by"]}

        # load_band's direct rests_on names only the figure it combines and
        # its own dial; the closure must reach through the figure to both
        # fact kinds without listing the figure itself.
        assert moved("shop_courier.load_band") == {
            ("fact", "shop_order"),
            ("fact", "shop_courier"),
            ("setting", "limits.carrying.over"),
        }
        assert moved("shop_courier.carrying") == {
            ("fact", "shop_order"),
            ("fact", "shop_courier"),
        }
        assert moved("shop_order.carried_by") == {("fact", "shop_order")}

        for declaration in world["declarations"]:
            assert all(
                d["type"] in ("fact", "setting") for d in declaration["moved_by"]
            ), (
                f"{declaration['name']}: moved_by is the impact answer, and an "
                "intermediate declaration in it would re-open the walk it exists "
                "to close"
            )


async def test_moved_by_carries_settings_found_deep_in_the_chain(pg_dsn: str) -> None:
    """A reading windowing a figure over a zone-bucketed group is moved by the
    zone dial three hops down. The closure must surface it, or 'nothing else
    can move this number' becomes false exactly where it is hardest to see."""
    from .world import WORLD

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=WORLD.to_document())).status_code == 200
        assert (
            await http.put("/definitions", json={"source": FULL_SOURCE})
        ).status_code == 200
        world = (await http.get("/ui/api/world")).json()
        by_name = {d["name"]: d for d in world["declarations"]}
        lead_time = {
            (d["type"], d["name"]) for d in by_name["team_person.lead_time"]["moved_by"]
        }
        assert ("setting", "tenant.timezone") in lead_time, (
            "the zone dial lives on the group under the figure under the reading"
        )
        assert ("fact", "code_change") in lead_time
        assert ("fact", "team_person") in lead_time


# ----------------------------------------------------------- membership --


async def _feed_couriers(http: httpx.AsyncClient) -> None:
    pushed = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": COURIER,
                "shop_order": {
                    "o0": {"ref": "A-0", "courier_id": "c1", "status": "riding"},
                    "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"},
                    "o2": {"ref": "A-2", "courier_id": "c1", "status": "delivered"},
                },
            }
        },
    )
    assert pushed.status_code == 200, pushed.text


async def test_a_filter_shows_the_records_it_matches(pg_dsn: str) -> None:
    """Clicking a filter must answer "which records pass?" with the records --
    the stored membership the engine actually computed with, joined back to
    names, beside the honest 'N of M' so a filter matching everything or
    nothing is visible at a glance."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _feed_couriers(http)

        held = (await http.get("/ui/api/tenants/t1/membership/shop_order.open")).json()
        assert held["kind"] == "filter"
        assert held["fact_kind"] == "shop_order"
        assert held["state"]["ok"] is True
        assert held["members"] == 2, "the delivered order must not be in"
        assert held["population"] == 3, (
            "the N-of-M line needs the M: without it a filter matching "
            "everything and a filter matching one record read the same"
        )

        page = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.open/members?bucket="
            )
        ).json()
        assert {(r["key"], r["name"]) for r in page["records"]} == {
            ("o0", "A-0"),
            ("o1", "A-1"),
        }
        assert all(r["held"] for r in page["records"])
        assert page["more"] is False and page["total"] == 2

        first = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.open/members?bucket=&limit=1"
            )
        ).json()
        assert len(first["records"]) == 1
        assert first["more"] is True
        assert first["total"] == 2, "a page must never shrink the population it reports"

        rest = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.open/members"
                f"?bucket=&limit=1&after={first['records'][0]['key']}"
            )
        ).json()
        assert len(rest["records"]) == 1
        assert rest["records"][0]["key"] != first["records"][0]["key"]


async def test_a_group_shows_its_buckets_with_counts(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _feed_couriers(http)
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "o3": {"ref": "B-0", "courier_id": "c2", "status": "riding"}
                    }
                }
            },
        )

        held = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.carried_by")
        ).json()
        assert held["kind"] == "group"
        assert held["state"]["ok"] is True
        assert {(b["bucket"], b["members"]) for b in held["buckets"]} == {
            ("c1", 3),
            ("c2", 1),
        }
        assert held["members"] == 4
        assert held["buckets_total"] == 2

        c1 = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.carried_by/members?bucket=c1"
            )
        ).json()
        assert {r["key"] for r in c1["records"]} == {"o0", "o1", "o2"}


async def test_membership_before_any_run_is_an_absence_not_a_zero(
    pg_dsn: str,
) -> None:
    """A tenant the engine has never bucketed has no membership to report, and
    'no records match' would be a fabricated zero -- the exact lie rule three
    exists to prevent. The same honesty applies when the definitions moved
    after the last pass: the stored rows describe the old text."""
    async with serve(pg_dsn) as http:
        await _teach(http)

        never = (
            await http.get("/ui/api/tenants/nobody/membership/shop_order.open")
        ).json()
        assert never["state"]["ok"] is False
        assert never["state"]["because"] == "never-computed"
        assert never["members"] == 0 and never["buckets"] == [], (
            "counts under a not-ok state must not carry numbers a reader "
            "could mistake for an answer"
        )

        await _feed_couriers(http)
        ok = (await http.get("/ui/api/tenants/t1/membership/shop_order.open")).json()
        assert ok["state"]["ok"] is True

        # The definitions move (the filter flips), and no pass has run since:
        # the stored membership describes the old filter, and serving it as
        # current would show records "matching" a predicate they do not.
        moved = COURIER_SOURCE.replace('status != "delivered"', 'status == "delivered"')
        put = await http.put("/definitions", json={"source": moved})
        assert put.status_code == 200, put.text
        stale = (await http.get("/ui/api/tenants/t1/membership/shop_order.open")).json()
        assert stale["state"]["ok"] is False
        assert stale["state"]["because"] == "behind-deploy"


async def test_membership_of_a_non_grouping_is_a_404_with_a_forwarding_address(
    pg_dsn: str,
) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        figure = await http.get("/ui/api/tenants/t1/membership/shop_courier.carrying")
        assert figure.status_code == 404
        assert "figure" in figure.json()["detail"], (
            "'no' with no forwarding address dead-ends the reader; the detail "
            "must say where that declaration's data actually lives"
        )
        missing = await http.get("/ui/api/tenants/t1/membership/no.such.thing")
        assert missing.status_code == 404


# ------------------------------------------------------------- measured --


async def test_a_measure_shows_each_records_measurement(pg_dsn: str) -> None:
    """Clicking a measure must answer "what does this read off my records?"
    record by record, rendered by the server -- and a record the measure has
    nothing for shows an absence, never a nought."""
    from .world import WORLD

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=WORLD.to_document())).status_code == 200
        assert (
            await http.put("/definitions", json={"source": FULL_SOURCE})
        ).status_code == 200
        pushed = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "code_change": {
                        "mr1": {
                            "title": "Fix the thing",
                            "created_at": "2026-01-01T00:00:00Z",
                            "merged_at": "2026-01-01T02:00:00Z",
                            "author_account_id": "acc1",
                            "updated_at": "2026-01-01T02:00:00Z",
                        },
                        "mr2": {
                            "title": "Still open",
                            "created_at": "2026-01-01T00:00:00Z",
                            "author_account_id": "acc1",
                            "updated_at": "2026-01-01T00:00:00Z",
                        },
                    }
                }
            },
        )
        assert pushed.status_code == 200, pushed.text

        page = (
            await http.get("/ui/api/tenants/t1/measured/code_change.open_seconds")
        ).json()
        assert page["fact_kind"] == "code_change"
        by_key = {r["key"]: r for r in page["records"]}
        assert by_key["mr1"]["display"] == "2.0h", (
            "the measurement arrives rendered -- a raw 7200 would invite the "
            "browser to divide"
        )
        assert by_key["mr1"]["name"] == "Fix the thing"
        assert by_key["mr2"]["display"] is None, (
            "an unmerged change has no open-duration; a rendered '0s' here "
            "would be a fabricated measurement"
        )
        assert page["total"] == 2

        clock = (
            await http.get(
                "/ui/api/tenants/t1/measured/code_review_request.waiting_seconds"
            )
        ).json()
        assert clock["records"] == [] and clock["total"] == 0, (
            "a clock measure over an empty kind lists nothing, but must not 500 "
            "on the missing instant"
        )

        not_a_measure = await http.get(
            "/ui/api/tenants/t1/measured/team_person.time_to_merge"
        )
        assert not_a_measure.status_code == 404


# ------------------------------------------------------------ the record --


async def test_a_record_page_states_its_classification(pg_dsn: str) -> None:
    """The leaf of every trace: one record, everything stored about it, and
    where every grouping over its kind filed it -- including the ones that
    did NOT take it, because "not a member" is a finding a verification
    surface must state rather than leave to inference."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _feed_couriers(http)

        riding = (await http.get("/ui/api/tenants/t1/facts/shop_order/o0")).json()
        assert riding["kind"] == "shop_order" and riding["key"] == "o0"
        assert riding["name"] == "A-0"
        assert riding["value"]["status"] == "riding"
        filed = {f["index"]: f for f in riding["filed"]}
        assert filed["shop_order.open"]["member"] is True
        assert filed["shop_order.carried_by"]["member"] is True
        assert filed["shop_order.carried_by"]["buckets"] == ["c1"]
        assert riding["filed_state"]["ok"] is True

        delivered = (await http.get("/ui/api/tenants/t1/facts/shop_order/o2")).json()
        filed = {f["index"]: f for f in delivered["filed"]}
        assert filed["shop_order.open"]["member"] is False, (
            "the filter that rejected this record must say so"
        )
        assert filed["shop_order.open"]["buckets"] == []
        assert filed["shop_order.carried_by"]["member"] is True

        courier = (await http.get("/ui/api/tenants/t1/facts/shop_courier/c1")).json()
        assert courier["filed"] == [], (
            "no grouping is keyed by shop_courier ids in this library, and an "
            "empty classification must be an empty list, not an invented row"
        )

        missing = await http.get("/ui/api/tenants/t1/facts/shop_order/nope")
        assert missing.status_code == 404


async def test_a_record_page_carries_its_measurements(pg_dsn: str) -> None:
    from .world import WORLD

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=WORLD.to_document())).status_code == 200
        assert (
            await http.put("/definitions", json={"source": FULL_SOURCE})
        ).status_code == 200
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "code_change": {
                        "mr1": {
                            "title": "Fix the thing",
                            "url": "https://git.example/mr1",
                            "created_at": "2026-01-01T00:00:00Z",
                            "merged_at": "2026-01-01T02:00:00Z",
                            "author_account_id": "acc1",
                            "updated_at": "2026-01-01T02:00:00Z",
                        }
                    }
                }
            },
        )
        record = (await http.get("/ui/api/tenants/t1/facts/code_change/mr1")).json()
        assert record["url"] == "https://git.example/mr1", (
            "the schema's url_field resolves here for the same reason name "
            "does: the browser must not guess which field is the link"
        )
        measured = {m["measure"]: m["display"] for m in record["measured"]}
        assert measured == {"code_change.open_seconds": "2.0h"}, (
            "every measure over this record's kind reports, and only those -- "
            "a review-request measure has nothing to say about a change"
        )


async def test_the_drill_reads_the_taught_schema_not_the_stored_one(
    pg_dsn: str,
) -> None:
    """A fact-taught world (0.4.0) carries its name and url fields in the
    .fig, not in the stored schema document. The drill surfaces must read
    through the same completion the world route does, or every record on
    them is nameless while the Facts tab two clicks away names it fine."""
    from .test_facts import SOURCE, TAUGHT

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=TAUGHT.to_document())).status_code == 200
        put = await http.put(
            "/definitions",
            json={
                "source": SOURCE
                + "\nmeasure shop_order.wait_seconds = delivered_at - placed_at\n"
            },
        )
        assert put.status_code == 200, put.text
        pushed = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": {"c1": {"display_name": "Aki"}},
                    "shop_order": {
                        "o0": {
                            "ref": "A-0",
                            "link": "https://shop.example/o0",
                            "courier_id": "c1",
                            "status": "riding",
                        }
                    },
                }
            },
        )
        assert pushed.status_code == 200, pushed.text

        record = (await http.get("/ui/api/tenants/t1/facts/shop_order/o0")).json()
        assert record["name"] == "A-0"
        assert record["url"] == "https://shop.example/o0"

        members = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.open/members?bucket="
            )
        ).json()
        assert [r["name"] for r in members["records"]] == ["A-0"]

        # The measured page reads the same taught completion.
        measured = (
            await http.get("/ui/api/tenants/t1/measured/shop_order.wait_seconds")
        ).json()
        assert [r["name"] for r in measured["records"]] == ["A-0"]

        # A fact declaration IS a leaf: its impact answer is deliberately
        # empty, and the page words that differently rather than printing
        # "nothing moves this" about the records everything moves with.
        world = (await http.get("/ui/api/world")).json()
        for declaration in world["declarations"]:
            if declaration["kind"] == "fact":
                assert declaration["moved_by"] == []


# ------------------------------------------------------ the drill's edges --


async def test_keyed_as_membership_resolves_in_the_id_space(pg_dsn: str) -> None:
    """`keyed as` files one kind's records under another kind's ids. The
    members page must join the id space (or every member reads missing and
    unnamed), the ghost half must stay listed (`held: false` with the total
    still counting it -- hiding a membership row would un-say the engine's
    claim), and the record pages on BOTH sides must classify honestly."""
    from uratori import Schema

    world = Schema(
        kinds=frozenset({"shop_order", "shop_review"}),
        name_fields={"shop_order": "ref", "shop_review": "note"},
    )
    source = 'filter shop_review.signed_off keyed as shop_order where approved == true'
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=world.to_document())).status_code == 200
        put = await http.put("/definitions", json={"source": source})
        assert put.status_code == 200, put.text
        pushed = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {"o0": {"ref": "A-0"}},
                    "shop_review": {
                        "o0": {"note": "looks fine", "approved": True},
                        "o9": {"note": "orphan", "approved": True},
                    },
                }
            },
        )
        assert pushed.status_code == 200, pushed.text

        held = (
            await http.get("/ui/api/tenants/t1/membership/shop_review.signed_off")
        ).json()
        assert held["fact_kind"] == "shop_review"
        assert held["id_space"] == "shop_order"
        assert held["members"] == 2
        assert held["population"] == 1, "the population is the id space's records"

        page = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_review.signed_off/members?bucket="
            )
        ).json()
        rows = {r["key"]: r for r in page["records"]}
        assert rows["o0"]["name"] == "A-0", (
            "member names come from the id space's table -- joining the fact "
            "kind's would mark every member missing"
        )
        assert rows["o0"]["held"] is True
        assert rows["o9"]["held"] is False and rows["o9"]["name"] is None
        assert page["total"] == 2, "the ghost stays counted as well as listed"

        order = (await http.get("/ui/api/tenants/t1/facts/shop_order/o0")).json()
        filed = {f["index"]: f for f in order["filed"]}
        assert filed["shop_review.signed_off"]["member"] is True, (
            "the id-space record is what the membership is keyed by"
        )
        review = (await http.get("/ui/api/tenants/t1/facts/shop_review/o0")).json()
        assert review["filed"] == [], (
            "no grouping is keyed by shop_review ids; claiming membership "
            "here would classify under ids the index never used"
        )


async def test_member_pages_and_the_record_page_refuse_a_stale_filing(
    pg_dsn: str,
) -> None:
    """Rule three reaches the pages, not just the summary: a members page for
    a tenant never bucketed is a 409, and after the definitions move, both
    the members page and the record page's classification are withheld with
    the reason -- serving the old filing as current would show records
    matching a predicate they do not."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        never = await http.get(
            "/ui/api/tenants/nobody/membership/shop_order.open/members?bucket="
        )
        assert never.status_code == 409
        assert "bucketed" in never.json()["detail"]

        await _feed_couriers(http)
        moved = COURIER_SOURCE.replace('status != "delivered"', 'status == "delivered"')
        assert (
            await http.put("/definitions", json={"source": moved})
        ).status_code == 200

        stale = await http.get(
            "/ui/api/tenants/t1/membership/shop_order.open/members?bucket="
        )
        assert stale.status_code == 409
        assert "moved" in stale.json()["detail"]

        record = (await http.get("/ui/api/tenants/t1/facts/shop_order/o0")).json()
        assert record["filed_state"]["ok"] is False
        assert record["filed_state"]["because"] == "behind-deploy"
        assert record["filed"] == [], (
            "a classification served beside a not-ok state would be read "
            "instead of it"
        )


async def test_a_clock_measure_renders_and_shares_one_instant(pg_dsn: str) -> None:
    """`now - x` must actually render (the earlier empty-kind assertion never
    reached measure_of), and two identical records must render identically --
    the cheap purchase on the one-instant-per-page claim."""
    from datetime import UTC, datetime, timedelta

    from .world import WORLD

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=WORLD.to_document())).status_code == 200
        assert (
            await http.put("/definitions", json={"source": FULL_SOURCE})
        ).status_code == 200
        asked = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "code_review_request": {
                        "r1": {"title": "first", "requested_at": asked},
                        "r2": {"title": "second", "requested_at": asked},
                    }
                }
            },
        )
        page = (
            await http.get(
                "/ui/api/tenants/t1/measured/code_review_request.waiting_seconds"
            )
        ).json()
        displays = [r["display"] for r in page["records"]]
        assert displays == ["2.0h", "2.0h"], (
            "a clock measurement must render, and identically for identical "
            "records -- a per-record clock would let the two waits disagree"
        )


async def test_measured_pages_honestly(pg_dsn: str) -> None:
    from .world import WORLD

    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=WORLD.to_document())).status_code == 200
        assert (
            await http.put("/definitions", json={"source": FULL_SOURCE})
        ).status_code == 200
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "code_change": {
                        f"mr{i}": {
                            "title": f"change {i}",
                            "created_at": "2026-01-01T00:00:00Z",
                            "merged_at": "2026-01-01T01:00:00Z",
                            "author_account_id": "acc1",
                            "updated_at": "2026-01-01T01:00:00Z",
                        }
                        for i in range(3)
                    }
                }
            },
        )
        first = (
            await http.get("/ui/api/tenants/t1/measured/code_change.open_seconds?limit=2")
        ).json()
        assert len(first["records"]) == 2
        assert first["more"] is True
        assert first["total"] == 3, "a page must never shrink the population it reports"

        rest = (
            await http.get(
                "/ui/api/tenants/t1/measured/code_change.open_seconds"
                f"?limit=2&after={first['records'][-1]['key']}"
            )
        ).json()
        assert [r["key"] for r in rest["records"]] == ["mr2"]
        assert rest["more"] is False and rest["total"] == 3

        found = (
            await http.get("/ui/api/tenants/t1/measured/code_change.open_seconds?q=change 1")
        ).json()
        assert [r["key"] for r in found["records"]] == ["mr1"]
        assert found["total"] == 1, "a search's total is the number of hits"


async def test_a_fanned_out_member_counts_once_and_buckets_page(pg_dsn: str) -> None:
    """One record in two buckets is one member (the distinct count is the
    claim), and the bucket list pages like everything else -- a group can
    hold thousands of buckets, and a capped list with no way forward makes
    most of them unreachable."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        put = await http.put(
            "/definitions",
            json={"source": COURIER_SOURCE + "\ngroup shop_order.by_tag from tags\n"},
        )
        assert put.status_code == 200, put.text
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "o0": {
                            "ref": "A-0",
                            "courier_id": "c1",
                            "status": "riding",
                            "tags": ["blue", "red"],
                        }
                    }
                }
            },
        )
        held = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.by_tag")
        ).json()
        assert held["members"] == 1, "two buckets, one record: distinct, not doubled"
        assert held["buckets_total"] == 2

        first = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.by_tag?buckets_limit=1"
            )
        ).json()
        assert [b["bucket"] for b in first["buckets"]] == ["blue"]
        assert first["buckets_more"] is True
        assert first["buckets_total"] == 2, "the cap must not shrink the total"

        rest = (
            await http.get(
                "/ui/api/tenants/t1/membership/shop_order.by_tag"
                "?buckets_limit=1&buckets_after=blue"
            )
        ).json()
        assert [b["bucket"] for b in rest["buckets"]] == ["red"]
        assert rest["buckets_more"] is False


async def test_effort_rendering_reads_the_dial_and_names_it_in_moved_by(
    pg_dsn: str,
) -> None:
    """`format_value` divides an effort by tenant.hoursPerDay at render time,
    so the dial appears in no compiled plan -- and a closure built only from
    plan edges would let the page claim 'nothing else can move this' about a
    number whose text moves the moment the dial does."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        put = await http.put(
            "/definitions",
            json={
                "source": COURIER_SOURCE
                + "\nmeasure shop_order.spent = work_seconds in effort\n"
            },
        )
        assert put.status_code == 200, put.text
        world = (await http.get("/ui/api/world")).json()
        spent = next(d for d in world["declarations"] if d["name"] == "shop_order.spent")
        assert {"type": "setting", "name": "tenant.hoursPerDay"} in spent["moved_by"]

        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "o0": {
                            "ref": "A-0",
                            "courier_id": "c1",
                            "status": "riding",
                            "work_seconds": 28_800,
                        }
                    }
                }
            },
        )
        page = (await http.get("/ui/api/tenants/t1/measured/shop_order.spent")).json()
        assert page["records"][0]["display"] == "1.0d", "28,800s at 8h/day is one day"

        await http.put(
            "/tenants/t1/settings",
            json={"document": {"tenant": {"hoursPerDay": 4}}},
        )
        moved = (await http.get("/ui/api/tenants/t1/measured/shop_order.spent")).json()
        assert moved["records"][0]["display"] == "2.0d", (
            "the dial is read at render time -- which is exactly why it must "
            "appear in moved_by"
        )


async def test_an_effort_measure_without_the_dial_is_a_409_naming_it(
    pg_dsn: str,
) -> None:
    """The checker never requires the dial, so this misconfiguration compiles
    and only fails at render time. It must fail as the raiser's own sentence
    naming the dial, not as a 500 the operator has to go digging for."""
    from uratori import Schema

    bare = Schema(kinds=frozenset({"shop_order"}), name_fields={"shop_order": "ref"})
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=bare.to_document())).status_code == 200
        put = await http.put(
            "/definitions",
            json={"source": "measure shop_order.spent = work_seconds in effort"},
        )
        assert put.status_code == 200, put.text
        await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_order": {"o0": {"ref": "A-0", "work_seconds": 60}}}},
        )
        refused = await http.get("/ui/api/tenants/t1/measured/shop_order.spent")
        assert refused.status_code == 409
        assert "hoursPerDay" in refused.json()["detail"]

        record = await http.get("/ui/api/tenants/t1/facts/shop_order/o0")
        assert record.status_code == 409
        assert "hoursPerDay" in record.json()["detail"]


async def test_a_dotted_name_field_names_records_on_every_surface(
    pg_dsn: str,
) -> None:
    """A name field is a path in the schema's own terms. The list and the
    record page share one resolver now, so both must name a nested field --
    two surfaces disagreeing about one record's name is two answers to one
    question."""
    from uratori import Schema

    nested = Schema(
        kinds=frozenset({"shop_order"}), name_fields={"shop_order": "dropoff.street"}
    )
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=nested.to_document())).status_code == 200
        assert (
            await http.put(
                "/definitions", json={"source": 'filter shop_order.any where x != "y"'}
            )
        ).status_code == 200
        await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_order": {"o0": {"dropoff": {"street": "Elm"}}}}},
        )
        listed = (await http.get("/ui/api/tenants/t1/facts/shop_order")).json()
        assert listed["records"][0]["name"] == "Elm"
        record = (await http.get("/ui/api/tenants/t1/facts/shop_order/o0")).json()
        assert record["name"] == "Elm"


async def test_membership_states_its_dial_caveat(pg_dsn: str) -> None:
    """An age filter's filing depends on a dial the index-set hash cannot
    see, so a moved dial with no pass since is invisible to `state`. The
    response says so itself -- the weaker guarantee stated beats an Ok it
    has not earned. A predicate filter carries no such caveat."""
    from uratori import Schema

    world = Schema(
        kinds=frozenset({"shop_order"}),
        bucket_settings=("thresholds.staleDays",),
        defaults={"thresholds": {"staleDays": 3}},
    )
    source = (
        "filter shop_order.stale where placed_at older than thresholds.staleDays\n"
        'filter shop_order.open where status != "delivered"\n'
    )
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=world.to_document())).status_code == 200
        put = await http.put("/definitions", json={"source": source})
        assert put.status_code == 200, put.text
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {
                        "o0": {"placed_at": "2020-01-01T00:00:00Z", "status": "riding"}
                    }
                }
            },
        )
        aged = (await http.get("/ui/api/tenants/t1/membership/shop_order.stale")).json()
        assert aged["note"] is not None and "thresholds.staleDays" in aged["note"]
        plain = (await http.get("/ui/api/tenants/t1/membership/shop_order.open")).json()
        assert plain["note"] is None


async def test_measured_of_a_grouping_forwards_to_membership(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        await _teach(http)
        refused = await http.get("/ui/api/tenants/t1/measured/shop_order.open")
        assert refused.status_code == 404
        assert "membership" in refused.json()["detail"], (
            "'no' with no forwarding address dead-ends the reader"
        )


async def test_a_key_with_a_slash_reaches_its_record_page(pg_dsn: str) -> None:
    """Provider keys are the provider's business -- GitLab writes `grp/proj!7`
    shapes -- and the route must resolve a percent-encoded slash rather than
    404 on half the key."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_order": {"grp/proj!7": {"ref": "A-7", "status": "riding"}}
                }
            },
        )
        record = await http.get("/ui/api/tenants/t1/facts/shop_order/grp%2Fproj!7")
        assert record.status_code == 200, record.text
        assert record.json()["key"] == "grp/proj!7"
        assert record.json()["name"] == "A-7"


# ----------------------------------------------------- the log's history --


async def test_deletions_and_full_passes_are_logged_as_what_they_were(
    pg_dsn: str,
) -> None:
    """A delete-only batch is loud (a departure is a cause, not a quiet run)
    and its row says deleted=1; a forced rebuild says full=true. These are
    the fields an investigator filters the log by, and the untested half of
    the loud predicate."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": _orders(1)}},
        )
        gone = await http.post(
            "/tenants/t1/facts", json={"deletes": {"shop_order": ["o0"]}}
        )
        assert gone.status_code == 200, gone.text
        rebuilt = await http.post("/tenants/t1/runs", json={"full": True})
        assert rebuilt.status_code == 200

        page = (await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()
        by_trigger = [(r["trigger"], r["full"], r["written"], r["deleted"]) for r in page["runs"]]
        assert by_trigger == [
            ("run", True, 0, 0),
            ("facts", False, 0, 1),
            ("facts", False, 2, 0),
        ]

        loud_only = (await http.get("/ui/api/tenants/t1/activity")).json()
        assert any(r["deleted"] == 1 for r in loud_only["runs"]), (
            "a delete-only pass must be loud -- hiding departures behind "
            "quiet is the origin project's worst activity bug"
        )


async def test_removing_a_tenant_removes_its_history_too(pg_dsn: str) -> None:
    """A recreated tenant must start with an empty log: inheriting the old
    tenant's cascade history would attribute another world's movements to
    this one."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await http.post("/tenants/t1/facts", json={"writes": {"shop_courier": COURIER}})
        assert len((await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()["runs"]) == 1

        removed = await http.delete("/tenants/t1")
        assert removed.status_code == 200

        page = (await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()
        assert page["runs"] == [] and page["total"] == 0


async def test_the_log_and_the_refusal_survive_a_restart(pg_dsn: str) -> None:
    """Two claims, one boot cycle. The run log is Postgres, not process
    memory -- a restarted container still answers what last week's fact
    cascaded to. And a stored source this build's compiler refuses leaves a
    world whose payload says why the library is empty, rather than an
    inexplicably bare page."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)
                await http.post(
                    "/tenants/t1/facts", json={"writes": {"shop_courier": COURIER}}
                )

        # Between the two boots, the stored source rots (an upgrade across a
        # language change, simulated the direct way).
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(
                f"update {name}.engine_world set source = 'this is not a definition'"
            )
        finally:
            await connection.close()

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                page = (await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()
                assert len(page["runs"]) == 1, (
                    "the log is the database's memory, not the process's"
                )

                world = (await http.get("/ui/api/world")).json()
                assert world["declarations"] == []
                assert world["refusal"], (
                    "a stored-but-refused source must say so on the page"
                )
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


# -------------------------------------------------------- the environment --


def test_uratori_ui_env_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment path the kwargs short-circuit everywhere else: the
    documented spellings parse, empty means unset (a compose file's
    `URATORI_UI=` must not fail the boot), and garbage refuses to boot
    rather than silently meaning the default -- the sentence the docs sell
    as a security property."""

    def paths(router: Any) -> set[str]:
        # This FastAPI keeps an included router nested rather than flattening
        # its routes, so the probe walks both shapes.
        found: set[str] = set()
        for route in router.routes:
            path = getattr(route, "path", None)
            if path is not None:
                found.add(path)
            nested = getattr(route, "original_router", None)
            if nested is not None:
                found |= paths(nested)
        return found

    def mounted(app: Any) -> bool:
        return "/ui/" in paths(app.router)

    monkeypatch.delenv("URATORI_UI", raising=False)
    assert mounted(create_app(dsn="postgres://unused"))
    assert not mounted(create_app(dsn="postgres://unused", token="secret"))

    for yes in ("on", "TRUE", "1", "yes"):
        monkeypatch.setenv("URATORI_UI", yes)
        assert mounted(create_app(dsn="postgres://unused", token="secret")), yes
    for no in ("off", "False", "0", "no"):
        monkeypatch.setenv("URATORI_UI", no)
        assert not mounted(create_app(dsn="postgres://unused")), no

    monkeypatch.setenv("URATORI_UI", "")
    assert mounted(create_app(dsn="postgres://unused"))
    assert not mounted(create_app(dsn="postgres://unused", token="secret"))

    monkeypatch.setenv("URATORI_UI", "fales")
    with pytest.raises(RuntimeError, match="URATORI_UI"):
        create_app(dsn="postgres://unused")

    monkeypatch.delenv("URATORI_UI", raising=False)
    monkeypatch.setenv("URATORI_UI_FRAME_ANCESTORS", "'self'\r\nX-Evil: 1")
    with pytest.raises(RuntimeError, match="newlines"):
        # The value is pasted into a response header; a newline in it is a
        # header-smuggling vector, refused at boot.
        create_app(dsn="postgres://unused")


# ------------------------------------------------------------- the editor --
#
# The editor turns the investigation page into a workbench: the stored source
# served for editing, a dry-run compile for validation, and a save that is the
# same teach `PUT /definitions` performs -- same adoption rules, same refusal
# prose, same persistence. These tests are the spec: what the endpoints serve,
# what the grant gates, and what a save may never silently do.


EDITED_SOURCE = '''
group shop_order.carried_by from courier_id
filter shop_order.open where status != "done"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)

# Whether this courier is carrying anything at all.
figure shop_courier.idle:
    display "{value}"
    combine:
        carrying = shop_courier.carrying
    calculate:
        when carrying == 0 then "idle"
        otherwise "busy"
'''
"""COURIER_SOURCE with one edit of every diff class: the filter's predicate
moved (changed), `load_band` is gone (removed) and `idle` arrived (new)."""


FACT_SOURCE = '''
# A courier on shift.
fact shop_courier:
    name name
    name as text

# An order somebody placed.
fact shop_order:
    name ref
    url url
    ref as text
    url as text
    courier_id as text
    status as text
    many tags:
        tag as text
''' + COURIER_SOURCE
"""The same world, taught by the source itself -- the editor's road to
declaring facts without touching `PUT /schema`."""


async def test_the_editor_serves_the_source_the_deployment_holds(
    pg_dsn: str,
) -> None:
    """The text served is the text stored, verbatim -- an editor loading a
    reconstruction would save back its own artefacts as if a person wrote
    them. Beside it, everything a completion needs: the kinds (fields unknown
    in a schema-taught world, and the payload must say so with an empty list
    rather than invent them), every declarable dial plus the reserved
    rendering dial, and the declared names."""
    async with serve(pg_dsn) as http:
        await _teach(http)

        page = (await http.get("/ui/api/source")).json()
        assert page["source"] == COURIER_SOURCE
        assert page["editable"] is True, "an open server grants editing"
        assert page["refusal"] is None
        assert len(page["fingerprint"]) == 12
        assert page["dials"] == ["limits.carrying.over", "tenant.hoursPerDay"]
        assert page["kinds"] == {"shop_courier": [], "shop_order": []}
        assert {d["name"]: d["kind"] for d in page["declarations"]} == {
            "shop_order.carried_by": "group",
            "shop_order.open": "filter",
            "shop_courier.carrying": "figure",
            "shop_courier.load_band": "figure",
        }

        world = (await http.get("/ui/api/world")).json()
        assert world["editable"] is True, (
            "the page decides whether to offer the editor from the world "
            "payload it already loads"
        )


async def test_the_editor_without_a_schema_names_the_gap(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        answer = await http.get("/ui/api/source")
        assert answer.status_code == 409
        assert "schema" in answer.json()["detail"].lower()


async def test_check_reports_a_syntax_refusal_with_line_and_column(
    pg_dsn: str,
) -> None:
    """The lexer and parser know the column; the editor needs it as a number
    to place a caret, not re-parsed out of prose."""
    async with serve(pg_dsn) as http:
        await _teach(http)

        bad = '\nfilter shop_order.bad where status == "x'
        out = (await http.post("/ui/api/check", json={"source": bad})).json()
        assert out["ok"] is False
        assert out["declarations"] == []
        assert out["refusal"]["line"] == 2
        assert out["refusal"]["column"] == bad.split("\n")[1].index('"'), (
            "the caret goes where the refusing token starts; any other integer "
            "points a reader at the wrong character"
        )
        assert out["refusal"]["message"]

        # A refused check is still a dry run: the draft it refused must not
        # have touched the stored world -- a check that persisted what it
        # just refused would take the deployment unready.
        assert (await http.get("/ui/api/source")).json()["source"] == COURIER_SOURCE
        world = (await http.get("/ui/api/world")).json()
        assert len(world["declarations"]) == 4 and world["refusal"] is None


async def test_check_reports_a_checker_refusal_by_line_alone(pg_dsn: str) -> None:
    """The checker carries no column, and the payload must say so with null
    rather than a fabricated zero a client would dutifully point at."""
    async with serve(pg_dsn) as http:
        await _teach(http)

        bad = (
            "# Sums a set nobody declared.\n"
            "figure shop_courier.x:\n"
            '    display "x"\n'
            "    depends:\n"
            "        mine = shop_order.nowhere:{shop_courier}\n"
            "    calculate:\n"
            "        count(mine)\n"
        )
        out = (await http.post("/ui/api/check", json={"source": bad})).json()
        assert out["ok"] is False
        assert out["refusal"]["column"] is None
        assert out["refusal"]["line"] == 5
        assert "nowhere" in out["refusal"]["message"]


async def test_check_classifies_what_a_save_would_change(pg_dsn: str) -> None:
    """The diff is the review surface, and it must tell the cascade's truth:
    editing the filter moves the version of every figure whose plan hashes its
    text in, so `carrying` reports changed though its own lines are untouched.
    A diff that only marked the edited lines would promise stability the
    engine will not deliver."""
    async with serve(pg_dsn) as http:
        await _teach(http)

        out = (await http.post("/ui/api/check", json={"source": EDITED_SOURCE})).json()
        assert out["ok"] is True and out["refusal"] is None
        changes = {d["name"]: (d["kind"], d["change"]) for d in out["declarations"]}
        assert changes == {
            "shop_order.carried_by": ("group", "unchanged"),
            "shop_order.open": ("filter", "changed"),
            "shop_courier.carrying": ("figure", "changed"),
            "shop_courier.idle": ("figure", "new"),
            "shop_courier.load_band": ("figure", "removed"),
        }, (
            "a removed entry carries the kind it HAD -- the candidate source "
            "has no answer, and the reviewer is being told what is being lost"
        )

        # A check is a dry run: nothing was taught, nothing was stored.
        held = (await http.get("/ui/api/source")).json()
        assert held["source"] == COURIER_SOURCE
        names = {d["name"] for d in (await http.get("/ui/api/world")).json()["declarations"]}
        assert "shop_courier.load_band" in names and "shop_courier.idle" not in names


async def test_a_save_teaches_and_survives_a_restart(pg_dsn: str) -> None:
    """A save is `db.save_world`, not a swap of process memory -- the proof is
    a second boot serving the edited text."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)
                held = (await http.get("/ui/api/source")).json()
                saved = await http.put(
                    "/ui/api/source",
                    json={"source": EDITED_SOURCE, "expected": held["fingerprint"]},
                )
                assert saved.status_code == 200, saved.text
                body = saved.json()
                assert body["fingerprint"] != held["fingerprint"]
                changes = {d["name"]: d["change"] for d in body["declarations"]}
                assert changes["shop_courier.idle"] == "new"

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                page = (await http.get("/ui/api/source")).json()
                assert page["source"] == EDITED_SOURCE
                names = {
                    d["name"]
                    for d in (await http.get("/ui/api/world")).json()["declarations"]
                }
                assert "shop_courier.idle" in names
                assert "shop_courier.load_band" not in names
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_a_save_that_does_not_compile_changes_nothing(pg_dsn: str) -> None:
    """The refusal arrives structured (message, line, column) because the
    editor is the caller -- and the stored world must be exactly what it was,
    because a half-taught save is a server that cannot boot."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        held = (await http.get("/ui/api/source")).json()

        bad = '\nfilter shop_order.bad where status == "x'
        refused = await http.put(
            "/ui/api/source", json={"source": bad, "expected": held["fingerprint"]}
        )
        assert refused.status_code == 422
        detail = refused.json()["detail"]
        assert detail["line"] == 2 and detail["message"]

        again = (await http.get("/ui/api/source")).json()
        assert again["source"] == COURIER_SOURCE
        assert again["fingerprint"] == held["fingerprint"]

        # A checker refusal through the SAVE path carries its line and a null
        # column, same as through check -- the editor renders both doors'
        # refusals with one code path.
        unresolved = (
            "# Sums a set nobody declared.\n"
            "figure shop_courier.x:\n"
            '    display "x"\n'
            "    depends:\n"
            "        mine = shop_order.nowhere:{shop_courier}\n"
            "    calculate:\n"
            "        count(mine)\n"
        )
        refused = await http.put(
            "/ui/api/source",
            json={"source": unresolved, "expected": held["fingerprint"]},
        )
        assert refused.status_code == 422
        detail = refused.json()["detail"]
        assert detail["line"] == 5 and detail["column"] is None


async def test_two_editors_cannot_silently_overwrite_each_other(
    pg_dsn: str,
) -> None:
    """The fingerprint is the whole concurrency story: a save names the text
    it was editing, and a save against text that has moved is refused with the
    state of play, never merged and never silently last-writer-wins."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        held = (await http.get("/ui/api/source")).json()

        stale = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": "000000000000"},
        )
        assert stale.status_code == 409
        assert "changed" in stale.json()["detail"].lower()
        assert (await http.get("/ui/api/source")).json()["source"] == COURIER_SOURCE

        fresh = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": held["fingerprint"]},
        )
        assert fresh.status_code == 200, fresh.text


async def test_the_editor_can_declare_facts_and_adopt_the_world(
    pg_dsn: str,
) -> None:
    """A fact-bearing save through the editor is the same adoption
    `PUT /definitions` performs: the schema's kinds retire, the source's fact
    declarations become the world -- and the completion payload now knows the
    fields, nested ones flattened to the dotted paths definitions write.

    Two claims a lighter test missed. The retirement must be STATED -- it is
    a change no per-declaration diff row carries, and record names and links
    hang on it. And the retired schema must be what is PERSISTED: a save
    that swapped memory but stored the old kind-declaring document would
    answer 200 and brick the deployment at its next boot, when the stored
    source and the stored schema refuse each other."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)

                checked = (
                    await http.post("/ui/api/check", json={"source": FACT_SOURCE})
                ).json()
                assert checked["ok"] is True, checked
                assert checked["adoption"] and "retires" in checked["adoption"]
                fated = {d["name"]: (d["kind"], d["change"]) for d in checked["declarations"]}
                assert fated["shop_courier"] == ("fact", "new")
                assert fated["shop_order"] == ("fact", "new")

                held = (await http.get("/ui/api/source")).json()
                saved = await http.put(
                    "/ui/api/source",
                    json={"source": FACT_SOURCE, "expected": held["fingerprint"]},
                )
                assert saved.status_code == 200, saved.text
                assert saved.json()["adoption"], (
                    "the save is the moment of retirement; saying it only at "
                    "check time lets a direct save pass silently"
                )

                schema_doc = (await http.get("/schema")).json()
                assert schema_doc["kinds"] == [], (
                    "the schema's kinds retired with the save"
                )
                page = (await http.get("/ui/api/source")).json()
                assert page["kinds"] == {
                    "shop_courier": ["name"],
                    "shop_order": ["courier_id", "ref", "status", "tags.tag", "url"],
                }
                world = (await http.get("/ui/api/world")).json()
                assert sorted(world["kinds"]) == ["shop_courier", "shop_order"]
                assert world["name_fields"]["shop_order"] == "ref"

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                health = (await http.get("/health")).json()
                assert health["ready"] is True, (
                    "the retired schema was persisted with the fact-taught "
                    "source; anything else refuses itself at the next boot"
                )
                world = (await http.get("/ui/api/world")).json()
                assert sorted(world["kinds"]) == ["shop_courier", "shop_order"]
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_the_editor_repairs_a_world_this_build_refused(pg_dsn: str) -> None:
    """The boot path's promise -- unready, repairable by a corrected teach --
    is only kept if the editor can see the refused text and save over it. A
    404 or an empty editor here would leave curl as the only repair tool."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)

        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(
                f"update {name}.engine_world set source = 'this is not a definition'"
            )
        finally:
            await connection.close()

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                page = (await http.get("/ui/api/source")).json()
                assert page["source"] == "this is not a definition"
                assert page["refusal"], "the editor must say why the library is bare"

                checked = (
                    await http.post("/ui/api/check", json={"source": COURIER_SOURCE})
                ).json()
                assert checked["ok"] is True
                assert all(d["change"] == "new" for d in checked["declarations"]), (
                    "against a refused (empty) library everything is new -- "
                    "there is no old version to diff against"
                )

                saved = await http.put(
                    "/ui/api/source",
                    json={"source": COURIER_SOURCE, "expected": page["fingerprint"]},
                )
                assert saved.status_code == 200, saved.text
                health = (await http.get("/health")).json()
                assert health["ready"] is True
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_the_editor_can_teach_the_first_definitions(pg_dsn: str) -> None:
    """A schema-taught server with no definitions yet serves an empty editor,
    not an error: teaching the first source is the experiment the editor
    exists for."""
    async with serve(pg_dsn) as http:
        put = await http.put("/schema", json=COURIER_WORLD.to_document())
        assert put.status_code == 200, put.text

        page = (await http.get("/ui/api/source")).json()
        assert page["source"] == ""
        assert page["declarations"] == []

        saved = await http.put(
            "/ui/api/source",
            json={"source": COURIER_SOURCE, "expected": page["fingerprint"]},
        )
        assert saved.status_code == 200, saved.text
        names = {
            d["name"] for d in (await http.get("/ui/api/world")).json()["declarations"]
        }
        assert "shop_courier.carrying" in names


async def test_a_pass_can_be_run_from_the_editor(pg_dsn: str) -> None:
    """A save leaves every tenant honestly behind-deploy until a pass runs;
    without a door to run one, the editor's loop dead-ends at curl. The pass
    is recorded in the activity log like any other, because it IS one."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        fed = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
        )
        assert fed.status_code == 200, fed.text

        held = (await http.get("/ui/api/source")).json()
        saved = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": held["fingerprint"]},
        )
        assert saved.status_code == 200, saved.text

        stale = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.open")
        ).json()
        assert stale["state"]["ok"] is False
        assert stale["state"]["because"] == "behind-deploy"

        ran = await http.post("/ui/api/tenants/t1/runs", json={})
        assert ran.status_code == 200, ran.text
        assert "changed" in ran.json()

        fresh = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.open")
        ).json()
        assert fresh["state"]["ok"] is True

        page = (await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()
        assert page["runs"][0]["trigger"] == "run"
        assert page["runs"][0]["full"] is False

        full = await http.post("/ui/api/tenants/t1/runs", json={"full": True})
        assert full.status_code == 200, full.text
        newest = (await http.get("/ui/api/tenants/t1/activity?quiet=1")).json()["runs"][0]
        assert newest["full"] is True, (
            "a silently downgraded full rebuild, logged as partial, is the "
            "kind of lie the run log exists to prevent"
        )

        nobody = await http.post("/ui/api/tenants/nobody/runs", json={})
        assert nobody.status_code == 404, (
            "a pass for a tenant nobody has fed computes nothing and forges "
            "an activity row that makes the name look real"
        )


async def test_editing_is_a_grant_not_a_default_beside_a_token(
    pg_dsn: str,
) -> None:
    """Beside a token the UI is read-only unless the operator says otherwise:
    a token'd API with a silently writable UI would let anyone who can reach
    the port redefine every figure the token was protecting."""
    async with serve(pg_dsn, token="secret", ui=True) as http:
        await _teach(http, headers={"Authorization": "Bearer secret"})

        read = await http.get("/ui/api/source")
        assert read.status_code == 200, "reading the source stays fine"
        page = read.json()
        assert page["editable"] is False
        assert (await http.get("/ui/api/world")).json()["editable"] is False

        checked = await http.post("/ui/api/check", json={"source": COURIER_SOURCE})
        assert checked.status_code == 403
        assert "URATORI_UI_EDIT" in checked.json()["detail"]
        saved = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": page["fingerprint"]},
        )
        assert saved.status_code == 403
        ran = await http.post("/ui/api/tenants/t1/runs", json={})
        assert ran.status_code == 403, (
            "a pass rebuilds and recomputes; without the grant the UI may "
            "not spend that either"
        )

    async with serve(pg_dsn, token="secret", ui=True, ui_edit=True) as http:
        await _teach(http, headers={"Authorization": "Bearer secret"})
        page = (await http.get("/ui/api/source")).json()
        assert page["editable"] is True
        saved = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": page["fingerprint"]},
        )
        assert saved.status_code == 200, (
            "the explicit grant is the whole gate -- the editor itself "
            "carries no token"
        )


def test_uratori_ui_edit_env_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same contract as URATORI_UI: documented spellings parse, empty means
    unset, garbage refuses to boot -- and a grant on a server whose UI is off
    is a contradiction refused at boot, not a flag that silently does
    nothing."""
    monkeypatch.delenv("URATORI_UI", raising=False)
    monkeypatch.delenv("URATORI_UI_EDIT", raising=False)

    for yes in ("on", "True", "1", "yes"):
        monkeypatch.setenv("URATORI_UI_EDIT", yes)
        create_app(dsn="postgres://unused")
    for no in ("off", "False", "0", "no"):
        monkeypatch.setenv("URATORI_UI_EDIT", no)
        create_app(dsn="postgres://unused")

    monkeypatch.setenv("URATORI_UI_EDIT", "")
    create_app(dsn="postgres://unused")

    monkeypatch.setenv("URATORI_UI_EDIT", "fales")
    with pytest.raises(RuntimeError, match="URATORI_UI_EDIT"):
        create_app(dsn="postgres://unused")

    monkeypatch.setenv("URATORI_UI_EDIT", "on")
    with pytest.raises(RuntimeError, match="URATORI_UI_EDIT"):
        # The UI is off (token, no override); granting edits to a UI that is
        # not mounted is a configuration contradiction, not a no-op.
        create_app(dsn="postgres://unused", token="secret")

    monkeypatch.delenv("URATORI_UI_EDIT", raising=False)
    with pytest.raises(RuntimeError, match="URATORI_UI_EDIT"):
        create_app(dsn="postgres://unused", token="secret", ui=False, ui_edit=True)


async def test_a_cosmetic_edit_is_reported_as_touching_nothing(pg_dsn: str) -> None:
    """Re-spaced expressions, comments slipped into a body, a reworded
    display: none of it moves a version or a token, so the diff must say
    `unchanged` -- a diff that cried `changed` here would send the saved
    panel claiming a behind-deploy that will never happen, and offering a
    pass that recomputes nothing. The save still stores the new text."""
    cosmetic = COURIER_SOURCE.replace(
        'display "{value} orders in hand"', 'display "orders held: {value}"'
    ).replace(
        "        mine = shop_order.carried_by:{shop_courier} & shop_order.open",
        "        # counted, not summed\n"
        "        mine = shop_order.carried_by:{shop_courier}  &  shop_order.open",
    )
    assert cosmetic != COURIER_SOURCE
    async with serve(pg_dsn) as http:
        await _teach(http)

        out = (await http.post("/ui/api/check", json={"source": cosmetic})).json()
        assert out["ok"] is True
        assert {d["change"] for d in out["declarations"]} == {"unchanged"}

        held = (await http.get("/ui/api/source")).json()
        saved = await http.put(
            "/ui/api/source", json={"source": cosmetic, "expected": held["fingerprint"]}
        )
        assert saved.status_code == 200, saved.text
        assert {d["change"] for d in saved.json()["declarations"]} == {"unchanged"}
        assert (await http.get("/ui/api/source")).json()["source"] == cosmetic


async def test_simultaneous_saves_cannot_both_win(pg_dsn: str) -> None:
    """Two editors, one loaded text, two saves in the same instant: exactly
    one wins and the stored source is the winner's. The fingerprint check
    reads process memory and the save awaits the database before swapping
    it -- without serialisation, both requests pass the check inside that
    window, both answer 200, and one author's work vanishes with no signal
    anywhere. That silent overwrite is the one thing the fingerprint
    contract exists to make impossible."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        fingerprint = (await http.get("/ui/api/source")).json()["fingerprint"]

        first, second = await asyncio.gather(
            http.put(
                "/ui/api/source",
                json={"source": EDITED_SOURCE, "expected": fingerprint},
            ),
            http.put(
                "/ui/api/source",
                json={"source": FACT_SOURCE, "expected": fingerprint},
            ),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409], (
            first.text + " / " + second.text
        )
        winner = EDITED_SOURCE if first.status_code == 200 else FACT_SOURCE
        assert (await http.get("/ui/api/source")).json()["source"] == winner


async def test_a_saves_fingerprint_names_the_text_it_stored(pg_dsn: str) -> None:
    """Consecutive saves are the editor's normal life: each response's
    fingerprint must open the door to the next save, and a no-op save must
    say so -- everything unchanged, the fingerprint standing still."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        held = (await http.get("/ui/api/source")).json()

        first = await http.put(
            "/ui/api/source",
            json={"source": EDITED_SOURCE, "expected": held["fingerprint"]},
        )
        assert first.status_code == 200, first.text
        stamp = first.json()["fingerprint"]

        again = await http.put(
            "/ui/api/source", json={"source": EDITED_SOURCE, "expected": stamp}
        )
        assert again.status_code == 200, (
            "the fingerprint a save answers must name the text it stored; "
            "anything else 409s every save after the first: " + again.text
        )
        body = again.json()
        assert body["fingerprint"] == stamp
        assert {d["change"] for d in body["declarations"]} == {"unchanged"}


async def test_check_and_save_without_a_schema_name_the_gap(pg_dsn: str) -> None:
    async with serve(pg_dsn) as http:
        checked = await http.post("/ui/api/check", json={"source": COURIER_SOURCE})
        assert checked.status_code == 409
        assert "schema" in checked.json()["detail"].lower()
        saved = await http.put(
            "/ui/api/source", json={"source": COURIER_SOURCE, "expected": "000000000000"}
        )
        assert saved.status_code == 409
        assert "schema" in saved.json()["detail"].lower()
        ran = await http.post("/ui/api/tenants/t1/runs", json={})
        assert ran.status_code == 409, (
            "a pass needs a taught world; the 409 says which step is missing"
        )


async def test_the_env_grant_is_honoured_not_just_parsed(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spelling test proves the env values parse; this proves they
    GOVERN. An operator writing URATORI_UI_EDIT=off on an open server is
    turning the editor off, and a build that parsed the flag and then
    ignored it would ship that operator a writable UI."""
    monkeypatch.setenv("URATORI_UI_EDIT", "off")
    async with serve(pg_dsn) as http:
        await _teach(http)
        page = (await http.get("/ui/api/source")).json()
        assert page["editable"] is False
        refused = await http.post("/ui/api/check", json={"source": COURIER_SOURCE})
        assert refused.status_code == 403

    monkeypatch.setenv("URATORI_UI_EDIT", "on")
    async with serve(pg_dsn, token="secret", ui=True) as http:
        await _teach(http, headers={"Authorization": "Bearer secret"})
        page = (await http.get("/ui/api/source")).json()
        assert page["editable"] is True
    monkeypatch.delenv("URATORI_UI_EDIT", raising=False)


async def test_an_emptied_library_stays_taught_across_a_restart(pg_dsn: str) -> None:
    """Saving an empty source is removing every declaration, and the server
    answers ready with zero figures. The same stored state must answer the
    same way after a restart -- a boot that quietly demoted the empty source
    to "no definitions loaded" would make readiness depend on which side of
    a restart you ask from."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)
                held = (await http.get("/ui/api/source")).json()
                saved = await http.put(
                    "/ui/api/source", json={"source": "", "expected": held["fingerprint"]}
                )
                assert saved.status_code == 200, saved.text
                assert {d["change"] for d in saved.json()["declarations"]} == {"removed"}
                assert (await http.get("/health")).json()["ready"] is True

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                assert (await http.get("/health")).json()["ready"] is True
                page = (await http.get("/ui/api/source")).json()
                assert page["source"] == "" and page["declarations"] == []
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_a_source_beyond_reason_is_refused_not_compiled(pg_dsn: str) -> None:
    """The check fires on every pause in typing, on the single-worker event
    loop; one pasted blob without a ceiling stalls every pass and socket on
    the deployment. 413 with the ceiling named, and nothing stored."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        blob = "# padding\n" * 300_000
        checked = await http.post("/ui/api/check", json={"source": blob})
        assert checked.status_code == 413
        saved = await http.put(
            "/ui/api/source", json={"source": blob, "expected": "000000000000"}
        )
        assert saved.status_code == 413
        assert (await http.get("/ui/api/source")).json()["source"] == COURIER_SOURCE


async def test_the_save_says_whether_a_pass_is_owed(pg_dsn: str) -> None:
    """`stale` must agree with the engine's own behind-deploy verdict --
    they are two views of one fact. A cosmetic edit owes nothing and the
    membership stays served; a label edit changes what serves but stores
    nothing, so it owes nothing either; a predicate edit moves the index
    set and the membership honestly refuses until a pass runs. A `stale`
    that disagreed with the membership state in either direction would
    have the saved panel inviting rebuilds that move nothing, or promising
    freshness over pages that answer behind-deploy."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _feed_couriers(http)

        async def save(source: str) -> dict[str, Any]:
            held = (await http.get("/ui/api/source")).json()
            answer = await http.put(
                "/ui/api/source", json={"source": source, "expected": held["fingerprint"]}
            )
            assert answer.status_code == 200, answer.text
            return dict(answer.json())

        async def membership_ok() -> bool:
            page = (
                await http.get("/ui/api/tenants/t1/membership/shop_order.open")
            ).json()
            return bool(page["state"]["ok"])

        cosmetic = COURIER_SOURCE.replace(
            'display "{value} orders in hand"', 'display "orders carried: {value}"'
        )
        saved = await save(cosmetic)
        assert saved["stale"] is False
        assert await membership_ok(), (
            "nothing stored moved, so the stored membership still serves"
        )

        labelled = cosmetic.replace(
            'filter shop_order.open where status != "delivered"',
            'filter shop_order.open where status != "delivered" label "open"',
        )
        saved = await save(labelled)
        assert saved["stale"] is (not await membership_ok()), (
            "stale and the membership's behind-deploy verdict are two views "
            "of one fact and may not disagree"
        )

        flipped = labelled.replace('status != "delivered"', 'status == "delivered"')
        saved = await save(flipped)
        assert saved["stale"] is True
        assert not await membership_ok(), (
            "the predicate moved this grouping's spec; serving the old "
            "membership as current would show records matching a predicate "
            "they do not"
        )

        # A grouping nobody's figure reads: its spec is the ONLY thing that
        # can notice the edit, so a stale verdict degraded to name-set
        # comparison would tell the operator no pass is owed while every
        # page filtering through it goes behind-deploy.
        parked = flipped + '\nfilter shop_order.parked where status == "parked" label "parked"\n'
        saved = await save(parked)
        assert saved["stale"] is True
        edited = parked.replace('status == "parked"', 'status == "queued"')
        saved = await save(edited)
        assert saved["stale"] is True, (
            "same names, moved spec: the one case only a per-grouping "
            "version comparison can see"
        )
        relabelled = edited.replace('label "parked"', 'label "waiting"')
        saved = await save(relabelled)
        assert saved["stale"] is False, (
            "a label is prose; a pass for it would rebuild nothing -- and "
            "this pins the absolute, not just agreement with the membership "
            "page"
        )


async def test_an_arrived_filter_does_not_unseat_its_neighbours(pg_dsn: str) -> None:
    """The complaint that forced per-index staleness, pinned end to end: a
    saved source with one NEW filter must leave every existing grouping's
    page serving (its buckets are exactly what the pass built), withhold
    only the newcomer, and a pass then seats the newcomer -- without the
    whole index set being rebuilt on the way."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        await _feed_couriers(http)
        assert (
            await http.get("/ui/api/tenants/t1/membership/shop_order.open")
        ).json()["state"]["ok"] is True

        grown = COURIER_SOURCE + 'filter shop_order.done where status == "delivered" label "done"\n'
        held = (await http.get("/ui/api/source")).json()
        saved = await http.put(
            "/ui/api/source", json={"source": grown, "expected": held["fingerprint"]}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["stale"] is True

        neighbour = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.open")
        ).json()
        assert neighbour["state"]["ok"] is True, (
            "shop_order.open's buckets are exactly what the last pass built; "
            "unseating it for a filter it never met is the rebuild-the-world "
            "behaviour this change ends"
        )
        newcomer = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.done")
        ).json()
        assert newcomer["state"]["ok"] is False
        assert newcomer["state"]["because"] == "behind-deploy"

        ran = await http.post("/ui/api/tenants/t1/runs", json={})
        assert ran.status_code == 200, ran.text
        seated = (
            await http.get("/ui/api/tenants/t1/membership/shop_order.done")
        ).json()
        assert seated["state"]["ok"] is True
        assert seated["members"] == 1, (
            "exactly one seeded order is delivered (o2); any other count "
            "means the newcomer was seated over the wrong records"
        )


async def test_membership_honours_the_upgrade_window(pg_dsn: str) -> None:
    """Between a 0.7 upgrade and a tenant's first pass, the per-grouping
    stamps do not exist yet -- only the old whole-set stamp. The membership
    pages must accept the same proof of currency the pass's seed will: a
    matching stamp serves, a mismatched one answers behind-deploy (the
    tenant WAS bucketed, under an older library; never-computed would tell
    an upgraded deployment its history vanished)."""
    from uratori import compile_source as compile_lib
    from uratori.engine.engine import _index_set_version

    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        app = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)
                await _feed_couriers(http)

                # What the upgrade leaves behind, planted directly: rows
                # built, per-grouping stamps absent, the whole-set stamp
                # standing with the hash the old engine recorded.
                stamp = _index_set_version(compile_lib(COURIER_SOURCE, COURIER_WORLD))
                pool = app.state.uratori.pool
                await pool.execute("delete from index_built where tenant_id = 't1'")
                await pool.execute(
                    "insert into index_state (tenant_id, version) values ('t1', $1)",
                    stamp,
                )
                page = (
                    await http.get("/ui/api/tenants/t1/membership/shop_order.open")
                ).json()
                assert page["state"]["ok"] is True, (
                    "the stamp proves the buckets current; a 409 here means "
                    "every upgraded deployment's pages go dark until a pass"
                )

                await pool.execute(
                    "update index_state set version = 'another-library' "
                    "where tenant_id = 't1'"
                )
                page = (
                    await http.get("/ui/api/tenants/t1/membership/shop_order.open")
                ).json()
                assert page["state"]["ok"] is False
                assert page["state"]["because"] == "behind-deploy"
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


BOARD_SOURCE = COURIER_SOURCE + """
# The open orders, listed.
projection shop_order.board:
    from shop_order.open

    field:
        key = ref as text
"""


async def test_an_unchanged_batch_still_serves_the_board(pg_dsn: str) -> None:
    """The scheduled sync whose writes deduplicate to nothing: the facts door
    was used, so the pass is the sync moment and the board re-serves --
    projections' clock-worded sentences are refreshed at syncs, and the
    quiet weeks are exactly when the clock is the only thing moving them.
    Pinned at the DOOR, not just the facade: the door builds its `written`
    from what actually changed, and a door-level normalisation of the empty
    dict to None would reintroduce the freeze invisibly."""
    async with serve(pg_dsn) as http:
        put = await http.put("/schema", json=COURIER_WORLD.to_document())
        assert put.status_code == 200, put.text
        put = await http.put("/definitions", json={"source": BOARD_SOURCE})
        assert put.status_code == 200, put.text

        first = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_order": {"o1": {"ref": "A-1", "status": "riding"}}}},
        )
        assert first.status_code == 200, first.text
        assert any(r["name"] == "shop_order.board" for r in first.json()["results"])

        again = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_order": {"o1": {"ref": "A-1", "status": "riding"}}}},
        )
        assert again.status_code == 200, again.text
        body = again.json()
        assert body["written"] == 0, "the batch deduplicated to nothing"
        assert any(r["name"] == "shop_order.board" for r in body["results"]), (
            "the door was the facts door; an unchanged batch is still the sync"
        )


async def test_the_run_guard_knows_a_settings_only_tenant(pg_dsn: str) -> None:
    """`tenant_exists` unions facts, settings and run history, like the
    tenant list it replaced -- a tenant taught settings but never fed is
    exactly the misconfiguration an investigator pokes at, and 404ing its
    run door would tell them it does not exist."""
    async with serve(pg_dsn) as http:
        await _teach(http)
        put = await http.put("/tenants/t7/settings", json={"document": {}})
        assert put.status_code == 200, put.text
        ran = await http.post("/ui/api/tenants/t7/runs", json={})
        assert ran.status_code == 200, ran.text


async def test_the_editor_pass_pays_deferred_debt_in_full(pg_dsn: str) -> None:
    """A bulk import that deferred its pass leaves the tenant owing a FULL
    one; the editor's run door must upgrade like the API's, and the log must
    say a full pass is what ran -- a silently downgraded debt payment leaves
    stored answers describing the pre-import world."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        app = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach(http)
                await _feed_couriers(http)
                pool = app.state.uratori.pool
                await pool.execute(
                    "insert into import_debt (tenant_id) values ('t1') "
                    "on conflict do nothing"
                )
                ran = await http.post("/ui/api/tenants/t1/runs", json={})
                assert ran.status_code == 200, ran.text
                newest = (
                    await http.get("/ui/api/tenants/t1/activity?quiet=1")
                ).json()["runs"][0]
                assert newest["full"] is True, (
                    "the debt upgrades the pass, and the log tells it as full"
                )
                owed = await pool.fetchval(
                    "select count(*) from import_debt where tenant_id = 't1'"
                )
                assert owed == 0, "the paid debt is settled, not carried"
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


# ---------------------------------------------------------------- about --


ABOUT_WORLD = dataclasses.replace(
    COURIER_WORLD,
    bucket_settings=("tenant.timezone",),
    defaults={
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {"carrying": {"over": 3}},
    },
)
"""The courier world plus the one dial a day group needs. A schema of its
own so the shared COURIER_WORLD keeps meaning what every other test says."""

ABOUT_SOURCE = COURIER_SOURCE + """
# One row per courier-day, so a courier's record page carries day rows whose
# `dimension` is the day -- the shape the NFL demo's by-day figures have.
group shop_order.by_courier_day from (courier_id, placed by day in tenant.timezone)

# How many orders this courier took on, day by day.
figure shop_courier.daily:
    display "{value} orders"
    depends:
        slice = shop_order.by_courier_day:{shop_courier}
    calculate:
        count(slice)

# The open orders, listed: the page a record's row appears on.
projection shop_order.board:
    from shop_order.open

    field:
        key = ref as text

# Which couriers look alive at all.
filter shop_courier.active where name is set

# A courier's traffic weighted by the roster size -- deliberately reads TWO
# id spaces (orders and couriers), so its citations span kinds and its
# `_cited_kind` is None. The about page must still list it on both kinds'
# records: it counted them.
figure shop_courier.weighted:
    display "{value}"
    unit count
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
        roster = shop_courier.active
    calculate:
        count(mine) + count(roster)

# The single loudest order: a page with a sort and a limit, so "in the
# from-set" and "on the page" come apart -- the shape that catches a
# bespoke single-record evaluation, which would show a row the real page
# refused.
projection shop_order.spotlight:
    from shop_order.open
    sort by key descending
    limit 1

    field:
        key = ref as text
"""


async def _teach_about(http: httpx.AsyncClient) -> None:
    put = await http.put("/schema", json=ABOUT_WORLD.to_document())
    assert put.status_code == 200, put.text
    put = await http.put("/definitions", json={"source": ABOUT_SOURCE})
    assert put.status_code == 200, put.text


def _placed_orders(n: int) -> dict[str, dict[str, Any]]:
    """n riding orders for c1, each placed on its own day, so the day-grained
    figure holds n rows for the one courier."""
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    return {
        f"o{i}": {
            "ref": f"A-{i}",
            "courier_id": "c1",
            "status": "riding",
            "placed": f"{start + timedelta(days=i)}T10:00:00Z",
        }
        for i in range(n)
    }


async def _about(
    http: httpx.AsyncClient, kind: str, key: str, tenant: str = "t1"
) -> dict[str, Any]:
    from urllib.parse import quote

    got = await http.get(
        f"/ui/api/tenants/{tenant}/about/{kind}/{quote(key, safe='')}"
    )
    assert got.status_code == 200, got.text
    return got.json()


def pages_of(about: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["projection"]: p for p in about["pages"]}


async def test_a_record_serves_the_figures_scoped_to_its_kind(pg_dsn: str) -> None:
    """The drill Sean asked for by name: start from the record, read what the
    library computes *for* it. Every figure scoped to the record's kind
    answers -- the exact subject row, plus the day rows a grained figure files
    under `subject@day` -- rendered exactly as the figure's own page would
    render them, because a second rendering would be a second calculation
    system."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": COURIER,
                    "shop_order": _placed_orders(3),
                }
            },
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_courier", "c1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        assert set(by_name) == {
            "shop_courier.carrying",
            "shop_courier.load_band",
            "shop_courier.daily",
            "shop_courier.weighted",
        }, "every figure scoped to shop_courier, and nothing scoped elsewhere"

        carrying = by_name["shop_courier.carrying"]["result"]
        assert carrying["state"]["ok"] is True
        assert [s["id"] for s in carrying["subjects"]] == ["c1"]
        assert carrying["subjects"][0]["display"] == "3", (
            "rendered by the same format_value the figure page uses -- the "
            "unit rendering, not the display template"
        )

        band = by_name["shop_courier.load_band"]["result"]
        assert [s["value"] for s in band["subjects"]] == ["over"], (
            "three orders meets the schema's default limit of three"
        )

        daily = by_name["shop_courier.daily"]["result"]
        assert [s["dimension"] for s in daily["subjects"]] == [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
        ], "the grained figure's rows carry their day, in day order"
        assert all(s["display"] == "1" for s in daily["subjects"])
        assert by_name["shop_courier.daily"]["more"] is False

        # "Exactly as the figure's own page would render them" is a claim
        # about equality, not about this fixture: the about entry must BE the
        # figure page's rows for this subject, field for field, so any second
        # rendering path dies here even on shapes no fixture anticipated.
        full = await http.get("/ui/api/tenants/t1/results/shop_courier.daily")
        assert full.status_code == 200, full.text
        mine = [
            s for s in full.json()["subjects"] if s["id"].split("@", 1)[0] == "c1"
        ]
        assert daily["subjects"] == mine

        # The rollup stays off the citation list even on the page where its
        # stored members -- literally ["c1"], a courier key -- would match:
        # its parts' own citations already name what it was built from. The
        # two-id-space figure stays ON it, because it counted this courier.
        cited = {c["figure"]: c for c in about["cited"]}
        assert set(cited) == {"shop_courier.weighted"}
        assert [r["subject"] for r in cited["shop_courier.weighted"]["rows"]] == ["c1"]

        # The other direction of the same claim: a kind no figure is scoped
        # to serves an empty section, not somebody else's figures.
        order_about = await _about(http, "shop_order", "o1")
        assert order_about["figures"] == []


async def test_a_record_lists_the_values_that_counted_it(pg_dsn: str) -> None:
    """The other half of the trace: which stored values cite this record.
    Every leaf figure whose members are keys of this kind reports -- the
    citing subject, its label, and the value as rendered now -- and 'this
    figure did not count it' is stated by an entry with no rows rather than
    left to inference. A rollup never appears: its members are stored cells,
    and its parts' own citations already name the records."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        orders = _placed_orders(3)
        orders["o9"] = {
            "ref": "A-9",
            "courier_id": "c1",
            "status": "delivered",
            "placed": "2026-02-01T10:00:00Z",
        }
        push = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": orders}},
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_order", "o1")
        cited = {c["figure"]: c for c in about["cited"]}
        assert set(cited) == {
            "shop_courier.carrying",
            "shop_courier.daily",
            "shop_courier.weighted",
        }, (
            "every leaf figure that counts shop_order records -- the "
            "two-id-space one included, because it counted this order too -- "
            "and not the rollup, whose members are cells"
        )
        assert [r["subject"] for r in cited["shop_courier.weighted"]["rows"]] == [
            "c1"
        ], "the mixed figure's citation is real: c1's value counted o1"

        carrying = cited["shop_courier.carrying"]
        assert carrying["scope"] == "shop_courier", "the link target's kind"
        assert carrying["state"]["ok"] is True
        assert [(r["subject"], r["name"]) for r in carrying["rows"]] == [("c1", "Aki")]
        assert carrying["rows"][0]["display"] == "3", (
            "the citing value as the figure page renders it"
        )

        daily = cited["shop_courier.daily"]
        assert [(r["subject"], r["dimension"]) for r in daily["rows"]] == [
            ("c1", "2026-01-02")
        ], "the citing value is the day cell, and the row says which day"

        # The delivered order: not open, so carrying never counted it -- an
        # entry with no rows says so. The day figure has no filter and did.
        about = await _about(http, "shop_order", "o9")
        cited = {c["figure"]: c for c in about["cited"]}
        assert cited["shop_courier.carrying"]["rows"] == []
        assert [r["dimension"] for r in cited["shop_courier.daily"]["rows"]] == ["2026-02-01"]


async def test_the_about_page_is_honest_when_values_are_behind_the_deploy(
    pg_dsn: str,
) -> None:
    """A definition moved and no pass has run: both directions must answer
    with the state, never with rows from the old era and not with a silent
    empty section -- a blank that means 'not computed yet' printed the same
    way as 'computed, and nothing found' is the absence-as-zero mistake."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {"shop_courier": COURIER, "shop_order": _placed_orders(3)}
            },
        )
        assert push.status_code == 200, push.text

        moved = ABOUT_SOURCE.replace('status != "delivered"', 'status == "riding"')
        put = await http.put("/definitions", json={"source": moved})
        assert put.status_code == 200, put.text

        about = await _about(http, "shop_courier", "c1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        carrying = by_name["shop_courier.carrying"]["result"]
        assert carrying["state"]["ok"] is False
        assert carrying["state"]["because"] == "behind-deploy"
        assert carrying["subjects"] == []
        daily = by_name["shop_courier.daily"]["result"]
        assert daily["state"]["ok"] is True and len(daily["subjects"]) == 3, (
            "the moved filter is not in daily's plan; one stale figure must "
            "not blank its untouched neighbours in the same payload"
        )

        about = await _about(http, "shop_order", "o1")
        cited = {c["figure"]: c for c in about["cited"]}
        assert cited["shop_courier.carrying"]["state"]["because"] == "behind-deploy"
        assert cited["shop_courier.carrying"]["rows"] == []


async def test_a_records_projection_row_travels_on_its_about_page(
    pg_dsn: str,
) -> None:
    """Projections of the record's kind answer with this record's row as the
    page would show it -- and a record the page does not hold answers
    `present: false` with the reason spelled out, because 'not on the page'
    is a verdict the definition reached, not a gap."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        orders = _placed_orders(2)
        orders["o9"] = {
            "ref": "A-9",
            "courier_id": "c1",
            "status": "delivered",
            "placed": "2026-02-01T10:00:00Z",
        }
        push = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": COURIER, "shop_order": orders}},
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_order", "o1")
        pages = {p["projection"]: p for p in about["pages"]}
        assert set(pages) == {"shop_order.board", "shop_order.spotlight"}
        page = pages["shop_order.board"]
        assert page["state"]["ok"] is True
        assert page["present"] is True
        assert page["row"]["row"]["display"]["key"] == "A-1", (
            "the row exactly as the projection page serves it"
        )
        assert pages["shop_order.spotlight"]["present"] is True, (
            "A-1 sorts to the top of the descending one-row page"
        )

        # The from-set holds o0; the sort and the limit keep it off the page.
        # Only a whole-page evaluation can know that -- a bespoke single-row
        # path would re-run the formulas, find o0 open, and print a row the
        # real page refused.
        about = await _about(http, "shop_order", "o0")
        assert pages_of(about)["shop_order.board"]["present"] is True
        spot = pages_of(about)["shop_order.spotlight"]
        assert spot["present"] is False
        assert spot["row"] is None
        assert spot["note"], "the absence carries its reason as a sentence"

        about = await _about(http, "shop_order", "o9")
        page = pages_of(about)["shop_order.board"]
        assert page["present"] is False
        assert page["row"] is None
        assert page["note"], "the absence carries its reason as a sentence"

        # A kind no projection is of: an empty section, not an invented page.
        about = await _about(http, "shop_courier", "c1")
        assert about["pages"] == []


async def test_about_rows_are_capped_and_say_so(pg_dsn: str) -> None:
    """A subject with hundreds of day rows must not make its record page a
    dump: the section caps and says it did, and the figure page keeps the
    rest. A silent cap would read as 'this is everything', which on this
    surface is a lie about the evidence."""
    from uratori.server.ui import ABOUT_ROWS

    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": COURIER,
                    "shop_order": _placed_orders(ABOUT_ROWS + 5),
                }
            },
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_courier", "c1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        daily = by_name["shop_courier.daily"]
        assert len(daily["result"]["subjects"]) == ABOUT_ROWS
        assert daily["more"] is True
        assert by_name["shop_courier.carrying"]["more"] is False

        # WHICH rows survive is part of the requirement: the latest, because
        # this page exists to verify what a number says now, and a cap that
        # kept the first sixty days of 1999 would show nothing current.
        days = [s["dimension"] for s in daily["result"]["subjects"]]
        assert days[0] == "2026-01-06" and days[-1] == "2026-03-06", (
            "65 days pushed, the newest 60 kept, still in day order"
        )

        # The citations cap the same way: o0 is cited once per figure, so the
        # flag stays down there -- the cap is per section entry, not global.
        about = await _about(http, "shop_order", "o0")
        cited = {c["figure"]: c for c in about["cited"]}
        assert cited["shop_courier.daily"]["more"] is False


async def test_a_row_set_exactly_at_the_cap_is_whole_not_truncated(
    pg_dsn: str,
) -> None:
    """The boundary: exactly the cap's worth of rows is a complete answer,
    and `more` must say so -- "we truncated" printed over a complete list is
    the same lie as a silent cap, in the other direction."""
    from uratori.server.ui import ABOUT_ROWS

    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": COURIER,
                    "shop_order": _placed_orders(ABOUT_ROWS),
                }
            },
        )
        assert push.status_code == 200, push.text
        about = await _about(http, "shop_courier", "c1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        daily = by_name["shop_courier.daily"]
        assert len(daily["result"]["subjects"]) == ABOUT_ROWS
        assert daily["more"] is False


async def test_the_cited_side_caps_per_figure_and_says_so(pg_dsn: str) -> None:
    """A record cited by more values than the page holds: the entry caps at
    the page size and raises `more` -- and the cap is per figure, so one
    prolific citer cannot make the page print "did not count it" about a
    figure that did. The weighted figure cites every courier from every
    courier's row, which is exactly the fan-out that overflows."""
    from uratori.server.ui import ABOUT_ROWS

    async with serve(pg_dsn) as http:
        await _teach_about(http)
        couriers = {f"c{i}": {"name": f"Courier {i}"} for i in range(ABOUT_ROWS + 5)}
        push = await http.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_courier": couriers, "shop_order": _placed_orders(1)}},
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_courier", "c1")
        cited = {c["figure"]: c for c in about["cited"]}
        weighted = cited["shop_courier.weighted"]
        assert len(weighted["rows"]) == ABOUT_ROWS
        assert weighted["more"] is True


async def test_a_figure_taught_after_the_pass_is_never_computed_not_blank(
    pg_dsn: str,
) -> None:
    """The everyday absence: a figure added to the source after the last
    pass has no pointer at all. Both directions must answer never-computed
    -- an empty entry here would read "computed, and nothing found", which
    is the absence-as-zero mistake on the page built to prevent it."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {"shop_courier": COURIER, "shop_order": _placed_orders(2)}
            },
        )
        assert push.status_code == 200, push.text

        grown = ABOUT_SOURCE + """
# The same count, taught after the pass.
figure shop_courier.echo:
    display "{value}"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)
"""
        put = await http.put("/definitions", json={"source": grown})
        assert put.status_code == 200, put.text

        about = await _about(http, "shop_courier", "c1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        echo = by_name["shop_courier.echo"]["result"]
        assert echo["state"]["ok"] is False
        assert echo["state"]["because"] == "never-computed"
        assert echo["subjects"] == []

        about = await _about(http, "shop_order", "o1")
        cited = {c["figure"]: c for c in about["cited"]}
        assert cited["shop_courier.echo"]["state"]["because"] == "never-computed"
        assert cited["shop_courier.echo"]["rows"] == []


async def test_an_awkward_key_walks_the_about_route_end_to_end(
    pg_dsn: str,
) -> None:
    """Record keys carry underscores and percent signs in the wild (the
    NFL's game ids are full of `_`), and both are LIKE wildcards. This pins
    the whole path -- URL decoding, the store's escaping, the day-row prefix
    probe -- because an unescaped `_` would put c-x-1's day rows on c_1's
    page, a wrong number on the verification surface itself."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        push = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": {
                        "c_1": {"name": "Underscore"},
                        "cx1": {"name": "Wildcard bait"},
                        "c%1": {"name": "Percent"},
                    },
                    "shop_order": {
                        "o1": {
                            "ref": "A-1",
                            "courier_id": "c_1",
                            "status": "riding",
                            "placed": "2026-01-01T10:00:00Z",
                        },
                        "o2": {
                            "ref": "A-2",
                            "courier_id": "cx1",
                            "status": "riding",
                            "placed": "2026-02-01T10:00:00Z",
                        },
                        "o3": {
                            "ref": "A-3",
                            "courier_id": "c%1",
                            "status": "riding",
                            "placed": "2026-03-01T10:00:00Z",
                        },
                    },
                }
            },
        )
        assert push.status_code == 200, push.text

        about = await _about(http, "shop_courier", "c_1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        assert [s["dimension"] for s in by_name["shop_courier.daily"]["result"]["subjects"]] == [
            "2026-01-01"
        ], "c_1's page holds c_1's day and not cx1's -- `_` is text, not a wildcard"

        about = await _about(http, "shop_courier", "c%1")
        by_name = {f["result"]["name"]: f for f in about["figures"]}
        assert [s["dimension"] for s in by_name["shop_courier.daily"]["result"]["subjects"]] == [
            "2026-03-01"
        ]


async def test_about_with_no_library_states_the_absence(pg_dsn: str) -> None:
    """The boot-refused world: records stored, library None (an upgrade
    across a language change). The record stays browsable, and the about
    payload must say WHY its sections are empty -- "no figure is scoped to
    this kind" would be a verdict about definitions that do not exist."""
    name = f"uratori_ui_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    try:
        first = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with first.router.lifespan_context(first):
            transport = httpx.ASGITransport(app=first)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                await _teach_about(http)
                push = await http.post(
                    "/tenants/t1/facts", json={"writes": {"shop_courier": COURIER}}
                )
                assert push.status_code == 200, push.text

        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(
                f"update {name}.engine_world set source = 'this is not a definition'"
            )
        finally:
            await connection.close()

        second = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with second.router.lifespan_context(second):
            transport = httpx.ASGITransport(app=second)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://uratori"
            ) as http:
                about = await _about(http, "shop_courier", "c1")
                assert about["state"]["ok"] is False
                assert about["state"]["because"] == "never-computed"
                assert (
                    about["figures"] == []
                    and about["cited"] == []
                    and about["pages"] == []
                )
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_about_without_a_schema_is_a_409(pg_dsn: str) -> None:
    """No schema means no world to ask about -- the same refusal the record
    route gives, so the two halves of one page cannot disagree about whether
    the deployment exists yet."""
    async with serve(pg_dsn) as http:
        got = await http.get("/ui/api/tenants/t1/about/shop_courier/c1")
        assert got.status_code == 409


async def test_about_a_record_nobody_stored_is_a_404(pg_dsn: str) -> None:
    """An empty about page for a key nobody stored would read "this record
    exists and the library derives nothing" -- the absence-as-zero mistake
    at the route level. The refusal, like the record route's, names the
    key."""
    async with serve(pg_dsn) as http:
        await _teach_about(http)
        got = await http.get("/ui/api/tenants/t1/about/shop_order/ghost")
        assert got.status_code == 404
        assert "ghost" in got.json()["detail"], (
            "the refusal names the key -- and distinguishes 'no such record' "
            "from a route that does not exist"
        )


# ----------------------------------------------------------------- bundles --

BUNDLED_SOURCE = COURIER_SOURCE + '''
# The courier's tile.
bundle shop_courier.card:
    carrying = figure shop_courier.carrying
    band = figure shop_courier.load_band
'''


async def test_the_editor_diff_sees_a_recomposed_or_deleted_bundle(pg_dsn: str) -> None:
    """The bundle's hash exists for exactly one purpose -- a changed tile is
    a moved hash on the review surface -- so the editor's diff must see it.
    Blind, a reordered tile (which changes the positions a screen binds to)
    and a deleted one both reported "everything unchanged", which is a review
    record that lies about what the save did."""
    async with serve(pg_dsn) as http:
        await http.put("/schema", json=COURIER_WORLD.to_document())
        put = await http.put("/definitions", json={"source": BUNDLED_SOURCE})
        assert put.status_code == 200, put.text

        reordered = BUNDLED_SOURCE.replace(
            "    carrying = figure shop_courier.carrying\n    band = figure shop_courier.load_band\n",
            "    band = figure shop_courier.load_band\n    carrying = figure shop_courier.carrying\n",
        )
        out = (await http.post("/ui/api/check", json={"source": reordered})).json()
        assert out["ok"] is True, out
        changes = {d["name"]: (d["kind"], d["change"]) for d in out["declarations"]}
        assert changes["shop_courier.card"] == ("bundle", "changed"), changes
        assert changes["shop_courier.carrying"] == ("figure", "unchanged")

        out = (await http.post("/ui/api/check", json={"source": COURIER_SOURCE})).json()
        changes = {d["name"]: (d["kind"], d["change"]) for d in out["declarations"]}
        assert changes["shop_courier.card"] == ("bundle", "removed"), (
            "a deleted tile must be on the review record of what the save loses"
        )


async def test_the_world_payload_lists_a_bundle_with_its_slots(pg_dsn: str) -> None:
    """A bundle is a declaration like any other, so the catalogue must carry
    it: its review hash, its prose, its source as written, and -- because a
    slot is the address a client binds to -- the slot-to-member table in
    declaration order, window arguments included. A catalogue blind to
    bundles would leave the one composed surface unverifiable from the UI."""
    from .test_bundle import SERVE_SOURCE, SERVE_WORLD

    async with serve(pg_dsn) as http:
        assert (
            await http.put("/schema", json=SERVE_WORLD.to_document())
        ).status_code == 200
        put = await http.put("/definitions", json={"source": SERVE_SOURCE})
        assert put.status_code == 200, put.text

        world = (await http.get("/ui/api/world")).json()
        by_name = {d["name"]: d for d in world["declarations"]}
        assert "shop_courier.card" in by_name, sorted(by_name)
        card = by_name["shop_courier.card"]

        local = compile_source(SERVE_SOURCE, SERVE_WORLD)
        plan = local.bundle("shop_courier.card")
        assert plan is not None
        assert card["kind"] == "bundle"
        assert card["version"] == plan.version, (
            "the hash served is the one the committed artifact carries -- the "
            "review surface breaks if the two drift"
        )
        assert card["doc"] == "The courier tile."
        assert "typical = reading shop_courier.typical_ride" in card["source"]

        assert [
            (s["slot"], s["kind"], s["name"], s["windows"]) for s in card["slots"]
        ] == [
            ("typical", "reading", "shop_courier.typical_ride", ["9", "1"]),
            ("carrying", "figure", "shop_courier.carrying", None),
            ("board", "projection", "shop_order.board", None),
            ("book", "summary", "shop_order.book", None),
        ], "slots travel in declaration order, window arguments spelled canonically"

        # The edges make the members reachable and the closure honest: the
        # trace from the tile must bottom out at the same records and dials
        # that can move the numbers on it.
        rests = {(d["type"], d["name"]) for d in card["rests_on"]}
        assert rests == {
            ("reading", "shop_courier.typical_ride"),
            ("figure", "shop_courier.carrying"),
            ("projection", "shop_order.board"),
            ("summary", "shop_order.book"),
        }
        moved = {(d["type"], d["name"]) for d in card["moved_by"]}
        assert ("fact", "shop_order") in moved
        assert ("fact", "shop_courier") in moved
        assert ("setting", "tenant.timezone") in moved


async def test_a_bundle_answers_through_the_ui_route_with_each_members_own_provenance(
    pg_dsn: str,
) -> None:
    """The UI's results route serves the tile exactly as the API does: members
    under their slots in declaration order, each member's Result citing its
    own name and version (the bundle hash cites nothing), the summarise member
    travelling as a population row with no subject rows, and a reading
    member's windows resolved to the spans the bundle declared."""
    from datetime import UTC, datetime, timedelta

    from .test_bundle import SERVE_SOURCE, SERVE_WORLD

    async with serve(pg_dsn) as http:
        assert (
            await http.put("/schema", json=SERVE_WORLD.to_document())
        ).status_code == 200
        put = await http.put("/definitions", json={"source": SERVE_SOURCE})
        assert put.status_code == 200, put.text

        now = datetime.now(tz=UTC)
        iso = lambda dt: dt.isoformat()  # noqa: E731
        pushed = await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "shop_courier": {"c1": {"name": "Aki"}},
                    "shop_order": {
                        "r1": {
                            "ref": "R-1",
                            "courier_id": "c1",
                            "status": "delivered",
                            "picked_up_at": iso(now - timedelta(hours=26)),
                            "delivered_at": iso(now - timedelta(hours=24)),
                        },
                        "r2": {
                            "ref": "R-2",
                            "courier_id": "c1",
                            "status": "delivered",
                            "picked_up_at": iso(now - timedelta(hours=2)),
                            "delivered_at": iso(now - timedelta(hours=1)),
                        },
                        "r3": {"ref": "R-3", "courier_id": "c1", "status": "riding"},
                    },
                }
            },
        )
        assert pushed.status_code == 200, pushed.text

        answer = await http.get("/ui/api/tenants/t1/results/shop_courier.card")
        assert answer.status_code == 200, answer.text
        card = answer.json()
        assert card["kind"] == "bundle"

        local = compile_source(SERVE_SOURCE, SERVE_WORLD)
        plan = local.bundle("shop_courier.card")
        assert plan is not None
        assert card["version"] == plan.version

        assert [
            (m["slot"], m["result"]["kind"], m["result"]["name"])
            for m in card["results"]
        ] == [
            ("typical", "reading", "shop_courier.typical_ride"),
            ("carrying", "figure", "shop_courier.carrying"),
            ("board", "projection", "shop_order.board"),
            ("book", "summary", "shop_order.book"),
        ]

        by_slot = {m["slot"]: m["result"] for m in card["results"]}
        reading_plan = local.reading("shop_courier.typical_ride")
        figure_plan = local.figure("shop_courier.carrying")
        assert reading_plan is not None and figure_plan is not None
        assert by_slot["typical"]["version"] == reading_plan.version, (
            "a member cites its own version, never the bundle's"
        )
        assert by_slot["carrying"]["version"] == figure_plan.version

        [subject] = by_slot["typical"]["subjects"]
        assert [(w["span"], w["bucket"]) for w in subject["windows"]] == [
            ("9", "day"),
            ("1", "day"),
        ], "the windows are the bundle's own declaration, span by span"

        book = by_slot["book"]
        assert book["subjects"] == [], (
            "a summarise member travels without its projection's rows"
        )
        assert book["summary"] is not None
        assert book["summary"]["display"].get("orders") == "3", (
            "the population row counts every row, not the page the projection's "
            "limit serves"
        )

        board = by_slot["board"]
        assert [s["row"]["display"]["ref"] for s in board["subjects"]] == [
            "R-1",
            "R-2",
        ], "the projection member keeps its own sort and limit"


async def test_a_bundle_on_the_wrong_route_gets_a_forwarding_address(
    pg_dsn: str,
) -> None:
    """The membership and measured routes must know the name, or their 404
    dead-ends the exact reader the drill-down exists for."""
    async with serve(pg_dsn) as http:
        await http.put("/schema", json=COURIER_WORLD.to_document())
        put = await http.put("/definitions", json={"source": BUNDLED_SOURCE})
        assert put.status_code == 200, put.text

        refused = await http.get("/ui/api/tenants/t1/membership/shop_courier.card")
        assert refused.status_code == 404
        assert "bundle" in refused.json()["detail"], refused.text
        assert "results" in refused.json()["detail"]
