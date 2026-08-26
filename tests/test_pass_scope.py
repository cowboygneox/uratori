"""A pass's tail is proportional to what its cause can reach.

The engine's whole claim is that the dependency graph is known with
certainty: every figure names the groupings and figures it reads, every
projection names its population and reads. 0.7.0 made the *rebuild* obey
that graph; these tests make the rest of the pass obey it too. A
definition-only pass (no facts written, nothing deleted, not full) knows
exactly what it invalidated -- so it re-serves exactly the answers
downstream of that, sweeps nothing it cannot have changed, and a pass
whose change reaches nothing serves nothing and wakes nobody.

The carve-out is deliberate and pinned here too: a pass carrying facts (or
`full`) is the host's sync moment, and every projection re-serves on it --
records arrived and the clock advanced, and the projections' sentences
("3 days ago") are only ever refreshed at sync moments. A definition
deploy is not that moment, and letting it masquerade as one is how eight
seconds of serving nobody hid inside "add a filter".
"""

from __future__ import annotations

from uratori import Uratori
from uratori.results import Result
from uratori.store import MemoryEngineStore, MemoryFactStore

from .test_pass_cost import CountingFacts, CountingStore
from .world import WORLD, compile_source

TENANT = "t1"

LIB = """
filter code_change.open where state == "open"
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
filter work_issue.active where active == true

# In progress.
figure team_person.wip:
    display "{value}"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)

# Load band.
figure team_person.wip_level:
    display "{value}"
    combine:
        wip = team_person.wip
    calculate:
        when wip >= thresholds.wip.over then "over"
        otherwise "ok"

# Open changes.
projection code_change.card:
    from code_change.open

    field:
        key = title as text

# Every issue.
projection work_issue.list:
    field:
        key = title as text
"""

PARKED = 'filter work_issue.parked where active == false label "parked"\n'


def _seed(facts: MemoryFactStore) -> None:
    facts.put(TENANT, "team_person", "p1",
              {"display_name": "Aki", "accounts": [{"account_id": "a1"}]})
    facts.put(TENANT, "work_issue", "i1",
              {"title": "Ship", "assignee_account_id": "a1", "active": True})
    facts.put(TENANT, "work_issue", "i2",
              {"title": "Shelved", "assignee_account_id": "a1", "active": False})
    facts.put(TENANT, "code_change", "c1", {"title": "c1", "state": "open"})
    facts.put(TENANT, "code_change", "c2", {"title": "c2", "state": "merged"})


def _facade(source: str, store: MemoryEngineStore, facts: MemoryFactStore) -> Uratori:
    return Uratori(schema=WORLD, library=compile_source(source), store=store, facts=facts)


def _served(results: tuple[Result, ...]) -> set[str]:
    return {result.name for result in results}


async def test_a_change_that_reaches_nothing_serves_nothing() -> None:
    """The user's sentence, verbatim: we added a filter, there is no
    downstream of it, and we know that with certainty. The pass must act on
    that certainty -- rebuild the one grouping, serve no answer (none can
    have moved), wake no listener, and read nothing it does not need: one
    fact scan for the rebuild, no bucket loads, no stored values."""
    store, facts = CountingStore(), CountingFacts()
    _seed(facts)
    await _facade(LIB, store, facts).run(TENANT, full=True)

    grown = _facade(LIB + PARKED, store, facts)
    heard: list[tuple[str, object, tuple[Result, ...]]] = []

    async def listen(tenant: str, outcome: object, results: tuple[Result, ...]) -> None:
        heard.append((tenant, outcome, results))

    grown.subscribe(listen)
    facts.kind_scans = 0
    store.bucket_loads = store.member_loads = store.value_loads = 0

    report = await grown.run(TENANT)
    assert report.outcome.reindexed == ("work_issue.parked",)
    assert report.results == (), (
        "nothing downstream of the new filter, so nothing can have moved -- "
        "a served answer here is the board re-pushed to nobody's benefit"
    )
    assert heard == [], "a pass with nothing to hear must wake no listener"
    assert facts.kind_scans == 1, (
        "one grouping to rebuild means one scan of its kind; more means the "
        "tail is still doing fixed work the change cannot reach"
    )
    assert store.value_loads == 0 and store.bucket_loads == 0, (
        "no gap sweep and no serving: the pass read stored state it had no "
        "reason to read"
    )


async def test_a_definition_pass_serves_exactly_what_it_reached() -> None:
    """Edit the population filter a projection reads: that projection's rows
    may genuinely differ, so it re-serves -- and the projection over another
    kind entirely does not. Certainty cuts both ways."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    _seed(facts)
    await _facade(LIB, store, facts).run(TENANT, full=True)

    edited = LIB.replace('state == "open"', 'state == "merged"')
    report = await _facade(edited, store, facts).run(TENANT)
    assert report.outcome.reindexed == ("code_change.open",)
    assert _served(report.results) == {"code_change.card"}, (
        "the edited filter reaches exactly one projection; serving less "
        "hides real movement, serving more is the fixed tail again"
    )


async def test_a_moved_figure_dial_serves_the_figure_not_the_board() -> None:
    """A figure dial turns: the figure recomputes and re-serves (its value
    moved), and no projection does -- neither reads it. The definition-only
    gate is about reach, not about kinds of change."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    _seed(facts)
    facade = _facade(LIB, store, facts)
    await facade.run(TENANT, full=True)

    turned = {"thresholds": {"wip": {"over": 1}}}
    report = await facade.run(TENANT, turned)
    assert "team_person.wip_level" in _served(report.results), (
        "the band flipped under the new dial; the moved figure must re-serve"
    )
    assert not _served(report.results) & {"code_change.card", "work_issue.list"}, (
        "no projection reads the dialled figure, and a dial is not a sync"
    )


async def test_a_facts_pass_is_still_the_serving_moment() -> None:
    """The carve-out: records arrived, so every projection re-serves --
    including the one whose kind the batch never mentioned, because the
    clock is one of a projection's inputs and the sync IS the moment that
    contract pays out."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    _seed(facts)
    facade = _facade(LIB, store, facts)
    await facade.run(TENANT, full=True)

    facts.put(TENANT, "code_change", "c3", {"title": "c3", "state": "open"})
    report = await facade.run(TENANT, written={"code_change": ["c3"]})
    assert {"code_change.card", "work_issue.list"} <= _served(report.results), (
        "a sync pass re-serves the board; gating it here would freeze every "
        "clock-worded sentence until a definition happened to change"
    )

    # The door decides, not the batch: a scheduled sync whose writes
    # deduplicated to nothing is still the sync moment, and the quiet weeks
    # are exactly when the clock is the only thing moving the sentences.
    report = await facade.run(TENANT, written={})
    assert {"code_change.card", "work_issue.list"} <= _served(report.results)
