"""On-change data: sparse facts, dense buckets.

Some of the world is recorded as *changes* rather than as states. Somebody
sets a target in February and nobody touches it until June; a goal, a
staffing level, a facility setting, a price -- one record each time the value
moves, and nothing at all in between. Every screen wants the opposite shape:
a value per month, including the months nobody changed anything.

Nothing in this file is about goals or facilities in particular. The pattern
is the point, and the fixture below is deliberately a nameless `setting_change`
so that the rules read as rules rather than as one domain's arrangements.

The stack is four declarations:

- a **fact** that is one change (`set_at` says when, `value` says to what),
- a **filter** that picks which setting we mean -- the fact stream carries
  several, and narrowing is a filter's job,
- a **group** that is metric-agnostic and buckets by subject and month, so
  one group serves every setting the stream carries,
- a **figure** whose name is the only place "this setting, monthly" is
  claimed, backed by the parts it visibly intersects.

That factoring matters. Putting the setting's name in the group would mean a
group per setting; putting the month in the filter would mean a filter that
knows about calendars. Each declaration answers exactly one question, and the
figure's name is the claim the other three back up.
"""

from __future__ import annotations

import pytest

from uratori import CheckError, Schema, SyntaxError_, compile_source

ONCHANGE = Schema(
    kinds=frozenset(),
)

WORLD = '''
# A place where work happens.
fact site:
    name name
    name as text
    timezone as text

# One change to one setting, at one site, by one person. Sparse by nature:
# a record exists only when somebody changed something, so a month with no
# record is a month nobody touched it -- never a month the setting lapsed.
fact setting_change:
    site_id as text
    setting as text
    value as number
    set_at as moment
    set_by as text

# Work that happened, so there is something to compare a target against.
fact job:
    site_id as text
    cost as number
    started_at as moment
    finished_at as moment

filter setting_change.target where setting == "target_minutes"
group setting_change.at_site from site_id
group setting_change.by_month from (site_id, set_at by month in site.timezone)
group job.by_month from (site_id, finished_at by month in site.timezone)
group job.at_site from site_id
measure job.length = finished_at - started_at
'''


def compile_world(extra: str = ""):
    return compile_source(WORLD + extra, ONCHANGE)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_world(extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"{fragment!r} not in {message!r}"
    return message


# A figure over the metric-agnostic group and the metric filter. Written out
# once here because most tests below are a variation on it.
GOAL = '''
# The target in force each month.
figure site.target_month bucketed:
    display "{site} target"

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        count(sets)
'''


# --------------------------------------------------- the bucketed header --


def test_a_bucketed_figure_over_a_sequenced_group_compiles() -> None:
    lib = compile_world(GOAL)
    plan = lib.figure("site.target_month")
    assert plan is not None
    assert plan.grain == "month"


def test_a_sequenced_figure_without_bucketed_is_refused_and_names_the_group() -> None:
    """The declaration is the whole point of the keyword.

    Without it a reader of the figure cannot tell that it holds one value per
    month rather than one per site -- and every reader downstream is then
    silently wrong in a different way: a projection binds a column that will
    never resolve, a bundle subscribes to every stored bucket of every
    subject, a rollup totals a sequence as though it were a scalar. It is the
    same argument `across` makes, which is why the two are refused in the
    same place.

    The message names the *group*, because that is where the disagreement
    lives: the figure says one value per subject and the group says one per
    subject per month, and only one of them can be right.
    """
    message = refuses(
        '''
# The target in force each month.
figure site.target_month:
    display "{site} target"

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        count(sets)
''',
        "setting_change.by_month",
        "bucketed",
        "month",
    )
    assert "site.target_month" in message


def test_bucketed_on_a_figure_with_no_sequence_is_refused() -> None:
    """The mirror refusal, and it is not symmetry for its own sake: a figure
    declaring a sequence it does not have would have readings written over it
    that resolve to no buckets at all, and answer an absence for ever."""
    refuses(
        '''
# How many changes have ever been made here.
figure site.changes bucketed:
    display "{site} changes"

    depends:
        sets = setting_change.at_site:{site} & setting_change.target

    calculate:
        count(sets)
''',
        "bucketed",
    )


def test_bucketed_and_across_cannot_both_be_declared() -> None:
    """A bucket of time is not a dimension -- it has no roster and no name --
    so a figure claiming both is claiming two different things about one
    second key.

    The message is asserted, not just the refusal: unasserted, this stayed
    green for a missing `#` comment, a renamed group, or any future rule
    that happened to fire first.
    """
    refuses(
        '''
# Both at once.
figure site.confused bucketed across job:
    display "{site} confused"

    depends:
        sets = setting_change.by_month:{site}

    calculate:
        count(sets)
''',
        "A bucket of time is not a dimension",
        "site.confused",
    )

    # The control: each alone compiles, so the refusal is about the pair.
    assert compile_world(GOAL).figure("site.target_month") is not None


def test_bucketed_is_not_in_the_version_hash() -> None:
    """The keyword is a checker-verified *mirror* of the group's spec, and
    the group's spec is already hashed everywhere it is read.

    So it earns its place the way `keyed as` does: it decides what the
    checker permits, never what the arithmetic produces. Hashing it would
    have made a required keyword a history-rebuilding change -- every
    sequenced figure in every deployment recomputed to store byte-identical
    values under a new version.

    Pinned against the versions these two figures had *before* the keyword
    existed, because that is the actual claim: not that the flag hashes
    consistently, but that adding it moved nothing that was already stored.

    The pin moved once, deliberately: `team_person.time_to_merge` buckets by
    day, and the calendar that decides which day is a field on the subject's
    record rather than a tenant dial. That changes the group's spec, so it
    changes what the figure hashes -- a real rebuild, for a real change in
    what a bucket means. The claim the pin makes is unchanged, and it is
    still the claim that matters: nothing *incidental* moves a version.
    """
    from .test_lang import BASE
    from .world import compile_source as compile_suite

    lib = compile_suite(BASE)
    versions = {f.name: f.version for f in lib.figures}
    assert versions["team_person.time_to_merge"] == "5b655b06ef70", (
        "a sequenced figure's version moved when `bucketed` became required; "
        "every tenant would rebuild its whole history to store the same numbers"
    )
    assert versions["team_person.wip"] == "d7fe57cb385c", (
        "an unsequenced figure's version moved, which the keyword cannot even "
        "be written on"
    )


def test_a_bucketed_figures_formula_is_still_served() -> None:
    """The header pattern that finds a declaration's own text has to admit
    the new keyword.

    It does not fail loudly when it does not: the block is simply not found
    and the definition serves a **blank** formula to the very page that
    exists to show it. A figure whose formula is only readable by checking
    the repository out is the thing this project is arranged against, so the
    regression is worth a test of its own rather than leaving it to the
    server suite to notice sideways.
    """
    from uratori.lang.source import declaration_source

    lib = compile_world(GOAL)
    text = declaration_source(lib, "site.target_month")
    assert text, "a bucketed figure served no formula at all"
    assert "bucketed" in text
    assert "count(sets)" in text, "the block was found but truncated"


# ------------------------------------------- reading a declared field --
#
# The canonical shape: `latest(setting_change.value over sets)` -- the value
# the most recent record in the bucket set it to. A measure alias for it
# would be a second name for one field, written only to satisfy the grammar,
# and the language already refuses a second place to write one thing.


CARRIED = '''
# The target in force each month: the latest one set within the month,
# carried across the months where nobody changed it.
figure site.target_month bucketed:
    display "{site} target"
    unit duration

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward
'''


def test_a_declared_field_is_readable_by_name() -> None:
    lib = compile_world(CARRIED)
    plan = lib.figure("site.target_month")
    assert plan is not None
    assert plan.grain == "month"
    assert plan.carried is True


def test_the_latest_of_a_field_is_ordered_by_the_groups_own_time_part() -> None:
    """Which record is "latest" is not a second thing to declare.

    The group already said `set_at by month` -- that field is when the change
    happened, and it is the only ordering in sight. Asking the definition to
    name it again would be a second place for the two to disagree, and the
    disagreement would be silent: order by the wrong field and the bucket
    reports a superseded value with no error anywhere.
    """
    lib = compile_world(CARRIED)
    plan = lib.figure("site.target_month")
    assert plan is not None
    assert plan.ordered_by == "set_at"


def test_a_field_read_over_a_kind_the_set_does_not_hold_is_refused() -> None:
    """The same silence a measure over the wrong kind produces: every lookup
    misses and the figure answers nothing, for everybody, for ever."""
    refuses(
        '''
# Wrong kind.
figure site.confused bucketed:
    display "{site} confused"
    unit count

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(job.cost over sets) carried forward
''',
        "holds setting_change ids",
    )


def test_a_field_that_does_not_exist_is_refused_with_what_the_record_carries() -> None:
    refuses(
        '''
# Typo.
figure site.typo bucketed:
    display "{site} typo"
    unit count

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.valeu over sets) carried forward
''',
        "valeu",
    )


def test_reading_a_text_field_is_refused() -> None:
    """A figure's value is a number, a word from a ladder, or a list. A
    word straight off a record would be arbitrary text with a version hash --
    the thing the ladder's closed vocabulary exists to prevent."""
    refuses(
        '''
# Who set it.
figure site.who bucketed:
    display "{site} who"
    unit count

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.set_by over sets) carried forward
''',
        "set_by",
    )


def test_a_field_read_must_declare_its_unit() -> None:
    """Declare only what cannot be derived -- and nothing can derive this
    one.

    A count is a count and a sum of an effort measure is an effort, because
    the construct says what the number is. A field read says only that a
    record carries a number: the fact layer is structural on purpose, so
    there is no unit riding on `value as number` to inherit. Left to a
    default, the same integer prints as `144000` or as `5d` and neither
    throws.
    """
    refuses(
        '''
# No unit.
figure site.unitless bucketed:
    display "{site} unitless"

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward
''',
        "unit",
    )


def test_a_measure_extreme_still_returns_an_instant() -> None:
    """The two readings of `latest` sit side by side and must not blur: over
    a *moment measure* it answers when, over a *field* it answers what.

    The control for the whole section -- if the field form had quietly taken
    over the word, this would now return a number.
    """
    lib = compile_world(
        '''
measure setting_change.when = moment set_at

# When this site was last reconfigured.
figure site.last_change:
    display "{site} last change"

    depends:
        sets = setting_change.at_site:{site} & setting_change.target

    calculate:
        latest(setting_change.when over sets)
'''
    )
    plan = lib.figure("site.last_change")
    assert plan is not None
    assert plan.unit == "moment"


# --------------------------------------------- the field read, evaluated --


async def _run(source: str, changes: list[tuple[str, str, float, str, str]]):
    """A tenant with one site and a list of changes, passed through a full run."""
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    engine = Uratori(
        schema=ONCHANGE,
        library=compile_world(source),
        store=MemoryEngineStore(),
        facts=facts,
    )
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, at, value, who, _ in changes:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": at,
                "set_by": who,
            },
        )
    await engine.run("t1", full=True)
    return engine


async def test_the_latest_record_is_chosen_by_set_at_and_not_by_its_key() -> None:
    """The ordering field is the group's, and nothing else.

    Every other fixture here numbers its records in the order they happened,
    so ignoring `set_at` entirely and sorting by key gives the same answer --
    and a mutation that did exactly that passed the whole suite. Here the
    keys run backwards against the clock, so the two disagree and only the
    right one produces 25.
    """
    engine = await _run(
        CARRIED,
        [
            ("c9", "2026-02-03T09:00:00Z", 30.0, "Aki", ""),
            ("c1", "2026-02-20T09:00:00Z", 25.0, "Bo", ""),
        ],
    )
    answer = await engine.answer("t1", "site.target_month")
    values = {s.id: s.value for s in answer.subjects}
    assert values.get("s1@2026-02") == 25.0, (
        "the bucket reported the record with the larger key rather than the "
        "later set_at"
    )


async def test_the_bucket_reports_the_last_change_made_within_it() -> None:
    """Two changes in one month: the later one is what the month says.

    The earlier is not averaged in and not preferred -- it was superseded
    inside the month, and the month's answer is the value in force at its
    end.
    """
    engine = await _run(
        CARRIED,
        [
            ("c1", "2026-02-03T09:00:00Z", 30.0, "Aki", ""),
            ("c2", "2026-02-20T09:00:00Z", 25.0, "Bo", ""),
        ],
    )
    answer = await engine.answer("t1", "site.target_month")
    values = {s.id: s.value for s in answer.subjects}
    assert values.get("s1@2026-02") == 25.0, (
        "the month reported a change that had already been replaced within it"
    )


async def test_a_tie_inside_a_bucket_resolves_the_same_way_every_time() -> None:
    """Two changes stamped at the same instant are a data problem, and the
    engine's job is to be *stable* about it rather than right.

    Answering differently on each pass would be a figure that moves with
    nothing behind it -- a change report, a push to every subscribed screen,
    and no cause anybody can point at.
    """
    stamped = [
        ("c1", "2026-02-03T09:00:00Z", 30.0, "Aki", ""),
        ("c2", "2026-02-03T09:00:00Z", 25.0, "Bo", ""),
    ]
    # Pinned, not merely stable: the later key wins a tie. Left unpinned,
    # flipping the tie-break direction changes every tenant's stored value
    # and no test notices.
    first = await _run(CARRIED, stamped)
    second = await _run(CARRIED, list(reversed(stamped)))
    a = {s.id: s.value for s in (await first.answer("t1", "site.target_month")).subjects}
    b = {s.id: s.value for s in (await second.answer("t1", "site.target_month")).subjects}
    assert a["s1@2026-02"] == b["s1@2026-02"], (
        "the same two records in a different write order gave two answers"
    )
    assert a["s1@2026-02"] == 25.0, "the tie broke towards the earlier key"


async def test_the_evidence_is_the_one_change_the_value_came_from() -> None:
    """"Why 25 in February?" has to be answerable, and the answer is one
    record -- not the bucket it was in."""
    engine = await _run(
        CARRIED,
        [
            ("c1", "2026-02-03T09:00:00Z", 30.0, "Aki", ""),
            ("c2", "2026-02-20T09:00:00Z", 25.0, "Bo", ""),
        ],
    )
    evidence = await engine.evidence("t1", "site.target_month", "s1@2026-02")
    assert [m.key for m in evidence.members] == ["c2"], (
        "the citation named the whole bucket, burying the change that answered"
    )


# ------------------------------------------------------ carried forward --


CHANGES = [
    ("c1", "2026-02-10T09:00:00Z", 1800.0, "Aki", ""),
    ("c2", "2026-06-03T09:00:00Z", 1500.0, "Bo", ""),
]

# Sean's pinned scenario: 30m set in February, 25m set in June. January is
# absent because nothing had ever been set; February and June are anchors;
# March to May and July onward are carried.
EXPECTED = {
    "s1@2026-01": None,
    "s1@2026-02": 1800.0,
    "s1@2026-03": 1800.0,
    "s1@2026-04": 1800.0,
    "s1@2026-05": 1800.0,
    "s1@2026-06": 1500.0,
    "s1@2026-07": 1500.0,
    "s1@2026-08": 1500.0,
}

AT = 1_787_572_800_000.0  # 2026-08-24T12:00Z -- "now" for these tests


async def _carried(changes=CHANGES, at: float = AT):
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in changes:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    await engine.run("t1", full=True, at_ms=at)
    return engine, store, library, facts


async def _stored(engine, store, library) -> dict[str, float | None]:
    plan = library.figure("site.target_month")
    rows = await store.values("t1", plan.name, plan.version)
    return {r.subject: r.value for r in rows}


async def test_a_pass_fills_every_month_from_the_first_change_to_the_present() -> None:
    """The scenario, whole. Nothing before February, the two anchors, and a
    value in every month between and after -- to *this* month and no
    further."""
    engine, store, library, _ = await _carried()
    stored = await _stored(engine, store, library)
    assert stored.get("s1@2026-01") is None, (
        "January reported a target that had not been set yet"
    )
    for label, value in EXPECTED.items():
        if value is not None:
            assert stored.get(label) == value, f"{label} reads {stored.get(label)}"


async def test_a_bucket_before_the_first_change_is_absent_not_a_nought() -> None:
    """The one a band would get wrong: a nought here sits comfortably under
    every threshold and would colour January green for a target nobody had
    set.

    (An earlier version of this asserted `EXPECTED["s1@2026-01"] is None` --
    a dict literal in this file, with no engine involved. It was a comment
    with an `assert` in front of it.)
    """
    engine, store, library, _ = await _carried()
    stored = await _stored(engine, store, library)
    assert stored, "nothing was stored at all, so absence proves nothing"
    assert "s1@2026-01" not in stored, (
        "January holds a value for a target that had not been set yet"
    )
    plan = library.figure("site.target_month")
    assert await store.value("t1", plan.name, plan.version, "s1@2026-01") is None


async def test_materialisation_never_runs_past_the_present_bucket() -> None:
    engine, store, library, _ = await _carried()
    stored = await _stored(engine, store, library)
    assert all(label.split("@")[1] <= "2026-08" for label in stored), (
        f"a bucket past the present month was written: {sorted(stored)}"
    )


async def test_a_later_change_rewrites_forward_and_leaves_history_alone() -> None:
    """A change dated in April, entered after the June one already existed.

    April and May must move to the new value; February and March must be
    **byte-identical** to what they were. History is never rewritten by a
    later arrival -- which is the property that makes these rows safe to
    store at all.
    """
    engine, store, library, facts = await _carried()
    before = await _stored(engine, store, library)

    facts.put(
        "t1",
        "setting_change",
        "c3",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1200.0,
            "set_at": "2026-04-05T09:00:00Z",
            "set_by": "Cyd",
        },
    )
    await engine.run("t1", written={"setting_change": ["c3"]}, at_ms=AT)
    after = await _stored(engine, store, library)

    assert after["s1@2026-02"] == before["s1@2026-02"] == 1800.0
    assert after["s1@2026-03"] == before["s1@2026-03"] == 1800.0
    assert after["s1@2026-04"] == 1200.0, "the anchor month did not take the new value"
    assert after["s1@2026-05"] == 1200.0, "the month after the change still carries the old one"
    assert after["s1@2026-06"] == 1500.0, "June has its own change and must keep it"
    assert after["s1@2026-07"] == 1500.0


async def test_every_trigger_writes_byte_identical_rows() -> None:
    """The rule that makes three triggers legal at all.

    A pass, a fact landing and a read all reach the same materialiser, so
    the rows cannot differ -- and this asserts it literally rather than
    trusting the structure, because "they share a function" is a claim about
    today's code and this is a claim about the answer.
    """
    whole, whole_store, whole_lib, _ = await _carried()
    by_pass = await _stored(whole, whole_store, whole_lib)

    # Trigger two: the same world assembled one change at a time, each
    # arriving through the facts door.
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    incremental = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    await incremental.run("t1", full=True, at_ms=AT)
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
        await incremental.run("t1", written={"setting_change": [key]}, at_ms=AT)
    by_fact = await _stored(incremental, store, library)

    assert by_fact == by_pass, (
        "a world built change-by-change disagrees with the same world built in "
        "one pass -- the two triggers are not the one materialiser they claim"
    )


async def test_a_carried_grain_finer_than_a_pass_can_honour_is_refused() -> None:
    """Extension is the pass noticing time, and the pass is the only event
    there is -- the clock itself never is.

    A carried figure at minute grain would owe a new bucket every minute and
    get one per sync, so its most recent bucket would read as an absence for
    as long as the gap. Fenced the way `older than` is fenced: that clause
    admits only dials in whole days precisely so the unenforceable version
    cannot be written, and this admits only grains a pass can plausibly keep
    up with.
    """
    refuses(
        '''
group setting_change.by_minute from (site_id, set_at by minute)

# Far too fine.
figure site.target_minute bucketed:
    display "{site} target"
    unit count

    depends:
        sets = setting_change.by_minute:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward
''',
        "minute",
        "carried",
    )


PACE = CARRIED + '''
# How the target moved across the months in view.
reading site.target_pace(range):
    display "{site} target pace"

    depends:
        t = site.target_month in range

    calculate:
        median(t)
        delta(t)
'''


async def _paced(at: float = AT):
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(PACE)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    await engine.run("t1", full=True, at_ms=at)
    return engine, store, library, facts


async def test_a_pass_serves_against_its_own_clock_not_the_wall_clock() -> None:
    """`at_ms` is the embedding host's clock and it reached the bucketing and
    not the serving, so a pass filed its buckets against the clock it was
    handed and then materialised a carried figure forward to *today*.

    Two costs. A host replaying history writes rows for months its own clock
    says have not happened, and they persist -- no later pass removes a bucket
    the anchors still justify. And the three tests below could only be read as
    passing on the days the wall clock happened to agree with the fixture,
    which is how this went unnoticed: it broke overnight, at a UTC month
    boundary, with nothing in the diff.
    """
    _, store, library, _facts = await _paced(at=AT)
    figure = library.figure("site.target_month")
    filed = {r.subject for r in await store.values("t1", figure.name, figure.version)}
    assert max(filed) == "s1@2026-08", (
        f"the pass ran at 2026-08-24 and filled past its own clock: {sorted(filed)}"
    )


async def test_a_read_after_the_pass_fills_the_buckets_time_has_added() -> None:
    """A pass extends to the bucket it ran in, so between passes the newest
    bucket has no row.

    Told "never computed" there, a screen would report an absence for a value
    that has demonstrably been in force for months -- which is exactly the
    wrong answer for a figure whose whole point is that it persists. The read
    materialises what time has added and then serves it.
    """
    from uratori.engine.serve import serve_reading

    _, store, library, facts = await _paced(at=AT)
    plan = library.reading("site.target_pace")
    figure = library.figure("site.target_month")

    before = {r.subject for r in await store.values("t1", figure.name, figure.version)}
    assert "s1@2026-09" not in before, "August was the present month at the pass"

    # Two months pass with no sync at all, and then somebody looks.
    later = 1_792_843_200_000.0  # 2026-10-24T12:00Z
    result = await serve_reading(
        store, library, "t1", plan, [3], at_ms=later, facts=facts
    )
    after = {r.subject for r in await store.values("t1", figure.name, figure.version)}
    assert {"s1@2026-09", "s1@2026-10"} <= after, (
        "a read found unmaterialised buckets and served an absence instead of "
        f"filling them: {sorted(after)}"
    )
    window = result.subjects[0].windows[0]
    assert window.median == 1500.0, "the carried value did not reach the reader"


async def test_the_read_writes_the_same_rows_a_pass_would_have() -> None:
    """The third trigger, held to the same claim as the other two.

    One world is filled by a read at October; another by a pass at October.
    Every row must match -- value and evidence -- or the lazy path is a
    second implementation wearing the first one's name.
    """
    from uratori.engine.serve import serve_reading

    later = 1_792_843_200_000.0  # 2026-10-24T12:00Z

    _, read_store, read_lib, read_facts = await _paced(at=AT)
    await serve_reading(
        read_store,
        read_lib,
        "t1",
        read_lib.reading("site.target_pace"),
        [3],
        at_ms=later,
        facts=read_facts,
    )
    figure = read_lib.figure("site.target_month")
    read_rows = {
        r.subject: (r.value, r.members)
        for r in await read_store.values("t1", figure.name, figure.version)
    }

    _, pass_store, pass_lib, _ = await _paced(at=later)
    pass_figure = pass_lib.figure("site.target_month")
    pass_rows = {
        r.subject: (r.value, r.members)
        for r in await pass_store.values("t1", pass_figure.name, pass_figure.version)
    }

    assert read_rows == pass_rows, (
        "the lazy fill and the pass disagree about what a carried bucket says"
    )


async def test_two_first_readers_race_benignly_and_only_one_creates_each_row() -> None:
    """Both readers compute the same rows, because there is one materialiser
    -- so the race cannot corrupt anything.

    What it must not do is let both *report* having created a bucket: a
    movement is pushed to every subscribed screen, and the same bucket
    announced twice is a board that flickers for no reason anybody can point
    at. The insert-or-nothing write is what settles it.
    """
    import asyncio

    from uratori.engine.carry import materialise

    _, store, library, _facts = await _paced(at=AT)
    figure = library.figure("site.target_month")
    later = 1_792_843_200_000.0

    first, second = await asyncio.gather(
        materialise(
            store, figure, "t1", ["s1"], at_ms=later, zones={}, trigger="read"
        ),
        materialise(
            store, figure, "t1", ["s1"], at_ms=later, zones={}, trigger="read"
        ),
    )
    created = [s for s, _, _ in first] + [s for s, _, _ in second]
    assert len(created) == len(set(created)), (
        f"a bucket was reported created twice: {sorted(created)}"
    )
    assert {"s1@2026-09", "s1@2026-10"} <= set(created)


async def test_the_run_log_names_the_figures_a_pass_carried() -> None:
    """Ran-versus-never-ran must not be something a reader infers from
    whether a bucket happens to hold a value."""
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    outcome = await engine.execute("t1", full=True, at_ms=AT)
    assert outcome.carried == ("site.target_month",)

    # A second pass at the same instant has nothing left to extend, and says
    # so by naming nothing -- not by naming the figure with no rows behind it.
    again = await engine.execute("t1", full=True, at_ms=AT)
    assert again.carried == ()


# ------------------------------------------------ the {bucket} selector --
#
# A sequenced figure is a point-in-time value, and its bare name in an
# expression reads like a static declaration. So it may only be reached
# through `:{bucket}` -- the same coordinate the reading is already at.

MEDIAN = '''
# The median job length each month.
figure site.actual_month bucketed:
    display "{site} actual"

    depends:
        done = job.by_month:{site}

    calculate:
        median(job.length over done)
'''

DELTA_FIGURE = MEDIAN + CARRIED + '''
# How far each month ran over or under the target in force that month.
figure site.gap_month bucketed:
    display "{site} gap"
    unit duration

    calculate:
        site.actual_month:{bucket} - site.target_month:{bucket}

    band:
        when value > 0 then "over"
        otherwise "ok"
'''


def test_two_sequenced_figures_join_per_coordinate() -> None:
    lib = compile_world(DELTA_FIGURE)
    plan = lib.figure("site.gap_month")
    assert plan is not None
    assert plan.grain == "month"


def test_a_bare_sequenced_name_in_calculate_is_refused() -> None:
    """`goal` on its own reads like a static declaration, and it is a
    point-in-time value.

    Worse than unclear: with two sequences in one expression there is nothing
    saying the arithmetic is per coordinate, so the obvious implementation is
    a positional zip -- which is right until one source starts a month later
    than the other, and then every number is silently paired with the wrong
    month.
    """
    refuses(
        MEDIAN + CARRIED + '''
# Bare.
figure site.bare bucketed:
    display "{site} bare"
    unit duration

    calculate:
        site.actual_month - site.target_month
''',
        "bucket",
    )


def test_a_band_rung_may_compare_against_the_same_coordinate() -> None:
    lib = compile_world(
        MEDIAN
        + CARRIED
        + '''
# The month's actual, banded against the target in force that month.
figure site.actual_banded bucketed:
    display "{site} actual"

    depends:
        done = job.by_month:{site}

    calculate:
        median(job.length over done)

    band:
        when value > site.target_month:{bucket} then "over"
        otherwise "ok"
'''
    )
    plan = lib.figure("site.actual_banded")
    assert plan is not None
    assert plan.band is not None


def test_a_band_rung_naming_a_sequenced_figure_bare_is_refused() -> None:
    refuses(
        MEDIAN
        + CARRIED
        + '''
# Bare in a rung.
figure site.rung_bare bucketed:
    display "{site} actual"

    depends:
        done = job.by_month:{site}

    calculate:
        median(job.length over done)

    band:
        when value > site.target_month then "over"
        otherwise "ok"
''',
        "bucket",
    )


def test_a_threshold_dial_keeps_its_bare_spelling() -> None:
    """Bare means scalar and a selector means sequenced, so the two are
    visually distinct by construction -- a reader never has to look up which
    kind of thing a name is."""
    lib = compile_world(
        MEDIAN
        + '''
# Actual, against a fixed dial.
figure site.actual_dialled bucketed:
    display "{site} actual"

    depends:
        done = job.by_month:{site}

    calculate:
        median(job.length over done)

    band:
        when value > 0 then "over"
        otherwise "ok"
'''
    )
    assert lib.figure("site.actual_dialled") is not None


def test_an_offset_selector_does_not_exist() -> None:
    """`:{bucket - 1}` is refused rather than supported.

    A stored figure whose answer needs a bucket outside the population in
    view cannot be audited from the response that carries it: the reader is
    shown a number and, one coordinate back, nothing to check it against.
    The same refusal `delta`'s oldest cell gets, one layer down.
    """
    with pytest.raises(SyntaxError_) as caught:
        compile_world(
            MEDIAN
            + CARRIED
            + '''
# Offset.
figure site.offset bucketed:
    display "{site} offset"
    unit duration

    calculate:
        site.actual_month:{bucket} - site.target_month:{bucket - 1}
'''
        )
    assert "bucket - 1" in caught.value.message, (
        "the refusal must name the thing that does not exist, so an author "
        "reaching for an offset is told it is absent rather than mistyped"
    )


def test_a_coordinate_read_of_an_unsequenced_figure_is_refused() -> None:
    """The mirror: a selector on something with no sequence names a
    coordinate that does not exist."""
    refuses(
        MEDIAN
        + '''
# A plain per-site count.
figure site.changes:
    display "{site} changes"

    depends:
        sets = setting_change.at_site:{site}

    calculate:
        count(sets)

# Selector over a scalar, beside one that really is sequenced -- so the
# figure has a sequence and the refusal is about the scalar, not about a
# missing `bucketed`.
figure site.nonsense bucketed:
    display "{site} nonsense"
    unit count

    calculate:
        site.actual_month:{bucket} - site.changes:{bucket}
''',
        "site.changes",
        "one value per subject",
    )


# ------------------------------- distribution statistics over a bucket --


def test_a_bucketed_figure_may_take_a_median_of_its_own_records() -> None:
    """A declared bucket boundary is what makes the statistic a claim.

    The standing refusal is against a mean of *aggregates* -- a mean of daily
    counts is a mean per day wearing a per-record label. Here the population
    is the bucket's own records, and the bucket is declared in the group, so
    "the median job length in August" is a sentence with a checkable
    population behind it.
    """
    lib = compile_world(MEDIAN)
    plan = lib.figure("site.actual_month")
    assert plan is not None
    assert plan.unit == "duration"


def test_a_distribution_statistic_outside_a_bucketed_figure_is_refused() -> None:
    """Without a declared boundary the population is "everything ever", and
    a median over that is a number whose meaning drifts with the data's age
    -- it moves when nothing happened, and no reader can say what it is a
    median *of*."""
    refuses(
        '''
# Everything, ever.
figure site.all_time:
    display "{site} all time"

    depends:
        done = job.at_site:{site}

    calculate:
        median(job.length over done)
''',
        "bucketed",
    )


def test_a_bucket_statistic_may_not_run_over_a_combined_figure() -> None:
    """A statistic over stored values is a statistic over aggregates again,
    one construct along -- the mean-of-means this language refuses a reading
    for the same reason.

    Written `median(m)` this test was merely ungrammatical, and the parser's
    "expected `over`" said nothing about the rule. `over` makes it a real
    attempt at the construct.
    """
    refuses(
        MEDIAN
        + '''
# Median of medians.
figure site.worse bucketed:
    display "{site} worse"

    calculate:
        median(site.actual_month over site.actual_month)
''',
        "m",
    )


async def test_a_carried_bucket_cites_the_change_it_carried_from() -> None:
    """"Why 25 in July?" is answerable, and the answer is a June record.

    A carried bucket holds no records of its own -- nobody changed anything
    that month -- so a naive evidence chain would cite the empty bucket and
    dead-end exactly where the reader started asking. The row carries its
    anchor's evidence instead, which is what makes the number walkable back
    to the change, its instant and its author.
    """
    engine, _, _, _ = await _carried()

    july = await engine.evidence("t1", "site.target_month", "s1@2026-07")
    assert [m.key for m in july.members] == ["c2"], (
        "a carried bucket cited something other than the change in force"
    )

    # And the anchor month cites the same record, because it is the same
    # change -- the two buckets differ in label, never in provenance.
    june = await engine.evidence("t1", "site.target_month", "s1@2026-06")
    assert [m.key for m in june.members] == ["c2"]

    # March carries February's, not June's: the chain follows the value in
    # force at that coordinate rather than the latest change overall.
    march = await engine.evidence("t1", "site.target_month", "s1@2026-03")
    assert [m.key for m in march.members] == ["c1"]


async def test_the_carried_row_is_headed_by_the_subjects_name() -> None:
    """A carried bucket is a row on a board like any other, and a row headed
    by a raw id reads as broken data rather than as a carried value."""
    _, store, library, _ = await _carried()
    plan = library.figure("site.target_month")
    rows = {r.subject: r.label for r in await store.values("t1", plan.name, plan.version)}
    assert rows["s1@2026-07"] == "Northgate"


# --------------------------------------- what the first review round found --


async def test_editing_a_changes_value_recomputes_without_moving_a_bucket() -> None:
    """Correcting a typo in a change is the most ordinary edit an on-change
    stream gets, and it moves no bucket at all: same subject, same month.

    The warm path notices a record whose *fields* moved only for figures that
    name a measure, and a declared-field read names none. Left out, a
    corrected target is ignored by every warm pass -- and the carry then
    spreads the stale number over every later bucket, which is the cardinal
    sin with a multiplier.
    """
    engine, store, library, facts = await _carried()
    assert (await _stored(engine, store, library))["s1@2026-07"] == 1500.0

    facts.put(
        "t1",
        "setting_change",
        "c2",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1200.0,
            "set_at": "2026-06-03T09:00:00Z",
            "set_by": "Bo",
        },
    )
    await engine.run("t1", written={"setting_change": ["c2"]}, at_ms=AT)

    after = await _stored(engine, store, library)
    assert after["s1@2026-06"] == 1200.0, "the corrected month kept the old value"
    assert after["s1@2026-07"] == 1200.0, "the carry spread the stale value forward"


async def test_a_read_anchored_in_the_future_writes_no_future_buckets() -> None:
    """`?at=` is a caller's argument, and an argument may narrow what is
    reported but never change what is stored.

    The lazy fill counts back from the anchor it is given, so an anchor in
    2031 makes every month between now and then a bucket "in the past" --
    and they are written, and they stay. Months that have not happened would
    then hold confident values for ever, which no later pass removes.
    """
    from uratori.engine.serve import serve_reading

    _, store, library, facts = await _paced(at=AT)
    figure = library.figure("site.target_month")
    before = {r.subject for r in await store.values("t1", figure.name, figure.version)}

    # Through the door a caller actually reaches: `?at=YYYY-MM-DD`, which
    # the HTTP layer validates for spelling and nothing else.
    await serve_reading(
        store,
        library,
        "t1",
        library.reading("site.target_pace"),
        [3],
        at_day="2031-06-15",
        at_ms=AT,
        facts=facts,
    )
    after = {r.subject for r in await store.values("t1", figure.name, figure.version)}
    assert after == before, (
        "a read anchored in the future materialised buckets that have not "
        f"happened: {sorted(after - before)}"
    )


async def test_moving_the_first_change_later_leaves_no_fabricated_history() -> None:
    """A change re-dated from February to May must not leave March and April
    reporting a target that was never in force.

    Nothing revisits buckets before the earliest anchor, so the rows written
    under the old date simply stay -- and they are indistinguishable from
    real ones, because they were real until somebody corrected the date.
    """
    engine, store, library, facts = await _carried()
    assert (await _stored(engine, store, library))["s1@2026-03"] == 1800.0

    facts.put(
        "t1",
        "setting_change",
        "c1",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1800.0,
            "set_at": "2026-05-10T09:00:00Z",
            "set_by": "Aki",
        },
    )
    await engine.run("t1", written={"setting_change": ["c1"]}, at_ms=AT)

    after = await _stored(engine, store, library)
    assert "s1@2026-03" not in after, (
        "March still reports a target that was never in force there"
    )
    assert "s1@2026-04" not in after
    assert after["s1@2026-05"] == 1800.0


async def test_a_figure_over_a_carried_one_is_not_a_pass_behind() -> None:
    """The doc's flagship example, evaluated rather than merely compiled.

    The carry runs after the ordinary recomputes, so a figure reading a
    carried source at `:{bucket}` sees anchor-only rows and answers an
    absence at exactly the carried coordinates -- on a first build, for
    every month the value was carried into. It heals on the next pass, which
    is the worst shape: right in every test that runs twice.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(DELTA_FIGURE)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    for month in ("03", "04", "07"):
        facts.put(
            "t1",
            "job",
            f"j{month}",
            {
                "site_id": "s1",
                "started_at": f"2026-{month}-04T09:00:00Z",
                "finished_at": f"2026-{month}-04T09:40:00Z",
            },
        )
    await engine.run("t1", full=True, at_ms=AT)

    gap = library.figure("site.gap_month")
    rows = {
        r.subject: r.value for r in await store.values("t1", gap.name, gap.version)
    }
    assert rows.get("s1@2026-03") == 2400.0 - 1800.0, (
        "a month whose target was carried answered an absence on the first "
        f"pass: {rows}"
    )
    assert rows.get("s1@2026-07") == 2400.0 - 1500.0


def test_carried_forward_on_a_figure_with_no_records_of_its_own_is_refused() -> None:
    """A carry anchors on the buckets that hold *records*, so a figure built
    from other figures has nothing to anchor on.

    Left to compile it did exactly nothing -- both triggers skip a figure
    with no scope index -- while the suffix sat in the version hash claiming
    a behaviour the engine never performed. Declared and silently absent is
    the worst of the three states.
    """
    refuses(
        MEDIAN
        + CARRIED
        + '''
# Carried, but built on figures.
figure site.echo_month bucketed:
    display "{site} echo"
    unit duration

    calculate:
        site.target_month:{bucket} carried forward
''',
        "carried forward",
        "records",
    )


def test_a_coordinate_read_beside_a_scalar_read_is_refused() -> None:
    """One is looked up under `s1@2026-02` and the other under `s1`, so the
    scalar resolves to nothing at every coordinate and the figure answers an
    absence for ever -- a definition that can never produce a number."""
    refuses(
        MEDIAN
        + '''
# A plain per-site count.
figure site.changes:
    display "{site} changes"

    depends:
        sets = setting_change.at_site:{site}

    calculate:
        count(sets)

# One of each.
figure site.mixed bucketed:
    display "{site} mixed"
    unit count

    calculate:
        site.actual_month:{bucket} - site.changes
''',
        "site.changes",
    )


def test_a_coordinate_passthrough_keeps_the_sources_unit() -> None:
    """`goal:{bucket}` on its own is the source's own number at a
    coordinate, so it is in the source's own unit.

    Derived rather than declared -- declaring it is refused as redundant --
    so getting the derivation wrong leaves no way to say what the number is:
    1800 seconds renders as "1800", which is the "0.6 prints as 60% or 0.6"
    harm exactly.
    """
    lib = compile_world(
        CARRIED
        + '''
# The target, echoed.
figure site.echo_month bucketed:
    display "{site} echo"

    calculate:
        site.target_month:{bucket}
'''
    )
    plan = lib.figure("site.echo_month")
    assert plan is not None
    assert plan.unit == "duration", (
        "a passthrough lost its source's unit, so the number renders as a "
        "bare integer with nothing saying what it is"
    )


async def test_a_reach_refusal_does_not_take_the_rest_of_the_pass_down() -> None:
    """One figure's first change being older than the ceiling is a fact
    about that tenant's data, not a reason to stop computing everything
    else.

    Raised, it aborted the whole pass -- no run log, no pushes, nothing
    recomputed -- and did so identically on every pass after, with no
    recovery short of editing the definition or deleting the record.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    daily = '''
group setting_change.by_day from (site_id, set_at by day)

# A target carried day by day, whose first change is far older than the reach.
figure site.target_day bucketed:
    display "{site} daily target"
    unit duration

    depends:
        sets = setting_change.by_day:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward

# An ordinary figure that must keep working.
figure site.changes:
    display "{site} changes"

    depends:
        sets = setting_change.at_site:{site}

    calculate:
        count(sets)
'''
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(daily)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    facts.put(
        "t1",
        "setting_change",
        "ancient",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1800.0,
            "set_at": "1998-01-04T09:00:00Z",
            "set_by": "Aki",
        },
    )

    outcome = await engine.execute("t1", full=True, at_ms=AT)
    assert outcome.carried == (), "the over-reaching figure reported carrying"

    counted = library.figure("site.changes")
    rows = await store.values("t1", counted.name, counted.version)
    assert [r.value for r in rows] == [1.0], (
        "an unrelated figure lost its pass because another figure reached too "
        "far back"
    )


async def test_two_readers_taking_the_same_empty_snapshot_create_each_row_once() -> None:
    """A real race, not two calls in sequence.

    The in-memory store never yields at an `await`, so two `materialise`
    calls gathered together simply run one after the other -- the second
    sees the first's rows in its own snapshot and never reaches the insert
    at all. That version of this test stayed green with the
    insert-or-nothing guard removed entirely. Holding both readers at the
    snapshot until each has taken it is what makes them contend.
    """
    import asyncio

    from uratori.engine.carry import materialise

    _, store, library, _facts = await _paced(at=AT)
    figure = library.figure("site.target_month")
    later = 1_792_843_200_000.0

    both_have_read = asyncio.Event()
    arrived = 0
    real_values = store.values
    inserts: list[tuple[str, bool]] = []
    real_insert = store.save_if_absent

    async def values(*a, **k):
        nonlocal arrived
        rows = await real_values(*a, **k)
        arrived += 1
        if arrived >= 2:
            both_have_read.set()
        await both_have_read.wait()
        return rows

    async def save_if_absent(tenant, name, version, subject, *a, **k):
        did = await real_insert(tenant, name, version, subject, *a, **k)
        inserts.append((subject, did))
        return did

    store.values = values  # type: ignore[method-assign]
    store.save_if_absent = save_if_absent  # type: ignore[method-assign]
    try:
        first, second = await asyncio.gather(
            materialise(store, figure, "t1", ["s1"], at_ms=later, zones={}, trigger="read"),
            materialise(store, figure, "t1", ["s1"], at_ms=later, zones={}, trigger="read"),
        )
    finally:
        store.values = real_values  # type: ignore[method-assign]
        store.save_if_absent = real_insert  # type: ignore[method-assign]

    assert any(not did for _, did in inserts), (
        "no insert ever lost, so the two readers never actually contended and "
        "the guard is untested"
    )
    created = [s for s, _, _ in first] + [s for s, _, _ in second]
    assert len(created) == len(set(created)), (
        f"a bucket was reported created twice: {sorted(created)}"
    )
    assert {"s1@2026-09", "s1@2026-10"} <= set(created)


async def test_a_new_carried_bucket_reaches_the_push_surface() -> None:
    """A bucket materialised by a pass is a movement, and every screen
    watching the figure has to hear about it.

    Reported nowhere, a subscribed board would sit on last month's value
    until something unrelated moved -- the failure the change stream exists
    to prevent, arriving through a write nobody else makes.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    report = await engine.run("t1", full=True, at_ms=AT)

    assert "site.target_month" in report.moved, (
        "a carried figure gained five buckets and told no subscriber"
    )
    carried_changes = [
        c
        for c in report.outcome.changes
        if c.figure == "site.target_month"
        and c.subject in ("s1@2026-07", "s1@2026-08")
    ]
    assert carried_changes, "no carried bucket appears in the run's movements"
    assert report.outcome.carried == ("site.target_month",)


def test_carried_forward_moves_the_version_hash() -> None:
    """The asymmetry with `bucketed` beside it, asserted rather than argued.

    The same records under the same calculation give two buckets without the
    suffix and seven with it, so a version reused across the change would
    serve carried numbers from a definition that never claimed any.
    """
    plain = CARRIED.replace(" carried forward", "")
    assert plain != CARRIED
    without = compile_world(plain).figure("site.target_month")
    with_carry = compile_world(CARRIED).figure("site.target_month")
    assert without is not None and with_carry is not None
    assert without.version != with_carry.version

    # And prose does not move it, for a carried figure like any other.
    reworded = CARRIED.replace(
        "# The target in force each month: the latest one set within the month,\n"
        "# carried across the months where nobody changed it.",
        "# Whatever the target currently is, month by month.",
    )
    assert reworded != CARRIED
    assert compile_world(reworded).figure("site.target_month").version == with_carry.version


async def test_a_coordinate_absent_on_one_side_answers_absence_not_nought() -> None:
    """The join's own rule, evaluated.

    A month the work happened in but no target was ever set for must answer
    *nothing*. A nought there is a confident "exactly on target", and the
    band beside it would colour it comfortable -- and a positional zip would
    be worse still, pairing every later month with the wrong target.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(DELTA_FIGURE)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    # The target starts in June; the work starts in March.
    facts.put(
        "t1",
        "setting_change",
        "c2",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1500.0,
            "set_at": "2026-06-03T09:00:00Z",
            "set_by": "Bo",
        },
    )
    for month in ("03", "07"):
        facts.put(
            "t1",
            "job",
            f"j{month}",
            {
                "site_id": "s1",
                "cost": 1.0,
                "started_at": f"2026-{month}-04T09:00:00Z",
                "finished_at": f"2026-{month}-04T09:40:00Z",
            },
        )
    await engine.run("t1", full=True, at_ms=AT)

    gap = library.figure("site.gap_month")
    rows = {r.subject: r.value for r in await store.values("t1", gap.name, gap.version)}
    assert rows.get("s1@2026-03") is None, (
        "a month with no target on either side answered a number: "
        f"{rows.get('s1@2026-03')}"
    )
    assert rows.get("s1@2026-07") == 2400.0 - 1500.0, (
        "the month that does have both sides was shifted or lost"
    )


async def test_a_bucket_statistic_is_the_median_of_that_buckets_records() -> None:
    """Evaluated, not merely compiled: swapping `mean` for `median` in the
    evaluator passed the whole suite, because nothing ever created a job."""
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEDIAN)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    # 10, 20 and 60 minutes: a median of 20 and a mean of 30, so the two
    # statistics cannot be confused for one another.
    for key, ends in (("j1", "09:10"), ("j2", "09:20"), ("j3", "10:00")):
        facts.put(
            "t1",
            "job",
            key,
            {
                "site_id": "s1",
                "cost": 1.0,
                "started_at": "2026-03-04T09:00:00Z",
                "finished_at": f"2026-03-04T{ends}:00Z",
            },
        )
    await engine.run("t1", full=True, at_ms=AT)

    plan = library.figure("site.actual_month")
    rows = {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}
    assert rows["s1@2026-03"] == 20 * 60.0, (
        f"expected the median of the bucket's own records, got {rows['s1@2026-03']}"
    )
    assert "s1@2026-04" not in rows, "a month with no jobs invented a statistic"


async def test_a_warm_edit_under_a_derived_sequenced_figure_completes() -> None:
    """Correcting a change, with a figure reading the carried one at
    `:{bucket}` above it.

    A part that moves makes its totals stale, and the engine propagates that
    by subject -- stripping `@bucket` on the way, which is right for a
    roster-keyed total and wrong for a sequenced one. The sequenced reader
    was then asked for the bare site, found every coordinate row under it,
    and aborted the whole pass rather than pick one. The figure is left half
    written, nothing is pushed, and the movement is never reported at all
    because the retry finds the edit already landed.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(DELTA_FIGURE)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    for key, when, value, who, _ in CHANGES:
        facts.put(
            "t1",
            "setting_change",
            key,
            {
                "site_id": "s1",
                "setting": "target_minutes",
                "value": value,
                "set_at": when,
                "set_by": who,
            },
        )
    facts.put(
        "t1",
        "job",
        "j7",
        {
            "site_id": "s1",
            "cost": 1.0,
            "started_at": "2026-07-04T09:00:00Z",
            "finished_at": "2026-07-04T09:40:00Z",
        },
    )
    await engine.run("t1", full=True, at_ms=AT)

    facts.put(
        "t1",
        "setting_change",
        "c2",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1200.0,
            "set_at": "2026-06-03T09:00:00Z",
            "set_by": "Bo",
        },
    )
    # The pass must complete rather than raise.
    await engine.run("t1", written={"setting_change": ["c2"]}, at_ms=AT)

    gap = library.figure("site.gap_month")
    rows = {r.subject: r.value for r in await store.values("t1", gap.name, gap.version)}
    assert rows.get("s1@2026-07") == 2400.0 - 1200.0, (
        "the derived figure did not follow the corrected target"
    )


async def test_one_subject_reaching_too_far_back_does_not_lose_the_others() -> None:
    """The ceiling is a fact about one site's data, not about the figure.

    Let out of the materialiser, it discarded every row the loop had already
    written for the subjects before it: they stayed on the board, appeared in
    no change stream, and the next pass found them equal and said nothing
    either -- so they were never announced at all, and the run reported
    carrying nothing while the figure demonstrably had.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    daily = '''
group setting_change.by_day from (site_id, set_at by day)

# A target carried day by day.
figure site.target_day bucketed:
    display "{site} daily target"
    unit duration

    depends:
        sets = setting_change.by_day:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward
'''
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(daily)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    for site in ("s1", "s2"):
        facts.put("t1", "site", site, {"name": site.upper(), "timezone": "UTC"})
    # s1 sorts first and is ordinary; s2 reaches back past the ceiling.
    facts.put(
        "t1",
        "setting_change",
        "recent",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1800.0,
            "set_at": "2026-08-20T09:00:00Z",
            "set_by": "Aki",
        },
    )
    facts.put(
        "t1",
        "setting_change",
        "ancient",
        {
            "site_id": "s2",
            "setting": "target_minutes",
            "value": 1500.0,
            "set_at": "1998-01-04T09:00:00Z",
            "set_by": "Bo",
        },
    )
    outcome = await engine.execute("t1", full=True, at_ms=AT)

    plan = library.figure("site.target_day")
    stored = {r.subject for r in await store.values("t1", plan.name, plan.version)}
    carried_rows = {s for s in stored if s.startswith("s1@") and s != "s1@2026-08-20"}
    assert carried_rows, "the ordinary site was not carried at all"

    announced = {
        c.subject for c in outcome.changes if c.figure == "site.target_day"
    }
    assert carried_rows <= announced, (
        "rows reached the board and no change stream: "
        f"{sorted(carried_rows - announced)}"
    )
    assert outcome.carried == ("site.target_day",), (
        "the run reported carrying nothing while the figure demonstrably had"
    )


# ---------------------------- what the mutation audit found unprotected --


async def test_a_bucket_nobody_could_measure_is_absent_rather_than_nought() -> None:
    """A month whose only job the measure cannot read answers **nothing**.

    The mistake this refuses: treating "no sample" as a sample of nought, so
    a month reports a median job length of zero -- a confident claim that
    everything that month was instantaneous, and one every band would colour
    comfortable.

    Distinct from the case the median test covers. A month with no records at
    all has no bucket key, so no row is written and the empty branch is never
    reached; only a bucket that exists and whose members are every one
    unmeasurable gets there.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEDIAN)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    # Finished, so it buckets by month -- but never started, so the length
    # measure has nothing to subtract from.
    facts.put(
        "t1",
        "job",
        "j1",
        {"site_id": "s1", "cost": 1.0, "finished_at": "2026-03-04T09:40:00Z"},
    )
    await engine.run("t1", full=True, at_ms=AT)

    plan = library.figure("site.actual_month")
    rows = {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}
    assert "s1@2026-03" in rows, (
        "the bucket was never written at all, so its absence proves nothing "
        "about the empty-sample branch"
    )
    assert rows["s1@2026-03"] is None, (
        f"a month nobody could measure reported {rows['s1@2026-03']} rather than "
        "an absence"
    )


async def test_a_bucket_statistic_cites_only_the_records_it_could_measure() -> None:
    """The evidence is the sample, not the bucket.

    Citing every record in the month shows a reader tracing "why 20 minutes
    in March?" a job that contributed nothing -- and invites them to check
    the arithmetic against a population the number was never taken over.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEDIAN)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "UTC"})
    facts.put(
        "t1",
        "job",
        "j1",
        {"site_id": "s1", "cost": 1.0, "finished_at": "2026-03-04T09:40:00Z"},
    )
    facts.put(
        "t1",
        "job",
        "j2",
        {
            "site_id": "s1",
            "cost": 1.0,
            "started_at": "2026-03-05T09:00:00Z",
            "finished_at": "2026-03-05T09:20:00Z",
        },
    )
    await engine.run("t1", full=True, at_ms=AT)

    evidence = await engine.evidence("t1", "site.actual_month", "s1@2026-03")
    assert [m.key for m in evidence.members] == ["j2"], (
        "the citation named a job the statistic never measured"
    )


async def test_a_departed_subject_stops_gaining_buckets() -> None:
    """A carry extends the subjects that still exist, and no others.

    Taking the bases straight off the index would keep writing a new bucket
    every pass for a site nobody can open: a grouping goes on holding a
    departed site's months for as long as the records naming it survive, so
    the figure would grow for ever with no subject behind it.

    Deliberately a *partial* pass -- the removal sweep is gated behind
    `deleted`, so on an ordinary sync it never runs and the population gate
    is the only thing standing between those rows and another month.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    early = 1_774_353_600_000.0  # 2026-03-24
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    for site in ("s1", "s2"):
        facts.put("t1", "site", site, {"name": site.upper(), "timezone": "UTC"})
        facts.put(
            "t1",
            "setting_change",
            f"c-{site}",
            {
                "site_id": site,
                "setting": "target_minutes",
                "value": 1800.0,
                "set_at": "2026-02-10T09:00:00Z",
                "set_by": "Aki",
            },
        )
    await engine.run("t1", full=True, at_ms=early)

    facts.drop("t1", "site", "s2")
    await engine.run("t1", at_ms=AT)

    plan = library.figure("site.target_month")
    rows = {r.subject for r in await store.values("t1", plan.name, plan.version)}
    assert "s1@2026-08" in rows, "the site that is still here did not reach the present month"
    assert "s2@2026-08" not in rows, (
        f"a site with no record of its own gained five more months: {sorted(rows)}"
    )


async def test_the_carry_extends_along_the_groups_own_calendar() -> None:
    """Whose midnight decides where the sequence stops.

    Extending along UTC while the group bucketed in the tenant's zone agrees
    for most of a month -- which is why it would survive every other test
    here -- and then on the evening of the 31st writes next month: a bucket
    no window will ask for, holding a confident value for a month that has
    not begun.

    The instant is chosen so the two calendars disagree: 2026-09-01T02:00Z is
    still 21:00 on 2026-08-31 in Chicago.
    """
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori

    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(CARRIED)
    engine = Uratori(schema=ONCHANGE, library=library, store=store, facts=facts)
    facts.put("t1", "site", "s1", {"name": "Northgate", "timezone": "America/Chicago"})
    facts.put(
        "t1",
        "setting_change",
        "c1",
        {
            "site_id": "s1",
            "setting": "target_minutes",
            "value": 1800.0,
            "set_at": "2026-06-10T09:00:00Z",
            "set_by": "Aki",
        },
    )
    await engine.run(
        "t1",
        full=True,
        at_ms=1_788_228_000_000.0,  # 2026-09-01T02:00Z -- 2026-08-31 in Chicago
    )

    plan = library.figure("site.target_month")
    rows = {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}
    assert rows.get("s1@2026-08") == 1800.0, (
        "the present month in the tenant's own calendar is missing"
    )
    assert "s1@2026-09" not in rows, (
        f"a month that has not begun where the buckets were written: {sorted(rows)}"
    )


def test_a_record_with_no_readable_stamp_does_not_win_earliest() -> None:
    """An unreadable ordering instant means the record takes no part.

    Sorted to nought instead of skipped, it would win every `earliest` for
    ever -- nought is a real instant, 1970 -- and the bucket would report a
    value chosen by a missing field rather than by when anything happened.

    Evaluated directly, because the whole point is a member the readers
    cannot answer for, which no well-formed fixture produces.
    """
    from uratori.engine.evaluate import Readers, _eval
    from uratori.lang.ast import FieldPick
    from uratori.lang.plan import FigurePlan

    pick = FieldPick(
        which="earliest",
        kind="setting_change",
        field="value",
        set="sets",
        ordered_by="set_at",
    )
    plan = FigurePlan(
        name="site.target_month",
        scope="site",
        doc="",
        display="",
        unit="duration",
        calculate=pick,
    )
    stamps: dict[str, float | None] = {"c1": 1.0, "c2": None}
    values = {"c1": 1800.0, "c2": 25.0}
    readers = Readers(
        buckets=lambda index, subject: frozenset(),
        measures=lambda measure, member: None,
        moments=lambda measure, member: None,
        parts=lambda source, subject: None,  # type: ignore[arg-type,return-value]
        settings=lambda path: 0.0,
        fields=lambda kind, path, member: values.get(member),
        instants=lambda kind, path, member: stamps.get(member),
    )

    answer = _eval(pick, plan, "s1@2026-02", {"sets": frozenset({"c1", "c2"})}, readers)
    assert answer == 1800.0, (
        f"the record with no readable stamp was treated as the earliest, answering {answer}"
    )


def test_a_field_read_outside_a_sequence_is_refused_by_the_rule_that_names_it() -> None:
    """`latest(<kind>.<field> over ...)` needs a bucket to be the latest *in*.

    Over a figure with one value per subject, "the latest" means the latest
    of everything ever collected -- so the number moves as records age with
    nothing having happened. The author must be told about the missing
    sequence, not about a missing ordering field, which is a consequence of
    it.
    """
    refuses(
        '''
# The target in force at this site.
figure site.target_now:
    display "{site} target"
    unit duration

    depends:
        sets = setting_change.at_site:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets)
''',
        "no sequence of buckets",
    )


def test_a_field_read_across_a_list_is_refused_rather_than_taking_the_first() -> None:
    """One record holding several of a field has no "the latest" among them.

    Resolved anyway, the figure reports a number picked by JSON ordering,
    cites the record it came from as though that settled it, and carries it
    forward across every month after -- a fabrication about the wrong
    element, with a full audit trail behind it.
    """
    world = '''
# A place where work happens.
fact site:
    name name
    name as text
    timezone as text

# One change, with the revisions it went through.
fact setting_change:
    site_id as text
    setting as text
    value as number
    set_at as moment
    set_by as text
    many revision:
        amount as number

filter setting_change.target where setting == "target_minutes"
group setting_change.by_month from (site_id, set_at by month in site.timezone)

# The revised target each month.
figure site.revised bucketed:
    display "{site} revised"
    unit duration

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.revision.amount over sets) carried forward
'''
    with pytest.raises(CheckError) as caught:
        compile_source(world, ONCHANGE)
    assert "crosses a list" in caught.value.message, caught.value.message
