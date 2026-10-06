"""`audit` plans in the engine, with no model and no `audit` grammar at all.

5a's whole job: a plan the engine stores values for but never computes, a
new door for an outside writer (`accept`) to put one in, and the one thing a
definition may legally do with a word it did not itself compute -- compare
it in a ladder rung. Nothing here compiles an `audit` declaration (that is
5b); the plans below are hand-built exactly the way `test_level_preservation.py`
hand-builds a `FigurePlan`, so this package's engine work is provable before
any language or model code exists.
"""

from __future__ import annotations

from uratori.engine.engine import Engine
from uratori.lang.ast import Ladder, Number, Part, Rung, Text
from uratori.lang.plan import AuditPlan, FigurePlan, Library
from uratori.schema import Schema
from uratori.store import MemoryEngineStore, MemoryFactStore

TENANT = "t1"

SCHEMA = Schema(kinds=frozenset({"page"}), name_fields={"page": "title"})

AUDIT = AuditPlan(
    name="page.vitals_audit",
    scope="page",
    verifies=(),
    model="fake",
    doc="# A stub auditor, written by hand -- no grammar, no provider.",
    display="{page} vitals_audit",
    version="audit-v1",
)

# `when page.vitals_audit == "disagrees" then 1 otherwise 0`, built the way
# the checker would build it rather than through the parser: a bare combine
# read of a `level`-unit source, legal only inside a ladder rung's own
# operand.
CHECKED = FigurePlan(
    name="page.checked",
    scope="page",
    doc="# Flags a page whose auditor disagrees with the extract.",
    display="{page} checked",
    unit="count",
    calculate=Ladder(
        rungs=(
            Rung(left=Part(name="page.vitals_audit"), op="==", right=Text("disagrees"), then=Number(1)),
        ),
        otherwise=Number(0),
    ),
    combines={"page.vitals_audit": ("page.vitals_audit", None)},
    reads=("page.vitals_audit",),
    depth=1,
    version="checked-v1",
)

LIB = Library(
    indexes={},
    measures={},
    figures=(CHECKED,),
    readings=(),
    projections=(),
    summaries=(),
    source="",
    audits={"page.vitals_audit": AUDIT},
)


def build() -> tuple[Engine, MemoryFactStore, MemoryEngineStore]:
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    engine = Engine(store, facts, LIB, SCHEMA)
    return engine, facts, store


def seed_page(facts: MemoryFactStore, key: str, title: str = "Page") -> None:
    facts.put(TENANT, "page", key, {"title": title})


async def test_a_cold_pass_never_computes_an_audit_value() -> None:
    """`_backfill`/`_recompute` are figure-only: an audit plan carries no
    `calculate`, so a full pass must leave it exactly as unwritten as a
    figure the host has never touched."""
    engine, facts, store = build()
    seed_page(facts, "doc1/p0001")
    await engine.run(TENANT, full=True)
    held = await store.value(TENANT, "page.vitals_audit", AUDIT.version, "doc1/p0001")
    assert held is None


async def test_accept_writes_a_word_value_and_cascades_the_reader() -> None:
    engine, facts, store = build()
    seed_page(facts, "doc1/p0001")
    await engine.run(TENANT, full=True)
    # Before any reading exists, `page.checked`'s combine read is absent --
    # the ladder stops on an unknown rather than falling through to 0.
    before = await store.value(TENANT, "page.checked", CHECKED.version, "doc1/p0001")
    assert before is not None
    assert before.value is None

    outcome = await engine.accept(
        TENANT,
        "page.vitals_audit",
        "doc1/p0001",
        "disagrees",
        ["doc1/p0001"],
        "Page",
    )
    audit_row = await store.value(TENANT, "page.vitals_audit", AUDIT.version, "doc1/p0001")
    assert audit_row is not None
    assert audit_row.value == "disagrees"
    assert audit_row.members == ("doc1/p0001",)

    # The cascade: `page.checked` reads the audit by name, so accepting a
    # new verdict recomputed it without a pass ever running.
    checked = await store.value(TENANT, "page.checked", CHECKED.version, "doc1/p0001")
    assert checked is not None
    assert checked.value == 1.0

    figures_moved = {c.figure for c in outcome.changes}
    assert "page.vitals_audit" in figures_moved
    assert "page.checked" in figures_moved


async def test_a_full_pass_after_accept_leaves_both_values_intact() -> None:
    """The audit is still never backfilled or recomputed by the engine, and a
    figure reading it unchanged recomputes to the same number -- so a full
    pass reports nothing and changes nothing."""
    engine, facts, store = build()
    seed_page(facts, "doc1/p0001")
    await engine.run(TENANT, full=True)
    await engine.accept(TENANT, "page.vitals_audit", "doc1/p0001", "disagrees", ["doc1/p0001"], "Page")

    outcome = await engine.run(TENANT, full=True)

    audit_row = await store.value(TENANT, "page.vitals_audit", AUDIT.version, "doc1/p0001")
    assert audit_row is not None and audit_row.value == "disagrees"
    checked = await store.value(TENANT, "page.checked", CHECKED.version, "doc1/p0001")
    assert checked is not None and checked.value == 1.0
    assert not any(c.figure in ("page.vitals_audit", "page.checked") for c in outcome.changes)


async def test_accept_an_unchanged_verdict_reports_nothing() -> None:
    engine, facts, _store = build()
    seed_page(facts, "doc1/p0001")
    await engine.run(TENANT, full=True)
    await engine.accept(TENANT, "page.vitals_audit", "doc1/p0001", "disagrees", ["doc1/p0001"], "Page")

    again = await engine.accept(
        TENANT, "page.vitals_audit", "doc1/p0001", "disagrees", ["doc1/p0001"], "Page"
    )
    assert again.changes == ()


async def test_removing_the_page_removes_the_audit_row_on_the_next_pass() -> None:
    """`_remove_departed` sweeps `library.audits` exactly as it sweeps
    `library.figures`: a page that is gone must not keep its auditor's
    verdict for ever."""
    engine, facts, store = build()
    seed_page(facts, "doc1/p0001")
    await engine.run(TENANT, full=True)
    await engine.accept(TENANT, "page.vitals_audit", "doc1/p0001", "disagrees", ["doc1/p0001"], "Page")

    facts.drop(TENANT, "page", "doc1/p0001")
    outcome = await engine.run(TENANT, deleted={"page": ["doc1/p0001"]})

    held = await store.value(TENANT, "page.vitals_audit", AUDIT.version, "doc1/p0001")
    assert held is None
    assert any(c.figure == "page.vitals_audit" and c.kind == "removed" for c in outcome.changes)


async def test_accept_refuses_a_name_that_is_not_a_declared_audit() -> None:
    engine, _facts, _store = build()
    try:
        await engine.accept(TENANT, "page.checked", "doc1/p0001", "x", [], "Page")
    except KeyError:
        pass
    else:
        raise AssertionError("accept must refuse a figure name -- only an audit plan owns this door")
