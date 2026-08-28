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

from uratori import CheckError, Schema, compile_source

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
    minutes as number
    finished_at as moment

filter setting_change.target where setting == "target_minutes"
group setting_change.at_site from site_id
group setting_change.by_month from (site_id, set_at by month in tenant.timezone)
group job.by_month from (site_id, finished_at by month in tenant.timezone)
measure job.length = minutes in count
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
