"""A pass rebuilds the groupings that moved, and only those.

Figures have always had this discipline: a per-figure pointer, so saving one
threshold recomputes one figure. Memberships did not -- one hash over the
whole index set meant adding a single filter re-bucketed every grouping over
every record, and on a tenant with a million facts that turned a one-line
edit into a rebuild of the world. These tests are the spec for the per-index
replacement: each grouping carries its own built version, a pass rebuilds
exactly the stale ones, retires exactly the removed ones, and the one-time
migration from the whole-set stamp neither loses work nor invents it.

The spies sit on the store protocol -- the boundary the engine pays its
writes through, the same seam test_pass_cost.py counts reads across -- so a
rewrite that keeps the protocol keeps the test. What is asserted is *work*:
which groupings were rebuilt, which were dropped. That is the requirement
itself, not an implementation detail; the whole point of the change is which
work a pass performs.
"""

from __future__ import annotations

from uratori.engine.engine import Engine
from uratori.store import MemoryEngineStore, MemoryFactStore

from .world import DEFAULTS, WORLD, compile_source

TENANT = "t1"

BASE = """
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
filter work_issue.active where active == true

# In progress.
figure team_person.wip:
    display "{value}"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)
"""

PARKED = 'filter work_issue.parked where active == false label "parked"\n'


class Ledger(MemoryEngineStore):
    """Counts the rebuild work by name, at the protocol seam."""

    def __init__(self) -> None:
        super().__init__()
        self.rebuilt: list[str] = []
        self.dropped: list[str] = []

    async def replace_index(self, tenant, index, wanted):  # type: ignore[override]
        self.rebuilt.append(index)
        return await super().replace_index(tenant, index, wanted)

    async def drop_index(self, tenant, index):  # type: ignore[override]
        self.dropped.append(index)
        return await super().drop_index(tenant, index)


def _seed(facts: MemoryFactStore) -> None:
    facts.put(TENANT, "team_person", "p1", {"display_name": "Aki", "accounts": [{"account_id": "a1"}]})
    facts.put(TENANT, "work_issue", "i1", {"title": "Ship", "assignee_account_id": "a1", "active": True})
    facts.put(TENANT, "work_issue", "i2", {"title": "Shelved", "assignee_account_id": "a1", "active": False})


async def test_an_arrived_filter_rebuilds_itself_and_nothing_else() -> None:
    """The complaint this whole change answers: one new filter must cost one
    grouping's scan, not the world's. And the new grouping must actually be
    built by that pass -- skipping the others is only allowed because the
    stale one was paid for."""
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    await Engine(store, facts, compile_source(BASE), WORLD).run(TENANT, DEFAULTS, full=True)
    assert set(store.rebuilt) == {"work_issue.assigned_to", "work_issue.active"}

    store.rebuilt.clear()
    grown = Engine(store, facts, compile_source(BASE + PARKED), WORLD)
    await grown.run(TENANT, DEFAULTS)
    assert store.rebuilt == ["work_issue.parked"], (
        "the untouched groupings were re-bucketed for a filter they never met"
    )
    assert set(await store.members(TENANT, "work_issue.parked", "")) == {"i2"}, (
        "narrow work is only honest if the new grouping really got built"
    )
    assert store.dropped == []


async def test_an_edited_filter_rebuilds_only_itself() -> None:
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    await Engine(store, facts, compile_source(BASE + PARKED), WORLD).run(
        TENANT, DEFAULTS, full=True
    )
    store.rebuilt.clear()

    edited = (BASE + PARKED).replace(
    'filter work_issue.parked where active == false label "parked"',
    'filter work_issue.parked where title == "Shelved" label "parked"',
    )
    await Engine(store, facts, compile_source(edited), WORLD).run(TENANT, DEFAULTS)
    assert store.rebuilt == ["work_issue.parked"]


async def test_a_label_moves_no_bucket_and_costs_no_rebuild() -> None:
    """Prose is not a spec: relabelling a filter serves a new word and owes
    no work. Under the whole-set hash this held only because the label was
    kept out of the hash; per index, the same rule, per grouping."""
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    await Engine(store, facts, compile_source(BASE + PARKED), WORLD).run(
        TENANT, DEFAULTS, full=True
    )
    store.rebuilt.clear()

    relabelled = (BASE + PARKED).replace('label "parked"', 'label "on ice"')
    await Engine(store, facts, compile_source(relabelled), WORLD).run(TENANT, DEFAULTS)
    assert store.rebuilt == [] and store.dropped == []


async def test_a_removed_filter_is_dropped_not_orphaned() -> None:
    """Nothing ever removed a dead grouping's rows before staleness went
    per-index -- they simply accumulated. Retirement is new behaviour, and
    it must be deliberate: rows a definition left behind must not serve for
    ever as if it still existed."""
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    await Engine(store, facts, compile_source(BASE + PARKED), WORLD).run(
        TENANT, DEFAULTS, full=True
    )
    assert set(await store.members(TENANT, "work_issue.parked", "")) == {"i2"}
    store.rebuilt.clear()

    await Engine(store, facts, compile_source(BASE), WORLD).run(TENANT, DEFAULTS)
    assert store.dropped == ["work_issue.parked"]
    assert store.rebuilt == []
    assert set(await store.members(TENANT, "work_issue.parked", "")) == set()
    assert (await store.index_stamps(TENANT)).keys() == {
        "work_issue.assigned_to",
        "work_issue.active",
    }


async def test_a_moved_bucket_dial_rebuilds_the_dialled_groupings_only() -> None:
    """An age filter's membership depends on a dial the index hash
    deliberately excludes; the figure reading it notices through its own
    settings fingerprint. When that figure goes pending, the rebuild must
    reach the groupings whose spec actually reads a dial -- and not tax the
    dial-free ones beside them."""
    aged = BASE + (
        'filter work_issue.fresh where updated_at younger than thresholds.staleChangeDays label "fresh"\n'
        "\n"
        "# Recent load.\n"
        "figure team_person.fresh_wip:\n"
        '    display "{value}"\n'
        "    depends:\n"
        "        mine = work_issue.assigned_to:{team_person} & work_issue.fresh\n"
        "    calculate:\n"
        "        count(mine)\n"
    )
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    facts.put(
        TENANT, "work_issue", "i3",
        {"title": "New", "assignee_account_id": "a1", "active": True,
         "updated_at": "2026-08-25T00:00:00Z"},
    )
    library = compile_source(aged)
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)
    store.rebuilt.clear()

    turned = dict(DEFAULTS)
    turned["thresholds"] = dict(DEFAULTS["thresholds"], staleChangeDays=99)
    await Engine(store, facts, library, WORLD).run(TENANT, turned)
    assert "work_issue.fresh" in store.rebuilt, (
        "the dial moved and the grouping reading it was not re-bucketed -- "
        "its membership now describes the old dial"
    )
    assert "work_issue.assigned_to" not in store.rebuilt
    assert "work_issue.active" not in store.rebuilt


async def test_a_full_pass_still_rebuilds_everything() -> None:
    """`full` is the repair word: whatever the per-index bookkeeping says,
    a full pass re-buckets the lot -- it is also what re-crosses age
    thresholds whose only input is the clock."""
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    library = compile_source(BASE + PARKED)
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)
    store.rebuilt.clear()
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)
    assert set(store.rebuilt) == {
        "work_issue.assigned_to",
        "work_issue.active",
        "work_issue.parked",
    }


async def test_the_legacy_stamp_seeds_an_up_to_date_tenant() -> None:
    """The pre-0.7 store held one whole-set stamp. A tenant whose stamp
    matches the current library was fully built the day before the upgrade,
    and its first pass after must not pay for a rebuild it does not owe --
    the stamp converts to per-index versions and retires."""
    from uratori.engine.engine import _index_set_version

    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    library = compile_source(BASE)
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)

    # What an upgrade finds: membership rows present, per-index versions
    # absent, the old stamp still standing. Planted through the memory
    # store's own legacy fields, which exist exactly to model this state.
    store._index_stamps.clear()
    store._index_sets[TENANT] = _index_set_version(library)

    store.rebuilt.clear()
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS)
    assert store.rebuilt == [], (
        "the stamp said every grouping was current; rebuilding anyway taxes "
        "every tenant once per upgrade for nothing"
    )
    assert (await store.index_stamps(TENANT)).keys() == {
        "work_issue.assigned_to",
        "work_issue.active",
    }
    assert await store.legacy_index_set(TENANT) is None, (
        "the stamp must retire once converted, or it shadows every later save"
    )


async def test_a_stale_legacy_stamp_forces_the_rebuild_it_names() -> None:
    """The other side of the seed rule: a stamp naming a different set means
    the buckets were built under other definitions, and trusting them would
    serve memberships nobody defined."""
    from uratori.engine.engine import _index_set_version

    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    old = compile_source(BASE)
    await Engine(store, facts, old, WORLD).run(TENANT, DEFAULTS, full=True)
    store._index_stamps.clear()
    store._index_sets[TENANT] = _index_set_version(old)

    store.rebuilt.clear()
    grown = compile_source(BASE + PARKED)
    await Engine(store, facts, grown, WORLD).run(TENANT, DEFAULTS)
    assert set(store.rebuilt) == {
        "work_issue.assigned_to",
        "work_issue.active",
        "work_issue.parked",
    }
    assert await store.legacy_index_set(TENANT) is None


async def test_a_reinstated_filter_cannot_narrow_the_figures_it_feeds() -> None:
    """The trap in narrow rebuilds: retire a filter and its figure, reinstate
    them unchanged, and the figure's version never moves -- so the next pass
    is warm, and its deltas are applied through a grouping whose rows were
    dropped at retirement. Counting through that near-empty grouping and
    stopping would store a narrowed population's number permanently: the
    cardinal sin. The pass must recompute every figure whose grouping it
    wholesale-rebuilt, because a rebuild is diffless and the deltas above it
    saw the wrong world."""
    store, facts = Ledger(), MemoryFactStore()
    facts.put(TENANT, "team_person", "p1",
              {"display_name": "Aki", "accounts": [{"account_id": "a1"}]})
    for n in range(3):
        facts.put(TENANT, "work_issue", f"i{n}",
                  {"title": f"T{n}", "assignee_account_id": "a1", "active": True})
    with_figure = compile_source(BASE)
    await Engine(store, facts, with_figure, WORLD).run(TENANT, DEFAULTS, full=True)
    plan = next(p for p in with_figure.figures if p.name == "team_person.wip")
    held = await store.value(TENANT, "team_person.wip", plan.version, "p1")
    assert held is not None and held.value == 3.0

    # Retired: the figure and the filter leave; the grouping's rows go with it.
    bare = compile_source(
        "group work_issue.assigned_to from assignee_account_id "
        "through team_person.accounts.account_id\n"
    )
    await Engine(store, facts, bare, WORLD).run(TENANT, DEFAULTS)
    assert "work_issue.active" in store.dropped

    # Reinstated verbatim, and the next pass carries an ordinary delta.
    outcome = await Engine(store, facts, with_figure, WORLD).run(
        TENANT, DEFAULTS, written={"work_issue": ["i0"]}
    )
    healed = await store.value(TENANT, "team_person.wip", plan.version, "p1")
    assert healed is not None and healed.value == 3.0, (
        "the delta was counted through the not-yet-rebuilt grouping and "
        "nothing recomputed after the rebuild -- a narrowed population, "
        "stored until a full pass"
    )
    assert "team_person.wip" in outcome.rebuilt, (
        "the recompute is work, and work is reported"
    )


async def test_a_pass_dying_mid_rebuild_pays_only_the_remaining_debt() -> None:
    """Stamps are recorded per grouping, after each rebuild: a death halfway
    leaves exactly the unbuilt groupings stale, and the retry pays exactly
    that -- not the groupings already rebuilt before the crash."""

    class Dies(Ledger):
        wounded = True

        async def replace_index(self, tenant, index, wanted):  # type: ignore[override]
            if self.wounded and index == "work_issue.parked":
                raise RuntimeError("the database went away mid-rebuild")
            return await super().replace_index(tenant, index, wanted)

    import pytest

    store, facts = Dies(), MemoryFactStore()
    _seed(facts)
    library = compile_source(BASE + PARKED)
    with pytest.raises(RuntimeError):
        await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)
    assert "work_issue.parked" not in (await store.index_stamps(TENANT)), (
        "stamped before its rebuild ran -- the next pass would trust buckets "
        "that were never written"
    )

    store.wounded = False
    store.rebuilt.clear()
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS)
    assert store.rebuilt == ["work_issue.parked"], (
        "the retry owed one grouping and paid for something else"
    )


async def test_a_moved_dial_reaches_the_groupings_that_read_it_even_unread() -> None:
    """A dial-reading grouping nobody's figure reads -- an age filter kept
    for a projection, a calendar group kept for browsing -- has no pointer
    to notice a settings move for it. The stamp's own dial fingerprint is
    what notices: the next pass rebuilds exactly the groupings reading the
    moved dial, pending figures or none."""
    dialled = BASE + (
        'filter work_issue.fresh where updated_at younger than thresholds.staleChangeDays label "fresh"\n'
        "group work_issue.by_day from updated_at by day in tenant.timezone\n"
    )
    store, facts = Ledger(), MemoryFactStore()
    _seed(facts)
    facts.put(TENANT, "work_issue", "i3",
              {"title": "New", "assignee_account_id": "a1", "active": True,
               "updated_at": "2026-08-25T00:00:00Z"})
    library = compile_source(dialled)
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)

    store.rebuilt.clear()
    aged = dict(DEFAULTS)
    aged["thresholds"] = dict(DEFAULTS["thresholds"], staleChangeDays=99)
    await Engine(store, facts, library, WORLD).run(TENANT, aged)
    assert store.rebuilt == ["work_issue.fresh"], (
        "the age dial moved and only the grouping reading it owes a rebuild"
    )

    store.rebuilt.clear()
    moved = dict(aged)
    moved["tenant"] = dict(DEFAULTS["tenant"], timezone="Asia/Tokyo")
    await Engine(store, facts, library, WORLD).run(TENANT, moved)
    assert store.rebuilt == ["work_issue.by_day"], (
        "the zone moved and every day boundary with it; the zoned grouping "
        "alone owes the rebuild"
    )


POPULATION = """
filter code_change.open where state == "open"

# Open changes.
projection code_change.card:
    from code_change.open

    field:
        key = title as text
"""


async def test_a_projection_is_not_unseated_by_an_unrelated_arrival() -> None:
    """The population gate compares the groupings THIS projection reads. An
    unrelated filter arriving elsewhere in the library must not hold the
    page behind-deploy -- its own buckets are exactly what the pass built."""
    from uratori.engine.serve import project_rows

    store, facts = Ledger(), MemoryFactStore()
    facts.put(TENANT, "code_change", "c1", {"title": "c1", "state": "open"})
    facts.put(TENANT, "code_change", "c2", {"title": "c2", "state": "merged"})
    await Engine(store, facts, compile_source(POPULATION), WORLD).run(
        TENANT, DEFAULTS, full=True
    )

    grown = compile_source(POPULATION + PARKED)
    plan = grown.projection("code_change.card")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, grown, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is True, (
        "an unrelated filter arrived and the page went behind-deploy -- the "
        "whole-set gate back under a new name"
    )
    assert [r.id for r in rows] == ["c1"]


async def test_a_projection_over_an_edited_population_waits_for_the_pass() -> None:
    """The other half of the gate: the population filter's own spec moved,
    so the stored buckets describe the old predicate, and serving through
    them would be an Ok page with records silently missing -- or wrongly
    present. Presence of a stamp is not enough; the version must match."""
    from uratori.engine.serve import project_rows

    store, facts = Ledger(), MemoryFactStore()
    facts.put(TENANT, "code_change", "c1", {"title": "c1", "state": "open"})
    facts.put(TENANT, "code_change", "c2", {"title": "c2", "state": "merged"})
    await Engine(store, facts, compile_source(POPULATION), WORLD).run(
        TENANT, DEFAULTS, full=True
    )

    edited_source = POPULATION.replace('state == "open"', 'state == "merged"')
    edited = compile_source(edited_source)
    plan = edited.projection("code_change.card")
    assert plan is not None
    rows, state, _ = await project_rows(store, facts, edited, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is False and state.because == "behind-deploy"
    assert rows == []

    await Engine(store, facts, edited, WORLD).run(TENANT, DEFAULTS)
    rows, state, _ = await project_rows(store, facts, edited, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is True
    assert [r.id for r in rows] == ["c2"]


async def test_the_projection_gate_honours_the_upgrade_window() -> None:
    """The reader half of the legacy seed, same rule, no writes: a matching
    whole-set stamp serves the page; a mismatched one answers behind-deploy
    -- never `never-computed`, which would tell an upgraded deployment its
    history vanished."""
    from uratori.engine.engine import _index_set_version
    from uratori.engine.serve import project_rows

    store, facts = Ledger(), MemoryFactStore()
    facts.put(TENANT, "code_change", "c1", {"title": "c1", "state": "open"})
    library = compile_source(POPULATION)
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS, full=True)
    plan = library.projection("code_change.card")
    assert plan is not None

    # What an upgrade finds: rows built, per-index stamps absent, the old
    # whole-set stamp standing.
    store._index_stamps.clear()
    store._index_sets[TENANT] = _index_set_version(library)
    rows, state, _ = await project_rows(store, facts, library, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is True and [r.id for r in rows] == ["c1"]

    store._index_sets[TENANT] = "another-library-entirely"
    _, state, _ = await project_rows(store, facts, library, TENANT, plan, DEFAULTS, 0.0)
    assert state.ok is False and state.because == "behind-deploy", (
        "a mismatched stamp means bucketed-under-other-definitions, and "
        "never-computed would deny the history"
    )
