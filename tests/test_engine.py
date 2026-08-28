"""The engine, and the properties the whole product rests on.

The library below is written out rather than imported, so it can hold exactly
the shapes a property needs and nothing else. That costs something and the cost
is worth naming: it can drift from the records the product actually collects,
and it did -- every field here was camelCase while every record type is
snake_case, so these properties were held over definitions that could never have
run. `test_the_inline_library_reads_fields_that_exist` below is the guard, and
`test_fields.py` covers the shipped definitions.

Two of these are not negotiable and each carries a control:

  - **a scoped run leaves the state a full run would.** The cheap incremental
    path may never narrow the population a calculation is performed over. v1's
    worst bug was exactly this one layer along.
  - **the change stream is complete.** Every value that moved is reported and
    every value that was removed is reported, because the socket is fed from
    this and a missing report is a screen that never corrects itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from uratori.engine.engine import Engine
from uratori.store import MemoryEngineStore, MemoryFactStore

from .world import DEFAULTS, WORLD, compile_source

TENANT = "t1"

LIB_SOURCE = """
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
filter work_issue.active where active == true
filter code_change.open where state == "open"
group code_change.by_source from connection_id
group code_change.authored_in from (author_account_id through team_person.accounts.account_id, connection_id)
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)

measure work_issue.estimate = estimate_seconds in effort
measure code_change.open_seconds = merged_at - created_at

# In progress.
figure team_person.wip:
    display "{team_person} in progress"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)

# Load band.
figure team_person.wip_level:
    display "{team_person} load"
    combine:
        wip = team_person.wip
    calculate:
        when wip >= thresholds.wip.over then "over"
        when wip >= thresholds.wip.warn then "warn"
        otherwise "ok"

# Effort in flight.
figure team_person.wip_effort:
    display "{team_person} effort"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        sum(work_issue.estimate over mine)

# Open MRs per source.
figure team_person.open_mrs_by_source across data_connection:
    display "{team_person} open in {data_connection}"
    depends:
        mine = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(mine)

# Open MRs.
figure team_person.open_mrs:
    display "{team_person} open MRs"
    combine:
        sources = team_person.open_mrs_by_source over data_connection
    calculate:
        sum(sources)
"""

LIB = compile_source(LIB_SOURCE)


def test_the_inline_library_reads_fields_that_exist() -> None:
    """This file's library must read the same fields the suite's specimens carry.

    A suite-internal consistency guard now, not a product one: the specimens
    are literals in `world.py`, so what this catches is this file's library
    drifting from the records these tests write -- the failure it was born
    from was camelCase index paths over snake_case records, properties
    perfectly true about definitions that could never have run. The
    product-side half (shipped definitions against real record types) lives
    in the origin project's own test_fields.py, beside the record
    constructors it needs.
    """
    from uratori.engine.buckets import read_path
    from uratori.lang.check import _index_fields

    from .world import SPECIMENS

    for name, index in LIB.indexes.items():
        specimen = SPECIMENS[index.kind]
        for part in _index_fields(index.spec):
            assert read_path(specimen, part.field), f"{name} reads {part.field}, which no record has"
            if part.through is not None:
                owner = SPECIMENS[part.through.kind]
                assert read_path(owner, part.through.path), (
                    f"{name} resolves through {part.through.path}, which no record has"
                )
    for name, measure in LIB.measures.items():
        specimen = SPECIMENS[measure.kind]
        path = measure.field_path or measure.moment or measure.earlier
        assert path is not None
        assert read_path(specimen, path), f"{name} reads {path}, which no record has"


def build() -> tuple[Engine, MemoryFactStore, MemoryEngineStore]:
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    engine = Engine(store, facts, LIB, WORLD)
    return engine, facts, store


def person(facts: MemoryFactStore, pid: str, name: str, accounts: list[str]) -> None:
    facts.put(
        TENANT,
        "team_person",
        pid,
        {"display_name": name, "accounts": [{"account_id": a} for a in accounts]},
    )


def issue(facts: MemoryFactStore, key: str, assignee: str, active: bool, estimate: int = 0) -> None:
    facts.put(
        TENANT,
        "work_issue",
        key,
        {
            "key": key,
            "title": key,
            "assignee_account_id": assignee,
            "active": active,
            "estimate_seconds": estimate,
        },
    )


def change(facts: MemoryFactStore, cid: str, author: str, source: str, state: str = "open") -> None:
    facts.put(
        TENANT,
        "code_change",
        cid,
        {"title": cid, "author_account_id": author, "connection_id": source, "state": state},
    )


def connection(facts: MemoryFactStore, cid: str, label: str) -> None:
    facts.put(TENANT, "data_connection", cid, {"label": label})


async def seed(facts: MemoryFactStore) -> None:
    person(facts, "p1", "Ada Kensit", ["jira:ada", "gitlab:ada"])
    person(facts, "p2", "Bo Rivers", ["jira:bo"])
    connection(facts, "c1", "Platform GitLab")
    connection(facts, "c2", "Legacy GitLab")
    issue(facts, "CX-1", "jira:ada", True, 3600)
    issue(facts, "CX-2", "jira:ada", True, 7200)
    issue(facts, "CX-3", "jira:bo", False)
    change(facts, "c1:1", "gitlab:ada", "c1")
    change(facts, "c1:2", "gitlab:ada", "c1")
    change(facts, "c2:1", "gitlab:ada", "c2")


def value_of(store: MemoryEngineStore, figure: str, subject: str) -> Any:
    plan = LIB.figure(figure)
    assert plan is not None
    held = store._values.get((TENANT, figure, plan.version, subject))
    return held.value if held is not None else None


# ----------------------------------------------------------------- basics --


async def test_a_cold_run_computes_every_figure_for_every_subject() -> None:
    engine, facts, store = build()
    await seed(facts)
    outcome = await engine.run(TENANT, DEFAULTS, full=True)

    assert value_of(store, "team_person.wip", "p1") == 2
    assert value_of(store, "team_person.wip", "p2") == 0
    assert value_of(store, "team_person.wip_effort", "p1") == 10800
    assert outcome.changes


async def test_a_measured_nought_is_written_for_everybody_on_the_roster() -> None:
    """This is what makes an absence mean *not computed* rather than "none".
    Without it, every reader downstream has to guess which it is looking at."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p2") == 0


async def test_a_band_reads_the_figure_beneath_it() -> None:
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    # Two in progress, warn at three: comfortable.
    assert value_of(store, "team_person.wip_level", "p1") == "ok"
    for n in range(3, 8):
        issue(facts, f"CX-{n}", "jira:ada", True)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip_level", "p1") == "over"


async def test_a_rollup_equals_the_sum_of_its_parts() -> None:
    """The whole point of writing a total as its parts: the two cannot disagree,
    because there is only one count."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.open_mrs_by_source", "p1@c1") == 2
    assert value_of(store, "team_person.open_mrs_by_source", "p1@c2") == 1
    assert value_of(store, "team_person.open_mrs", "p1") == 3


async def test_a_dimensioned_figure_writes_no_pair_that_never_existed() -> None:
    """Crossing every person with every source would write a nought against
    connections that categorically cannot hold a merge request."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.open_mrs_by_source", "p2@c1") is None


# ------------------------------------------------------- the change stream --


async def test_a_departed_subject_is_reported_as_removed() -> None:
    """v1 deleted these silently, which is the single reason its socket could
    not be fed from the change stream. A screen that is never told keeps
    counting somebody who is gone."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p2") == 0

    facts.drop(TENANT, "team_person", "p2")
    outcome = await engine.run(TENANT, DEFAULTS, full=True)

    removed = [c for c in outcome.changes if c.kind == "removed" and c.subject == "p2"]
    assert removed, "a departed person must appear in the change stream"
    assert value_of(store, "team_person.wip", "p2") is None


async def test_a_removed_change_carries_the_name_the_subject_had() -> None:
    """Rendered when it happened and never re-derived. A subject whose fact has
    just departed has no name left to look up, so freezing it is the only way the
    report says anything a reader can use."""
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    facts.drop(TENANT, "team_person", "p2")
    outcome = await engine.run(TENANT, DEFAULTS, full=True)
    removed = next(c for c in outcome.changes if c.kind == "removed" and c.subject == "p2")
    assert removed.label == "Bo Rivers"


async def test_a_departed_dimension_removes_its_pairs() -> None:
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.open_mrs_by_source", "p1@c2") == 1

    facts.drop(TENANT, "data_connection", "c2")
    outcome = await engine.run(TENANT, DEFAULTS, full=True)

    assert value_of(store, "team_person.open_mrs_by_source", "p1@c2") is None
    assert any(c.subject == "p1@c2" and c.kind == "removed" for c in outcome.changes)


async def test_a_run_that_moved_nothing_reports_nothing() -> None:
    """A sync in which nothing happened filling the log is how the log stops
    being read. This is also the control for every test above: if the engine
    reported on every recompute, they would all pass for the wrong reason."""
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    again = await engine.run(TENANT, DEFAULTS, full=True)
    assert again.changes == ()


async def test_a_movement_reports_both_ends() -> None:
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    issue(facts, "CX-4", "jira:ada", True)
    outcome = await engine.run(TENANT, DEFAULTS, written={"work_issue": ["CX-4"]})
    moved = next(c for c in outcome.changes if c.figure == "team_person.wip")
    assert (moved.before, moved.after) == (2, 3)


async def test_a_band_change_is_reported_even_though_both_ends_are_words() -> None:
    """The suppression test has to ask whether the *values* are the same, not
    whether two numbers are equal -- written the naive way it compares None with
    None and discards every band change on the board."""
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    for n in range(4, 10):
        issue(facts, f"CX-{n}", "jira:ada", True)
    outcome = await engine.run(TENANT, DEFAULTS, full=True)
    band = next(c for c in outcome.changes if c.figure == "team_person.wip_level")
    assert (band.before, band.after) == ("ok", "over")


# --------------------------------------------------------- the scoped rule --


async def test_a_scoped_run_leaves_the_state_a_full_run_would() -> None:
    """The property this product rests on. The cheap incremental path may never
    narrow the population a calculation is performed over."""
    scoped_engine, scoped_facts, scoped_store = build()
    full_engine, full_facts, full_store = build()
    await seed(scoped_facts)
    await seed(full_facts)
    await scoped_engine.run(TENANT, DEFAULTS, full=True)
    await full_engine.run(TENANT, DEFAULTS, full=True)

    for facts in (scoped_facts, full_facts):
        issue(facts, "CX-9", "jira:ada", True, 1800)
        change(facts, "c1:3", "gitlab:ada", "c1")
        issue(facts, "CX-1", "jira:ada", False, 3600)

    await scoped_engine.run(
        TENANT,
        DEFAULTS,
        written={"work_issue": ["CX-9", "CX-1"], "code_change": ["c1:3"]},
    )
    await full_engine.run(TENANT, DEFAULTS, full=True)

    assert scoped_store._values == full_store._values


async def test_the_control_a_deliberately_narrowed_run_disagrees() -> None:
    """Without this, a run that recomputed everything unconditionally would pass
    the property above and the property would be asserting nothing."""
    scoped_engine, scoped_facts, scoped_store = build()
    full_engine, full_facts, full_store = build()
    await seed(scoped_facts)
    await seed(full_facts)
    await scoped_engine.run(TENANT, DEFAULTS, full=True)
    await full_engine.run(TENANT, DEFAULTS, full=True)

    for facts in (scoped_facts, full_facts):
        issue(facts, "CX-9", "jira:ada", True)

    # Tell the scoped run about a *different* record than the one that moved.
    await scoped_engine.run(TENANT, DEFAULTS, written={"work_issue": ["CX-3"]})
    await full_engine.run(TENANT, DEFAULTS, full=True)

    assert scoped_store._values != full_store._values


async def test_a_field_edit_that_moves_no_bucket_still_moves_the_figure() -> None:
    """Typing an estimate into Jira moves nothing -- same assignee, still active
    -- and it is the most ordinary thing that can happen to an effort figure. A
    warm path driven by bucket movement alone would go stale until a reconcile."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip_effort", "p1") == 10800

    issue(facts, "CX-1", "jira:ada", True, 36000)
    outcome = await engine.run(TENANT, DEFAULTS, written={"work_issue": ["CX-1"]})

    assert value_of(store, "team_person.wip_effort", "p1") == 43200
    assert any(c.figure == "team_person.wip_effort" for c in outcome.changes)


async def test_a_part_moving_marks_the_total_above_it() -> None:
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.open_mrs", "p1") == 3

    change(facts, "c1:9", "gitlab:ada", "c1")
    await engine.run(TENANT, DEFAULTS, written={"code_change": ["c1:9"]})

    assert value_of(store, "team_person.open_mrs", "p1") == 4


async def test_identity_decides_the_subject_so_two_accounts_are_one_person() -> None:
    """Without the join a figure buckets by account and splits anybody with a
    Jira login and a GitLab login into two half-people."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p1") == 2
    assert value_of(store, "team_person.open_mrs", "p1") == 3


# --------------------------------------------------------------- pointers --


async def test_a_pointer_moves_only_when_a_definition_or_a_named_dial_does() -> None:
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    before = await store.pointer(TENANT, "team_person.wip_level")
    assert before is not None

    await engine.run(TENANT, DEFAULTS, full=True)
    assert await store.pointer(TENANT, "team_person.wip_level") == before

    moved = {**DEFAULTS, "thresholds": {**DEFAULTS["thresholds"], "wip": {"warn": 1, "over": 2}}}
    await engine.run(TENANT, moved, full=True)
    after = await store.pointer(TENANT, "team_person.wip_level")
    assert after is not None and after.settings_fingerprint != before.settings_fingerprint


async def test_moving_a_dial_only_rebuilds_the_figures_that_name_it() -> None:
    """The observable difference between a narrow save and a full rebuild is
    *work*, and a figure recomputed to the value it already held writes nothing
    -- so the outcome has to say how many were rebuilt or nothing can assert it."""
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)

    moved = {**DEFAULTS, "thresholds": {**DEFAULTS["thresholds"], "wip": {"warn": 1, "over": 2}}}
    outcome = await engine.run(TENANT, moved)

    assert outcome.rebuilt == ("team_person.wip_level",)


async def test_a_failed_run_raises_rather_than_reporting_nothing() -> None:
    """"Nothing changed" is information. A run that could not finish must stay
    distinguishable from one that finished and moved nothing."""

    class Broken(MemoryFactStore):
        async def of_kind(self, tenant: str, kind: str) -> list[Any]:
            raise RuntimeError("postgres went away")

    engine = Engine(MemoryEngineStore(), Broken(), LIB, WORLD)
    with pytest.raises(RuntimeError):
        await engine.run(TENANT, DEFAULTS, full=True)


async def test_a_departed_person_does_not_come_back_on_the_next_backfill() -> None:
    """The removal pass and the backfill run in the same cold pass, so a roster
    that ignored the facts would delete a departed subject and put it straight
    back -- with the delete reported, which is worse than not reporting it."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    facts.drop(TENANT, "team_person", "p2")
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p2") is None
    again = await engine.run(TENANT, DEFAULTS, full=True)
    assert again.changes == ()


async def test_a_scoped_run_catches_a_field_edit_that_moves_no_bucket_at_all() -> None:
    """The case `_mark`'s non-scope-index branch exists for, and the property
    above cannot see it.

    That scenario writes a *new* issue in the same batch, which marks the person
    through the scope index and repairs the staleness incidentally -- so deleting
    the whole branch left every engine test green. This one writes a single
    record, flips one field, and touches nothing else: the assignee is unchanged
    so the scope index does not move, and only the predicate index does.
    """
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p1") == 2

    # One field, one record, nothing else in the batch.
    issue(facts, "CX-1", "jira:ada", False, 3600)
    await engine.run(TENANT, DEFAULTS, written={"work_issue": ["CX-1"]})

    assert value_of(store, "team_person.wip", "p1") == 1, (
        "a record leaving a predicate index did not reach the figure that reads it"
    )


async def test_a_warm_run_reports_a_departed_subject_too() -> None:
    """A departed subject is not a moved bucket, and the warm path is driven by
    bucket movement -- so without a removal pass a person deleted between
    reconciles keeps every value they had, and the change stream says nothing."""
    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)
    assert value_of(store, "team_person.wip", "p2") == 0

    facts.drop(TENANT, "team_person", "p2")
    outcome = await engine.run(TENANT, DEFAULTS, deleted={"team_person": ["p2"]})

    assert value_of(store, "team_person.wip", "p2") is None
    assert any(c.subject == "p2" and c.kind == "removed" for c in outcome.changes), (
        "the departure was applied and not reported"
    )


async def test_a_warm_run_with_no_deletions_does_not_pay_for_the_scan() -> None:
    """The control on the fix above: the removal pass walks every stored value,
    and every ordinary sync deletes nothing. It must not run then."""
    engine, facts, _ = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)

    issue(facts, "CX-4", "jira:ada", True)
    outcome = await engine.run(TENANT, DEFAULTS, written={"work_issue": ["CX-4"]})
    assert all(c.kind == "moved" for c in outcome.changes)


# ---------------------------------------------------------- population --


POPULATED = compile_source(
    """
filter code_change.open where state == "open"

# Only the changes still open.
projection code_change.card:
    from code_change.open

    field:
        key = title as text

# Every change; the control.
projection code_change.every:
    field:
        key = title as text
"""
)


def _change(facts: MemoryFactStore, key: str, state: str) -> None:
    facts.put(TENANT, "code_change", key, {"title": key, "state": state})


async def test_a_projection_from_is_the_definition_of_on_the_page() -> None:
    """A record outside the population produces no row at all. The control
    beside it is the rule that matters: without `from` every record still gets
    a row, so what narrows the page is the declared, versioned population and
    never a cheap path."""
    from uratori.engine.serve import project_rows

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    _change(facts, "c1", "open")
    _change(facts, "c2", "merged")
    await Engine(store, facts, POPULATED, WORLD).run(TENANT, DEFAULTS, full=True)

    filtered = POPULATED.projection("code_change.card")
    control = POPULATED.projection("code_change.every")
    assert filtered is not None and control is not None

    rows, state, _ = await project_rows(store, facts, POPULATED, TENANT, filtered, DEFAULTS, 0.0)
    assert state.ok is True
    assert [r.id for r in rows] == ["c1"]

    rows, _, _ = await project_rows(store, facts, POPULATED, TENANT, control, DEFAULTS, 0.0)
    assert {r.id for r in rows} == {"c1", "c2"}


async def test_an_empty_population_is_an_empty_page_and_not_an_absence() -> None:
    """Records were collected, so the projection must answer Ok with no rows.
    `nothing-collected` here would claim the sync had never run -- and a summary
    withheld over a board that genuinely has nothing on the page is a headline
    that never appears."""
    from uratori.engine.serve import project_rows

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    _change(facts, "c2", "merged")
    await Engine(store, facts, POPULATED, WORLD).run(TENANT, DEFAULTS, full=True)

    filtered = POPULATED.projection("code_change.card")
    assert filtered is not None
    rows, state, _ = await project_rows(store, facts, POPULATED, TENANT, filtered, DEFAULTS, 0.0)
    assert state.ok is True, "an empty population is a truthful page, not a missing one"
    assert rows == []

    # The control that makes the emptiness attributable: the same store serves
    # the unfiltered projection its row, so the engine demonstrably ran and
    # the empty page above is the population's answer, not an index that was
    # never built.
    control = POPULATED.projection("code_change.every")
    assert control is not None
    control_rows, _, _ = await project_rows(store, facts, POPULATED, TENANT, control, DEFAULTS, 0.0)
    assert [r.id for r in control_rows] == ["c2"]


GATED = compile_source(
    """
# One row per change, with the parked ones off the page.
projection code_change.active_board:
    field:
        key = title as text
        parked = parked as flag
    omit when parked == 1

# The board, in one row.
summarise code_change.board_size over code_change.active_board:
    count changes
"""
)


async def test_an_omitted_row_is_off_the_page_and_out_of_the_summary() -> None:
    """`omit` decides *on the page* once, before the summary, the sort and the
    limit. A summary still counting a row the gate dropped would put three on
    the tile over a page showing two -- a discrepancy no reader can check,
    because the third row is visible nowhere."""
    from uratori.facade import Uratori

    facts = MemoryFactStore()
    facts.put(TENANT, "code_change", "c1", {"title": "c1", "parked": "false"})
    facts.put(TENANT, "code_change", "c2", {"title": "c2", "parked": "true"})
    facade = Uratori(
        schema=WORLD, library=GATED, store=MemoryEngineStore(), facts=facts
    )

    result = await facade.answer(TENANT, "code_change.active_board")
    assert result is not None
    assert [subject.id for subject in result.subjects] == ["c1"]
    assert result.summary is not None
    assert result.summary.values["changes"] == 1


async def test_an_omit_the_engine_cannot_answer_keeps_the_row() -> None:
    """The gate drops a row on evidence, never on the absence of it. A record
    missing the field the condition reads has not been shown to be parked, and
    dropping it would narrow the population by a cheap path -- a page that
    quietly loses a row corrects itself never, because nothing downstream can
    see what is not there."""
    from uratori.facade import Uratori

    facts = MemoryFactStore()
    facts.put(TENANT, "code_change", "c3", {"title": "c3"})
    # The control that makes the survival attributable: a record the gate CAN
    # answer, dropped in the same pass. Without it this test passes with the
    # gate disabled entirely.
    facts.put(TENANT, "code_change", "c4", {"title": "c4", "parked": "true"})
    facade = Uratori(
        schema=WORLD, library=GATED, store=MemoryEngineStore(), facts=facts
    )

    result = await facade.answer(TENANT, "code_change.active_board")
    assert result is not None
    assert [subject.id for subject in result.subjects] == ["c3"]


GROWN_BY_ONE_INDEX = """
filter code_change.closed where state == "closed"

# The changes that were closed.
projection code_change.archived:
    from code_change.closed

    field:
        head = title as text
"""
"""The deploy every test below stages: the same figures to the version, plus
one index no figure reads and the projection whose `from` reads it."""


async def test_a_new_population_index_is_built_by_the_next_sync_not_the_next_full_one() -> None:
    """A figure notices its indexes changed because their specs are hashed into
    its version and the moved pointer forces a cold pass. An index read only by
    a projection's `from` has no figure and no pointer -- so without a trigger
    of its own, the deploy that adds one serves an empty-then-partial page with
    confident headline numbers until the next *full* sync: a population
    narrowed by the rebuild path rather than by the definition."""
    from uratori.engine.serve import project_rows

    engine, facts, store = build()
    await seed(facts)
    await engine.run(TENANT, DEFAULTS, full=True)

    grown = compile_source(LIB_SOURCE + GROWN_BY_ONE_INDEX)
    change(facts, "c9:1", "gitlab:ada", "c1", state="closed")
    change(facts, "c9:2", "gitlab:ada", "c1", state="closed")
    plan = grown.projection("code_change.archived")
    assert plan is not None

    # Between the deploy and the tenant's first pass the population's buckets
    # do not exist, and an Ok page here would be a confident zero -- so the
    # serving path refuses, the way it refuses a figure behind a deploy.
    early, state, _ = await project_rows(store, facts, grown, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is False and early == []
    assert state.because == "behind-deploy"

    # The delta poll that follows the deploy: one record written, no full pass.
    upgraded = Engine(store, facts, grown, WORLD)
    await upgraded.run(TENANT, DEFAULTS, written={"code_change": ["c9:1"]})

    rows, state2, _ = await project_rows(store, facts, grown, TENANT, plan, DEFAULTS, 0.0)
    assert state2.ok is True
    assert {r.id for r in rows} == {"c9:1", "c9:2"}, (
        "the page after a deploy is the population, not whichever records the "
        "last delta happened to touch"
    )


async def test_a_population_before_any_pass_is_never_computed_rather_than_empty() -> None:
    """Records collected, engine never run: the honest answer is the same one
    a figure gives, `never-computed`, not an Ok page with every record
    silently missing. A board whose scheduler never fires -- no enabled
    connection, say -- would otherwise show a truthful-looking empty roadmap
    for ever."""
    from uratori.engine.serve import project_rows

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    _change(facts, "c1", "open")

    plan = POPULATED.projection("code_change.card")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, POPULATED, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is False and rows == []
    assert state.because == "never-computed"


async def test_a_failed_reindex_is_retried_rather_than_recorded_as_built() -> None:
    """A grouping's built version is recorded only after ITS rebuild ran.
    Recorded up front -- in a `finally`, say -- a pass that dies mid-rebuild
    marks the grouping built, the next pass sees nothing stale, and the
    population serves over buckets that were never rebuilt until the hourly
    full sync repairs it in silence."""
    from uratori.engine.serve import project_rows

    engine, facts, store = build()
    await seed(facts)
    # A closed record that predates the deploy and is never named in any
    # written batch: the warm path buckets written records itself, so only a
    # record like this distinguishes "the wholesale rebuild ran" from "the
    # deltas happened to cover everything".
    change(facts, "c8:0", "gitlab:ada", "c1", state="closed")
    await engine.run(TENANT, DEFAULTS, full=True)

    grown = compile_source(LIB_SOURCE + GROWN_BY_ONE_INDEX)
    change(facts, "c9:1", "gitlab:ada", "c1", state="closed")
    change(facts, "c9:2", "gitlab:ada", "c1", state="closed")

    upgraded = Engine(store, facts, grown, WORLD)
    real_reindex = upgraded._reindex

    async def dies(
        tenant: str, settings: Any, only: Any = None, *, now_ms: float = 0.0
    ) -> None:
        raise RuntimeError("the database went away mid-rebuild")

    upgraded._reindex = dies  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await upgraded.run(TENANT, DEFAULTS, written={"code_change": ["c9:1"]})
    upgraded._reindex = real_reindex  # type: ignore[method-assign]

    await upgraded.run(TENANT, DEFAULTS, written={"code_change": ["c9:2"]})

    plan = grown.projection("code_change.archived")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, grown, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is True
    assert {r.id for r in rows} == {"c8:0", "c9:1", "c9:2"}, (
        "the failed rebuild was recorded as done, so the retry never happened "
        "-- c8:0 is the tell, since no delta ever named it"
    )


async def test_a_cold_pass_that_reads_no_index_still_builds_a_new_population() -> None:
    """The cold path's reindex was gated on `full or any(p.indexes for p in
    pending)`. A deploy that moves a dial only a combined figure reads makes
    exactly one figure pending, and that figure reads no index -- so without
    the index-set trigger the cold pass would record nothing rebuilt, the
    gate would go green, and the projection would serve Ok over buckets that
    were never built: a confident empty page."""
    from copy import deepcopy

    from uratori.engine.serve import project_rows

    engine, facts, store = build()
    await seed(facts)
    change(facts, "c8:0", "gitlab:ada", "c1", state="closed")
    await engine.run(TENANT, DEFAULTS, full=True)

    grown = compile_source(LIB_SOURCE + GROWN_BY_ONE_INDEX)
    moved = deepcopy(dict(DEFAULTS))
    moved["thresholds"]["wip"]["warn"] = 4  # pending: wip_level alone, no indexes

    upgraded = Engine(store, facts, grown, WORLD)
    await upgraded.run(TENANT, moved)

    plan = grown.projection("code_change.archived")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, grown, TENANT, plan, moved, 0.0)
    assert state.ok is True
    assert {r.id for r in rows} == {"c8:0"}, (
        "the cold pass skipped the rebuild, so the page is not the population"
    )


async def test_redefining_a_population_index_rebuilds_its_buckets() -> None:
    """The index-set version must hash the *specs*, not the names. Hashed by
    name alone, redefining an existing index leaves the version unchanged, no
    rebuild triggers, the gate stays green -- and the page filters through
    buckets built under the old meaning, which is a narrowed population with
    an Ok state over it."""
    from uratori.engine.serve import project_rows

    engine, facts, store = build()
    await seed(facts)
    change(facts, "c8:0", "gitlab:ada", "c1", state="closed")
    await engine.run(TENANT, DEFAULTS, full=True)

    grown = compile_source(LIB_SOURCE + GROWN_BY_ONE_INDEX)
    await Engine(store, facts, grown, WORLD).run(TENANT, DEFAULTS)

    # The same index name, redefined to mean the opposite population.
    redefined_source = LIB_SOURCE + GROWN_BY_ONE_INDEX.replace(
        'where state == "closed"', 'where state == "merged"'
    )
    change(facts, "c9:9", "gitlab:ada", "c1", state="merged")
    redefined = compile_source(redefined_source)
    await Engine(store, facts, redefined, WORLD).run(TENANT, DEFAULTS)

    plan = redefined.projection("code_change.archived")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, redefined, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is True
    assert {r.id for r in rows} == {"c9:9"}, (
        "the page is still the old predicate's records: the redefinition "
        "moved no version, so nothing rebuilt the buckets"
    )


# ---------------------------------------------------- empty time buckets --


GRAINED = compile_source(
    """
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
group code_change.by_author from author_account_id through team_person.accounts.account_id

measure code_change.open_seconds = merged_at - created_at

# Every merge's duration, day by day.
figure team_person.merge_spans bucketed:
    display "{team_person} spans"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)

# Every merge span this person owns, kept whole.
figure team_person.span_list:
    display "{team_person} spans held"
    depends:
        mine = code_change.by_author:{team_person}
    calculate:
        list(code_change.open_seconds over mine)
"""
)


async def test_a_time_bucket_with_every_member_gated_off_is_an_absent_subject() -> None:
    """A day something merged but nothing could be measured (the record
    carries no created_at, so the span is unanswerable) is a day with no
    evidence -- the subject must be absent, not a stored empty list. Storing
    the list is what made the cold pass and the removal pass report that
    difference at each other for ever."""
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    person(facts, "p1", "Ada Kensit", ["jira:ada", "gitlab:ada"])
    facts.put(
        TENANT,
        "code_change",
        "c1",
        # merged_at buckets the record into a day; created_at is missing, so
        # the span measure answers None for it and the day's list is empty.
        {"title": "c1", "author_account_id": "gitlab:ada", "merged_at": "2026-01-02T03:04:05Z"},
    )

    engine = Engine(store, facts, GRAINED, WORLD)
    outcome = await engine.run(TENANT, DEFAULTS, full=True)

    plan = GRAINED.figure("team_person.merge_spans")
    assert plan is not None
    assert await store.values(TENANT, plan.name, plan.version) == [], (
        "the empty day was stored instead of being absent"
    )
    assert [c for c in outcome.changes if c.figure == plan.name] == []


async def test_full_passes_over_an_empty_time_bucket_stay_silent() -> None:
    """The flip-flop this fix removes: store [] on one pass, remove it on the
    next, store it again -- a change stream that reports the same non-event
    for ever. Three full passes must leave the subject absent and passes two
    and three must say nothing about the figure."""
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    person(facts, "p1", "Ada Kensit", ["jira:ada", "gitlab:ada"])
    facts.put(
        TENANT,
        "code_change",
        "c1",
        {"title": "c1", "author_account_id": "gitlab:ada", "merged_at": "2026-01-02T03:04:05Z"},
    )
    engine = Engine(store, facts, GRAINED, WORLD)
    plan = GRAINED.figure("team_person.merge_spans")
    assert plan is not None

    for attempt in range(3):
        outcome = await engine.run(TENANT, DEFAULTS, full=True)
        assert await store.values(TENANT, plan.name, plan.version) == []
        assert [c for c in outcome.changes if c.figure == plan.name] == [], (
            f"pass {attempt + 1} reported movement over a day that has none"
        )


async def test_a_roster_subjects_empty_list_is_a_measured_value_not_an_absence() -> None:
    """The control that keeps the absence rule narrow: a roster-scoped subject
    exists whatever their queue holds, so an empty list is a measured "none of
    it" -- stored, served, and quiet on the next pass. Removing it instead
    would make "nothing measured" indistinguishable from "never computed"."""
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    person(facts, "p1", "Ada Kensit", ["jira:ada", "gitlab:ada"])

    engine = Engine(store, facts, GRAINED, WORLD)
    plan = GRAINED.figure("team_person.span_list")
    assert plan is not None

    first = await engine.run(TENANT, DEFAULTS, full=True)
    held = await store.value(TENANT, plan.name, plan.version, "p1")
    assert held is not None
    assert held.value == []
    assert held.members == ()
    mine = [c for c in first.changes if c.figure == plan.name and c.subject == "p1"]
    assert [c.kind for c in mine] == ["moved"], "a measured nought arrives as a movement"

    second = await engine.run(TENANT, DEFAULTS, full=True)
    assert [c for c in second.changes if c.figure == plan.name] == [], (
        "an unchanged empty list must not be re-reported"
    )
    still = await store.value(TENANT, plan.name, plan.version, "p1")
    assert still is not None and still.value == []
