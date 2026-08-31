"""The schema is the host's declaration, and nothing about a host is baked in.

The proof that matters here is the foreign world: a vocabulary sharing not one
name with the origin project, run through the same compile, cascade, serve and
listener machinery. If any of those had quietly kept a hardwired kind, dial
list or name field, these tests are where it would surface.
"""

from __future__ import annotations

import pytest

from uratori import (
    CheckError,
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    Uratori,
    compile_source,
)

COURIER_WORLD = Schema(
    kinds=frozenset({"shop_order", "shop_courier"}),
    name_fields={"shop_courier": "name", "shop_order": "ref"},
    url_fields={"shop_order": "url"},
)

COURIER_SOURCE = '''
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)

# Whether a courier is over the carrying limit.
figure shop_courier.load_band:
    display "{value}"
    calculate:
        when shop_courier.carrying >= 3 then "over"
        otherwise "ok"
'''


def _facade() -> tuple[Uratori, MemoryFactStore]:
    library = compile_source(COURIER_SOURCE, COURIER_WORLD)
    facts = MemoryFactStore()
    engine = Uratori(
        schema=COURIER_WORLD,
        library=library,
        store=MemoryEngineStore(),
        facts=facts,
    )
    return engine, facts


async def test_a_foreign_world_compiles_computes_and_cascades() -> None:
    """The whole chain over kinds the engine has never heard of.

    The cascade is the part that would break silently: `load_band` is built on
    `carrying`, so the fourth order arriving must move both -- a band that
    stayed "ok" would mean depth ordering or part-to-total propagation still
    assumed something about the world it no longer knows.
    """
    engine, facts = _facade()
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for n in range(2):
        facts.put("t1", "shop_order", f"o{n}", {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"})

    report = await engine.run("t1", full=True)
    moved = {c.figure: c.after for c in report.outcome.changes}
    assert moved["shop_courier.carrying"] == 2.0
    assert moved["shop_courier.load_band"] == "ok"

    # One more order crosses the dial the schema declared (over at >= 3).
    facts.put("t1", "shop_order", "o2", {"ref": "A-2", "courier_id": "c1", "status": "riding"})
    report = await engine.run("t1", written={"shop_order": ["o2"]})
    moved = {c.figure: c.after for c in report.outcome.changes}
    assert moved["shop_courier.carrying"] == 3.0
    assert moved["shop_courier.load_band"] == "over", (
        "the figure moved and the band built on it did not -- the cascade "
        "narrowed somewhere"
    )


async def test_listeners_receive_the_same_objects_the_run_reports() -> None:
    engine, facts = _facade()
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_id": "c1", "status": "riding"})

    heard: list[tuple[str, tuple, tuple]] = []
    engine.subscribe(lambda tenant, outcome, results: heard.append((tenant, outcome.changes, results)))

    report = await engine.run("t1", full=True)
    assert len(heard) == 1
    tenant, changes, results = heard[0]
    assert tenant == "t1"
    # Identity, not equality: a listener handed copies is a listener handed a
    # second contract that can drift from the first.
    assert changes is report.outcome.changes
    assert results is report.results


def test_the_closed_world_is_the_schema_not_the_engine() -> None:
    """The control: a kind from the origin project's world must be refused
    here. If this compiled, the foreign-world test above would be passing
    because the engine still carries a builtin vocabulary that happens to be a
    superset -- the exact wrong reason."""
    with pytest.raises(CheckError, match="not a fact kind"):
        compile_source(
            'filter work_issue.active where active == true\n', COURIER_WORLD
        )


def test_a_kind_must_lex_as_one_identifier() -> None:
    with pytest.raises(ValueError, match="set difference"):
        Schema(kinds=frozenset({"code_review-request"}))


def test_a_name_field_for_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown kinds"):
        Schema(kinds=frozenset({"shop_order"}), name_fields={"shop_orders": "ref"})


def test_a_url_field_for_an_unknown_kind_is_refused() -> None:
    """The same typo, one field over: ignored, the kind it was meant for would
    serve linkless evidence for ever while everything looked configured."""
    with pytest.raises(ValueError, match="unknown kinds"):
        Schema(kinds=frozenset({"shop_order"}), url_fields={"shop_orders": "url"})


def test_schema_documents_round_trip() -> None:
    """`PUT /schema` carries `to_document`; a lossy trip would mean the world
    the service runs differs from the world the host declared, silently."""
    assert Schema.from_document(COURIER_WORLD.to_document()) == COURIER_WORLD


def test_the_server_contract_parses_documents_identically() -> None:
    from uratori.server.contract import SchemaIn

    assert SchemaIn(**COURIER_WORLD.to_document()).build() == COURIER_WORLD


async def test_a_pass_with_deletions_escalates_to_full() -> None:
    """The warm path honours `deleted`, but the cold branch never reads it --
    so a delete landing while any pointer is stale (the ordinary state between
    a deploy and the next pass) would drop the removal list on the floor and
    the departed record would keep its index memberships. Full in every branch
    is the only shape that cannot be wrong."""
    engine, facts = _facade()
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_id": "c1", "status": "riding"})
    await engine.run("t1", full=True)

    facts.drop("t1", "shop_order", "o1")
    report = await engine.run("t1", deleted={"shop_order": ["o1"]})
    assert report.outcome.rebuilt, (
        "a pass carrying deletions ran warm; a stale pointer would have "
        "dropped the removal list on the floor"
    )
    moved = {c.figure: c.after for c in report.outcome.changes}
    assert moved["shop_courier.carrying"] == 0.0


async def test_a_moved_through_kind_record_escalates_to_full() -> None:
    """A kind that indexes only resolve *through* is invisible to the warm
    path: no index is over the kind itself, so its write moves no bucket, and
    every record that resolves through the moved row stays filed under the old
    answer. The origin project shipped this bug -- an unhidden person came back
    reading a measured zero on every figure until the hourly reconcile."""
    world = Schema(
        kinds=frozenset({"shop_order", "shop_courier"}),
        name_fields={"shop_courier": "name", "shop_order": "ref"},
    )
    source = '''
group shop_order.carried_by from courier_ref through shop_courier.handles
filter shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)
'''
    library = compile_source(source, world)
    facts = MemoryFactStore()
    engine = Uratori(
        schema=world, library=library, store=MemoryEngineStore(), facts=facts
    )
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "handles": ["aki-yes"]})
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_ref": "aki-yes", "status": "riding"})
    await engine.run("t1", full=True)

    # The courier's handle changes: no shop_order record moved, but every
    # order that resolved through the old handle now belongs to nobody.
    facts.put("t1", "shop_courier", "c1", {"name": "Aki", "handles": ["aki-new"]})
    report = await engine.run("t1", written={"shop_courier": ["c1"]})
    moved = {c.figure: c.after for c in report.outcome.changes}
    assert moved.get("shop_courier.carrying") == 0.0, (
        "the order stayed filed under the departed handle -- the warm path "
        "could not see a through-kind move and nothing escalated"
    )

    # The control: a write to an ordinary kind does not pay the full pass.
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_ref": "aki-new", "status": "riding"})
    report = await engine.run("t1", written={"shop_order": ["o1"]})
    assert not report.outcome.rebuilt, (
        "an ordinary write escalated to full; every webhook would pay for a "
        "board-wide recompute"
    )
    moved = {c.figure: c.after for c in report.outcome.changes}
    assert moved.get("shop_courier.carrying") == 1.0


async def test_a_full_pass_reports_the_kinds_it_read_as_covered() -> None:
    """`covered` is the difference between *confirmed unchanged* and *not
    checked* -- a host re-dates its evidence on it. A full reconcile computes
    every figure from every fact it can read, so it must report the kinds the
    library reads; reporting the batch's kinds instead tells the host that a
    batch-less rebuild confirmed nothing, and nothing ever re-dates."""
    engine, facts = _facade()
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    facts.put("t1", "shop_order", "o1", {"ref": "A-1", "courier_id": "c1", "status": "riding"})

    first = await engine.run("t1", written={"shop_order": ["o1"]})
    assert "shop_order" in first.outcome.covered

    full = await engine.run("t1", full=True)
    assert full.outcome.covered == frozenset({"shop_order"}), (
        "a full pass read every shop_order to recompute both figures; an "
        "empty `covered` claims the reconcile confirmed nothing"
    )

    # The control: a warm, batch-less pass read nothing and must say so --
    # widening `covered` on the cheap path would re-date evidence that was
    # never confirmed.
    idle = await engine.run("t1")
    assert idle.outcome.covered == frozenset()
    """The round trip must carry all four settings lists, not whichever ones a
    convenient fixture happened to populate: a list dropped by `to_document`
    reaches the service empty, every definition naming one of its dials is
    refused at the next teach, and the affected boards read `setting-moved`
    for ever."""
    full = Schema(
        kinds=frozenset({"shop_order", "shop_courier"}),
        name_fields={"shop_courier": "name"},
    )
    assert Schema.from_document(full.to_document()) == full


