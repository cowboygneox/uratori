"""The built-in investigation UI: every declaration, every fact, every cascade.

The UI exists so a developer can stand behind the firewall and ask "what does
this deployment know, and why does this number say what it says" without
checking a repository out. These tests pin the three claims that make it an
investigation tool rather than a status page:

- **The world payload is complete.** Every declaration of every kind -- indexes
  and measures included, though they have no version of their own -- with its
  source text and its dependencies, typed all the way down to the fact kinds,
  so a reader can walk from any definition to the records it stands on.
- **The activity log is a cascade record.** A pushed fact leaves a persisted
  run whose movements say which figures moved and to what, frozen at the
  moment it happened.
- **The security posture is deliberate.** Unauthenticated by design (a
  firewall is the door), which is exactly why it must be OFF by default the
  moment the API itself is token-protected -- a token plus a silently open UI
  would leak everything the token guards.
"""

from __future__ import annotations

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
    """Indexes and measures included: they are the declarations the API's own
    LibraryOut reduces to counts, and the ones an investigator most needs --
    they are where a definition touches the facts."""
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
        assert carried_by["kind"] == "index"
        assert carried_by["version"] is None, (
            "an index has no version of its own -- its text is hashed into "
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
        assert ("index", "shop_order.carried_by") in rests
        assert ("index", "shop_order.open") in rests
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
    second request per hop, or one whose edges dead-end at an index, would
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
    """None, not a KeyError and not str(whatever was there): the name column
    is the schema's claim applied to the record, and a record that does not
    carry the field has no name to claim."""
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
        assert names == {"bare": None, "odd": None}


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

# Every declaration kind and every index spec the language has, so the world
# payload's dependency edges are pinned where they actually vary: a composite
# index hopping `through` another kind and bucketing `by day` in a zone dial,
# an age index reading a threshold, measures, a windowed and a live reading,
# a projection and its summary. The courier corpus alone cannot do this -- it
# holds two indexes and two figures, and a payload whose contract is "every
# declaration of every kind" needs a corpus that has them.
FULL_SOURCE = """
index code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
index code_review_request.asked_of from reviewer_account_id through team_person.accounts.account_id
index code_review_request.pending where pending == true
index code_change.stale where updated_at older than thresholds.staleChangeDays

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
            "code_change.merged_by_day": "index",
            "code_review_request.asked_of": "index",
            "code_review_request.pending": "index",
            "code_change.stale": "index",
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

        # A composite index: its own kind, the kind it hops through, and the
        # zone dial that decides which day a bucket is.
        assert rests("code_change.merged_by_day") == {
            ("fact", "code_change"),
            ("fact", "team_person"),
            ("setting", "tenant.timezone"),
        }
        # An age index reads a threshold dial.
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

        # A live reading: the measure it reads and the indexes that scope it.
        live = rests("team_person.queue")
        assert ("measure", "code_review_request.waiting_seconds") in live
        assert ("index", "code_review_request.asked_of") in live
        assert ("index", "code_review_request.pending") in live
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
