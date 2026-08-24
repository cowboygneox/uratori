"""The service websocket: paint on subscribe, push on movement, gate on token.

Driven through starlette's TestClient (its own event loop in a portal thread)
rather than the suite's async fixtures, because httpx's ASGI transport does
not speak websockets. Each test gets a schema of its own, exactly as
test_server.py's fixture does.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from uratori.server import create_app

from .test_schema import COURIER_SOURCE, COURIER_WORLD


def _sql(pg_dsn: str, statement: str) -> None:
    async def go() -> None:
        import asyncpg

        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(go())


@contextmanager
def _service(pg_dsn: str, token: str | None = None) -> Iterator[TestClient]:
    name = f"uratori_ws_{os.urandom(4).hex()}"
    _sql(pg_dsn, f"create schema {name}")
    try:
        app = create_app(dsn=pg_dsn, pg_schema=name, token=token, version="test")
        with TestClient(app) as client:
            yield client
    finally:
        _sql(pg_dsn, f"drop schema {name} cascade")


def _teach_and_feed(client: TestClient, headers: dict[str, str] | None = None) -> None:
    h = headers or {}
    assert client.put("/schema", json=COURIER_WORLD.to_document(), headers=h).status_code == 200
    assert (
        client.put("/definitions", json={"source": COURIER_SOURCE}, headers=h).status_code == 200
    )
    pushed = client.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": {"c1": {"name": "Aki"}},
                "shop_order": {"o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"}},
            }
        },
        headers=h,
    )
    assert pushed.status_code == 200, pushed.text


def test_subscribe_paints_everything_then_movement_pushes(pg_dsn: str) -> None:
    """The socket's whole contract: current answers on subscribe, and exactly
    the moved answers afterwards -- the same `Result` objects the routes
    return, or the browser has a second contract to drift from."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"

            socket.send_json({"type": "subscribe", "tenant": "t1"})
            first = {socket.receive_json()["result"]["name"] for _ in range(2)}
            assert first == {"shop_courier.carrying", "shop_courier.load_band"}, (
                "first paint must carry the current answer for every servable "
                "definition -- a blank board until the next sync is the "
                "regression this pins"
            )

            # A pass while subscribed pushes what moved -- and ONLY what moved:
            # the new order takes carrying from 1 to 2, while the band stays
            # "ok" (over is >= 3), and an unchanged recompute reports nothing.
            client.post(
                "/tenants/t1/facts",
                json={
                    "writes": {
                        "shop_order": {
                            "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"}
                        }
                    }
                },
            )
            pushed = socket.receive_json()
            assert pushed["type"] == "result"
            assert pushed["tenant"] == "t1"
            assert pushed["result"]["name"] == "shop_courier.carrying"
            assert [s["value"] for s in pushed["result"]["subjects"]] == [2.0]
            # And nothing else: race a ping past any pending frames -- a hub
            # that pushed the unmoved band too would answer with it here.
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong", (
                "an unmoved definition was pushed; the socket is republishing "
                "rather than reporting"
            )


def test_a_subscribe_to_another_tenant_hears_nothing_of_this_one(pg_dsn: str) -> None:
    """The control on delivery: a hub that broadcast to everybody would pass
    the test above -- and be a cross-tenant disclosure."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json({"type": "subscribe", "tenant": "t2"})
            # t2's first paint arrives (never-computed states), then t1 moves.
            paints = {socket.receive_json()["result"]["name"] for _ in range(2)}
            assert paints == {"shop_courier.carrying", "shop_courier.load_band"}
            client.post(
                "/tenants/t1/facts",
                json={
                    "writes": {
                        "shop_order": {
                            "o9": {"ref": "A-9", "courier_id": "c1", "status": "riding"}
                        }
                    }
                },
            )
            # Nothing for t2: prove the socket is silent by racing a ping past
            # any pending frames -- the next thing heard must be the pong.
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong", (
                "another tenant's movement was delivered to this subscriber"
            )


def test_a_malformed_frame_is_ignored_not_fatal(pg_dsn: str) -> None:
    """A client that sends nonsense is a bug in that client; taking the
    connection down takes the board's live updates with it and makes the bug
    look like an outage."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_text("this is not json")
            socket.send_json({"type": "subscribe"})  # no tenant: an error frame
            answer = socket.receive_json()
            assert answer["type"] == "error"
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"


def test_the_token_gates_the_stream_before_accept(pg_dsn: str) -> None:
    """An inverted or forgotten gate here is every tenant's figures pushed to
    whoever connects. Closed with 4401 before accept, so a client retrying a
    network fault can tell it is an auth problem; the token travels in the
    header only, because a query string lands in every access log between
    here and the client."""
    with _service(pg_dsn, token="s3cret") as client:
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect("/stream"):
            pass
        assert refused.value.code == 4401

        # The query-param path must NOT work: a token in a URL is a token in
        # every access log between here and the client.
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/stream?token=s3cret"),
        ):
            pass
        assert refused.value.code == 4401

        _teach_and_feed(client, headers={"Authorization": "Bearer s3cret"})
        with client.websocket_connect(
            "/stream", headers={"Authorization": "Bearer s3cret"}
        ) as socket:
            socket.send_json({"type": "subscribe", "tenant": "t1"})
            assert socket.receive_json()["type"] == "result"


def test_a_departed_subscriber_stops_being_delivered_to(pg_dsn: str) -> None:
    """`hub.leave` in the disconnect path is what this pins: a hub that kept
    dead sockets would spend every pass writing to them, and `watching` -- the
    count a delivery test trusts -- would lie."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        state: Any = client.app.state.uratori  # type: ignore[attr-defined]
        with client.websocket_connect("/stream") as socket:
            socket.send_json({"type": "subscribe", "tenant": "t1"})
            socket.receive_json()
            assert state.hub.watching("t1") == 1
        # The context manager closed the socket; the hub must have let go.
        assert state.hub.watching("t1") == 0
