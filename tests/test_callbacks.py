"""Listeners: delivery is not the calculation, and must never break it.

The values are committed by the time delivery starts, so a listener raising
has exactly one acceptable consequence -- a log line. Anything louder turns a
subscriber bug into a board that stops computing, which is the failure the
origin project's activity log made the same call about.
"""

from __future__ import annotations

import asyncio

from uratori import MemoryEngineStore, MemoryFactStore, Uratori

from .world import WORLD, compile_source

SOURCE = '''
index work_issue.active where active == true
index work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id

figure team_person.wip:
    """In progress."""
    display "{team_person} in progress"
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
    calculate:
        count(mine)
'''


def _engine() -> tuple[Uratori, MemoryFactStore]:
    facts = MemoryFactStore()
    engine = Uratori(
        schema=WORLD,
        library=compile_source(SOURCE),
        store=MemoryEngineStore(),
        facts=facts,
    )
    return engine, facts


def _seed(facts: MemoryFactStore) -> None:
    facts.put("t1", "team_person", "p1", {"display_name": "Aki", "accounts": [{"account_id": "a1"}]})
    facts.put("t1", "work_issue", "i1", {"title": "T", "active": True, "assignee_account_id": "a1"})


async def test_a_raising_listener_is_isolated_and_the_rest_still_hear() -> None:
    engine, facts = _engine()
    _seed(facts)

    heard: list[str] = []

    def broken(tenant: str, outcome: object, results: object) -> None:
        heard.append("broken-ran")
        raise RuntimeError("subscriber bug")

    engine.subscribe(broken)
    engine.subscribe(lambda t, o, r: heard.append("second"))

    report = await engine.run("t1", full=True)

    assert report.outcome.changes, "the control: this run must actually move something"
    assert heard == ["broken-ran", "second"], (
        "either the raising listener was skipped (it must run and fail alone) "
        "or it took the listener after it down with it"
    )


async def test_async_listeners_are_awaited_before_run_returns() -> None:
    """A host that publishes to a socket from a listener needs the publish to
    have happened when `run` hands back -- 'fire and forget' here would let a
    caller record a pass whose delivery is still in flight."""
    engine, facts = _engine()
    _seed(facts)

    delivered = asyncio.Event()

    async def push(tenant: str, outcome: object, results: object) -> None:
        await asyncio.sleep(0)
        delivered.set()

    engine.subscribe(push)
    await engine.run("t1", full=True)
    assert delivered.is_set()


async def test_unsubscribe_detaches_exactly_that_listener() -> None:
    engine, facts = _engine()
    _seed(facts)

    heard: list[str] = []
    stop = engine.subscribe(lambda t, o, r: heard.append("first"))
    engine.subscribe(lambda t, o, r: heard.append("kept"))
    stop()

    await engine.run("t1", full=True)
    assert heard == ["kept"]


async def test_a_pass_that_moved_nothing_notifies_nobody() -> None:
    """A poll in which nothing happened notifying every listener is how
    listeners stop being read -- the log-noise rule, one layer out."""
    engine, facts = _engine()
    _seed(facts)

    heard: list[str] = []
    await engine.run("t1", full=True)
    engine.subscribe(lambda t, o, r: heard.append("noise"))

    report = await engine.run("t1")
    assert not report.outcome.changes, "the control: the second pass must be quiet"
    assert heard == []
