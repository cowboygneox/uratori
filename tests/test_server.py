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
from typing import Any

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
        "/definitions", json={"source": "index work_issue.active where active == true\n"}
    )
    assert refused.status_code == 422
    assert "not a fact kind" in refused.json()["detail"]

    health = (await server.http.get("/health")).json()
    assert health["ready"] is False, "a refused load must leave nothing half-taught"


async def test_taught_fed_and_asked_end_to_end(server: Server) -> None:
    versions = await _teach(server.http)
    # The versions the server reports are the ones this build compiled locally
    # -- the check a host runs to know the server serves what was reviewed.
    local = compile_source(COURIER_SOURCE, COURIER_WORLD)
    assert versions["figures"] == [
        {"name": p.name, "version": p.version} for p in local.figures
    ]

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


async def test_a_moved_dial_rebuilds_and_rebands(server: Server) -> None:
    await _teach(server.http)
    await server.http.post(
        "/tenants/t1/facts",
        json={"writes": {"shop_courier": COURIER, "shop_order": _orders(3)}},
    )
    before = (await server.http.get("/tenants/t1/results/shop_courier.load_band")).json()
    assert before["subjects"][0]["value"] == "over"

    saved = await server.http.put(
        "/tenants/t1/settings",
        json={"document": {"limits": {"carrying": {"over": 10}}}},
    )
    assert saved.status_code == 200

    ran = await server.http.post("/tenants/t1/runs", json={})
    assert "shop_courier.load_band" in ran.json()["rebuilt"], (
        "a moved dial must make the figures naming it pending -- silently "
        "keeping their pointers means banding against the old number for ever"
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
