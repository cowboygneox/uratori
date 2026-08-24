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
from uratori.lang.settings import fingerprint

COURIER_WORLD = Schema(
    kinds=frozenset({"shop_order", "shop_courier"}),
    name_fields={"shop_courier": "name", "shop_order": "ref"},
    figure_settings=("limits.carrying.over",),
    defaults={"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}},
)

COURIER_SOURCE = '''
index shop_order.carried_by from courier_id
index shop_order.open where status != "delivered"

figure shop_courier.carrying:
    """How many orders this courier is carrying right now."""
    display "{value} orders in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)

figure shop_courier.load_band:
    """Whether a courier is over the carrying limit."""
    display "{value}"
    combine:
        carrying = shop_courier.carrying
    calculate:
        when carrying >= limits.carrying.over then "over"
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
            'index work_issue.active where active == true\n', COURIER_WORLD
        )


def test_a_kind_must_lex_as_one_identifier() -> None:
    with pytest.raises(ValueError, match="set difference"):
        Schema(kinds=frozenset({"code_review-request"}))


def test_a_name_field_for_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown kinds"):
        Schema(kinds=frozenset({"shop_order"}), name_fields={"shop_orders": "ref"})


def test_schema_documents_round_trip() -> None:
    """`PUT /schema` carries `to_document`; a lossy trip would mean the world
    the service runs differs from the world the host declared, silently."""
    assert Schema.from_document(COURIER_WORLD.to_document()) == COURIER_WORLD


def test_the_server_contract_parses_documents_identically() -> None:
    from uratori.server.contract import SchemaIn

    assert SchemaIn(**COURIER_WORLD.to_document()).build() == COURIER_WORLD


def test_settings_merge_lands_a_whole_band_as_one_value() -> None:
    """A band written whole replaces whole -- `flatten` emits bands whole, so
    that is the only shape a stored document carries."""
    schema = Schema(
        kinds=frozenset({"shop_order"}),
        defaults={"flow": {"speed": {"good": 2, "poor": 5}}},
    )
    merged = schema.settings_for({"flow": {"speed": {"good": 1, "poor": 9}}})
    assert merged["flow"]["speed"] == {"good": 1, "poor": 9}

    # A non-band node merges leaf by leaf: setting one dial must not unset its
    # neighbours.
    schema = Schema(
        kinds=frozenset({"shop_order"}),
        defaults={"limits": {"a": 1, "b": 2}},
    )
    merged = schema.settings_for({"limits": {"a": 9}})
    assert merged["limits"] == {"a": 9, "b": 2}


def test_completing_a_document_never_touches_the_defaults() -> None:
    """The defaults are shared by every tenant of a deployment, so a merge
    that mutates them turns one tenant's dial into everybody's new default.

    This is not hypothetical: the first implementation assigned the defaults'
    nested dicts into the merged document by reference and then merged the
    overrides *into those shared objects* -- one tenant's override quietly
    rewrote the schema's defaults for the life of the process, and the rest of
    this suite caught it as tests poisoning each other through a module-level
    world."""
    schema = Schema(
        kinds=frozenset({"shop_order"}),
        defaults={"limits": {"carrying": {"over": 3}}},
    )
    merged = schema.settings_for({"limits": {"carrying": {"over": 10}}})
    assert merged["limits"]["carrying"]["over"] == 10
    assert schema.defaults["limits"]["carrying"]["over"] == 3, (
        "the tenant's dial leaked into the shared defaults"
    )
    # And the other direction: editing the merged document afterwards must not
    # reach back either.
    fresh = schema.settings_for({})
    fresh["limits"]["carrying"]["over"] = 99
    assert schema.defaults["limits"]["carrying"]["over"] == 3


def test_a_dial_set_to_its_default_fingerprints_like_an_unset_one() -> None:
    """What makes 'complete the document, then fingerprint' equivalent to the
    old 'fingerprint sparse with a default fallback': the stored fingerprints
    written before the extraction must keep validating, or every tenant pays a
    full rebuild for a refactor that changed no value."""
    named = ["limits.carrying.over"]
    unset = fingerprint(COURIER_WORLD.settings_for({}), named)
    explicit = fingerprint(
        COURIER_WORLD.settings_for({"limits": {"carrying": {"over": 3}}}), named
    )
    changed = fingerprint(
        COURIER_WORLD.settings_for({"limits": {"carrying": {"over": 4}}}), named
    )
    assert unset == explicit
    assert unset != changed, "the control: a moved dial must move the fingerprint"


def test_a_missing_dial_raises_rather_than_guessing() -> None:
    """A definition named the dial, so there is nothing to guess -- a fallback
    would band every subject against a number nobody chose."""
    from uratori.lang.settings import setting_value

    with pytest.raises(KeyError, match="no value for setting"):
        setting_value(COURIER_WORLD.settings_for({}), "limits.unheard.of")


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
index shop_order.carried_by from courier_ref through shop_courier.handles
index shop_order.open where status != "delivered"

figure shop_courier.carrying:
    """How many orders this courier is carrying right now."""
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


def test_every_declared_list_survives_the_document_round_trip() -> None:
    """The round trip must carry all four settings lists, not whichever ones a
    convenient fixture happened to populate: a list dropped by `to_document`
    reaches the service empty, every definition naming one of its dials is
    refused at the next teach, and the affected boards read `setting-moved`
    for ever."""
    full = Schema(
        kinds=frozenset({"shop_order", "shop_courier"}),
        name_fields={"shop_courier": "name"},
        bucket_settings=("tenant.timezone", "windows.historyDays"),
        figure_settings=("limits.carrying.over",),
        reading_settings=("flow.speed",),
        project_settings=("limits.carrying.over", "windows.historyDays"),
        defaults={"tenant": {"timezone": "UTC", "hoursPerDay": 8}},
    )
    assert Schema.from_document(full.to_document()) == full


def test_the_travelling_document_shares_no_state_with_the_schema() -> None:
    """`to_document` is built, retried and persisted; a document aliasing the
    schema's own defaults would carry whatever happened to process state in
    the window between building and sending -- the exact route by which one
    tenant's dial once became the deployment's stored default."""
    schema = Schema(
        kinds=frozenset({"shop_order"}),
        defaults={"limits": {"carrying": {"over": 3}}},
    )
    document = schema.to_document()
    document["defaults"]["limits"]["carrying"]["over"] = 99
    assert schema.defaults["limits"]["carrying"]["over"] == 3
