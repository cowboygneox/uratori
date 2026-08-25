"""The records behind one stored value, served so a reader can check them.

The claim the engine exists for is that any served number is traceable to the
records that moved it. The engine has always stored the citation --
`StoredValue.members`, the record ids a value was computed from, positionally
aligned with a list figure's values -- and until `serve_evidence` nothing
served it: a day of durations could read "15.4d" fourteen times over with no
way to see *which* records those were. A citation nobody can read is not a
citation.

These tests drive `serve_evidence` over the memory stores; the route is
covered in `test_server.py`, because "the rule is right and the route stopped
calling it" is its own class of gap.
"""

from __future__ import annotations

from typing import Any

from uratori.engine.serve import serve_evidence
from uratori.lang.settings import fingerprint as settings_fingerprint
from uratori.store import MemoryEngineStore, MemoryFactStore, Pointer

from .world import DEFAULTS, WORLD, compile_source

TENANT = "t1"

LIB = compile_source(
    """
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
group work_issue.delivered_by_day from (assignee_account_id through team_person.accounts.account_id, completed_at by day in tenant.timezone)
group code_change.authored_in from (author_account_id through team_person.accounts.account_id, connection_id)
filter code_change.open where state == "open"
filter code_review.approved keyed as code_change where was_approved == true
filter work_issue.active where active == true
group work_issue.assigned from assignee_account_id through team_person.accounts.account_id

measure code_change.open_seconds = merged_at - created_at
measure work_issue.estimate = estimate_seconds in effort
measure work_issue.rework = rework_seconds in effort

# Every merge's duration, kept whole for the readings over it.
figure team_person.time_to_merge:
    display "{team_person} time to merge"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)

# How many items this person closed on each day.
figure team_person.delivered_issues:
    display "{team_person} delivered"
    depends:
        mine = work_issue.delivered_by_day:{team_person}
    calculate:
        count(mine)

# Open MRs, split across the connection they came from.
figure team_person.open_mrs_by_source across data_connection:
    display "{team_person} open in {data_connection}"
    depends:
        mine = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(mine)

# The total across sources, as the sum of its parts.
figure team_person.open_mrs:
    display "{team_person} open"
    combine:
        sources = team_person.open_mrs_by_source over data_connection
    calculate:
        sum(sources)

# Rework on what this person carries, added up.
figure team_person.rework_effort:
    display "{team_person} rework"
    depends:
        mine = work_issue.assigned:{team_person}
    calculate:
        sum(work_issue.rework over mine)

# Estimates on what this person carries, added up.
figure team_person.planned_effort:
    display "{team_person} planned"
    depends:
        mine = work_issue.assigned:{team_person}
    calculate:
        sum(work_issue.estimate over mine)

# The share of planned effort that was rework.
figure team_person.rework_share:
    display "{team_person} rework share"
    unit share

    combine:
        redone = team_person.rework_effort
        planned = team_person.planned_effort
    calculate:
        redone / planned

# Merges of this person's that were approved.
figure team_person.approved_merges:
    display "{team_person} approved"
    depends:
        mine = code_change.merged_by_day:{team_person} & code_review.approved
    calculate:
        count(mine)

# How much is active around this person, tenant-wide.
figure team_person.busy_context:
    display "{team_person} context"
    depends:
        merged = code_change.merged_by_day:{team_person}
        busy = work_issue.active
    calculate:
        count(busy)
"""
)


async def _ready(store: MemoryEngineStore, name: str) -> None:
    """Point the tenant at the figure's live version, so availability is Ok
    and the tests below are about evidence rather than about the gate."""
    plan = LIB.figure(name)
    assert plan is not None
    await store.set_pointer(
        TENANT,
        name,
        Pointer(
            version=plan.version,
            settings_fingerprint=settings_fingerprint(dict(DEFAULTS), list(plan.settings)),
        ),
    )


async def _evidence(store: MemoryEngineStore, facts: MemoryFactStore, name: str, subject: str) -> Any:
    plan = LIB.figure(name)
    assert plan is not None
    return await serve_evidence(store, facts, LIB, WORLD, TENANT, plan, DEFAULTS, subject)


async def test_each_member_carries_its_record_and_its_own_measurement() -> None:
    """The row a reader clicked said "1.0h, 2.0h"; this is what says the first
    hour was `c2` and the second was `c1`. The members are stored out of
    lexical order on purpose: the pairing is positional, so a serve path that
    sorted -- or a store that reordered -- would print the right numbers under
    the wrong merge requests, and a fixture already in sorted order could
    never catch it."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1@2026-08-20"])
    await _ready(store, "team_person.time_to_merge")
    version = LIB.figure("team_person.time_to_merge").version  # type: ignore[union-attr]
    await store.save(
        TENANT,
        "team_person.time_to_merge",
        version,
        "p1@2026-08-20",
        [3600.0, 7200.0],
        ("c2", "c1"),
        "Aki",
    )
    facts.put(TENANT, "code_change", "c1", {"title": "Fix the parser", "url": "https://git/1"})
    facts.put(TENANT, "code_change", "c2", {"title": "Widen the lexer", "url": "https://git/2"})

    evidence = await _evidence(store, facts, "team_person.time_to_merge", "p1@2026-08-20")

    assert evidence is not None
    assert evidence.state.ok is True
    assert evidence.version == version
    assert evidence.kind == "code_change"
    assert evidence.display == "1.0h, 2.0h"
    assert [m.key for m in evidence.members] == ["c2", "c1"]
    assert [m.title for m in evidence.members] == ["Widen the lexer", "Fix the parser"]
    assert [m.url for m in evidence.members] == ["https://git/2", "https://git/1"]
    assert [m.display for m in evidence.members] == ["1.0h", "2.0h"]
    assert all(m.held for m in evidence.members)
    assert evidence.parts is False
    # The control for the misalignment note below: a healthy row carries none.
    assert evidence.note is None


async def test_a_member_whose_record_is_gone_is_listed_and_marked_not_dropped() -> None:
    """The suspicious sample a reader came to check is exactly the one whose
    record may have since been deleted at the source. Dropping it would show a
    list quietly shorter than the value beside it -- the one check this page
    offers, silently guaranteed to fail confusingly. The measurement still
    prints: it is stored evidence, and it is what the reader is chasing."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1@2026-08-20"])
    await _ready(store, "team_person.time_to_merge")
    await store.save(
        TENANT,
        "team_person.time_to_merge",
        LIB.figure("team_person.time_to_merge").version,  # type: ignore[union-attr]
        "p1@2026-08-20",
        [3600.0, 7200.0],
        ("c1", "c2"),
        "Aki",
    )
    facts.put(TENANT, "code_change", "c1", {"title": "Fix the parser", "url": "https://git/1"})

    evidence = await _evidence(store, facts, "team_person.time_to_merge", "p1@2026-08-20")

    gone = evidence.members[1]
    assert gone.key == "c2"
    assert gone.held is False
    assert gone.title is None
    assert gone.url is None
    assert gone.display == "2.0h"
    # The control: its neighbour is whole.
    assert evidence.members[0].held is True


async def test_a_count_figures_members_have_no_measurement_of_their_own() -> None:
    """A count stores no per-record number, so none is served. Inventing one --
    a "1" beside each record -- would be arithmetic theatre: a number nothing
    computed, on the page whose claim is that every number was."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "work_issue.delivered_by_day", "i1", ["p1@2026-08-20"])
    await _ready(store, "team_person.delivered_issues")
    await store.save(
        TENANT,
        "team_person.delivered_issues",
        LIB.figure("team_person.delivered_issues").version,  # type: ignore[union-attr]
        "p1@2026-08-20",
        2,
        ("i1", "i2"),
        "Aki",
    )
    facts.put(TENANT, "work_issue", "i1", {"title": "Ship the drawer", "url": "https://jira/1"})
    facts.put(TENANT, "work_issue", "i2", {"title": "Name the rule"})

    evidence = await _evidence(store, facts, "team_person.delivered_issues", "p1@2026-08-20")

    assert evidence.kind == "work_issue"
    assert evidence.display == "2"
    assert [m.title for m in evidence.members] == ["Ship the drawer", "Name the rule"]
    assert [m.display for m in evidence.members] == [None, None]
    # A record with no url gets none -- not a link to nowhere.
    assert [m.url for m in evidence.members] == ["https://jira/1", None]


async def test_a_rollup_cites_its_parts_not_the_records_underneath_them() -> None:
    """A total's evidence is the cells it added, and re-listing the records
    would be re-deriving the number a second way -- the thing writing it as a
    sum was meant to delete. A part missing at the source's live version is a
    different claim from a part holding nought, so it is listed and marked."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.authored_in", "c1", ["p1@conn-a"])
    source_version = LIB.figure("team_person.open_mrs_by_source").version  # type: ignore[union-attr]
    await store.save(
        TENANT,
        "team_person.open_mrs_by_source",
        source_version,
        "p1@conn-a",
        2,
        ("c1", "c2"),
        "Aki in conn-a",
    )
    await _ready(store, "team_person.open_mrs")
    await store.save(
        TENANT,
        "team_person.open_mrs",
        LIB.figure("team_person.open_mrs").version,  # type: ignore[union-attr]
        "p1",
        3,
        ("p1@conn-a", "p1@conn-b"),
        "Aki",
    )

    evidence = await _evidence(store, facts, "team_person.open_mrs", "p1")

    assert evidence.parts is True
    assert evidence.source == "team_person.open_mrs_by_source"
    assert evidence.kind is None
    counted, gone = evidence.members
    assert counted.key == "p1@conn-a"
    assert counted.figure == "team_person.open_mrs_by_source"
    assert counted.title == "Aki in conn-a"
    assert counted.display == "2"
    assert counted.held is True
    assert gone.key == "p1@conn-b"
    assert gone.figure == "team_person.open_mrs_by_source"
    assert gone.held is False
    assert gone.display is None


async def test_a_calculation_over_several_figures_cites_every_operand() -> None:
    """`rework_share` is a quotient. Taking the first source holding a row and
    stopping would show the numerator as the sole citation with the
    denominator invisible, and for a `max` would print the very operand the
    calculation rejected. Every (member, source) pair is a row, each named for
    its figure and rendered in its own unit; the operand with no stored row is
    listed and marked, because "the cell has gone" is what explains a share
    that looks wrong."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "work_issue.assigned", "i1", ["p1"])
    rework_version = LIB.figure("team_person.rework_effort").version  # type: ignore[union-attr]
    await store.save(
        TENANT, "team_person.rework_effort", rework_version, "p1", 3600.0, ("i1",), "Aki's rework"
    )
    await _ready(store, "team_person.rework_share")
    await store.save(
        TENANT,
        "team_person.rework_share",
        LIB.figure("team_person.rework_share").version,  # type: ignore[union-attr]
        "p1",
        0.25,
        ("p1",),
        "Aki",
    )

    evidence = await _evidence(store, facts, "team_person.rework_share", "p1")

    assert evidence.parts is True
    # Several sources: no single figure to name up top, so none is -- each row
    # names its own instead.
    assert evidence.source is None
    assert evidence.display == "25.0%"
    numerator, denominator = evidence.members
    assert numerator.key == "p1"
    assert numerator.figure == "team_person.rework_effort"
    assert numerator.held is True
    # Rendered in the *source's* unit: an hour of rework, not "360000.0%".
    assert numerator.display == "1.0h"
    assert denominator.figure == "team_person.planned_effort"
    assert denominator.held is False
    assert denominator.display is None


async def test_an_unavailable_figure_says_why_instead_of_listing_nothing() -> None:
    """No pointer means never computed, and an empty members list under an Ok
    state would read as "this value cites nothing" -- a confident claim about a
    figure the tenant has never run."""
    store, facts = MemoryEngineStore(), MemoryFactStore()

    evidence = await _evidence(store, facts, "team_person.time_to_merge", "p1@2026-08-20")

    assert evidence is not None
    assert evidence.state.ok is False
    assert evidence.state.because == "never-computed"
    assert evidence.members == []
    assert evidence.display is None


async def test_a_subject_never_stored_is_an_absence_not_an_empty_list() -> None:
    """`None`, which a route turns into a 404 with the reason. An empty
    evidence payload would claim the value exists and cites nothing."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1@2026-08-20"])
    await _ready(store, "team_person.time_to_merge")

    evidence = await _evidence(store, facts, "team_person.time_to_merge", "p9@2026-08-20")

    assert evidence is None


async def test_a_row_whose_values_and_members_disagree_serves_no_pairings() -> None:
    """The positional contract is `values[i] measures members[i]`. A stored row
    that breaks it -- written by some earlier era of the engine -- must not be
    papered over by pairing what aligns: that prints the right numbers under
    the wrong records, which is worse than printing none. The records still
    list; the measurements are withheld."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1@2026-08-20"])
    await _ready(store, "team_person.time_to_merge")
    await store.save(
        TENANT,
        "team_person.time_to_merge",
        LIB.figure("team_person.time_to_merge").version,  # type: ignore[union-attr]
        "p1@2026-08-20",
        [3600.0],
        ("c1", "c2"),
        "Aki",
    )
    facts.put(TENANT, "code_change", "c1", {"title": "Fix the parser"})
    facts.put(TENANT, "code_change", "c2", {"title": "Widen the lexer"})

    evidence = await _evidence(store, facts, "team_person.time_to_merge", "p1@2026-08-20")

    assert [m.key for m in evidence.members] == ["c1", "c2"]
    assert [m.display for m in evidence.members] == [None, None]
    # And the withholding is said, not silently identical to a count's list:
    # the reason must reach the reader rather than stop in a server comment.
    assert evidence.note is not None
    assert "disagree" in evidence.note


async def test_the_titles_come_from_the_kind_the_calculation_counts() -> None:
    """`busy_context` is scoped by a code_change index and counts a
    tenant-wide work_issue set. Reading the kind off the scope index would
    look every member up in code_change: every row falsely marked missing,
    and the footer pointing at the wrong table."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1"])
    await _ready(store, "team_person.busy_context")
    await store.save(
        TENANT,
        "team_person.busy_context",
        LIB.figure("team_person.busy_context").version,  # type: ignore[union-attr]
        "p1",
        1,
        ("i1",),
        "Aki",
    )
    facts.put(TENANT, "work_issue", "i1", {"title": "Live incident"})

    evidence = await _evidence(store, facts, "team_person.busy_context", "p1")

    assert evidence.kind == "work_issue"
    (member,) = evidence.members
    assert member.title == "Live incident"
    assert member.held is True


async def test_a_keyed_as_index_looks_titles_up_in_the_id_space() -> None:
    """`code_review.approved` buckets review records under code_change ids.
    The member keys are code_change keys, so the id space -- not the index's
    own kind -- is where the titles live; the other choice lists every
    approved merge as missing."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1@2026-08-20"])
    await _ready(store, "team_person.approved_merges")
    await store.save(
        TENANT,
        "team_person.approved_merges",
        LIB.figure("team_person.approved_merges").version,  # type: ignore[union-attr]
        "p1@2026-08-20",
        1,
        ("c1",),
        "Aki",
    )
    facts.put(TENANT, "code_change", "c1", {"title": "Fix the parser", "url": "https://git/1"})

    evidence = await _evidence(store, facts, "team_person.approved_merges", "p1@2026-08-20")

    assert evidence.kind == "code_change"
    (member,) = evidence.members
    assert member.title == "Fix the parser"
    assert member.held is True


async def test_a_kind_with_no_declared_url_field_serves_no_links() -> None:
    """The url is schema-declared, like the name field: the engine knows
    nothing about any host's record shapes. A kind whose schema declares no
    url field serves bare titles, even when its records happen to carry a
    field called "url" -- reading it anyway would be the engine guessing at a
    host convention it was never taught."""
    from dataclasses import replace

    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "work_issue.delivered_by_day", "i1", ["p1@2026-08-20"])
    await _ready(store, "team_person.delivered_issues")
    plan = LIB.figure("team_person.delivered_issues")
    assert plan is not None
    await store.save(
        TENANT, plan.name, plan.version, "p1@2026-08-20", 1, ("i1",), "Aki"
    )
    facts.put(TENANT, "work_issue", "i1", {"title": "Live incident", "url": "https://jira/9"})

    unlinked = replace(WORLD, url_fields={})
    evidence = await serve_evidence(
        store, facts, LIB, unlinked, TENANT, plan, DEFAULTS, "p1@2026-08-20"
    )

    (member,) = evidence.members
    # The control: the name field still resolves, so only the link is absent.
    assert member.title == "Live incident"
    assert member.url is None


async def test_the_facades_refusals_each_say_where_the_evidence_lives() -> None:
    """Four refusal arms, four different forwarding addresses. Collapsed into
    one generic "no figure called X", the reader who arrived from a reading's
    window or a projection row would be told their number does not exist --
    when it exists and simply stores nothing."""
    import re

    import pytest

    from uratori.facade import Uratori

    lib = compile_source(
        """
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
group code_review_request.asked_of from reviewer_account_id through team_person.accounts.account_id
filter code_review_request.pending where pending == true

measure code_change.open_seconds = merged_at - created_at
measure code_review_request.waiting_seconds = now - requested_at

# Every merge's duration.
figure team_person.time_to_merge:
    display "{team_person} time to merge"
    depends:
        merged = code_change.merged_by_day:{team_person}
    calculate:
        list(code_change.open_seconds over merged)

# The typical merge, over a window.
reading team_person.lead_time(range):
    display "{value}"
    depends:
        m = team_person.time_to_merge in range
    calculate:
        mean(m)

# What is waiting right now.
reading team_person.queue():
    display "{value}"
    depends:
        w = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(w)

# One row per issue.
projection work_issue.card:
    field:
        key = title as text

# The backlog, in one row.
summarise work_issue.backlog over work_issue.card:
    count items
"""
    )
    facade = Uratori(
        schema=WORLD, library=lib, store=MemoryEngineStore(), facts=MemoryFactStore()
    )

    with pytest.raises(LookupError, match=re.escape("team_person.time_to_merge")):
        # The windowed reading forwards to the figure whose days it summarises.
        await facade.evidence(TENANT, "team_person.lead_time", "p1")

    with pytest.raises(LookupError, match="live reading"):
        await facade.evidence(TENANT, "team_person.queue", "p1")

    with pytest.raises(LookupError, match="rows are the evidence"):
        await facade.evidence(TENANT, "work_issue.card", "i1")

    with pytest.raises(LookupError, match="rows are the evidence"):
        await facade.evidence(TENANT, "work_issue.backlog", "i1")

    with pytest.raises(LookupError, match=re.escape("No figure called no.such")):
        await facade.evidence(TENANT, "no.such", "p1")


async def test_evidence_behind_a_deploy_says_so_rather_than_vanishing() -> None:
    """behind-deploy is the state a reader actually hits: they clicked a row
    rendered before a deploy landed. The answer must carry that state with no
    members -- falling through to "nothing stored" would read as the value
    never having existed, when it exists at a version this build no longer
    serves."""
    store, facts = MemoryEngineStore(), MemoryFactStore()
    plan = LIB.figure("team_person.time_to_merge")
    assert plan is not None
    await store.set_pointer(
        TENANT,
        plan.name,
        Pointer(version="an-older-build", settings_fingerprint=""),
    )
    await store.save(
        TENANT, plan.name, "an-older-build", "p1@2026-08-20", [3600.0], ("c1",), "Aki"
    )

    evidence = await _evidence(store, facts, plan.name, "p1@2026-08-20")

    assert evidence is not None
    assert evidence.state.ok is False
    assert evidence.state.because == "behind-deploy"
    assert evidence.members == []
    assert evidence.display is None


async def test_members_spanning_two_fact_kinds_are_served_bare_and_claim_nothing() -> None:
    """Arithmetic over sets of two kinds has no one table to look titles up
    in. The keys are served with nothing claimed about them -- and `held`
    stays True, because "not held" is a claim only a lookup can earn. The
    load-bearing half: a record that WOULD resolve in one of the kinds still
    gets no title, proving no lookup was attempted rather than one that
    happened to miss."""
    mixed = compile_source(
        """
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
filter work_issue.active where active == true

# How much busier the board is than this person.
figure team_person.context_gap:
    display "{team_person} gap"
    unit count

    depends:
        merged = code_change.merged_by_day:{team_person}
        busy = work_issue.active
    calculate:
        count(busy) - count(merged)
"""
    )
    plan = mixed.figure("team_person.context_gap")
    assert plan is not None
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "code_change.merged_by_day", "c1", ["p1"])
    await store.set_pointer(
        TENANT,
        plan.name,
        Pointer(
            version=plan.version,
            settings_fingerprint=settings_fingerprint(dict(DEFAULTS), list(plan.settings)),
        ),
    )
    await store.save(TENANT, plan.name, plan.version, "p1", 1, ("c1", "i1"), "Aki")
    facts.put(TENANT, "work_issue", "i1", {"title": "Live incident"})
    facts.put(TENANT, "code_change", "c1", {"title": "Fix the parser"})

    evidence = await serve_evidence(store, facts, mixed, WORLD, TENANT, plan, DEFAULTS, "p1")

    assert evidence is not None
    assert evidence.kind is None
    assert evidence.parts is False
    assert [m.key for m in evidence.members] == ["c1", "i1"]
    assert all(m.title is None and m.url is None for m in evidence.members), (
        "a title looked up despite the mixed kinds would be a guess about "
        "which table the key belongs to"
    )
    assert all(m.held for m in evidence.members)


async def test_a_kind_with_no_name_field_serves_bare_keys_still_held() -> None:
    """`held` and `title` are separate claims: the record IS here, the schema
    just declares no field to name it by. Conflating them would mark every
    record of an unnamed kind as missing."""
    from dataclasses import replace

    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "work_issue.delivered_by_day", "i1", ["p1@2026-08-20"])
    await _ready(store, "team_person.delivered_issues")
    plan = LIB.figure("team_person.delivered_issues")
    assert plan is not None
    await store.save(TENANT, plan.name, plan.version, "p1@2026-08-20", 1, ("i1",), "Aki")
    facts.put(TENANT, "work_issue", "i1", {"title": "Live incident"})

    nameless = replace(WORLD, name_fields={}, url_fields={})
    evidence = await serve_evidence(
        store, facts, LIB, nameless, TENANT, plan, DEFAULTS, "p1@2026-08-20"
    )

    (member,) = evidence.members
    assert member.title is None
    assert member.held is True


async def test_a_part_whose_source_left_the_library_is_listed_and_marked() -> None:
    """A build can serve a rollup whose plan names a source the loaded library
    no longer compiles. The honest degradation is the one a deleted part
    gets: listed, held False, no measurement -- never dropped, and never a
    crash that takes the whole citation down."""
    smaller = compile_source(
        """
group work_issue.assigned from assignee_account_id through team_person.accounts.account_id

measure work_issue.rework = rework_seconds in effort

# Rework on what this person carries, added up.
figure team_person.rework_effort:
    display "{team_person} rework"
    depends:
        mine = work_issue.assigned:{team_person}
    calculate:
        sum(work_issue.rework over mine)
"""
    )
    plan = LIB.figure("team_person.rework_share")
    assert plan is not None
    store, facts = MemoryEngineStore(), MemoryFactStore()
    await store.set_buckets(TENANT, "work_issue.assigned", "i1", ["p1"])
    rework = smaller.figure("team_person.rework_effort")
    assert rework is not None
    await store.save(TENANT, rework.name, rework.version, "p1", 3600.0, ("i1",), "Aki")
    await store.set_pointer(
        TENANT,
        plan.name,
        Pointer(
            version=plan.version,
            settings_fingerprint=settings_fingerprint(dict(DEFAULTS), list(plan.settings)),
        ),
    )
    await store.save(TENANT, plan.name, plan.version, "p1", 0.25, ("p1",), "Aki")

    evidence = await serve_evidence(store, facts, smaller, WORLD, TENANT, plan, DEFAULTS, "p1")

    assert evidence is not None
    assert evidence.parts is True
    by_figure = {m.figure: m for m in evidence.members}
    # The source still in the library serves normally -- the control.
    survivor = by_figure["team_person.rework_effort"]
    assert survivor.held is True
    assert survivor.display == "1.0h"
    gone = by_figure["team_person.planned_effort"]
    assert gone.held is False
    assert gone.display is None
    assert gone.key == "p1"
