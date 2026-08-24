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

LIB = compile_source(
    """
index work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
index work_issue.active where active == true
index code_change.open where state == "open"
index code_change.by_source from connection_id
index code_change.authored_in from (author_account_id through team_person.accounts.account_id, connection_id)
index code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)

measure work_issue.estimate = estimate_seconds in effort
measure code_change.open_seconds = merged_at - created_at

figure team_person.wip:
    \"\"\"In progress.\"\"\"
    display "{team_person} in progress"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)

figure team_person.wip_level:
    \"\"\"Load band.\"\"\"
    display "{team_person} load"
    combine:
        wip = team_person.wip
    calculate:
        when wip >= thresholds.wip.over then "over"
        when wip >= thresholds.wip.warn then "warn"
        otherwise "ok"

figure team_person.wip_effort:
    \"\"\"Effort in flight.\"\"\"
    display "{team_person} effort"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        sum(work_issue.estimate over mine)

figure team_person.open_mrs_by_source across data_connection:
    \"\"\"Open MRs per source.\"\"\"
    display "{team_person} open in {data_connection}"
    depends:
        mine = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(mine)

figure team_person.open_mrs:
    \"\"\"Open MRs.\"\"\"
    display "{team_person} open MRs"
    combine:
        sources = team_person.open_mrs_by_source over data_connection
    calculate:
        sum(sources)
"""
)


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
