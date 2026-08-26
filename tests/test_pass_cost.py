"""A pass reads its inputs per figure, never per subject.

The reader context a figure evaluates against -- its index buckets, the fact
tables its measures read, the stored parts a rollup totals -- is fixed for
the whole of that figure's turn in a pass: buckets were rebuilt before any
recompute started, facts do not move mid-pass, and depth ordering means a
source figure's values are all written before any total reads them. So the
number of times a pass asks the stores for those inputs must scale with the
library, not with the roster.

The mistake this pins down: rebuilding the context once per subject-value.
That is quadratic-shaped -- every stat line pushed at a season-sized tenant
re-scanned every fact of the kind, per player, per figure -- and it turned a
bulk load from seconds into tens of minutes while staying invisible at test
sizes. Both halves of a pass had it: the warm recompute and the cold
backfill, so both are measured here, separately. The assertion is growth,
not a magic number: the same library over a roster six times the size must
ask the stores for exactly the same number of loads -- and each pass must
demonstrably have done work, because a pass that recomputed nothing would
hold these equalities vacuously.
"""

from __future__ import annotations

from dataclasses import dataclass

from uratori.engine.engine import Engine
from uratori.store import MemoryEngineStore, MemoryFactStore

from .test_engine import LIB, TENANT, change, connection, issue, person
from .world import DEFAULTS, WORLD


class CountingFacts(MemoryFactStore):
    """The spies sit on the store protocols -- the boundary the engine pays
    its reads through -- so a rewrite that keeps the protocol keeps the test.
    Every whole-collection read `_readers` performs is counted: fact scans,
    bucket loads, filter members, and the stored parts a rollup totals. A
    partial hoist that still rebuilt one of them per subject would be
    exactly as quadratic as the original mistake."""

    def __init__(self) -> None:
        super().__init__()
        self.kind_scans = 0

    async def of_kind(self, tenant: str, kind: str):  # type: ignore[override]
        self.kind_scans += 1
        return await super().of_kind(tenant, kind)


class CountingStore(MemoryEngineStore):
    def __init__(self) -> None:
        super().__init__()
        self.bucket_loads = 0
        self.member_loads = 0
        self.value_loads = 0

    async def all_buckets(self, tenant: str, index: str):  # type: ignore[override]
        self.bucket_loads += 1
        return await super().all_buckets(tenant, index)

    async def members(self, tenant: str, index: str, bucket: str):  # type: ignore[override]
        self.member_loads += 1
        return await super().members(tenant, index, bucket)

    async def values(self, tenant: str, figure: str, version: str):  # type: ignore[override]
        self.value_loads += 1
        return await super().values(tenant, figure, version)


@dataclass
class Loads:
    kind_scans: int
    bucket_loads: int
    member_loads: int
    value_loads: int
    changes: int


def _seed(facts: CountingFacts, people: int) -> None:
    connection(facts, "c1", "Platform GitLab")
    for n in range(people):
        person(facts, f"p{n}", f"Person {n}", [f"jira:{n}", f"gitlab:{n}"])
        for i in range(2):
            issue(facts, f"CX-{n}-{i}", f"jira:{n}", active=True, estimate=3600)
        change(facts, f"c1:{n}", f"gitlab:{n}", "c1")


def _snapshot(facts: CountingFacts, store: CountingStore, changes: int) -> Loads:
    return Loads(
        kind_scans=facts.kind_scans,
        bucket_loads=store.bucket_loads,
        member_loads=store.member_loads,
        value_loads=store.value_loads,
        changes=changes,
    )


def _reset(facts: CountingFacts, store: CountingStore) -> None:
    facts.kind_scans = 0
    store.bucket_loads = 0
    store.member_loads = 0
    store.value_loads = 0


async def _loads_for(people: int) -> tuple[Loads, Loads]:
    """(cold full pass, warm bulk pass) over a `people`-sized roster."""
    facts = CountingFacts()
    store = CountingStore()
    engine = Engine(store, facts, LIB, WORLD)
    _seed(facts, people)

    _reset(facts, store)
    cold_outcome = await engine.run(TENANT, DEFAULTS, full=True)
    cold = _snapshot(facts, store, len(cold_outcome.changes))

    # The warm pass: touch every person's work at once -- a bulk sync, not a
    # webhook trickle -- and count what the recompute asks the stores for.
    written: dict[str, list[str]] = {"work_issue": []}
    for n in range(people):
        key = f"CX-{n}-new"
        issue(facts, key, f"jira:{n}", active=True, estimate=1800)
        written["work_issue"].append(key)
    _reset(facts, store)
    warm_outcome = await engine.run(TENANT, DEFAULTS, written=written)
    warm = _snapshot(facts, store, len(warm_outcome.changes))
    return cold, warm


async def test_a_pass_loads_its_inputs_per_figure_not_per_subject() -> None:
    small_cold, small_warm = await _loads_for(4)
    large_cold, large_warm = await _loads_for(24)

    # The equalities below are vacuous over a pass that did nothing, so
    # first: both passes must actually have computed. The cold pass writes
    # every figure for every subject; the warm one moves each person's wip,
    # wip_effort and wip_level -- three changes per person.
    assert small_cold.changes > 0 and large_cold.changes > small_cold.changes
    assert small_warm.changes == 3 * 4
    assert large_warm.changes == 3 * 24

    for name, small, large in (("cold", small_cold, large_cold), ("warm", small_warm, large_warm)):
        for load in ("kind_scans", "bucket_loads", "member_loads", "value_loads"):
            got_small = getattr(small, load)
            got_large = getattr(large, load)
            assert got_large == got_small, (
                f"the {name} pass over 24 people did {got_large} {load} against "
                f"{got_small} for 4 people -- a reader context is being rebuilt "
                "per subject instead of per figure"
            )
