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
    bucket_settings=("tenant.timezone",),
    figure_settings=("limits.target.over",),
    reading_settings=("flow.targetMinutes",),
    defaults={
        "tenant": {"timezone": "UTC", "hoursPerDay": 8},
        "limits": {"target": {"over": 0}},
        "flow": {"targetMinutes": {"good": 2, "poor": 5}},
    },
)

WORLD = '''
# A place where work happens.
fact site:
    name name
    name as text

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
    started_at as moment
    finished_at as moment

filter setting_change.target where setting == "target_minutes"
group setting_change.at_site from site_id
group setting_change.by_month from (site_id, set_at by month in tenant.timezone)
group job.by_month from (site_id, finished_at by month in tenant.timezone)
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
    second key."""
    with pytest.raises((CheckError, Exception)):
        compile_world(
            '''
# Both at once.
figure site.confused bucketed across job:
    display "{site} confused"

    depends:
        sets = setting_change.by_month:{site}

    calculate:
        count(sets)
'''
        )


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
    """
    from .test_lang import BASE
    from .world import compile_source as compile_suite

    lib = compile_suite(BASE)
    versions = {f.name: f.version for f in lib.figures}
    assert versions["team_person.time_to_merge"] == "31cafa78b3bd", (
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
        latest(job.minutes over sets) carried forward
''',
        "job",
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
    facts.put("t1", "site", "s1", {"name": "Northgate"})
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
    first = await _run(CARRIED, stamped)
    second = await _run(CARRIED, list(reversed(stamped)))
    a = {s.id: s.value for s in (await first.answer("t1", "site.target_month")).subjects}
    b = {s.id: s.value for s in (await second.answer("t1", "site.target_month")).subjects}
    assert a["s1@2026-02"] == b["s1@2026-02"], (
        "the same two records in a different write order gave two answers"
    )


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
    facts.put("t1", "site", "s1", {"name": "Northgate"})
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


def test_a_bucket_before_the_first_change_is_absent_not_a_nought() -> None:
    """Restated as its own claim because it is the one a band would get
    wrong: a nought here is comfortably under every threshold, and would
    colour January green for a target nobody had set."""
    assert EXPECTED["s1@2026-01"] is None


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
    facts.put("t1", "site", "s1", {"name": "Northgate"})
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
group setting_change.by_minute from (site_id, set_at by minute in tenant.timezone)

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
    facts.put("t1", "site", "s1", {"name": "Northgate"})
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
    return engine, store, library


async def test_a_read_after_the_pass_fills_the_buckets_time_has_added() -> None:
    """A pass extends to the bucket it ran in, so between passes the newest
    bucket has no row.

    Told "never computed" there, a screen would report an absence for a value
    that has demonstrably been in force for months -- which is exactly the
    wrong answer for a figure whose whole point is that it persists. The read
    materialises what time has added and then serves it.
    """
    from uratori.engine.serve import serve_reading

    _, store, library = await _paced(at=AT)
    plan = library.reading("site.target_pace")
    figure = library.figure("site.target_month")

    before = {r.subject for r in await store.values("t1", figure.name, figure.version)}
    assert "s1@2026-09" not in before, "August was the present month at the pass"

    # Two months pass with no sync at all, and then somebody looks.
    later = 1_792_843_200_000.0  # 2026-10-24T12:00Z
    result = await serve_reading(
        store, library, "t1", plan, ONCHANGE.defaults, [3], at_ms=later
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

    _, read_store, read_lib = await _paced(at=AT)
    await serve_reading(
        read_store,
        read_lib,
        "t1",
        read_lib.reading("site.target_pace"),
        ONCHANGE.defaults,
        [3],
        at_ms=later,
    )
    figure = read_lib.figure("site.target_month")
    read_rows = {
        r.subject: (r.value, r.members)
        for r in await read_store.values("t1", figure.name, figure.version)
    }

    _, pass_store, pass_lib = await _paced(at=later)
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

    _, store, library = await _paced(at=AT)
    figure = library.figure("site.target_month")
    later = 1_792_843_200_000.0

    first, second = await asyncio.gather(
        materialise(
            store, figure, "t1", ["s1"], at_ms=later, zone="UTC", trigger="read"
        ),
        materialise(
            store, figure, "t1", ["s1"], at_ms=later, zone="UTC", trigger="read"
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
    facts.put("t1", "site", "s1", {"name": "Northgate"})
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

    combine:
        actual = site.actual_month
        goal = site.target_month

    calculate:
        actual:{bucket} - goal:{bucket}

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

    combine:
        actual = site.actual_month
        goal = site.target_month

    calculate:
        actual - goal
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
        when value > limits.target.over then "over"
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

    combine:
        actual = site.actual_month
        goal = site.target_month

    calculate:
        actual:{bucket} - goal:{bucket - 1}
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
        '''
# A plain per-site count.
figure site.changes:
    display "{site} changes"

    depends:
        sets = setting_change.at_site:{site}

    calculate:
        count(sets)

# Selector over a scalar.
figure site.nonsense bucketed:
    display "{site} nonsense"
    unit count

    combine:
        c = site.changes

    calculate:
        c:{bucket} - c:{bucket}
''',
        "bucket",
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
    for the same reason."""
    with pytest.raises((CheckError, Exception)):
        compile_world(
            MEDIAN
            + '''
# Median of medians.
figure site.worse bucketed:
    display "{site} worse"

    combine:
        m = site.actual_month

    calculate:
        median(m)
'''
        )
