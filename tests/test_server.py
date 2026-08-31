"""The service end to end: taught over HTTP, fed facts, asked for answers.

Everything here goes through the same door a host uses -- the HTTP API against
a real Postgres -- because the service's whole claim is "start the container,
teach it, push facts, read answers", and a claim like that is only worth what
an end-to-end test says it is.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import httpx
import pytest

from uratori import compile_source
from uratori.server import create_app

from .test_schema import COURIER_SOURCE, COURIER_WORLD


@dataclass
class Server:
    http: httpx.AsyncClient
    app: Any
    pg_schema: str


@pytest.fixture
async def server(pg_dsn: str) -> AsyncIterator[Server]:
    """A fresh service on a Postgres schema of its own, torn down after.

    Fresh per test rather than shared, because half of what is tested here is
    the *unconfigured* states -- a shared taught world would make "409 before a
    schema is declared" impossible to reach after the first test ran.
    """
    name = f"uratori_srv_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()

    app = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://uratori") as http:
            yield Server(http=http, app=app, pg_schema=name)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


async def _teach(http: httpx.AsyncClient) -> dict[str, Any]:
    put = await http.put("/schema", json=COURIER_WORLD.to_document())
    assert put.status_code == 200, put.text
    put = await http.put("/definitions", json={"source": COURIER_SOURCE})
    assert put.status_code == 200, put.text
    return dict(put.json())


def _orders(n: int) -> dict[str, dict[str, Any]]:
    return {
        f"o{i}": {"ref": f"A-{i}", "courier_id": "c1", "status": "riding"} for i in range(n)
    }


def _more_orders(n: int) -> dict[str, dict[str, Any]]:
    """Keys disjoint from `_orders`, for batches that must not overwrite."""
    return {
        f"x{i}": {"ref": f"B-{i}", "courier_id": "c1", "status": "riding"} for i in range(n)
    }


COURIER = {"c1": {"name": "Aki"}}


async def test_an_untaught_server_names_what_is_missing(server: Server) -> None:
    """409 with which step, not 500: an unconfigured server is a state the
    client can fix, and it must be told whether the gap is the schema or the
    definitions."""
    health = (await server.http.get("/health")).json()
    assert health["ready"] is False

    refused = await server.http.post("/tenants/t1/facts", json={"writes": {}})
    assert refused.status_code == 409
    assert "schema" in refused.json()["detail"].lower()

    assert (await server.http.put("/schema", json=COURIER_WORLD.to_document())).status_code == 200

    refused = await server.http.post("/tenants/t1/facts", json={"writes": {}})
    assert refused.status_code == 409
    assert "definitions" in refused.json()["detail"].lower()


async def test_definitions_are_refused_in_the_checkers_own_words(server: Server) -> None:
    """The checker's refusals are the language's whole safety story; an API
    that flattened them to 'invalid' would send an author looking for a typo."""
    await server.http.put("/schema", json=COURIER_WORLD.to_document())
    refused = await server.http.put(
        "/definitions", json={"source": "filter work_issue.active where active == true\n"}
    )
    assert refused.status_code == 422
    assert "not a fact kind" in refused.json()["detail"]

    health = (await server.http.get("/health")).json()
    assert health["ready"] is False, "a refused load must leave nothing half-taught"


async def test_a_definition_that_does_not_even_parse_is_still_a_422(server: Server) -> None:
    """The 422 promise covers the whole compile, not just the checker. A
    missing colon is the first mistake a new author makes, and it fails one
    layer before the checker -- in the lexer or parser. A route that caught
    only `CheckError` turned exactly that mistake into a bare 500, with the
    parser's message (which names the line) discarded into the server log
    where the author cannot see it."""
    await server.http.put("/schema", json=COURIER_WORLD.to_document())
    refused = await server.http.put(
        "/definitions", json={"source": "this is not a definition\n"}
    )
    assert refused.status_code == 422, refused.text
    assert "line 1" in refused.json()["detail"]

    health = (await server.http.get("/health")).json()
    assert health["ready"] is False, "a refused load must leave nothing half-taught"


async def test_taught_fed_and_asked_end_to_end(server: Server) -> None:
    versions = await _teach(server.http)
    # The versions the server reports are the ones this build compiled locally
    # -- the check a host runs to know the server serves what was reviewed.
    local = compile_source(COURIER_SOURCE, COURIER_WORLD)
    assert {d["name"]: d["version"] for d in versions["figures"]} == {
        p.name: p.version for p in local.figures
    }

    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    assert pushed.status_code == 200, pushed.text
    run = pushed.json()
    assert run["written"] == 4
    moved = {c["figure"]: c for c in run["shown"]}
    assert moved["shop_courier.carrying"]["after_display"] == "3"
    assert moved["shop_courier.load_band"]["after_display"] == "over"
    assert {r["name"] for r in run["results"]} == {
        "shop_courier.carrying",
        "shop_courier.load_band",
    }

    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert answer.status_code == 200
    result = answer.json()
    assert result["state"]["ok"] is True
    assert [(s["name"], s["value"]) for s in result["subjects"]] == [("Aki", 3.0)]

    # The identical object, both doors: the run's copy and the route's copy.
    from_run = next(r for r in run["results"] if r["name"] == "shop_courier.carrying")
    assert from_run["subjects"] == result["subjects"]

    missing = await server.http.get("/tenants/t1/results/shop_courier.unheard_of")
    assert missing.status_code == 404

    # An identical re-push moves nothing and says so.
    again = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    assert again.json()["written"] == 0
    assert again.json()["changed"] == 0


async def test_a_deferred_batch_lands_facts_and_leaves_the_pass_to_the_closing_run(
    server: Server,
) -> None:
    """A bulk import is many batches and one computation. Without `defer`,
    every batch runs a pass over buckets the earlier batches already filled,
    so an import's cost grows with the square of its size -- the pass, not the
    writing, is what made a two-million-record import take days. `defer`
    writes a batch and runs nothing; the import closes with one full run."""
    await _teach(server.http)

    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}, "defer": True},
    )
    assert pushed.status_code == 200, pushed.text
    run = pushed.json()
    assert run["written"] == 4
    assert run["changed"] == 0
    assert run["results"] == []
    # Nothing was rebuilt, read or shown -- `covered` in particular is what a
    # host re-dates evidence on, and a deferred write confirmed nothing.
    assert (run["rebuilt"], run["covered"], run["shown"]) == ([], [], [])

    # Nothing has computed, and the answer says so honestly -- deferred facts
    # are facts the engine has not yet looked at, not a page of zeroes.
    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert answer.json()["state"]["ok"] is False
    assert answer.json()["state"]["because"] == "never-computed"

    closed = await server.http.post("/tenants/t1/runs", json={"full": True})
    assert closed.status_code == 200, closed.text
    assert closed.json()["changed"] > 0

    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    result = answer.json()
    assert result["state"]["ok"] is True
    assert [(s["name"], s["value"]) for s in result["subjects"]] == [("Aki", 3.0)]


async def test_a_deferred_batch_is_still_verified_whole(server: Server) -> None:
    """Deferring the pass must not defer the write boundary: a record that
    does not match the declared world lands nowhere, batch-mates included,
    exactly as it would on an ordinary push. Taught through `fact`
    declarations, because only a declared world has a boundary to enforce."""
    schema = {
        "kinds": [],
        "figure_settings": ["limits.carrying.over"],
        "defaults": {"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}},
    }
    declared = (
        "# An order.\n"
        "fact shop_order:\n"
        "    ref as text\n"
        "    courier_id as text\n"
        "    status as text\n"
        "# A courier.\n"
        "fact shop_courier:\n"
        "    name display_name\n"
        "    display_name as text\n"
        "group shop_order.carried_by from courier_id\n"
        "# Orders in this courier's hands.\n"
        "figure shop_courier.carrying:\n"
        '    display "{value} orders in hand"\n'
        "    depends:\n"
        "        mine = shop_order.carried_by:{shop_courier}\n"
        "    calculate:\n"
        "        count(mine)\n"
    )
    assert (await server.http.put("/schema", json=schema)).status_code == 200
    assert (
        await server.http.put("/definitions", json={"source": declared})
    ).status_code == 200, "the fact-declared world must teach cleanly"

    # A healthy courier and a healthy-looking order beside the mistyped one:
    # if per-record quarantine crept in, the batch-mates would land, the
    # order would bucket under c1, and the closing run would serve a
    # confident count of 1 -- exactly the narrowed population the whole-batch
    # refusal exists to prevent.
    refused = await server.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": {"c1": {"display_name": "Aki"}},
                "shop_order": {
                    "o1": {"ref": 7, "courier_id": "c1", "status": "riding"},
                    "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"},
                },
            },
            "defer": True,
        },
    )
    assert refused.status_code == 422, refused.text

    closed = await server.http.post("/tenants/t1/runs", json={"full": True})
    assert closed.status_code == 200
    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert answer.json()["state"]["because"] == "nothing-collected", (
        "nothing may land from a refused batch -- batch-mates included"
    )


async def test_a_deferred_delete_lands_and_the_closing_run_sweeps_it(server: Server) -> None:
    """Deletes ride deferred batches too: the rows go, nothing recomputes,
    and the stored answers honestly describe the pre-import world until the
    closing full run sweeps the departed records out of every figure."""
    await _teach(server.http)
    first = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    assert first.status_code == 200

    deferred = await server.http.post(
        "/tenants/t1/facts", json={"deletes": {"shop_order": ["o0"]}, "defer": True}
    )
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["deleted"] == 1
    assert deferred.json()["changed"] == 0

    # The stored answer still says 3: the pass has not run, and that is the
    # documented deal -- stale-but-computed, never a silently wrong rebuild.
    held = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert [s["value"] for s in held.json()["subjects"]] == [3.0]

    closed = await server.http.post("/tenants/t1/runs", json={"full": True})
    assert closed.status_code == 200
    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert [s["value"] for s in answer.json()["subjects"]] == [2.0]


async def test_a_deferred_debt_escalates_the_next_pass_whatever_its_shape(
    server: Server,
) -> None:
    """The documented close is `POST /runs {"full": true}` -- but a caller
    who closes any other way must not be served stale values as current,
    silently, for ever. A deferred batch leaves a debt on the tenant, and
    the next pass -- here a plain warm run -- runs full and settles it. The
    tenant starts *warm* (pointers current from an ordinary push), because
    on a cold tenant every pass rebuilds anyway and the debt would be
    invisible."""
    await _teach(server.http)
    first = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    assert first.status_code == 200

    deferred = await server.http.post(
        "/tenants/t1/facts", json={"writes": {"shop_order": _more_orders(2)}, "defer": True}
    )
    assert deferred.status_code == 200 and deferred.json()["written"] == 2

    # The wrong close: a warm run, not the documented full one. The debt
    # escalates it, so the deferred orders are computed all the same.
    closed = await server.http.post("/tenants/t1/runs", json={})
    assert closed.status_code == 200
    assert closed.json()["rebuilt"], "the owed pass must escalate to full"
    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert [s["value"] for s in answer.json()["subjects"]] == [5.0]

    # Settled: the next warm run is ordinary again, and does nothing.
    quiet = await server.http.post("/tenants/t1/runs", json={})
    assert quiet.json()["rebuilt"] == [] and quiet.json()["changed"] == 0


async def test_an_ordinary_push_settles_a_deferred_debt_too(server: Server) -> None:
    """The other wrong close: deferred batches followed by an ordinary push.
    Without the debt, that push's warm pass would recount only its own
    batch's buckets -- the deferred records were never indexed, so the
    served count would confidently miss them."""
    await _teach(server.http)
    first = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(1)}},
    )
    assert first.status_code == 200

    deferred = await server.http.post(
        "/tenants/t1/facts", json={"writes": {"shop_order": _more_orders(2)}, "defer": True}
    )
    assert deferred.status_code == 200

    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_order": {"o9": {"ref": "A-9", "courier_id": "c1", "status": "riding"}}}},
    )
    assert pushed.status_code == 200
    assert pushed.json()["rebuilt"], "the owed pass must escalate this push to full"
    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert [s["value"] for s in answer.json()["subjects"]] == [4.0]


async def test_defer_and_full_together_are_refused(server: Server) -> None:
    """`full` demands the most expensive pass and `defer` demands none;
    honouring either would silently ignore the other, so the contradiction is
    named instead of resolved."""
    await _teach(server.http)
    refused = await server.http.post(
        "/tenants/t1/facts", json={"writes": {}, "defer": True, "full": True}
    )
    assert refused.status_code == 422
    assert "defer" in refused.json()["detail"]


async def test_a_departed_subject_is_a_reported_removal(server: Server) -> None:
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
    )
    gone = await server.http.post(
        "/tenants/t1/facts", json={"deletes": {"shop_courier": ["c1"]}}
    )
    kinds = {(c["figure"], c["kind"]) for c in gone.json()["shown"]}
    assert ("shop_courier.carrying", "removed") in kinds, (
        "a subject leaving the board silently is the exact failure the change "
        "stream exists to end"
    )


async def test_a_moved_threshold_rebuilds_and_rebands(server: Server) -> None:
    """The threshold moved here by editing the definition, because that is
    where a threshold lives now: it used to be a dial, and a settings save was
    the trigger. What must hold is unchanged either way -- the figures whose
    answer the threshold decides go pending, and silently keeping their
    pointers means banding against the old number for ever."""
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    before = (await server.http.get("/tenants/t1/results/shop_courier.load_band")).json()
    assert before["subjects"][0]["value"] == "over"

    saved = await server.http.put(
        "/definitions",
        json={"source": COURIER_SOURCE.replace("carrying >= 3", "carrying >= 10")},
    )
    assert saved.status_code == 200, saved.text

    ran = await server.http.post("/tenants/t1/runs", json={})
    assert "shop_courier.load_band" in ran.json()["rebuilt"], (
        "the moved threshold must make the figure pending"
    )
    after = (await server.http.get("/tenants/t1/results/shop_courier.load_band")).json()
    assert after["subjects"][0]["value"] == "ok"


async def test_a_restart_restores_the_taught_world(server: Server, pg_dsn: str) -> None:
    """The schema document and the source survive; the library is recompiled
    from source at boot. A server that forgot its world on restart would need
    re-teaching before every answer, which is a cache, not a service."""
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
    )

    second = create_app(dsn=pg_dsn, pg_schema=server.pg_schema, token=None, version="test2")
    async with second.router.lifespan_context(second):
        transport = httpx.ASGITransport(app=second)
        async with httpx.AsyncClient(transport=transport, base_url="http://second") as http:
            health = (await http.get("/health")).json()
            assert health["ready"] is True
            answer = (await http.get("/tenants/t1/results/shop_courier.carrying")).json()
            assert answer["state"]["ok"] is True
            assert answer["subjects"][0]["value"] == 2.0


async def test_a_schema_change_that_breaks_the_definitions_is_refused_whole(
    server: Server,
) -> None:
    await _teach(server.http)
    smaller = COURIER_WORLD.to_document()
    # A world that is valid on its own -- the kind and its name field both gone
    # -- but that the loaded definitions name on their first line.
    smaller["kinds"] = ["shop_courier"]
    smaller["name_fields"] = {"shop_courier": "name"}
    smaller["url_fields"] = {}
    refused = await server.http.put("/schema", json=smaller)
    assert refused.status_code == 422
    assert "do not compile" in refused.json()["detail"]

    # The old world still stands, whole: a half-applied schema would be a
    # server that cannot rebuild its own library at the next boot.
    still = await server.http.post(
        "/tenants/t1/facts", json={"writes": {"shop_courier": COURIER}}
    )
    assert still.status_code == 200


async def test_tenants_are_data_partitions(server: Server) -> None:
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(1)}},
    )
    other = (await server.http.get("/tenants/t2/results/shop_courier.carrying")).json()
    assert other["state"]["ok"] is False
    assert other["state"]["because"] == "never-computed"


async def test_deleting_a_tenant_reports_what_went(server: Server) -> None:
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
    )
    removed = (await server.http.delete("/tenants/t1")).json()
    assert removed["facts_removed"] == 3
    assert removed["values_removed"] > 0

    after = (await server.http.get("/tenants/t1/results/shop_courier.carrying")).json()
    assert after["state"]["because"] == "never-computed"

    # The per-grouping stamps are tenant state and must go with the tenant:
    # left behind, they say "these buckets are built" about buckets that no
    # longer exist -- so a re-created tenant under the same name would never
    # be rebuilt, and the projection gate and every membership page would
    # answer Ok over nothing. (The old whole-set marker had the same duty;
    # its table survives only as the upgrade seed and nothing writes it.)
    stamps = await server.app.state.uratori.pool.fetchval(
        "select count(*) from index_built where tenant_id = $1", "t1"
    )
    assert stamps == 0


async def test_the_token_gates_everything_but_health(pg_dsn: str) -> None:
    name = f"uratori_srv_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()
    app = create_app(dsn=pg_dsn, pg_schema=name, token="s3cret", version="test")
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                assert (await http.get("/health")).status_code == 200, (
                    "health stays open: a probe with a credential is a "
                    "credential in every probe log"
                )
                body = COURIER_WORLD.to_document()
                assert (await http.put("/schema", json=body)).status_code == 401
                bad = {"Authorization": "Bearer wrong"}
                assert (
                    await http.put("/schema", json=body, headers=bad)
                ).status_code == 401
                good = {"Authorization": "Bearer s3cret"}
                assert (
                    await http.put("/schema", json=body, headers=good)
                ).status_code == 200
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_the_push_contract_carries_the_providers_stamps(server: Server) -> None:
    """End to end: a later push with an older stamp must not move a figure.

    Without this the service trusts arrival order, and arrival order is a race
    between a webhook and a reconcile that read the provider's lagging index.
    """
    await _teach(server.http)
    fresh = await server.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": COURIER,
                "shop_order": {"o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"}},
            },
            "stamps": {"shop_order": {"o1": "2026-08-24T12:00:00Z"}},
        },
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["written"] == 2

    stale = await server.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_order": {"o1": {"ref": "A-1", "courier_id": "c1", "status": "delivered"}}
            },
            "stamps": {"shop_order": {"o1": "2026-08-24T11:00:00Z"}},
        },
    )
    assert stale.json()["written"] == 0, "the stale snapshot was applied"
    answer = (await server.http.get("/tenants/t1/results/shop_courier.carrying")).json()
    assert answer["subjects"][0]["value"] == 1.0, (
        "the pre-event record came back and un-carried the order"
    )


async def test_the_bulk_results_route_serves_the_whole_first_paint(server: Server) -> None:
    """`GET /tenants/{t}/results` is what a client's first paint reads: every
    servable figure and reading, projections last. A gate that slipped from
    "touched or everything" to "touched or nothing" would open every board
    blank until the next pass, with the singular route still green."""
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
    )
    listed = await server.http.get("/tenants/t1/results")
    assert listed.status_code == 200
    names = [r["name"] for r in listed.json()]
    assert names == ["shop_courier.carrying", "shop_courier.load_band"], (
        "first paint is missing definitions (or ordering drifted); a screen "
        "subscribing now renders a blank board over stored answers"
    )
    assert all(r["state"]["ok"] is True for r in listed.json())


async def test_a_boot_across_a_language_change_comes_up_unready_not_crash_looping(
    pg_dsn: str,
) -> None:
    """The stored source was written by an engine whose syntax has since moved
    on (the docstring removal is exactly such a change). If the boot re-raised
    the refusal, the upgraded container would crash-loop -- with the only fix,
    a corrected PUT /definitions, locked out behind the crash. Unready-with-a-
    schema is a state every client of this API already knows how to repair."""
    name = f"uratori_srv_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()

    try:
        app = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://uratori") as http:
                await _teach(http)

        # What an older engine stored: the docstring spelling the language has
        # since removed. Planted directly because no current build can be made
        # to store it -- that is the point.
        stale = 'figure shop_courier.carrying:\n    """d"""\n    display "x"\n'
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(
                f"update {name}.engine_world set source = $1 where id = 1", stale
            )
        finally:
            await connection.close()

        app = create_app(dsn=pg_dsn, pg_schema=name, token=None, version="test")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://uratori") as http:
                health = (await http.get("/health")).json()
                assert health["ready"] is False

                # The 409 must say the truth. "No definitions have been
                # loaded" would send the operator hunting a data loss when
                # the stored source is right there, refused by this build.
                refused = await http.get("/tenants/t1/results")
                assert refused.status_code == 409
                assert "do not compile under this build" in refused.json()["detail"]

                # The host's own teach order is schema first, then
                # definitions, retried forever. If PUT /schema 422s on the
                # stale stored source, the repair the boot promised is never
                # reached -- the crash loop just becomes an unready loop.
                put = await http.put("/schema", json=COURIER_WORLD.to_document())
                assert put.status_code == 200, put.text

                put = await http.put("/definitions", json={"source": COURIER_SOURCE})
                assert put.status_code == 200, put.text
                assert (await http.get("/health")).json()["ready"] is True
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema {name} cascade")
        finally:
            await connection.close()


async def test_a_served_answer_carries_the_definitions_explanation(server: Server) -> None:
    """The `#` comment above the declaration is the customer-facing definition,
    and `Result.doc` is the only route it takes to a reader. A wire that
    dropped it would leave every citation blank -- with the whole suite
    green, because nothing else reads it."""
    await _teach(server.http)
    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(2)}},
    )
    assert pushed.status_code == 200, pushed.text

    answer = await server.http.get("/tenants/t1/results/shop_courier.carrying")
    assert answer.status_code == 200
    assert answer.json()["doc"] == "How many orders this courier is carrying right now."


# ------------------------------------------------------------- evidence --


ORDERS_WITH_URLS = {
    "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding", "url": "https://shop/o1"},
    "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding", "url": "https://shop/o2"},
}

BOARD_EXTRA = """

# Orders still on the road.
projection shop_order.board:
    from shop_order.open

    field:
        ref = ref as text
"""


async def test_the_evidence_route_serves_the_records_behind_a_stored_value(
    server: Server,
) -> None:
    """The whole chain over HTTP: the citation stored by the pass, joined back
    to the records the host pushed, titles and links resolved through the
    schema's own declarations -- because "the rule is right and the route
    stopped calling it" is the gap every serving bug lives in."""
    await _teach(server.http)
    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": ORDERS_WITH_URLS}},
    )
    assert pushed.status_code == 200, pushed.text

    answer = await server.http.get("/tenants/t1/evidence/shop_courier.carrying?subject=c1")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["state"]["ok"] is True
    assert body["kind"] == "shop_order"
    assert body["display"] == "2"
    assert [m["key"] for m in body["members"]] == ["o1", "o2"]
    assert [m["title"] for m in body["members"]] == ["A-1", "A-2"]
    assert [m["url"] for m in body["members"]] == ["https://shop/o1", "https://shop/o2"]
    assert all(m["held"] for m in body["members"])
    # A count's members carry no per-record measurement: a "1" beside each
    # record would be a number nothing computed.
    assert [m["display"] for m in body["members"]] == [None, None]


async def test_the_evidence_refusals_each_say_where_the_evidence_lives(
    server: Server,
) -> None:
    """Collapsed into one generic "no figure called X", the reader who arrived
    from a projection row would be told their number does not exist -- when it
    exists and simply stores nothing."""
    put = await server.http.put("/schema", json=COURIER_WORLD.to_document())
    assert put.status_code == 200, put.text
    put = await server.http.put("/definitions", json={"source": COURIER_SOURCE + BOARD_EXTRA})
    assert put.status_code == 200, put.text

    projected = await server.http.get("/tenants/t1/evidence/shop_order.board?subject=o1")
    assert projected.status_code == 404
    assert "rows are the evidence" in projected.json()["detail"]

    unknown = await server.http.get("/tenants/t1/evidence/no.such?subject=o1")
    assert unknown.status_code == 404
    # The body, not just the status: an unmatched path is also a 404, so a
    # status-only assertion would stay green with the whole route deleted.
    assert "No figure called no.such" in unknown.json()["detail"]


async def test_evidence_for_a_subject_the_store_does_not_hold_is_a_404_with_the_reason(
    server: Server,
) -> None:
    await _teach(server.http)
    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": ORDERS_WITH_URLS}},
    )
    assert pushed.status_code == 200, pushed.text

    answer = await server.http.get("/tenants/t1/evidence/shop_courier.carrying?subject=c9")
    assert answer.status_code == 404
    assert "c9" in answer.json()["detail"]


# ------------------------------------------------------------ population --


async def test_a_projection_population_is_enforced_over_http(server: Server) -> None:
    """`from` end to end: the pass that pushes facts is also the pass that
    buckets the population, so the very first read after it must already be
    the filtered page -- and a delivered order must never get a row."""
    put = await server.http.put("/schema", json=COURIER_WORLD.to_document())
    assert put.status_code == 200, put.text
    put = await server.http.put("/definitions", json={"source": COURIER_SOURCE + BOARD_EXTRA})
    assert put.status_code == 200, put.text

    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": COURIER,
                "shop_order": {
                    "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"},
                    "o2": {"ref": "A-2", "courier_id": "c1", "status": "delivered"},
                },
            }
        },
    )
    assert pushed.status_code == 200, pushed.text

    answer = await server.http.get("/tenants/t1/results/shop_order.board")
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["state"]["ok"] is True
    assert [s["id"] for s in result["subjects"]] == ["o1"], (
        "the delivered order got a row: the population was not enforced"
    )


async def test_a_new_population_index_holds_the_page_behind_deploy_until_a_pass(
    server: Server,
) -> None:
    """The gate, over HTTP: between a definitions deploy that adds a
    population index and the tenant's next pass, the buckets do not exist,
    and an Ok page there would be a confident zero. The answer must be the
    honest absence -- and the next pass, even one carrying no facts, must
    repair it."""
    await _teach(server.http)
    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": COURIER,
                "shop_order": {
                    "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"},
                    "o2": {"ref": "A-2", "courier_id": "c1", "status": "delivered"},
                },
            }
        },
    )
    assert pushed.status_code == 200, pushed.text

    # The deploy introduces a NEW index -- one no pass has bucketed yet. (A
    # `from` over an index the last pass already built serves immediately;
    # the control for that is the enforcement test above.)
    grown = (
        COURIER_SOURCE
        + """

filter shop_order.underway where status == "riding"

# Orders being ridden right now.
projection shop_order.live_board:
    from shop_order.underway

    field:
        ref = ref as text

# The live board, in one row.
summarise shop_order.live_flow over shop_order.live_board:
    count riding
"""
    )
    put = await server.http.put("/definitions", json={"source": grown})
    assert put.status_code == 200, put.text

    held = await server.http.get("/tenants/t1/results/shop_order.live_board")
    assert held.status_code == 200
    state = held.json()["state"]
    assert state["ok"] is False
    assert state["because"] == "behind-deploy"
    assert held.json()["subjects"] == []
    # And no summary beside the absence: `summarise` is total, so a summary
    # served here would be a confident table of noughts under a state saying
    # the page is not available -- on exactly the screen the gate protects.
    assert held.json()["summary"] is None

    ran = await server.http.post("/tenants/t1/runs", json={})
    assert ran.status_code == 200, ran.text

    after = await server.http.get("/tenants/t1/results/shop_order.live_board")
    assert after.status_code == 200
    assert after.json()["state"]["ok"] is True
    assert [s["id"] for s in after.json()["subjects"]] == ["o1"]
    # The control: once the page is real, the summary counts its rows.
    summary = after.json()["summary"]
    assert summary is not None
    assert summary["values"]["riding"] == 1


# ------------------------------------------------------- the described library --


async def test_the_definitions_route_serves_the_library_described(server: Server) -> None:
    """GET /definitions is the review surface AND the only way an API-only
    host can show its readers what a number means: every declaration of all
    six kinds, each carrying its prose, its formula, and the names it rests
    on. A host that had to import this package to render a catalogue would
    be embedding the engine to describe the engine -- the exact coupling the
    service exists to end."""
    await _teach(server.http)
    got = await server.http.get("/definitions")
    assert got.status_code == 200, got.text
    body = got.json()

    by_name = {d["name"]: d for d in body["figures"]}
    carrying = by_name["shop_courier.carrying"]
    assert carrying["declaration"] == "figure"
    assert carrying["version"], "a figure without a version is not citable"
    assert carrying["prose"] == "How many orders this courier is carrying right now."
    assert "count(mine)" in carrying["source"]
    assert carrying["prose"] not in carrying["source"], "prose and formula must be told apart"
    assert "display" not in carrying["source"], "the display template is presentation, not formula"
    assert carrying["kind"] == "shop_courier"
    assert carrying["unit"] == "count"
    assert carrying["display"] == "{value} orders in hand"
    assert carrying["grain"] is None and carrying["across"] is None
    assert carrying["indexes"] == ["shop_order.carried_by", "shop_order.open"]
    assert carrying["reads"] == []
    assert carrying["banded"] is False

    band = by_name["shop_courier.load_band"]
    assert band["reads"] == ["shop_courier.carrying"], (
        "a rollup's derivation is the figure it combines; without it the "
        "catalogue cannot say what the word rests on"
    )
    assert band["settings"] == [], (
        "a calculation reads no dials now -- its numbers come from the figures "
        "it combines and from what the definition says outright"
    )
    assert band["band_reads"] == [], (
        "load_band computes its word in calculate:, so what it rests on is a "
        "figure it combines -- the split matters because a figure a band "
        "compares against never forces a rebuild, only a re-serve, and a "
        "description that unioned the two would tell a host to pay for one"
    )

    indexes = {d["name"]: d for d in body["indexes"]}
    carried = indexes["shop_order.carried_by"]
    assert carried["declaration"] == "group", "a fan-out is a group, and the word carries it"
    assert carried["version"] is None, (
        "groups and filters are hashed into their readers, not versioned alone"
    )
    assert carried["kind"] == "shop_order"
    assert carried["id_space"] == "shop_order"
    assert "from courier_id" in carried["source"]
    assert indexes["shop_order.open"]["declaration"] == "filter"

    # The versions are the same ones the compile reports -- one review
    # surface, whether read as JSON here or compiled locally by a build.
    local = compile_source(COURIER_SOURCE, COURIER_WORLD)
    assert {d["name"]: d["version"] for d in body["figures"]} == {
        p.name: p.version for p in local.figures
    }


async def test_the_described_library_covers_reading_projection_and_summary(
    server: Server,
) -> None:
    """The three kinds the courier fixture lacks, described the same way --
    pinned separately so the shape cannot rot into a figures-only feature."""
    world = COURIER_WORLD.to_document()
    world["kinds"] = sorted([*world["kinds"], "shop_review", "shop_queue"])
    world["bucket_settings"] = ["tenant.timezone", "limits.staleDays"]
    world["defaults"] = {
        **world["defaults"],
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {**world["defaults"]["limits"], "staleDays": 3},
    }
    put = await server.http.put("/schema", json=world)
    assert put.status_code == 200, put.text
    grown = (
        COURIER_SOURCE
        + """
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)
filter shop_order.stale where picked_up_at older than 5 days
filter shop_review.signed_off keyed as shop_order where approved == true

measure shop_order.riding_seconds = delivered_at - picked_up_at
measure shop_order.waiting_seconds = now - requested_at

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
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)
        series(rides)

# What is waiting right now.
reading shop_courier.queue():
    display "{value}"
    depends:
        waits = shop_order.waiting_seconds over (shop_order.carried_by:{shop_courier} & shop_order.open)
    calculate:
        count(waits)

# Orders still on the road, named for their queue.
projection shop_order.board:
    from shop_order.open

    field:
        ref = ref as text
        queue_name = name from queue_id through shop_queue.id as text

# The board, in one row.
summarise shop_order.flow over shop_order.board:
    count riding
"""
    )
    put = await server.http.put("/definitions", json={"source": grown})
    assert put.status_code == 200, put.text
    body = (await server.http.get("/definitions")).json()

    reading = {d["name"]: d for d in body["readings"]}["shop_courier.typical_ride"]
    assert reading["declaration"] == "reading"
    assert reading["mode"] == "window"
    assert reading["reads"] == ["shop_courier.ride_times"], (
        "a reading's evidence lives in the figure it summarises; the "
        "catalogue must say which"
    )
    assert reading["prose"] == "The typical ride, over a window."

    day_figure = {d["name"]: d for d in body["figures"]}["shop_courier.ride_times"]
    assert day_figure["grain"] == "day", (
        "a host needs the grain to keep time-keyed figures off card surfaces"
    )

    projection = {d["name"]: d for d in body["projections"]}["shop_order.board"]
    assert projection["declaration"] == "projection"
    assert projection["kind"] == "shop_order"
    assert projection["indexes"] == ["shop_order.open"], (
        "the population's indexes are what the page is drawn from"
    )
    assert "from shop_order.open" in projection["source"]

    summary = {d["name"]: d for d in body["summaries"]}["shop_order.flow"]
    assert summary["over"] == "shop_order.board", (
        "a summary means nothing without the projection it counts"
    )

    measure = {d["name"]: d for d in body["measures"]}["shop_order.riding_seconds"]
    assert measure["declaration"] == "measure"
    assert measure["kind"] == "shop_order"
    assert measure["version"] is None
    assert measure["unit"] == "duration", "a span between two moments is a duration"
    assert measure["fields"] == ["delivered_at", "picked_up_at"]

    clock = {d["name"]: d for d in body["measures"]}["shop_order.waiting_seconds"]
    assert clock["fields"] == ["requested_at"], (
        "`now` is the clock, not a record field -- a drift guard told to look "
        "for a field called now alarms for ever"
    )

    live = {d["name"]: d for d in body["readings"]}["shop_courier.queue"]
    assert live["mode"] == "live"
    assert live["measures"] == ["shop_order.waiting_seconds"]
    assert live["statistics"] == ["count"]

    window = {d["name"]: d for d in body["readings"]}["shop_courier.typical_ride"]
    assert window["statistics"] == ["mean", "series"], (
        "the statistics a reading calculates are what a host may bind columns "
        "to; the others are absent from its answers, not zero"
    )

    board = {d["name"]: d for d in body["projections"]}["shop_order.board"]
    assert board["fields"] == ["ref", "queue_id"], (
        "a joined field reads OUR record through its local linking path; "
        "serving the other kind's path here broke the drift guard both ways"
    )
    assert board["through"] == ["shop_queue.id"]

    stale = {d["name"]: d for d in body["indexes"]}["shop_order.stale"]
    assert stale["declaration"] == "filter"
    assert stale["settings"] == [], (
        "an age filter against a written threshold reads no dial at all -- a "
        "host told otherwise would offer an operator a control that moves "
        "nothing"
    )

    by_day = {d["name"]: d for d in body["indexes"]}["shop_order.delivered_by_day"]
    assert by_day["settings"] == ["tenant.timezone"]
    assert by_day["grain"] == "day", "the declaration that carries the truncation says so"

    keyed = {d["name"]: d for d in body["indexes"]}["shop_review.signed_off"]
    assert keyed["kind"] == "shop_review"
    assert keyed["id_space"] == "shop_order", (
        "`keyed as` is the whole difference between kind and id space; serving "
        "kind in both slots would hide it"
    )

    # Every declaration serves a formula. A blank one means the source
    # scanner missed a declaration shape -- the absence-that-says-nothing
    # this payload must never ship.
    for entry in (
        *body["figures"], *body["readings"], *body["projections"],
        *body["summaries"], *body["indexes"], *body["measures"],
    ):
        assert entry["source"].strip(), f"{entry['name']} serves a blank formula"


async def test_the_described_library_names_the_fields_a_declaration_reads(
    server: Server,
) -> None:
    """The paths are what a host's own drift guard needs: "every field my
    definitions read exists on the records I collect" is the host's claim to
    hold, and it can only hold it without compiling anything if the wire says
    which fields those are."""
    await _teach(server.http)
    body = (await server.http.get("/definitions")).json()

    carried = {d["name"]: d for d in body["indexes"]}["shop_order.carried_by"]
    assert carried["fields"] == ["courier_id"]
    assert carried["through"] == []


# ---------------------------------------------------------------- anchors --

RIDES_SOURCE = (
    COURIER_SOURCE
    + """
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)

measure shop_order.riding_seconds = delivered_at - picked_up_at

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
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)
"""
)

# Kiritimati is UTC+14 with no DST: the local calendar furthest from UTC's, so
# an anchor resolved against UTC instead of the tenant's zone misfiles a whole
# day of rides -- the sharpest data these tests can hold the server to.
RIDES_ZONE = "Pacific/Kiritimati"

RIDES = {
    # Local day 2026-06-28: a one-hour ride.
    "r1": {
        "ref": "R-1",
        "courier_id": "c1",
        "status": "delivered",
        "picked_up_at": "2026-06-27T19:00:00Z",
        "delivered_at": "2026-06-27T20:00:00Z",
    },
    # Local day 2026-06-30: a two-hour ride.
    "r2": {
        "ref": "R-2",
        "courier_id": "c1",
        "status": "delivered",
        "picked_up_at": "2026-06-29T18:00:00Z",
        "delivered_at": "2026-06-29T20:00:00Z",
    },
    # Delivered on 2026-06-30 *UTC*, but the tenant's 2026-07-01: past an
    # anchor of 2026-06-30, so it belongs in no anchored window below.
    "r3": {
        "ref": "R-3",
        "courier_id": "c1",
        "status": "delivered",
        "picked_up_at": "2026-06-30T09:00:00Z",
        "delivered_at": "2026-06-30T12:00:00Z",
    },
}


async def _teach_rides(http: httpx.AsyncClient, zone: str = RIDES_ZONE) -> None:
    world = COURIER_WORLD.to_document()
    world["bucket_settings"] = ["tenant.timezone"]
    world["defaults"] = {
        **world["defaults"],
        "tenant": {"hoursPerDay": 8, "timezone": zone},
    }
    assert (await http.put("/schema", json=world)).status_code == 200
    put = await http.put("/definitions", json={"source": RIDES_SOURCE})
    assert put.status_code == 200, put.text
    pushed = await http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": RIDES}},
    )
    assert pushed.status_code == 200, pushed.text


async def test_an_anchored_reading_ends_its_windows_on_the_anchor_day(server: Server) -> None:
    """`?at=` is an argument, not a definition change: it moves which stored
    days a window covers and touches nothing else -- statistic, floor, band
    and version all serve unchanged. The windows must end on the anchor day
    in the *tenant's* calendar, and the ride the tenant filed under the next
    local day (r3, delivered 2026-06-30 by UTC's clock) must stay out."""
    await _teach_rides(server.http)

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "2026-06-30", "trailing": [7, 3]},
    )
    assert got.status_code == 200, got.text
    result = got.json()
    assert result["state"]["ok"] is True

    [subject] = result["subjects"]
    week, short = subject["windows"]
    assert (week["trailing"], week["frm"], week["to"]) == (7, "2026-06-24", "2026-06-30")
    assert (short["trailing"], short["frm"], short["to"]) == (3, "2026-06-28", "2026-06-30")
    for window in (week, short):
        assert window["mean"] == 5400.0, (
            "the mean should cover exactly the one-hour and two-hour rides; "
            "anything else means a day leaked across the anchor or fell out "
            "of the window"
        )
        assert window["buckets_covered"] == 2
    assert week["buckets_requested"] == 7 and short["buckets_requested"] == 3

    # The anchor travels on the answer as provenance: the served instant is
    # the anchor day's last moment in the tenant's calendar, not the wall
    # clock of whenever the request happened to run.
    served = datetime.fromisoformat(result["at"]).astimezone(ZoneInfo(RIDES_ZONE))
    assert served.date().isoformat() == "2026-06-30"
    assert (served.hour, served.minute, served.second) == (23, 59, 59)

    # The anchor is an argument, never part of the definition's identity: the
    # anchored answer and the current one cite the same version, or a cited
    # number's definition would depend on when it was asked about.
    unanchored = await server.http.get("/tenants/t1/results/shop_courier.typical_ride")
    assert unanchored.json()["version"] == result["version"]

    # The bulk route accepts the same anchor -- the first paint of a board
    # deliberately looking at June must not mix June readings with July ones.
    listed = await server.http.get(
        "/tenants/t1/results", params={"at": "2026-06-30", "trailing": 3}
    )
    assert listed.status_code == 200
    entry = next(r for r in listed.json() if r["name"] == "shop_courier.typical_ride")
    assert entry["subjects"][0]["windows"][0]["to"] == "2026-06-30"
    assert entry["subjects"][0]["windows"][0]["mean"] == 5400.0


async def test_an_unanchored_reading_still_ends_today(server: Server) -> None:
    """The control: `at` absent means now, exactly as before the anchor
    existed. The June rides sit outside a window ending today, so the honest
    unanchored answer is the absence machinery -- no subjects, a floor that
    names what fell short -- rather than June's mean served as current."""
    await _teach_rides(server.http)

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride", params={"trailing": 7}
    )
    assert got.status_code == 200, got.text
    result = got.json()
    assert result["state"]["ok"] is True

    today = datetime.now(tz=ZoneInfo(RIDES_ZONE)).date().isoformat()
    assert result["subjects"] == []
    [window] = result["empty"]["windows"]
    assert window["to"] == today
    assert window["mean"] is None
    assert window["buckets_covered"] == 0


async def test_an_anchor_before_any_data_is_an_absence_not_a_zero(server: Server) -> None:
    """A window anchored before anything was stored yields the same absence
    answer an empty board does: no subjects, statistics withheld with the
    floor named. Zeroes here would be numbers nobody measured."""
    await _teach_rides(server.http)

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "2020-01-01", "trailing": 7},
    )
    assert got.status_code == 200, got.text
    result = got.json()
    assert result["state"]["ok"] is True
    assert result["subjects"] == []
    [window] = result["empty"]["windows"]
    assert (window["frm"], window["to"]) == ("2019-12-26", "2020-01-01")
    assert window["mean"] is None
    assert window["buckets_covered"] == 0
    assert window["unmet"] == ["needs at least 1 value; there are 0"]


async def test_an_anchor_that_is_not_a_date_is_refused(server: Server) -> None:
    """Anything but YYYY-MM-DD is a 422, and the epoch numbers are the rows
    that earn this test: pydantic's lax `date` coerces a bare integer as a
    unix timestamp, so `?at=1782000000` would quietly become 2026-06-21 and
    serve a plausible window nobody asked for -- a wrong population wearing
    a right-looking label, to exactly the caller most likely to make the
    mistake, since epoch millis are what the engine speaks internally."""
    await _teach_rides(server.http)
    for wrong in ("June 30th", "2026-6-30", "20260630", "1782000000", "1782000000000", "0"):
        got = await server.http.get(
            "/tenants/t1/results/shop_courier.typical_ride", params={"at": wrong}
        )
        assert got.status_code == 422, f"{wrong!r}: {got.status_code} {got.text}"


async def test_an_anchor_at_the_calendars_edge_is_an_answer_not_a_500(server: Server) -> None:
    """9999-12-31 and 0001-01-01 are well-formed calendar days, so they get
    the same honest answer any other data-free anchor gets -- empty windows
    ending on the day asked about. Both used to overflow `date` arithmetic
    and surface as a 500, which reads as the server's fault for a question
    that merely has an empty answer."""
    await _teach_rides(server.http)
    for edge in ("9999-12-31", "0001-01-01"):
        got = await server.http.get(
            "/tenants/t1/results/shop_courier.typical_ride",
            params={"at": edge, "trailing": 30},
        )
        assert got.status_code == 200, f"{edge}: {got.status_code} {got.text}"
        [window] = got.json()["empty"]["windows"]
        assert window["to"] == edge
        assert window["buckets_covered"] == 0


async def test_the_calendars_far_edge_answers_west_of_utc_too(server: Server) -> None:
    """9999-12-31's last local millisecond in a zone west of UTC lands in the
    year 10000 by UTC's clock, which `datetime` cannot carry -- so the anchor
    used to leak as a traceback-worded 400 on the by-name route and an
    unhandled 500 on the bulk route. The Kiritimati fixture (east of UTC)
    never sees this, which is exactly why the zone is the test."""
    await _teach_rides(server.http, zone="America/New_York")

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "9999-12-31", "trailing": 30},
    )
    assert got.status_code == 200, f"{got.status_code} {got.text}"
    [window] = got.json()["empty"]["windows"]
    assert window["to"] == "9999-12-31"
    assert window["buckets_covered"] == 0

    listed = await server.http.get(
        "/tenants/t1/results", params={"at": "9999-12-31", "trailing": 30}
    )
    assert listed.status_code == 200, f"{listed.status_code} {listed.text}"


async def test_a_span_past_the_reach_ceiling_is_refused_on_both_routes(server: Server) -> None:
    """`?trailing=1000000` used to walk ~739k stored day points per subject
    before answering an empty window -- a request parameter must not be able
    to buy that walk. The refusal is a 422 that states the ceiling and why,
    on the by-name route and the bulk route alike."""
    await _teach_rides(server.http)

    for params in (
        {"trailing": "1000000"},
        {"trailing": "999991-1000000"},
        {"trailing": "each:1-1000000"},
    ):
        got = await server.http.get(
            "/tenants/t1/results/shop_courier.typical_ride", params=params
        )
        assert got.status_code == 422, f"{params}: {got.status_code} {got.text}"
        assert "3660" in got.text, got.text

        listed = await server.http.get("/tenants/t1/results", params=params)
        assert listed.status_code == 422, f"{params}: {listed.status_code}"

    # The boundary itself still answers.
    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride", params={"trailing": "3660"}
    )
    assert got.status_code == 200, got.text


async def test_each_expands_over_http_exactly_as_the_enumerated_spelling(
    server: Server,
) -> None:
    """`?trailing=each:1-3` is three one-bucket windows, nearest first,
    each with its own statistics and floor -- and byte-identical to the
    enumerated `1, 2-2, 3-3`, because the sugar expands at the door and
    nothing downstream can tell the spellings apart. An expansion that
    overlaps an enumerated span is the duplicate it looks like."""
    await _teach_rides(server.http)

    sugared = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "2026-06-30", "trailing": "each:1-3"},
    )
    assert sugared.status_code == 200, sugared.text
    [subject] = sugared.json()["subjects"]
    windows = subject["windows"]
    assert [w["span"] for w in windows] == ["1", "2-2", "3-3"]
    assert [w["to"] for w in windows] == ["2026-06-30", "2026-06-29", "2026-06-28"]

    spelled = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "2026-06-30", "trailing": ["1", "2-2", "3-3"]},
    )
    assert spelled.status_code == 200, spelled.text
    assert spelled.json()["subjects"] == sugared.json()["subjects"]

    overlapped = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"trailing": ["2-2", "each:1-3"]},
    )
    assert overlapped.status_code == 422, overlapped.text
    assert "twice" in overlapped.text


async def test_a_request_may_not_buy_unbounded_work_through_each(server: Server) -> None:
    """Two ceilings, and `each` is why the second exists.

    A span's bucket ceiling bounds one window. `each` turns one argument into
    one window *per bucket*, and the server answers every window for every
    subject -- so `each:1-3660` sits inside the bucket ceiling as a span while
    asking for 3,660 answers per subject. That product is what the window
    ceiling bounds, and nothing else does.

    Both refusals must arrive as 422s *before* the work: `each:1-20000000`
    once built twenty million one-bucket windows -- gigabytes of tuples on an
    unauthenticated route -- and only then found something to complain about.
    """
    await _teach_rides(server.http)

    for params, expected in (
        # Refused by the bucket ceiling, without expanding.
        ({"trailing": "each:1-20000000"}, "3660"),
        # Inside the bucket ceiling, refused by the window ceiling.
        ({"trailing": "each:1-3660"}, "366"),
        # The product across a list, every token of which is legal alone.
        ({"trailing": ["each:1-366", "each:1-10"]}, "366"),
    ):
        got = await server.http.get(
            "/tenants/t1/results/shop_courier.typical_ride", params=params
        )
        assert got.status_code == 422, f"{params}: {got.status_code} {got.text[:200]}"
        assert expected in got.text, got.text

        listed = await server.http.get("/tenants/t1/results", params=params)
        assert listed.status_code == 422, f"{params}: {listed.status_code}"

    # The boundary itself still answers, so the ceiling is a ceiling and not
    # a ban on `each`.
    ok = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"trailing": "each:1-366"},
    )
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["subjects"][0]["windows"]) == 366


async def test_a_malformed_window_token_is_a_422_never_a_coercion(server: Server) -> None:
    await _teach_rides(server.http)
    for wrong in ("0", "0-30", "60-31", "7.5", "30x", "1-2-3", "-30", "30-"):
        got = await server.http.get(
            "/tenants/t1/results/shop_courier.typical_ride", params={"trailing": wrong}
        )
        assert got.status_code == 422, f"{wrong!r}: {got.status_code} {got.text}"
    zero = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride", params={"trailing": "0-30"}
    )
    assert "anchor" in zero.text, "the 0-bound refusal must teach that day 1 is the anchor day"


async def test_offset_buckets_compose_with_the_anchor_over_http(server: Server) -> None:
    """`?trailing=1-3&trailing=4-6&at=2026-06-30`: two exact three-day
    buckets counted back from the anchor day. r2 (local 2026-06-30) sits in
    bucket 1-3 alone; r1 (local 2026-06-28) with it; nothing in 4-6 -- and
    each window's wire shape must say what was asked (`span`, `bucket`) and
    what was covered (`frm`/`to`), with `trailing` honest: null for the
    offset bucket rather than a number that reads like a trailing span."""
    await _teach_rides(server.http)

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"at": "2026-06-30", "trailing": ["1-3", "4-6"]},
    )
    assert got.status_code == 200, got.text
    [subject] = got.json()["subjects"]
    near, far = subject["windows"]

    assert (near["span"], near["bucket"]) == ("3", "day")
    assert near["trailing"] == 3, "1-3 IS the trailing three days, and may say so"
    assert (near["frm"], near["to"]) == ("2026-06-28", "2026-06-30")
    assert near["mean"] == 5400.0, "both rides sit inside the last three local days"

    assert (far["span"], far["bucket"]) == ("4-6", "day")
    assert far["trailing"] is None, (
        "an offset bucket wearing a trailing-looking number is the failure "
        "mode this shape exists to prevent"
    )
    assert (far["frm"], far["to"]) == ("2026-06-25", "2026-06-27")
    assert far["mean"] is None and far["buckets_covered"] == 0


async def test_a_duplicate_span_in_one_request_is_a_422(server: Server) -> None:
    """`?trailing=30&trailing=1-30` is the same window twice under two
    spellings -- refused the way a bundle's window list refuses its
    duplicates, and before any stored point is walked."""
    await _teach_rides(server.http)
    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride",
        params={"trailing": ["30", "1-30"]},
    )
    assert got.status_code == 422, f"{got.status_code} {got.text}"
    assert "twice" in got.text


QUARTER_RIDES_SOURCE = (
    RIDES_SOURCE
    + """
group shop_order.delivered_by_hour from (courier_id, delivered_at by hour in tenant.timezone)

# Every delivery's ride time, hour by hour.
figure shop_courier.ride_hours bucketed:
    display "{shop_courier} ride hours"
    depends:
        done = shop_order.delivered_by_hour:{shop_courier}
    calculate:
        list(shop_order.riding_seconds over done)

# The typical ride over recent hours.
reading shop_courier.recent_ride(range):
    display "{value}"
    depends:
        rides = shop_courier.ride_hours in range
    calculate:
        mean(rides)
"""
)


async def test_an_hour_span_serves_over_http_with_an_honest_wire_shape(
    server: Server,
) -> None:
    """The sub-day half of the wire contract, end to end: the figure's
    group declares `by hour`, so `?trailing=48` anchored on 2026-06-30
    covers that local day and the one before, hour bucket by hour bucket --
    r2 (local 2026-06-30) is in, r1 (local 2026-06-28) is out -- and the
    window says what it is: hour buckets, label bounds, `trailing` null
    because only a day sequence's trailing span is a count of days."""
    world = COURIER_WORLD.to_document()
    world["bucket_settings"] = ["tenant.timezone"]
    world["defaults"] = {
        **world["defaults"],
        "tenant": {"hoursPerDay": 8, "timezone": RIDES_ZONE},
    }
    assert (await server.http.put("/schema", json=world)).status_code == 200
    put = await server.http.put("/definitions", json={"source": QUARTER_RIDES_SOURCE})
    assert put.status_code == 200, put.text
    pushed = await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": RIDES}},
    )
    assert pushed.status_code == 200, pushed.text

    got = await server.http.get(
        "/tenants/t1/results/shop_courier.recent_ride",
        params={"at": "2026-06-30", "trailing": "1-48"},
    )
    assert got.status_code == 200, got.text
    [subject] = got.json()["subjects"]
    [window] = subject["windows"]
    assert (window["span"], window["bucket"]) == ("48", "hour")
    assert window["trailing"] is None, "an hour span must not wear a trailing-days number"
    assert window["frm"] == "2026-06-29T00:00"
    assert window["to"] == "2026-06-30T23:00"
    assert window["mean"] == 7200.0, (
        "the last 48 hour buckets of the anchor day hold only the two-hour "
        "ride; 5400 means the day-window arithmetic leaked in"
    )


async def test_a_unit_suffixed_window_token_is_refused_toward_the_group_clause(
    server: Server,
) -> None:
    """The v0.12 tokens are retired, not reinterpreted: `1-48h` becoming 48
    buckets would silently re-scale a bookmarked span. The 422 points at
    the group clause, where the unit now lives, on both routes."""
    await _teach_rides(server.http)
    got = await server.http.get(
        "/tenants/t1/results/shop_courier.typical_ride", params={"trailing": "1-48h"}
    )
    assert got.status_code == 422, f"{got.status_code} {got.text}"
    assert "retired" in got.text, got.text
    assert "group clause" in got.text, got.text

    listed = await server.http.get("/tenants/t1/results", params={"trailing": "1-48h"})
    assert listed.status_code == 422, f"{listed.status_code} {listed.text}"


# ---------------------------------------------------------------- bundles --


async def _teach_bundle(http: httpx.AsyncClient) -> None:
    from .test_bundle import SERVE_SOURCE, SERVE_WORLD, _feed

    assert (await http.put("/schema", json=SERVE_WORLD.to_document())).status_code == 200
    put = await http.put("/definitions", json={"source": SERVE_SOURCE})
    assert put.status_code == 200, put.text

    class _Sink:
        def __init__(self) -> None:
            self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}

        def put(self, tenant: str, kind: str, key: str, value: dict[str, Any]) -> None:
            self.rows[(tenant, kind, key)] = value

    sink = _Sink()
    _feed(sink)  # type: ignore[arg-type]  # the fixture only calls .put
    writes: dict[str, dict[str, dict[str, Any]]] = {}
    for (_tenant, kind, key), value in sink.rows.items():
        writes.setdefault(kind, {})[key] = value
    pushed = await http.post("/tenants/t1/facts", json={"writes": writes})
    assert pushed.status_code == 200, pushed.text


async def test_a_bundle_is_one_request_on_the_results_surface(server: Server) -> None:
    """The wire shape: a wrapper with the bundle's name and hash, and the
    members' ordinary Results in declaration order -- discriminated from a
    plain Result by `kind`, so a typed client branches on a field."""
    await _teach_bundle(server.http)

    got = await server.http.get("/tenants/t1/results/shop_courier.card")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "bundle"
    assert body["name"] == "shop_courier.card"

    from uratori import compile_source as compile_against

    from .test_bundle import SERVE_SOURCE, SERVE_WORLD

    library = compile_against(SERVE_SOURCE, SERVE_WORLD)
    card = library.bundle("shop_courier.card")
    assert card is not None
    assert body["version"] == card.version

    assert [(m["slot"], m["result"]["kind"], m["result"]["name"]) for m in body["results"]] == [
        ("typical", "reading", "shop_courier.typical_ride"),
        ("carrying", "figure", "shop_courier.carrying"),
        ("board", "projection", "shop_order.board"),
        ("book", "summary", "shop_order.book"),
    ]
    reading, figure, projection, summary = (m["result"] for m in body["results"])
    # The member arguments made it through HTTP: the bundle's own windows,
    # not the route's defaults.
    assert [w["trailing"] for w in reading["subjects"][0]["windows"]] == [9, 1]
    assert figure["subjects"][0]["value"] == 5.0
    # The summary member travels without rows and still counts them all;
    # the projection member keeps its own page.
    assert summary["subjects"] == []
    assert summary["summary"]["values"]["orders"] == 7.0
    assert len(projection["subjects"]) == 2

    # The bulk surface carries the tile too, since the socket's first paint
    # reads it: a client binding a tile must not render blank until a pass
    # happens to touch a member. The members' numbers therefore travel twice
    # -- bytes, not arithmetic.
    listed = await server.http.get("/tenants/t1/results")
    assert listed.status_code == 200
    tiles = [r for r in listed.json() if r["kind"] == "bundle"]
    assert [t["name"] for t in tiles] == ["shop_courier.card"]
    assert [m["slot"] for m in tiles[0]["results"]] == [
        "typical",
        "carrying",
        "board",
        "book",
    ]

    # An anchored bulk read serves no tiles, for the reason the by-name route
    # refuses `at` on a bundle: the non-reading members can only be served as
    # they stand, and a tile served as-it-stands inside a response the caller
    # anchored would disagree with itself under a wrapper claiming one clock.
    anchored = await server.http.get("/tenants/t1/results", params={"at": "2026-06-30"})
    assert anchored.status_code == 200
    assert all(r["kind"] != "bundle" for r in anchored.json())


async def test_the_described_library_lists_the_bundle_and_its_members_in_order(
    server: Server,
) -> None:
    await _teach_bundle(server.http)
    body = (await server.http.get("/definitions")).json()
    [bundle] = body["bundles"]
    assert bundle["declaration"] == "bundle"
    assert bundle["name"] == "shop_courier.card"
    assert bundle["version"]
    assert bundle["members"] == [
        "shop_courier.typical_ride",
        "shop_courier.carrying",
        "shop_order.board",
        "shop_order.book",
    ]
    assert bundle["prose"] == "The courier tile."
    assert "reading shop_courier.typical_ride over 9, 1" in bundle["source"]


async def test_bundle_evidence_over_http_says_where_the_evidence_lives(
    server: Server,
) -> None:
    await _teach_bundle(server.http)
    got = await server.http.get(
        "/tenants/t1/evidence/shop_courier.card", params={"subject": "c1"}
    )
    assert got.status_code == 404
    assert "member" in got.json()["detail"]


async def test_an_anchored_bundle_is_a_400_with_directions(server: Server) -> None:
    """`at` moves a reading's windows; a bundle's other members can only be
    served as they stand, so an anchored tile would misreport its own clock.
    Refused in the engine's words rather than served lying."""
    await _teach_bundle(server.http)
    got = await server.http.get(
        "/tenants/t1/results/shop_courier.card", params={"at": "2026-06-30"}
    )
    assert got.status_code == 400, got.text
    assert "one instant" in got.json()["detail"]


async def test_a_bundle_over_a_live_reading_is_a_501_like_the_reading_itself(
    server: Server,
) -> None:
    """The tile refuses whole rather than serving itself one member short --
    the same 501 the live reading's own route answers, through the same
    mapping."""
    from .test_bundle import SERVE_SOURCE, SERVE_WORLD

    live = SERVE_SOURCE + (
        "\nmeasure shop_order.waiting_seconds = now - picked_up_at\n"
        "\n# Orders waiting right now.\n"
        "reading shop_courier.waiting():\n"
        '    display "{value}"\n'
        "    depends:\n"
        "        waiting = shop_order.waiting_seconds over (shop_order.carried_by:{shop_courier} & shop_order.open)\n"
        "    calculate:\n"
        "        count(waiting)\n"
        "\n# A tile over the live queue.\n"
        "bundle shop_courier.live_card:\n"
        "    waiting = reading shop_courier.waiting\n"
    )
    assert (await server.http.put("/schema", json=SERVE_WORLD.to_document())).status_code == 200
    put = await server.http.put("/definitions", json={"source": live})
    assert put.status_code == 200, put.text

    got = await server.http.get("/tenants/t1/results/shop_courier.live_card")
    assert got.status_code == 501, got.text
    assert "live" in got.json()["detail"]
