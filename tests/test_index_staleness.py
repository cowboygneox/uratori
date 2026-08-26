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
    """All-or-nothing rebuilds erased retired groupings as a side effect of
    rebuilding everything. Narrow rebuilds must retire them deliberately, or
    the dead grouping's rows serve for ever as if the definition still
    existed."""
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
    assert (await store.index_versions(TENANT)).keys() == {
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
    store._index_versions.clear()
    store._index_sets[TENANT] = _index_set_version(library)

    store.rebuilt.clear()
    await Engine(store, facts, library, WORLD).run(TENANT, DEFAULTS)
    assert store.rebuilt == [], (
        "the stamp said every grouping was current; rebuilding anyway taxes "
        "every tenant once per upgrade for nothing"
    )
    assert (await store.index_versions(TENANT)).keys() == {
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
    store._index_versions.clear()
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
