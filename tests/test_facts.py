"""Facts declared in the language: the schema half of "every number, backed".

A host could always teach the engine its kinds, but the *fields* lived nowhere:
a definition naming a field that does not exist compiled into a silently empty
index, and a record arriving with a mistyped body was stored and never read.
These tests pin the three claims the `fact` declaration makes:

- **The declaration is the world.** Kinds, name fields and url fields derive
  from the facts; a schema that also declares kinds is refused, because two
  declarations of one world is where they drift.
- **Every path a definition reads must exist, with the right shape.** A typo'd
  field is a build failure here, not an empty bucket in production.
- **A record must match the schema to land.** An undeclared field or a wrong
  type refuses the batch by name, instead of storing a body nothing can read.

And the property that makes adoption safe: declaring facts moves **no
downstream version**. A fact schema decides what the checker permits and what
the write boundary accepts -- like `keyed as`, it never changes what the
arithmetic produces, so it stays out of every figure's hash.
"""

from __future__ import annotations

import pytest

from uratori import (
    CheckError,
    FactError,
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    SyntaxError_,
    Uratori,
    compile_source,
)

# Rebound as `service`: pytest names a fixture after its binding, and the
# plain name would shadow the import at every test signature.
from .test_server import Server
from .test_server import server as service  # noqa: F401

# The courier world again, declared in the language this time. The definitions
# half is byte-identical to test_schema.COURIER_SOURCE's -- that is what the
# hash-neutrality test leans on.

TAUGHT = Schema(
    kinds=frozenset(),
    bucket_settings=("limits.carrying.over",),
    figure_settings=("limits.carrying.over",),
    defaults={"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}},
)

FACTS = '''
# An order in the shop, as the provider last showed it.
fact shop_order:
    name ref
    url link
    ref as text
    link as text
    # Which courier holds it; absent until assigned.
    courier_id as text
    status as text
    placed_at as moment
    delivered_at as moment
    weight_grams as number
    rush as flag
    one dropoff:
        street as text
    many events:
        kind as text
        at as moment

# A courier on the road.
fact shop_courier:
    name display_name
    display_name as text
    many accounts:
        account_id as text
'''

DEFINITIONS = '''
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
    combine:
        carrying = shop_courier.carrying
    calculate:
        when carrying >= limits.carrying.over then "over"
        otherwise "ok"
'''

SOURCE = FACTS + DEFINITIONS


def compile_taught(source: str) -> object:
    return compile_source(source, TAUGHT)


def refuses(source: str, *needles: str) -> None:
    with pytest.raises((CheckError, SyntaxError_)) as caught:
        compile_taught(source)
    for needle in needles:
        assert needle in str(caught.value), str(caught.value)


# A minimal well-formed world for the refusal tests to append to.
BASE = '''
# An order.
fact shop_order:
    ref as text
    status as text
    placed_at as moment
    delivered_at as moment
    weight_grams as number
    rush as flag
    many events:
        kind as text
        at as moment
'''


# ---------------------------------------------------------------- parsing --


def test_facts_compile_and_carry_their_fields() -> None:
    library = compile_taught(SOURCE)
    order = library.facts["shop_order"]
    assert order.name_field == "ref"
    assert order.url_field == "link"
    types = {f.name: f.type for f in order.fields}
    assert types["placed_at"] == "moment"
    assert types["weight_grams"] == "number"
    assert types["rush"] == "flag"
    events = next(f for f in order.fields if f.name == "events")
    assert events.many and events.type is None
    assert {c.name for c in events.children} == {"kind", "at"}


def test_the_field_keyword_is_refused_with_directions() -> None:
    # The plausible spelling from every other schema language. Left to the
    # generic path it would fail as "expected as", pointing at nothing.
    refuses(
        "# An order.\nfact shop_order:\n    field ref as text\n",
        "field",
        "ref as text",
    )


def test_date_is_refused_by_name_in_a_fact() -> None:
    # `as date` is the projection binding; a fact field holding an instant is
    # a moment. Refused with the pointer rather than as an unknown word.
    refuses(
        "# An order.\nfact shop_order:\n    placed_at as date\n",
        "moment",
    )


def test_an_unknown_type_lists_the_vocabulary() -> None:
    refuses(
        "# An order.\nfact shop_order:\n    ref as string\n",
        "text",
        "number",
        "flag",
        "moment",
    )


def test_a_scalar_list_is_not_declarable() -> None:
    # No construct can read one: a predicate compares one field against one
    # literal and cannot test membership. A declared-but-unreadable field
    # would be the first construct nobody has checked.
    refuses(
        "# An order.\nfact shop_order:\n    many labels as text\n",
        "membership",
    )


def test_an_empty_fact_is_refused() -> None:
    refuses("# An order.\nfact shop_order:\n    name ref\n", "no fields")


def test_an_empty_nested_block_is_refused() -> None:
    refuses(
        "# An order.\nfact shop_order:\n    ref as text\n    one dropoff:\n",
        "fields of dropoff",
    )


def test_name_and_url_live_at_the_top_level_only() -> None:
    # A nested name field would name the *element*, and nothing renders one.
    refuses(
        "# An order.\nfact shop_order:\n    ref as text\n    one dropoff:\n"
        "        name street\n        street as text\n",
        "name",
    )


def test_a_fact_needs_an_explanation() -> None:
    # The explanation is what a reader clicking through to the schema sees --
    # rendered beside the fields, exactly as a figure's prose is beside its
    # formula.
    refuses("fact shop_order:\n    ref as text\n", "explanation")


def test_a_field_named_name_is_still_writable() -> None:
    # `name` is a directive only when the line is `name <field>`; a field
    # spelled `name as text` must parse as a field, or the vocabulary would
    # have grown a reserved word.
    library = compile_taught(
        "# A courier.\nfact shop_courier:\n    name as text\n    ref as text\n"
        "\nfilter shop_courier.named where name is set\n"
    )
    assert {f.name for f in library.facts["shop_courier"].fields} == {"name", "ref"}


def test_field_prose_is_carried() -> None:
    library = compile_taught(SOURCE)
    order = library.facts["shop_order"]
    courier_id = next(f for f in order.fields if f.name == "courier_id")
    assert "absent until assigned" in courier_id.doc


# --------------------------------------------------------------- checking --


def test_the_world_is_declared_in_one_place() -> None:
    # A schema with kinds AND a source with facts is two declarations of one
    # world; whichever a reader trusted, the other would drift.
    two_worlds = Schema(kinds=frozenset({"shop_order"}))
    with pytest.raises(CheckError) as caught:
        compile_source(BASE, two_worlds)
    assert "one place" in str(caught.value)


def test_schema_name_fields_conflict_the_same_way() -> None:
    sneaky = Schema(kinds=frozenset(), name_fields={})
    # name_fields over no kinds is empty and fine; the conflict is kinds.
    compile_source(BASE, sneaky)


def test_a_duplicate_fact_is_refused() -> None:
    refuses(BASE + "\n# Again.\nfact shop_order:\n    ref as text\n", "already")


def test_a_duplicate_field_is_refused() -> None:
    refuses(
        "# An order.\nfact shop_order:\n    ref as text\n    ref as number\n",
        "twice",
    )


def test_name_must_point_at_a_declared_text_field() -> None:
    refuses(
        "# An order.\nfact shop_order:\n    name weight\n    weight as number\n",
        "text",
    )
    refuses(
        "# An order.\nfact shop_order:\n    name ref\n    status as text\n",
        "ref",
    )


def test_an_unknown_kind_lists_the_declared_facts() -> None:
    refuses(BASE + "\ngroup shop_orb.by_x from x\n", "shop_orb", "shop_order")


def test_a_group_field_must_exist() -> None:
    refuses(
        BASE + "\ngroup shop_order.carried_by from courier_id\n",
        "courier_id",
        "shop_order",
    )


def test_a_filter_field_must_exist() -> None:
    refuses(BASE + '\nfilter shop_order.open where state != "closed"\n', "state")


def test_a_through_path_must_exist_on_the_other_kind() -> None:
    refuses(
        BASE
        + "\n# A courier.\nfact shop_courier:\n    display_name as text\n"
        + "    many accounts:\n        account_id as text\n"
        + "\ngroup shop_order.carried from ref through shop_courier.accounts.accountId\n",
        "accountId",
    )


def test_an_age_filter_needs_a_moment() -> None:
    refuses(
        BASE + "\nfilter shop_order.heavy where weight_grams older than limits.carrying.over\n",
        "moment",
    )


def test_a_time_bucket_needs_a_moment() -> None:
    refuses(
        BASE + "\ngroup shop_order.by_day from (ref, status by day)\n",
        "moment",
    )


def test_a_flag_field_takes_only_true_or_false() -> None:
    refuses(BASE + '\nfilter shop_order.rushed where rush == "yes"\n', "true")


def test_true_against_a_text_field_is_refused() -> None:
    refuses(BASE + "\nfilter shop_order.odd where status == true\n", "flag")


def test_a_duration_measure_needs_two_moments() -> None:
    refuses(
        BASE + "\nmeasure shop_order.span = delivered_at - ref\n",
        "moment",
    )


def test_a_field_measure_needs_a_number() -> None:
    refuses(BASE + "\nmeasure shop_order.w = status in count\n", "number")


def test_a_moment_measure_needs_a_moment() -> None:
    refuses(BASE + "\nmeasure shop_order.at = moment weight_grams\n", "moment")


def test_a_measure_may_not_cross_a_many() -> None:
    # read_number and read_instant answer one value; across a list they skip
    # or first-win, so the measure would be a silent nothing or a fabrication
    # for every record that carries two elements.
    refuses(BASE + "\nmeasure shop_order.at = moment events.at\n", "several")


def test_a_group_may_cross_a_many() -> None:
    # Bucketing flattens deliberately -- `accounts.account_id` means "any
    # account_id of any account" -- so a group over a list fans out.
    compile_taught(BASE + "\ngroup shop_order.by_event from events.kind\n")


def test_a_projection_field_path_must_exist_and_match() -> None:
    head = (
        "\n# Orders.\nprojection shop_order.item:\n"
    )
    refuses(BASE + head + "    field:\n        key = reff as text\n", "reff")
    refuses(BASE + head + "    field:\n        key = ref as date\n", "moment")
    refuses(
        BASE + head + "    field:\n        w = weight_grams as flag\n",
        "flag",
    )


def test_a_projection_field_may_not_cross_a_many() -> None:
    refuses(
        BASE + "\n# Orders.\nprojection shop_order.item:\n"
        "    field:\n        kind = events.kind as text\n",
        "several",
    )


def test_across_takes_its_name_field_from_the_facts() -> None:
    # Splitting across a kind with no name field renders raw ids; the rule
    # already existed against the schema and must hold against facts too.
    refuses(
        BASE
        + "\n# A courier.\nfact shop_courier:\n    display_name as text\n"
        + "\n# A depot.\nfact shop_depot:\n    code as number\n"
        + "\ngroup shop_order.per from (ref, status)\n"
        + "\n# x.\nfigure shop_order.x across shop_depot:\n"
        + '    display "{value}"\n'
        + "    depends:\n        mine = shop_order.per:{shop_order}\n"
        + "    calculate:\n        count(mine)\n",
        "name",
    )


# ---------------------------------------------------------------- hashing --


def test_declaring_facts_moves_no_downstream_version() -> None:
    """The property that makes adoption safe.

    The same definitions compiled against a kind-list schema and against
    declared facts must produce byte-identical figure versions: a fact schema
    decides what the checker permits and what the write boundary accepts --
    never what the arithmetic produces. If this fails, every host that adopts
    fact declarations rebuilds every tenant's history for a change that moved
    no number.
    """
    from .test_schema import COURIER_WORLD

    old = compile_source(DEFINITIONS, COURIER_WORLD)
    new = compile_taught(SOURCE)
    assert {f.name: f.version for f in old.figures} == {
        f.name: f.version for f in new.figures
    }


def test_prose_and_name_do_not_move_a_fact_version() -> None:
    one = compile_taught(BASE).facts["shop_order"].version
    reworded = BASE.replace("# An order.", "# An order, reworded.")
    named = reworded.replace("fact shop_order:\n", "fact shop_order:\n    name ref\n")
    assert compile_taught(named).facts["shop_order"].version == one


def test_a_type_change_moves_the_fact_version() -> None:
    one = compile_taught(BASE).facts["shop_order"].version
    widened = BASE.replace("weight_grams as number", "weight_grams as text")
    assert compile_taught(widened).facts["shop_order"].version != one


# ------------------------------------------------------------ verification --


def _facade(source: str = SOURCE) -> Uratori:
    return Uratori(
        schema=TAUGHT,
        library=compile_taught(source),
        store=MemoryEngineStore(),
        facts=MemoryFactStore(),
    )


def _order(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "ref": "A-1",
        "courier_id": "c1",
        "status": "riding",
        "placed_at": "2026-08-25T10:00:00Z",
        "weight_grams": 1200,
        "rush": False,
        "dropoff": {"street": "1 Way"},
        "events": [{"kind": "picked", "at": "2026-08-25T10:05:00Z"}],
    }
    record.update(overrides)
    return record


def _refused(engine: Uratori, record: dict[str, object], *needles: str) -> None:
    with pytest.raises(FactError) as caught:
        engine.verify(writes={"shop_order": {"o1": record}})
    for needle in needles:
        assert needle in str(caught.value), str(caught.value)


def test_a_conforming_batch_verifies() -> None:
    _facade().verify(writes={"shop_order": {"o1": _order()}})


def test_an_absent_field_is_not_an_error() -> None:
    # An absence is never a zero, and never a refusal either: a record may
    # omit any field, including as an explicit null.
    record = _order()
    del record["courier_id"]
    record["delivered_at"] = None
    _facade().verify(writes={"shop_order": {"o1": record}})


def test_an_unknown_kind_is_refused() -> None:
    engine = _facade()
    with pytest.raises(FactError) as caught:
        engine.verify(writes={"shop_orb": {"x": {}}})
    assert "shop_orb" in str(caught.value)
    with pytest.raises(FactError):
        engine.verify(deletes={"shop_orb": ["x"]})


def test_an_undeclared_field_is_refused_by_name() -> None:
    _refused(_facade(), _order(reff="A-1"), "shop_order", "o1", "reff")


def test_wrong_types_are_refused() -> None:
    engine = _facade()
    _refused(engine, _order(weight_grams="1200"), "weight_grams", "number")
    # A bool IS an int in Python; a guard that forgot that files `true` as 1.
    _refused(engine, _order(weight_grams=True), "weight_grams")
    _refused(engine, _order(rush="yes"), "rush", "flag")
    _refused(engine, _order(placed_at="yesterday"), "placed_at", "instant")
    _refused(engine, _order(ref=7), "ref", "text")


def test_shape_mismatches_are_refused() -> None:
    engine = _facade()
    _refused(engine, _order(dropoff=[{"street": "1 Way"}]), "dropoff", "one")
    _refused(engine, _order(events={"kind": "picked"}), "events", "list")
    _refused(engine, _order(status={"raw": "riding"}), "status")


def test_nested_fields_are_verified() -> None:
    engine = _facade()
    _refused(
        engine,
        _order(events=[{"kind": "picked", "when": "2026-08-25T10:05:00Z"}]),
        "events",
        "when",
    )


def test_an_untaught_world_verifies_kinds_only() -> None:
    # Schema-mode hosts declared no fields, so records stay arbitrary JSON --
    # but a write against a kind nobody declared has never been readable and
    # is a typo worth stopping at the door.
    from .test_schema import COURIER_SOURCE, COURIER_WORLD

    engine = Uratori(
        schema=COURIER_WORLD,
        library=compile_source(COURIER_SOURCE, COURIER_WORLD),
        store=MemoryEngineStore(),
        facts=MemoryFactStore(),
    )
    engine.verify(writes={"shop_order": {"o1": {"anything": ["goes", 3]}}})
    with pytest.raises(FactError):
        engine.verify(writes={"shop_orb": {"o1": {}}})


# ----------------------------------------------------------------- serving --


async def test_a_fact_taught_world_freezes_names() -> None:
    """The name field derived from the facts reaches the engine.

    If the facade kept the declared (empty) schema, every label would freeze
    as a raw id -- the exact failure name fields exist to prevent, arriving
    through the new door.
    """
    facts = MemoryFactStore()
    engine = Uratori(
        schema=TAUGHT,
        library=compile_taught(SOURCE),
        store=MemoryEngineStore(),
        facts=facts,
    )
    facts.put("t1", "shop_courier", "c1", {"display_name": "Aki"})
    facts.put("t1", "shop_order", "o1", _order())
    report = await engine.run("t1", full=True)
    carrying = next(
        r for r in report.results if r.name == "shop_courier.carrying"
    )
    labels = [s.name for s in carrying.subjects]
    assert labels == ["Aki"], labels


# ----------------------------------------------------------------- service --


async def test_the_service_serves_and_enforces_a_fact_taught_world(
    service: Server,  # noqa: F811 - the imported fixture, rebound
) -> None:
    """The whole loop over HTTP: teach with facts in the source, read the
    manifest back, and watch a bad batch bounce whole.

    The batch pairs a conforming record with a broken one on purpose: if the
    good record lands while the bad one 422s, the boundary quarantined
    per-record -- a narrowed population wearing an error message.
    """
    put = await service.http.put("/schema", json=TAUGHT.to_document())
    assert put.status_code == 200, put.text
    put = await service.http.put("/definitions", json={"source": SOURCE})
    assert put.status_code == 200, put.text

    manifest = {f["name"]: f for f in put.json()["facts"]}
    order = manifest["shop_order"]
    assert order["name_field"] == "ref" and order["version"]
    assert "provider last showed it" in order["prose"]
    leaves = {f["path"]: f for f in order["fields"]}
    assert leaves["placed_at"]["type"] == "moment"
    assert leaves["events.at"]["repeats"] is True
    assert "absent until assigned" in leaves["courier_id"]["prose"]

    bad = await service.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_order": {
                    "o1": _order(),
                    "o2": _order(weight_grams="heavy"),
                }
            }
        },
    )
    assert bad.status_code == 422, bad.text
    for named in ("shop_order", "o2", "weight_grams"):
        assert named in bad.json()["detail"]

    good = await service.http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "shop_courier": {"c1": {"display_name": "Aki"}},
                "shop_order": {"o1": _order()},
            }
        },
    )
    assert good.status_code == 200, good.text
    # written == 2 is the proof o1 did NOT land with the refused batch: an
    # identical re-push of an already-stored record writes nothing.
    assert good.json()["written"] == 2, good.json()


async def test_a_schema_with_kinds_cannot_replace_a_fact_taught_world(
    service: Server,  # noqa: F811 - the imported fixture, rebound
) -> None:
    put = await service.http.put("/schema", json=TAUGHT.to_document())
    assert put.status_code == 200
    put = await service.http.put("/definitions", json={"source": SOURCE})
    assert put.status_code == 200
    two_worlds = await service.http.put(
        "/schema", json={**TAUGHT.to_document(), "kinds": ["shop_order"]}
    )
    assert two_worlds.status_code == 422
    assert "one place" in two_worlds.json()["detail"]


# ------------------------------------------------------------------ source --


def test_the_data_screen_can_cite_a_fact() -> None:
    from uratori.lang.source import declaration_prose, declaration_source

    library = compile_taught(SOURCE)
    assert "as the provider last showed it" in declaration_prose(library, "shop_order")
    body = declaration_source(library, "shop_order")
    assert body is not None and "placed_at as moment" in body
