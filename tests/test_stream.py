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


def test_the_token_gates_the_stream_with_a_close_code_a_client_can_read(pg_dsn: str) -> None:
    """An inverted or forgotten gate here is every tenant's figures pushed to
    whoever connects. The refusal must arrive as close code 4401 *after* the
    handshake: uvicorn renders a close-before-accept as a bare 403 handshake
    rejection, which a browser's WebSocket API cannot tell from a network
    fault -- so a client retrying network faults would retry an auth problem
    for ever, which is the exact failure 4401 exists to prevent. The token
    travels in the header only, because a query string lands in every access
    log between here and the client."""
    with _service(pg_dsn, token="s3cret") as client:
        with client.websocket_connect("/stream") as socket:
            with pytest.raises(WebSocketDisconnect) as refused:
                socket.receive_json()
            assert refused.value.code == 4401

        # The query-param path must NOT work: a token in a URL is a token in
        # every access log between here and the client.
        with client.websocket_connect("/stream?token=s3cret") as socket:
            with pytest.raises(WebSocketDisconnect) as refused:
                socket.receive_json()
            assert refused.value.code == 4401

        _teach_and_feed(client, headers={"Authorization": "Bearer s3cret"})
        with client.websocket_connect(
            "/stream", headers={"Authorization": "Bearer s3cret"}
        ) as socket:
            socket.send_json({"type": "subscribe", "tenant": "t1"})
            assert socket.receive_json()["type"] == "result"


# ------------------------------------------------------- subscriptions --
#
# A subscription is a standing GET: the subscribe frame names calculations
# with the arguments the HTTP API takes, each entry is answered immediately
# with its current answer at those arguments, and thereafter a pass pushes an
# entry exactly when it is impacted -- evaluated at the SUBSCRIBED arguments,
# never at the serving defaults. A bare subscribe (no entries) keeps its old
# meaning: everything, at defaults.

from .test_push import SOURCE as TILE_SOURCE  # noqa: E402
from .test_push import WORLD as TILE_WORLD  # noqa: E402


def _teach_tiles(client: TestClient) -> None:
    assert client.put("/schema", json=TILE_WORLD.to_document()).status_code == 200
    assert (
        client.put("/definitions", json={"source": TILE_SOURCE}).status_code == 200
    ), "the tile world must teach"
    pushed = client.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": {"c1": {"name": "Aki"}},
                "shop_order": {
                    "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"},
                    "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"},
                },
            }
        },
    )
    assert pushed.status_code == 200, pushed.text


def _deliver(client: TestClient, key: str) -> None:
    """One delivered order, landing in today's bucket -- the write that
    touches `ride_times` and through it the reading and the card."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(tz=UTC)
    posted = client.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_order": {
                    key: {
                        "ref": key.upper(),
                        "courier_id": "c1",
                        "status": "delivered",
                        "picked_up_at": (now - timedelta(hours=2)).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "delivered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                }
            }
        },
    )
    assert posted.status_code == 200, posted.text


def test_a_named_subscription_fetches_now_and_follows_only_its_entries(pg_dsn: str) -> None:
    """Subscribe = fetch + follow. The follow half's precision is the point:
    the third order moves BOTH figures (carrying 2->3, and the band it feeds
    crosses to "over"), and the client watching only the band must hear only
    the band -- a hub that pushes everything impacted regardless of interest
    is the firehose wearing a subscription's name."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        client.post(
            "/tenants/t1/facts",
            json={"writes": {"shop_order": {"o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"}}}},
        )
        with client.websocket_connect("/stream") as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.load_band"}],
                }
            )
            fetched = socket.receive_json()
            assert fetched["type"] == "result"
            assert fetched["result"]["name"] == "shop_courier.load_band"
            assert [s["value"] for s in fetched["result"]["subjects"]] == ["ok"]
            # Nothing else was fetched: one entry, one answer.
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"

            client.post(
                "/tenants/t1/facts",
                json={"writes": {"shop_order": {"o3": {"ref": "A-3", "courier_id": "c1", "status": "riding"}}}},
            )
            pushed = socket.receive_json()
            assert pushed["type"] == "result"
            assert pushed["result"]["name"] == "shop_courier.load_band"
            assert [s["value"] for s in pushed["result"]["subjects"]] == ["over"]
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong", (
                "carrying moved too and was delivered to a client that never "
                "asked for it -- interest did not narrow the push"
            )


def test_subscribed_windows_reach_the_fetch_and_the_push(pg_dsn: str) -> None:
    """The entry's arguments are the standing GET's arguments: a reading
    subscribed over 9, 31-60 must arrive over those spans both times, not at
    the serving defaults -- same name, same version, different numbers is the
    drift this pins shut."""
    with _service(pg_dsn) as client:
        _teach_tiles(client)
        _deliver(client, "r1")
        with client.websocket_connect("/stream") as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [
                        {"name": "shop_courier.typical_ride", "trailing": ["9", "31-60"]}
                    ],
                }
            )
            fetched = socket.receive_json()
            assert fetched["result"]["name"] == "shop_courier.typical_ride"
            [subject] = fetched["result"]["subjects"]
            assert [w["span"] for w in subject["windows"]] == ["9", "31-60"]

            _deliver(client, "r2")
            pushed = socket.receive_json()
            assert pushed["result"]["name"] == "shop_courier.typical_ride"
            [subject] = pushed["result"]["subjects"]
            assert [w["span"] for w in subject["windows"]] == ["9", "31-60"], (
                "the push re-evaluated the entry at the serving defaults "
                "instead of the subscribed windows"
            )


def test_unsubscribe_by_entry_identity_silences_the_entry(pg_dsn: str) -> None:
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.carrying"}],
                }
            )
            assert socket.receive_json()["result"]["name"] == "shop_courier.carrying"
            socket.send_json(
                {"type": "unsubscribe", "entries": [{"name": "shop_courier.carrying"}]}
            )
            client.post(
                "/tenants/t1/facts",
                json={"writes": {"shop_order": {"o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"}}}},
            )
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong", (
                "an unsubscribed entry was still delivered"
            )


def test_a_bad_entry_is_refused_by_name_while_the_good_one_proceeds(pg_dsn: str) -> None:
    """Per-entry refusal, never silent dropping: the client is told which
    entry failed, in the API's own validation vocabulary, and the valid
    entries in the same frame still fetch and follow."""
    with _service(pg_dsn) as client:
        _teach_and_feed(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [
                        {"name": "no.such"},
                        {"name": "shop_courier.carrying", "trailing": ["banana"]},
                        {"name": "shop_courier.carrying"},
                    ],
                }
            )
            first = socket.receive_json()
            assert first["type"] == "error"
            assert first["name"] == "no.such"
            assert "No definition called no.such" in first["message"]
            second = socket.receive_json()
            assert second["type"] == "error"
            assert second["name"] == "shop_courier.carrying"
            assert "banana" in second["message"]
            third = socket.receive_json()
            assert third["type"] == "result"
            assert third["result"]["name"] == "shop_courier.carrying"

            client.post(
                "/tenants/t1/facts",
                json={"writes": {"shop_order": {"o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"}}}},
            )
            pushed = socket.receive_json()
            assert pushed["type"] == "result"
            assert pushed["result"]["name"] == "shop_courier.carrying"


def test_windows_on_a_bundle_entry_are_refused(pg_dsn: str) -> None:
    """A bundle's windows are declared in its definition; an entry that could
    move them would be a different tile under the same hash -- the same
    refusal `answer` gives the anchored bundle."""
    with _service(pg_dsn) as client:
        _teach_tiles(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.card", "trailing": ["7"]}],
                }
            )
            refused = socket.receive_json()
            assert refused["type"] == "error"
            assert refused["name"] == "shop_courier.card"
            assert "declared" in refused["message"]


def test_a_bundle_entry_fetches_and_follows_its_members_movement(pg_dsn: str) -> None:
    """The tile as a subscription: fetched whole on subscribe, re-served
    whole when a member moves, quiet when no member does -- and the client
    watching the OTHER tile hears nothing, which is the member-union doing
    its narrowing per client rather than per pass."""
    with _service(pg_dsn) as client:
        _teach_tiles(client)
        with client.websocket_connect("/stream") as card_socket, client.websocket_connect(
            "/stream"
        ) as reviews_socket:
            card_socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.card"}],
                }
            )
            fetched = card_socket.receive_json()
            assert fetched["type"] == "result"
            assert fetched["result"]["kind"] == "bundle"
            assert fetched["result"]["name"] == "shop_courier.card"
            slots = [m["slot"] for m in fetched["result"]["results"]]
            assert slots == ["typical", "carrying"]

            reviews_socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.reviews_card"}],
                }
            )
            assert reviews_socket.receive_json()["result"]["name"] == "shop_courier.reviews_card"

            # A third riding order moves carrying -- a member of card and of
            # nothing on the reviews tile.
            client.post(
                "/tenants/t1/facts",
                json={"writes": {"shop_order": {"o3": {"ref": "A-3", "courier_id": "c1", "status": "riding"}}}},
            )
            pushed = card_socket.receive_json()
            assert pushed["result"]["kind"] == "bundle"
            assert pushed["result"]["name"] == "shop_courier.card"

            reviews_socket.send_json({"type": "ping"})
            assert reviews_socket.receive_json()["type"] == "pong", (
                "a tile containing nothing this pass touched was pushed to "
                "its subscriber"
            )


def test_a_dial_move_pushes_the_watched_recolour_through_figure_and_tile(pg_dsn: str) -> None:
    """The settings half of the directive, end to end over the socket: a
    band threshold saved and picked up by a definition-only run must re-serve
    the banded figure and the tile holding it, to exactly the clients
    watching them -- while a client on an unrelated tile hears nothing."""
    with _service(pg_dsn) as client:
        _teach_tiles(client)
        with client.websocket_connect("/stream") as socket, client.websocket_connect(
            "/stream"
        ) as other:
            socket.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [
                        {"name": "shop_courier.carrying"},
                        {"name": "shop_courier.card"},
                    ],
                }
            )
            first = socket.receive_json()
            assert first["result"]["name"] == "shop_courier.carrying"
            assert [s["level"] for s in first["result"]["subjects"]] == ["ok"]
            assert socket.receive_json()["result"]["name"] == "shop_courier.card"

            other.send_json(
                {
                    "type": "subscribe",
                    "tenant": "t1",
                    "entries": [{"name": "shop_courier.reviews_card"}],
                }
            )
            assert other.receive_json()["result"]["name"] == "shop_courier.reviews_card"

            saved = client.put(
                "/tenants/t1/settings",
                json={"document": {"limits": {"carrying": {"over": 1}}}},
            )
            assert saved.status_code == 200
            ran = client.post("/tenants/t1/runs", json={})
            assert ran.status_code == 200, ran.text

            heard = {socket.receive_json()["result"]["name"] for _ in range(2)}
            assert heard == {"shop_courier.carrying", "shop_courier.card"}
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"

            other.send_json({"type": "ping"})
            assert other.receive_json()["type"] == "pong", (
                "a dial nothing on this client's tile names re-served it"
            )


def test_a_bare_subscribe_paints_tiles_too_and_serve_false_keeps_the_response_lean(
    pg_dsn: str,
) -> None:
    """Two contracts in one world: the bare subscribe keeps its old meaning
    -- everything at defaults, tiles now included -- and a caller that says
    `serve: false` gets `moved` names instead of evaluated results while the
    socket still delivers, because the socket's delivery is the server's job,
    not the HTTP caller's."""
    with _service(pg_dsn) as client:
        _teach_tiles(client)
        with client.websocket_connect("/stream") as socket:
            socket.send_json({"type": "subscribe", "tenant": "t1"})
            names = set()
            kinds = set()
            for _ in range(7):
                frame = socket.receive_json()
                assert frame["type"] == "result"
                names.add(frame["result"]["name"])
                kinds.add(frame["result"]["kind"])
            assert {
                "shop_courier.card",
                "shop_courier.reviews_card",
                "shop_order.board_card",
            } <= names, "first paint must carry the tiles"
            assert "bundle" in kinds

            posted = client.post(
                "/tenants/t1/facts",
                json={
                    "writes": {
                        "shop_order": {"o9": {"ref": "A-9", "courier_id": "c1", "status": "riding"}}
                    },
                    "serve": False,
                },
            )
            assert posted.status_code == 200, posted.text
            body = posted.json()
            assert body["results"] == [], (
                "serve:false still paid for (and shipped) every evaluated answer"
            )
            assert "shop_courier.carrying" in body["moved"]
            assert "shop_courier.card" in body["moved"]
            assert "shop_courier.reviews_card" not in body["moved"]

            heard = set()
            for _ in range(4):
                frame = socket.receive_json()
                assert frame["type"] == "result"
                heard.add(frame["result"]["name"])
            assert heard == {
                "shop_courier.carrying",
                "shop_order.board",
                "shop_courier.card",
                "shop_order.board_card",
            }
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"


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
