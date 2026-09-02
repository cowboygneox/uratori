"""A record that occupies a stretch of the calendar rather than a point on it.

Every bucket rule until now has answered "which bucket did this instant fall
in": one moment, one label. That is the right question about an event -- a
merge happened at 14:00 and belongs to that day and no other -- and the wrong
one about a *commitment*. A campaign that runs from August to October is not a
thing that happened in August; it occupies eleven weeks, and a chart of what is
running each week needs it in all eleven.

So a part may name two moments instead of one:

    starts_at until ends_at by week in ad_account.timezone

and the record is a member of every bucket the two ends cross, inclusive of
both. Everything downstream is unchanged -- the keys are the same labels the
same calendar produces, a composite still crosses its parts, and a figure over
the group reads it exactly as it reads any other.

`excluding weeks gone` is the second half, and it is what makes the rule about
the *future*. Buckets whose grain-period has already passed are dropped, so a
campaign half way through occupies the weeks it has left rather than the weeks
it was planned over. That makes membership move with the clock, which is a
property this engine already has -- an age filter does it -- and pays for it
the same way: the crossing is noticed at the next pass.

The tests below are written against calendars rather than against UTC wherever
a zone could hide a mistake, because the span's ends are truncated by the
subject's calendar exactly as a single instant is, and a rule that quietly used
UTC for one end would be invisible in a UTC-only fixture.
"""

from __future__ import annotations

import pytest

from uratori import (
    CheckError,
    MemoryEngineStore,
    MemoryFactStore,
    Schema,
    Uratori,
    compile_source,
)
from uratori.lang.ast import Sum

WORLD = Schema(kinds=frozenset())

SOURCE = '''
# Somebody who buys advertising, and the calendar their weeks are cut on.
fact ad_account:
    name name
    name as text
    timezone as text

# One campaign, booked between two dates, with money attached.
fact ad_campaign:
    name ref
    ref as text
    account_id as text
    starts_at as moment
    ends_at as moment
    booked_at as moment
    budget_cents as number

# One impression, so a per-campaign-per-week figure can hold a *different*
# number in each of a campaign's own weeks. Every other figure a span produces
# is constant across the buckets it spans -- an even division is the same
# number five times -- and a total that read a member at its bare subject key
# instead of at the bucket would be indistinguishable from a correct one
# against a constant source.
fact ad_impression:
    name ref
    ref as text
    campaign_id as text
    at as moment

# Impressions per campaign per week.
group ad_impression.by_week from (campaign_id, at by week in "UTC")

# Every week a campaign is booked to run in, for the account that booked it.
group ad_campaign.booked_weeks from (account_id, starts_at until ends_at by week in ad_account.timezone)

# Every week a campaign still has left to run.
group ad_campaign.weeks_left from (account_id, starts_at until ends_at by week excluding weeks gone in ad_account.timezone)

# How many campaigns this account has running in each week.
figure ad_account.running bucketed:
    display "{ad_account} campaigns running that week"
    depends:
        live = ad_campaign.booked_weeks:{ad_account}
    calculate:
        count(live)

# How many impressions this campaign drew in each week.
figure ad_campaign.shown bucketed:
    display "{ad_campaign} impressions that week"
    depends:
        seen = ad_impression.by_week:{ad_campaign}
    calculate:
        count(seen)

# The account's impressions in each week, added up from its campaigns'.
figure ad_account.shown bucketed:
    display "{ad_account} impressions that week"
    depends:
        live = ad_campaign.booked_weeks:{ad_account}
    calculate:
        sum(ad_campaign.shown over live)

# A span keyed by nothing but the dates -- one bucket sequence for the whole
# kind, no subject part. Here because every other group in this file is a
# composite, and the clip is detected on two code paths.
group ad_campaign.any_week from starts_at until ends_at by week excluding weeks gone in "UTC"

# The same, counting only weeks that have not gone.
figure ad_account.still_running bucketed:
    display "{ad_account} campaigns still to run that week"
    depends:
        live = ad_campaign.weeks_left:{ad_account}
    calculate:
        count(live)
'''

# Three weeks apart, chosen so every label below is checkable by eye:
# 2026-08-03 is a Monday, so W32 starts there and W36 starts 2026-08-31.
AUG_3 = "2026-08-03T09:00:00Z"
SEP_6 = "2026-09-06T17:00:00Z"

# 2026-08-03T12:00Z, the Monday W32 begins on -- a pass instant inside the
# first week of the long span, so nothing is clipped from it.
EARLY_AUGUST = 1_785_758_400_000.0

# 2026-08-17T12:00Z, a Monday inside W34 -- the pass instant for the clipping
# tests, so W32 and W33 have gone and W34 is the week in progress.
MID_AUGUST = 1_786_968_000_000.0


def compile_world(extra: str = ""):
    return compile_source(SOURCE + extra, WORLD)


async def board(
    accounts: dict[str, str],
    campaigns: dict[str, dict[str, object]],
    *,
    at_ms: float | None = None,
    impressions: dict[str, dict[str, object]] | None = None,
):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world()
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    for key, zone in accounts.items():
        facts.put("t1", "ad_account", key, {"name": key.upper(), "timezone": zone})
    for key, body in campaigns.items():
        facts.put("t1", "ad_campaign", key, body)
    for key, body in (impressions or {}).items():
        facts.put("t1", "ad_impression", key, body)
    await engine.run("t1", full=True, at_ms=at_ms)
    return engine, store, library, facts


async def rows(store, library, figure: str) -> dict[str, float | None]:
    plan = library.figure(figure)
    return {r.subject: r.value for r in await store.values("t1", plan.name, plan.version)}


def campaign(account: str, starts: str, ends: str, budget: int = 0) -> dict[str, object]:
    return {
        "ref": "C",
        "account_id": account,
        "starts_at": starts,
        "ends_at": ends,
        "budget_cents": budget,
    }


# --------------------------------------------------------------- membership --


async def test_a_span_is_a_member_of_every_bucket_it_crosses() -> None:
    """The whole point. One record, five weeks, and the two ends are in it --
    a rule that filed the record under its start alone would leave four weeks
    of a booked campaign showing nothing running."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
    )
    assert await rows(store, library, "ad_account.running") == {
        "a1@2026-W32": 1.0,
        "a1@2026-W33": 1.0,
        "a1@2026-W34": 1.0,
        "a1@2026-W35": 1.0,
        "a1@2026-W36": 1.0,
    }


async def test_both_ends_are_inside_the_span() -> None:
    """A campaign that starts and finishes inside one week is in that week,
    not in none. The half-open reading of a booking is the arithmetic mistake
    that loses the shortest campaigns entirely."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", "2026-08-04T09:00:00Z", "2026-08-06T09:00:00Z")},
    )
    assert await rows(store, library, "ad_account.running") == {"a1@2026-W32": 1.0}


async def test_overlapping_campaigns_stack_in_the_weeks_they_share() -> None:
    """Two spans over one account. The count per week is what a load chart is
    made of, and it is the sum over members of that bucket rather than of the
    records -- which is the same mechanism a day-keyed group already uses."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {
            "c1": campaign("a1", AUG_3, SEP_6),
            "c2": campaign("a1", "2026-08-17T09:00:00Z", "2026-08-28T09:00:00Z"),
        },
    )
    assert await rows(store, library, "ad_account.running") == {
        "a1@2026-W32": 1.0,
        "a1@2026-W33": 1.0,
        "a1@2026-W34": 2.0,
        "a1@2026-W35": 2.0,
        "a1@2026-W36": 1.0,
    }


async def test_each_end_is_cut_by_the_subjects_own_calendar() -> None:
    """Both ends, not just the first. An instant at 2026-08-02T13:00Z is the
    Sunday of W31 in London and already the Monday of W32 in Auckland, so a
    span starting there begins in a different week for each account -- and a
    rule that truncated one end in the subject's calendar and the other in UTC
    would agree with this test on the London row and quietly disagree on the
    Auckland one.
    """
    # Both ends land on a Sunday-in-London that is already a Monday in
    # Auckland, so *each* end alone decides a different week for the two
    # accounts. With only the near end straddling, truncating the far end in
    # UTC passed this test unchanged -- proved by mutation, which is why both
    # instants are chosen this way now.
    starts = "2026-08-02T13:00:00Z"
    ends = "2026-08-09T13:00:00Z"
    _e, store, library, _f = await board(
        {"a1": "Europe/London", "a2": "Pacific/Auckland"},
        {"c1": campaign("a1", starts, ends), "c2": campaign("a2", starts, ends)},
    )
    found = await rows(store, library, "ad_account.running")
    assert set(k for k in found if k.startswith("a1@")) == {
        "a1@2026-W31",
        "a1@2026-W32",
    }, f"London's span is wrong: {found}"
    assert set(k for k in found if k.startswith("a2@")) == {
        "a2@2026-W32",
        "a2@2026-W33",
    }, f"Auckland's span is wrong: {found}"


async def test_an_account_with_no_calendar_is_in_no_bucket() -> None:
    """The same refusal a single-instant rule makes, and for the same reason:
    a span cannot be cut into weeks nobody has said how to cut."""
    _e, store, library, _f = await board(
        {"a1": ""},
        {"c1": campaign("a1", AUG_3, SEP_6)},
    )
    assert await rows(store, library, "ad_account.running") == {}


async def test_a_span_missing_either_end_is_in_no_bucket() -> None:
    """Never a half-open span silently running to now, or to for ever. A
    campaign with no end date is a campaign nobody has scheduled, and inventing
    an end would put a booking in weeks no one committed to."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {
            "c1": {"ref": "C", "account_id": "a1", "starts_at": AUG_3, "budget_cents": 0},
            "c2": {"ref": "C", "account_id": "a1", "ends_at": SEP_6, "budget_cents": 0},
        },
    )
    assert await rows(store, library, "ad_account.running") == {}


async def test_a_span_that_ends_before_it_starts_is_in_no_bucket() -> None:
    """Backwards dates are somebody's typo, and the honest answer is no
    membership. Reversing them silently would report a campaign as running
    across a stretch nobody booked."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", SEP_6, AUG_3)},
    )
    assert await rows(store, library, "ad_account.running") == {}


# ----------------------------------------------------------------- clipping --


async def test_excluding_weeks_gone_drops_the_weeks_that_have_passed() -> None:
    """The clip, at a pass instant inside W34: the booked group still holds all
    five weeks and the clipped one holds three. Asserted against the unclipped
    group in the same board, so a clip that dropped everything -- or nothing --
    cannot pass."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
        at_ms=MID_AUGUST,
    )
    assert set(await rows(store, library, "ad_account.running")) == {
        "a1@2026-W32",
        "a1@2026-W33",
        "a1@2026-W34",
        "a1@2026-W35",
        "a1@2026-W36",
    }
    assert set(await rows(store, library, "ad_account.still_running")) == {
        "a1@2026-W34",
        "a1@2026-W35",
        "a1@2026-W36",
    }


async def test_the_week_in_progress_is_not_gone() -> None:
    """Inclusive at the near end, which is the boundary that decides whether
    the chart's first column is this week or next. A campaign ending on Friday
    is still work somebody has to do, and dropping the current week would make
    the near future -- the part anybody can act on -- the one part missing."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", "2026-08-10T09:00:00Z", "2026-08-21T09:00:00Z")},
        at_ms=MID_AUGUST,
    )
    assert set(await rows(store, library, "ad_account.still_running")) == {"a1@2026-W34"}


async def test_a_span_entirely_in_the_past_is_in_no_bucket_once_clipped() -> None:
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", "2026-07-06T09:00:00Z", "2026-07-24T09:00:00Z")},
        at_ms=MID_AUGUST,
    )
    assert await rows(store, library, "ad_account.still_running") == {}
    assert await rows(store, library, "ad_account.running") != {}, (
        "the unclipped group lost a span that really did happen"
    )


async def test_a_span_entirely_ahead_is_untouched_by_the_clip() -> None:
    """The case the clip must *not* touch, and the one a naive "start at now"
    rule gets wrong: a campaign booked for October occupies October, not the
    weeks between now and then."""
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", "2026-10-05T09:00:00Z", "2026-10-16T09:00:00Z")},
        at_ms=MID_AUGUST,
    )
    assert set(await rows(store, library, "ad_account.still_running")) == {
        "a1@2026-W41",
        "a1@2026-W42",
    }


async def test_the_clip_is_cut_by_the_subjects_calendar_too() -> None:
    """At 2026-08-17T11:00Z it is Monday of W34 in London and already Tuesday
    of W34 in Auckland -- the same week. Take the instant back to the Sunday
    and the two disagree about which week is current, which is what this
    asserts: the clip compares the pass instant in the *subject's* calendar,
    so an account whose week has not turned yet keeps the week an account in
    another calendar has already left behind.
    """
    # 2026-08-16T13:00Z: Sunday of W33 in London, already Monday of W34 in
    # Auckland -- the one instant where the two calendars disagree about which
    # week is current, which is the whole point of the fixture.
    sunday = 1_786_885_200_000.0
    _e, store, library, _f = await board(
        {"a1": "Europe/London", "a2": "Pacific/Auckland"},
        {
            "c1": campaign("a1", "2026-08-10T09:00:00Z", SEP_6),
            "c2": campaign("a2", "2026-08-10T09:00:00Z", SEP_6),
        },
        at_ms=sunday,
    )
    found = await rows(store, library, "ad_account.still_running")
    assert "a1@2026-W33" in found, f"London's current week was dropped early: {found}"
    assert "a2@2026-W33" not in found, f"Auckland kept a week it has left: {found}"


# ------------------------------------------------------------------- grains --


async def test_a_span_works_at_every_grain_a_pass_can_honour() -> None:
    """Day, month and quarter as well as week. The grain is the same
    vocabulary a single-instant rule uses, and a span that only worked at one
    of them would be a second calendar system."""
    extra = '''
# Every day a campaign is booked to run in.
group ad_campaign.booked_days from (account_id, starts_at until ends_at by day in ad_account.timezone)

# The same span, cut into months.
group ad_campaign.booked_months from (account_id, starts_at until ends_at by month in ad_account.timezone)

# How many campaigns run on each day.
figure ad_account.days bucketed:
    display "{ad_account} days"
    depends:
        live = ad_campaign.booked_days:{ad_account}
    calculate:
        count(live)

# How many campaigns run in each month.
figure ad_account.months bucketed:
    display "{ad_account} months"
    depends:
        live = ad_campaign.booked_months:{ad_account}
    calculate:
        count(live)
'''
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(extra)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    facts.put("t1", "ad_account", "a1", {"name": "A1", "timezone": "UTC"})
    facts.put(
        "t1",
        "ad_campaign",
        "c1",
        campaign("a1", "2026-08-30T09:00:00Z", "2026-09-02T09:00:00Z"),
    )
    await engine.run("t1", full=True)

    days = await rows(store, library, "ad_account.days")
    assert set(days) == {
        "a1@2026-08-30",
        "a1@2026-08-31",
        "a1@2026-09-01",
        "a1@2026-09-02",
    }
    months = await rows(store, library, "ad_account.months")
    assert set(months) == {"a1@2026-08", "a1@2026-09"}


# ------------------------------------------------------------- what is refused --


def test_a_span_below_day_grain_is_refused() -> None:
    """The same fence `carried forward` is behind, for the same reason: a pass
    is the only clock membership has, so a rule owing new buckets every minute
    cannot be honoured and the unenforceable version is refused rather than
    left to disappoint."""
    with pytest.raises(CheckError) as caught:
        compile_world('''
group ad_campaign.by_hour from (account_id, starts_at until ends_at by hour in ad_account.timezone)
''')
    assert "hour" in str(caught.value)


def test_a_span_with_no_grain_is_refused() -> None:
    """Without a grain there is nothing to enumerate between the two ends, and
    the natural misreading -- one bucket per distinct pair of values -- is a
    key nobody could window."""
    with pytest.raises(CheckError) as caught:
        compile_world('''
group ad_campaign.no_grain from (account_id, starts_at until ends_at)
''')
    assert "grain" in str(caught.value).lower() or "by" in str(caught.value)


def test_a_span_whose_far_end_is_not_a_moment_is_refused() -> None:
    """Both ends are instants. A span from a date to a *word* has no weeks
    between it, and left to run it would produce no buckets at all -- a group
    that silently holds nothing, which is the failure the checker exists to
    turn into a message."""
    with pytest.raises(CheckError) as caught:
        compile_world('''
group ad_campaign.to_text from (account_id, starts_at until ref by week in ad_account.timezone)
''')
    assert "ref" in str(caught.value)


def test_a_span_may_not_be_read_by_a_projections_population() -> None:
    """A span is a *bucketed* group, so the rule it falls under is the one
    every bucketed group falls under: read whole by a `from`, it looks for a
    bucket keyed by the empty string, finds nothing, and the page is empty
    while looking complete.

    Asserted on that reason and not on a clock one. The first version of this
    test claimed the age-filter rationale -- membership as fresh as the last
    reconcile -- and passed on an error that says nothing of the kind, which
    is a test agreeing with itself about a rule that was never written.
    """
    with pytest.raises(CheckError) as caught:
        compile_world('''
# A page of campaigns, which may not take its population from a span.
projection ad_campaign.sheet:
    from ad_campaign.weeks_left
    field:
        ref = ref as text
''')
    assert "weeks_left" in str(caught.value)
    assert "rather than holding a single bucket" in str(caught.value)


# ------------------------------------------------------------- the refresh --
#
# A clipped span's membership moves with the clock, and the clock is not an
# event -- nothing writes a fact when Monday arrives. What notices is the
# *stamp*: a grouping is rebuilt when what it was built under has changed, and
# for a clipped span that includes the day the pass is running on. So the cron
# this needs is the pass that already runs, and everything downstream comes out
# of the rebuild through the cascade that already exists.
#
# Daily rather than hourly, and the cost is why. A wholesale rebuild is diffless
# -- `replace_index` cannot say whose buckets moved -- so every figure reading a
# rebuilt grouping recomputes outright. An hourly stamp would buy a clip that is
# never more than an hour stale and pay a full recompute of every dependent
# figure, 24 times a day, to move a weekly bar. The lag it trades away is
# bounded by a day, in a chart whose narrowest column is a week.


async def _pass(engine, at_ms: float):
    """One pass with nothing written -- the cron, in other words."""
    return (await engine.run("t1", at_ms=at_ms)).outcome


DAY_MS = 86_400_000.0


async def test_the_clock_alone_rebuilds_a_clipped_span() -> None:
    """No facts written, no definitions changed -- only the day has moved. The
    grouping rebuilds because the day it was built on is part of what it was
    built under, which is what makes a cron out of the pass that already runs.
    """
    engine, _s, _l, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
        at_ms=MID_AUGUST,
    )
    outcome = await _pass(engine, MID_AUGUST + DAY_MS)
    assert "ad_campaign.weeks_left" in outcome.reindexed


async def test_the_same_day_twice_rebuilds_nothing() -> None:
    """The control on the test above: within a day the stamp has not moved, so
    two passes an hour apart do no work. Without this, a stamp wired to the
    instant rather than the day would pass the previous test and rebuild every
    span on every pass for ever -- and, because a wholesale rebuild is diffless,
    recompute every figure over it too.
    """
    engine, _s, _l, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
        at_ms=MID_AUGUST,
    )
    outcome = await _pass(engine, MID_AUGUST + 3_600_000.0)
    assert outcome.reindexed == ()


async def test_an_unclipped_span_never_moves_with_the_clock() -> None:
    """A span with no `excluding ... gone` is a fact about a booking, not a
    claim about the future: its buckets are decided by two dates on the record
    and nothing else. It must not be dragged into the daily rebuild, or every
    board pays for a refresh that cannot change an answer.
    """
    engine, _s, _l, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
        at_ms=MID_AUGUST,
    )
    outcome = await _pass(engine, MID_AUGUST + 30 * DAY_MS)
    # Exact, not `not in`: a refresh disabled outright would leave this empty
    # and satisfy the negative on its own, so the clipped group's presence is
    # what proves the pass did the work it was meant to skip for the other.
    assert outcome.reindexed == ("ad_campaign.any_week", "ad_campaign.weeks_left")


async def test_crossing_the_period_boundary_drops_the_week_that_went() -> None:
    """The whole point of the refresh, end to end: no facts move, a week
    passes, and the board is asking for one week less. A rebuild that fired but
    recomputed nothing downstream would leave the old buckets in place and pass
    every test above this one.
    """
    engine, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", AUG_3, SEP_6)},
        at_ms=MID_AUGUST,
    )
    assert set(await rows(store, library, "ad_account.still_running")) == {
        "a1@2026-W34",
        "a1@2026-W35",
        "a1@2026-W36",
    }
    await _pass(engine, MID_AUGUST + 7 * DAY_MS)
    assert set(await rows(store, library, "ad_account.still_running")) == {
        "a1@2026-W35",
        "a1@2026-W36",
    }, "the week that went is still being counted"


# --------------------------------------------------------------- dividing --
#
# A span puts a record in several buckets; on its own that means the record's
# quantity counts *in full* in every one of them. For a count of what is running
# that is right -- a campaign really is running in all five weeks. For a
# quantity it is five times the truth, and the two need different words.
#
# `spread` is the one that divides: a subject's value, shared evenly across the
# buckets it occupies. Evenly rather than by overlap, so the ends of a span are
# rounded up rather than apportioned -- a bounded error at the two edge buckets,
# in the direction that never hides a peak, against a model whose whole premise
# is already that nobody knows which day the work lands on.

SPREAD = '''
# What this campaign has left to spend.
figure ad_campaign.budget:
    display "{ad_campaign} budget"
    depends:
        mine = ad_campaign.own:{ad_campaign}
    calculate:
        sum(ad_campaign.money over mine)

# The campaign's own weeks, keyed by the campaign rather than the account.
group ad_campaign.my_weeks from (ref, starts_at until ends_at by week excluding weeks gone in "UTC")

# The campaign itself, so its budget has a set to be summed over.
group ad_campaign.own from ref

# What this campaign spends in each week it has left.
figure ad_campaign.weekly_spend bucketed:
    display "{ad_campaign} spend that week"
    depends:
        weeks = ad_campaign.my_weeks:{ad_campaign}
    calculate:
        spread(ad_campaign.budget over weeks)

# Deliberately unanswerable: a total over an empty set, divided by a count of
# nothing. Here because every other figure in this world floors at nought --
# `sum` counts a record it cannot read as nothing -- so an absent value has to
# be constructed to have one at all, and `spread` must not turn it into a
# number.
figure ad_campaign.unknowable:
    display "{ad_campaign} unknowable"
    unit count
    depends:
        mine = ad_campaign.own:{ad_campaign}
        nobody = ad_campaign.own:{ad_campaign} - ad_campaign.own:{ad_campaign}
    calculate:
        sum(ad_campaign.money over mine) / count(nobody)

# Spreading a value nobody can compute.
figure ad_campaign.weekly_unknown bucketed:
    display "{ad_campaign} unknown spend that week"
    depends:
        weeks = ad_campaign.my_weeks:{ad_campaign}
    calculate:
        spread(ad_campaign.unknowable over weeks)

# What the whole account spends in each week.
figure ad_account.weekly_spend bucketed:
    display "{ad_account} spend that week"
    depends:
        live = ad_campaign.weeks_left:{ad_account}
    calculate:
        sum(ad_campaign.weekly_spend over live)
'''

MEASURE = '''
measure ad_campaign.money = budget_cents in count
'''

EFFORT = '''
# The same world in a unit that renders, so a lost unit is visible rather than
# merely wrong in a field nobody reads. An effort prints as "40.0h"; a count
# prints the seconds.
measure ad_campaign.work = budget_cents in effort

# What this campaign has left to spend, in working time.
figure ad_campaign.effort:
    display "{ad_campaign} effort"
    depends:
        mine = ad_campaign.own:{ad_campaign}
    calculate:
        sum(ad_campaign.work over mine)

# That effort, shared across the weeks the campaign has left.
figure ad_campaign.weekly_effort bucketed:
    display "{ad_campaign} effort that week"
    depends:
        weeks = ad_campaign.my_weeks:{ad_campaign}
    calculate:
        spread(ad_campaign.effort over weeks)

# Every campaign's share for the week, added up for the account.
figure ad_account.weekly_effort bucketed:
    display "{ad_account} effort that week"
    depends:
        live = ad_campaign.weeks_left:{ad_account}
    calculate:
        sum(ad_campaign.weekly_effort over live)
'''


def test_dividing_a_quantity_and_adding_it_back_up_keeps_the_quantity_it_was_in():
    """A unit is derived wherever it can be, and both of these can be.

    Sharing an effort across five weeks leaves five efforts, and adding efforts
    gives an effort -- neither construct changes what the number *is*. Left
    underived they took `count`, which is not a field nobody reads: `count` is
    what makes the engine print an effort as its raw seconds, so every figure
    built on a span rendered "144000" where it meant "40.0h". A definition
    cannot paper over it either, because a derived unit is refused as a
    redundant declaration -- so the miss is unfixable from outside this file.
    """
    library = compile_world(MEASURE + SPREAD + EFFORT)
    by_name = {f.name: f for f in library.figures}

    assert by_name["ad_campaign.effort"].unit == "effort", "the control: the source"
    assert by_name["ad_campaign.weekly_effort"].unit == "effort", (
        "a spread share of an effort is an effort"
    )
    assert by_name["ad_account.weekly_effort"].unit == "effort", (
        "a total of efforts is an effort"
    )


def test_a_spread_count_stays_a_count():
    """The other side, so the fix is an inheritance rather than a blanket
    promotion: nothing here has an effort to inherit, and inventing one would
    render a number of campaigns in hours."""
    library = compile_world(MEASURE + SPREAD)
    by_name = {f.name: f for f in library.figures}

    assert by_name["ad_campaign.weekly_spend"].unit == "count"
    assert by_name["ad_account.weekly_spend"].unit == "count"


async def spread_board(campaigns: dict[str, dict[str, object]], *, at_ms: float):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEASURE + SPREAD)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    facts.put("t1", "ad_account", "a1", {"name": "A1", "timezone": "UTC"})
    for key, body in campaigns.items():
        facts.put("t1", "ad_campaign", key, body)
    await engine.run("t1", full=True, at_ms=at_ms)
    return engine, store, library


async def test_a_spread_value_is_shared_across_the_buckets_it_spans() -> None:
    """The division. 1000 across five weeks is 200 a week -- and a plain read
    of a multi-bucket membership would put the whole 1000 in each of them,
    reporting five times the money as though it were a fact."""
    _e, store, library = await spread_board(
        {"c1": {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 1000}},
        at_ms=EARLY_AUGUST,
    )
    found = await rows(store, library, "ad_campaign.weekly_spend")
    assert found == {
        "c1@2026-W32": 200.0,
        "c1@2026-W33": 200.0,
        "c1@2026-W34": 200.0,
        "c1@2026-W35": 200.0,
        "c1@2026-W36": 200.0,
    }


async def test_a_spread_over_one_bucket_is_the_whole_value() -> None:
    """The degenerate case that a divisor read off the wrong thing gets wrong:
    one bucket means no division at all, not a division by the number of
    records or by nought."""
    _e, store, library = await spread_board(
        {
            "c1": {
                **campaign("a1", "2026-08-04T09:00:00Z", "2026-08-06T09:00:00Z"),
                "ref": "c1",
                "budget_cents": 900,
            }
        },
        at_ms=EARLY_AUGUST,
    )
    assert await rows(store, library, "ad_campaign.weekly_spend") == {"c1@2026-W32": 900.0}


async def test_a_spread_divides_by_the_weeks_left_not_the_weeks_booked() -> None:
    """The clip and the division are one question, and this is why they have
    to be: at a pass inside W34 the campaign has three weeks left, so its
    remaining money is spread over three. Dividing by the five it was booked
    over would report a plan as comfortably affordable right up to the day it
    is not."""
    _e, store, library = await spread_board(
        {"c1": {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 900}},
        at_ms=MID_AUGUST,
    )
    assert await rows(store, library, "ad_campaign.weekly_spend") == {
        "c1@2026-W34": 300.0,
        "c1@2026-W35": 300.0,
        "c1@2026-W36": 300.0,
    }


async def test_a_spread_of_an_absent_value_is_absent_not_nought() -> None:
    """An absence is never a zero. A campaign whose budget nobody has recorded
    spends an unknown amount each week, and a nought would read as a campaign
    that costs nothing -- which is a number a reader would act on."""
    _e, store, library = await spread_board(
        {"c1": {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 1000}},
        at_ms=EARLY_AUGUST,
    )
    found = await rows(store, library, "ad_campaign.weekly_unknown")
    assert set(found) == {
        "c1@2026-W32",
        "c1@2026-W33",
        "c1@2026-W34",
        "c1@2026-W35",
        "c1@2026-W36",
    }
    assert all(v is None for v in found.values()), f"an unknown value became a number: {found}"
    # The control: the same campaign's *knowable* spend is a number in the same
    # buckets, so this is not a test that everything is absent.
    assert all(
        v is not None
        for v in (await rows(store, library, "ad_campaign.weekly_spend")).values()
    )


# ------------------------------------------------------------- adding them --


async def test_a_figure_can_be_totalled_across_the_records_in_a_bucket() -> None:
    """The second step, and the one `sum` could not do: the thing being added
    is a *computed* figure per campaign, not a field on a record. Two campaigns
    overlapping in W34 and W35 stack there and stand alone elsewhere."""
    _e, store, library = await spread_board(
        {
            "c1": {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 1000},
            "c2": {
                **campaign("a1", "2026-08-17T09:00:00Z", "2026-08-28T09:00:00Z"),
                "ref": "c2",
                "budget_cents": 400,
            },
        },
        at_ms=EARLY_AUGUST,
    )
    assert await rows(store, library, "ad_account.weekly_spend") == {
        "a1@2026-W32": 200.0,
        "a1@2026-W33": 200.0,
        "a1@2026-W34": 400.0,
        "a1@2026-W35": 400.0,
        "a1@2026-W36": 200.0,
    }


async def test_the_total_reads_each_record_in_its_own_bucket() -> None:
    """The mistake this is the control for: reading the source figure at the
    *subject's* key rather than at the coordinate would give every week the
    same number -- whichever value the campaign happened to store first -- and
    the chart would be flat while looking computed.

    The source has to **vary between a member's own buckets** for that to be
    detectable, which is why this is over impressions rather than over a
    spread. An even division is the same number in all five weeks, so a
    first-value read and a coordinate read agree exactly; a review proved the
    earlier version of this test green against the very bug it names.
    """
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {
            "c1": campaign("a1", AUG_3, "2026-08-14T09:00:00Z"),
            "c2": campaign("a1", "2026-08-17T09:00:00Z", "2026-08-28T09:00:00Z"),
        },
        impressions={
            # c1: one in W32, two in W33. c2: three in W34, one in W35.
            "i1": {"ref": "i1", "campaign_id": "c1", "at": "2026-08-04T09:00:00Z"},
            "i2": {"ref": "i2", "campaign_id": "c1", "at": "2026-08-11T09:00:00Z"},
            "i3": {"ref": "i3", "campaign_id": "c1", "at": "2026-08-12T09:00:00Z"},
            "i4": {"ref": "i4", "campaign_id": "c2", "at": "2026-08-18T09:00:00Z"},
            "i5": {"ref": "i5", "campaign_id": "c2", "at": "2026-08-19T09:00:00Z"},
            "i6": {"ref": "i6", "campaign_id": "c2", "at": "2026-08-20T09:00:00Z"},
            "i7": {"ref": "i7", "campaign_id": "c2", "at": "2026-08-25T09:00:00Z"},
        },
    )
    assert await rows(store, library, "ad_account.shown") == {
        "a1@2026-W32": 1.0,
        "a1@2026-W33": 2.0,
        "a1@2026-W34": 3.0,
        "a1@2026-W35": 1.0,
    }


def test_spreading_something_that_is_not_a_figure_is_refused() -> None:
    with pytest.raises(CheckError) as caught:
        compile_world(MEASURE + SPREAD + '''
# Nonsense: a measure is a field on a record, not a subject's value.
figure ad_campaign.wrong bucketed:
    display "wrong"
    depends:
        weeks = ad_campaign.my_weeks:{ad_campaign}
    calculate:
        spread(ad_campaign.money over weeks)
''')
    assert "ad_campaign.money" in str(caught.value)


def test_spreading_inside_an_unbucketed_figure_is_refused() -> None:
    """Without a sequence there are no buckets to spread across, and the
    natural misreading -- divide by one -- is a figure that silently equals its
    source.

    Over the *unbucketed* group, so the refusal reached is this one. Written
    over the span group instead, the figure is refused earlier for being fanned
    out by a bucketed group without saying `bucketed`, and the assertion below
    matched that other message by accident.
    """
    with pytest.raises(CheckError) as caught:
        compile_world(MEASURE + SPREAD + '''
# No sequence anywhere: neither the figure nor the group it names is bucketed.
figure ad_campaign.flat:
    display "flat"
    depends:
        mine = ad_campaign.own:{ad_campaign}
    calculate:
        spread(ad_campaign.budget over mine)
''')
    assert "no buckets to divide across" in str(caught.value)


# ----------------------------------------------------------- on the page --


async def test_a_clipped_span_tells_a_reader_its_filing_is_from_the_last_pass(
    pg_dsn: str,
) -> None:
    """The caveat an age filter already carries, for the same reason: a
    grouping whose membership moves with the calendar shows the filing the last
    pass drew, and a reader looking at it deserves to be told rather than left
    to wonder why a week they expected is missing.

    Asserted on the **served response**, not on the helpers behind it. Written
    against `_membership_note` and `_spec_clipped` directly it passed with the
    router hardcoded to `clipped=False` -- a test that the helper exists rather
    than that a reader is told, which is the shape the age filter's own test
    (`test_ui.py`) already avoids.
    """
    from tests.test_ui import serve

    source = (
        "# Every week a campaign is booked to run in.\n"
        "group ad_campaign.booked from "
        "(account_id, starts_at until ends_at by week in \"UTC\")\n"
        "# Every week it has left.\n"
        "group ad_campaign.left from "
        "(account_id, starts_at until ends_at by week excluding weeks gone in \"UTC\")\n"
    )
    world = Schema(kinds=frozenset({"ad_campaign"}))
    async with serve(pg_dsn) as http:
        assert (await http.put("/schema", json=world.to_document())).status_code == 200
        put = await http.put("/definitions", json={"source": source})
        assert put.status_code == 200, put.text
        await http.post(
            "/tenants/t1/facts",
            json={
                "writes": {
                    "ad_campaign": {
                        "c1": {
                            "account_id": "a1",
                            "starts_at": AUG_3,
                            "ends_at": SEP_6,
                        }
                    }
                }
            },
        )
        clipped = (
            await http.get("/ui/api/tenants/t1/membership/ad_campaign.left")
        ).json()
        assert clipped["note"] is not None and "already gone" in clipped["note"], (
            "a clipped span's filing is as old as the last pass, and a reader "
            "looking at it must be told so"
        )
        plain = (
            await http.get("/ui/api/tenants/t1/membership/ad_campaign.booked")
        ).json()
        assert plain["note"] is None, "an unclipped span carries no such caveat"


async def test_a_week_passing_redivides_what_is_left(engine_service: None = None) -> None:
    """The feature's headline, end to end and on the *incremental* path: no
    facts move, a week goes by, and the money left re-divides over the weeks
    left. Every other test here runs one full pass; this is the one that shows
    the thing the chart exists for actually happening.
    """
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEASURE + SPREAD)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    facts.put("t1", "ad_account", "a1", {"name": "A1", "timezone": "UTC"})
    facts.put(
        "t1",
        "ad_campaign",
        "c1",
        {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 900},
    )
    await engine.run("t1", full=True, at_ms=MID_AUGUST)
    assert await rows(store, library, "ad_campaign.weekly_spend") == {
        "c1@2026-W34": 300.0,
        "c1@2026-W35": 300.0,
        "c1@2026-W36": 300.0,
    }

    await engine.run("t1", at_ms=MID_AUGUST + 7 * DAY_MS)
    assert await rows(store, library, "ad_campaign.weekly_spend") == {
        "c1@2026-W35": 450.0,
        "c1@2026-W36": 450.0,
    }, "the same money over one week fewer is more money a week"

    await engine.run("t1", at_ms=MID_AUGUST + 21 * DAY_MS)
    assert await rows(store, library, "ad_campaign.weekly_spend") == {}, (
        "a span wholly in the past still has rows"
    )


async def test_a_span_at_quarter_grain_walks_quarters_not_every_third_month() -> None:
    """Quarter is in the spannable grains and carries the fiddliest arithmetic
    in the walk: the cursor has to snap to the quarter's own boundary first, or
    a span starting in February steps February, May, August -- periods three
    months apart that are not quarters. February to July is Q1, Q2 and Q3.
    """
    extra = '''
# Every quarter a campaign runs in.
group ad_campaign.booked_quarters from (account_id, starts_at until ends_at by quarter in ad_account.timezone)

# How many campaigns run in each quarter.
figure ad_account.quarters bucketed:
    display "{ad_account} quarters"
    depends:
        live = ad_campaign.booked_quarters:{ad_account}
    calculate:
        count(live)
'''
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(extra)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    facts.put("t1", "ad_account", "a1", {"name": "A1", "timezone": "UTC"})
    facts.put(
        "t1",
        "ad_campaign",
        "c1",
        campaign("a1", "2026-02-10T09:00:00Z", "2026-07-04T09:00:00Z"),
    )
    await engine.run("t1", full=True)
    assert set(await rows(store, library, "ad_account.quarters")) == {
        "a1@2026-Q1",
        "a1@2026-Q2",
        "a1@2026-Q3",
    }


async def test_a_span_ending_at_the_calendars_edge_answers_rather_than_dying() -> None:
    """`9999-12-31` is how a provider spells "runs for ever", so it arrives
    through the facts door. Stepping a cursor past the last representable day
    raises, and `Engine.run` raises on failure -- so one such record killed the
    whole tenant's pass, on every pass, until somebody edited the record.

    The same overflow this repo has already fixed twice elsewhere; the walker
    added here inherited neither the guard nor a test.
    """
    _e, store, library, _f = await board(
        {"a1": "UTC"},
        {"c1": campaign("a1", "9999-12-01T00:00:00Z", "9999-12-31T00:00:00Z")},
        at_ms=MID_AUGUST,
    )
    found = await rows(store, library, "ad_account.running")
    assert found, "a span at the calendar's edge produced no buckets at all"
    assert "a1@9999-W52" in found


async def test_a_span_with_no_subject_part_is_clipped_too() -> None:
    """The clip is detected on two code paths -- a bare field spec and a
    composite -- and every other group in this file is a composite, so the
    first was unexercised in both the engine's copy and the page's.
    """
    library = compile_world()
    from uratori.engine.engine import _clipped
    from uratori.server.ui import _spec_clipped

    bare = library.indexes["ad_campaign.any_week"]
    assert _clipped(bare) is True
    assert _spec_clipped(bare.spec) is True
    assert _clipped(library.indexes["ad_campaign.booked_weeks"]) is False


# ------------------------------------------------------- what a version is --


def _index_version_of(source: str, name: str) -> str:
    from uratori.engine.engine import _index_version

    return _index_version(compile_world(source).indexes[name])


def test_the_span_and_the_clip_are_both_in_the_version() -> None:
    """A rule that files a record differently is a different rule, and a
    version that could not tell them apart would let a board's whole chart
    change under a hash claiming nothing moved.

    Four spellings of one part, four versions -- and the pair that matters
    most is the last two, which differ only by `excluding weeks gone`: that
    clause is the difference between the weeks a campaign was booked for and
    the weeks it has left.
    """
    base = '''
# One instant, one bucket.
group ad_campaign.probe from (account_id, starts_at by week in ad_account.timezone)
'''
    span = '''
# A span, unclipped.
group ad_campaign.probe from (account_id, starts_at until ends_at by week in ad_account.timezone)
'''
    other_end = '''
# The same span, to a different far end.
group ad_campaign.probe from (account_id, starts_at until booked_at by week in ad_account.timezone)
'''
    clipped = '''
# The same span, clipped.
group ad_campaign.probe from (account_id, starts_at until ends_at by week excluding weeks gone in ad_account.timezone)
'''
    versions = [
        _index_version_of(src, "ad_campaign.probe")
        for src in (base, span, other_end, clipped)
    ]
    assert len(set(versions)) == 4, f"two rules share a version: {versions}"


def test_a_spread_and_a_total_are_not_the_same_calculation() -> None:
    """One divides a value across a sequence and the other adds values up
    across a population, and a hash that confused them would let a figure
    become the other under a version claiming nothing moved."""
    from uratori.lang.ast import FigureTotal, Spread
    from uratori.lang.check import _calc_hash

    spread = _calc_hash(Spread(figure="ad_campaign.budget", set="weeks"))
    total = _calc_hash(FigureTotal(figure="ad_campaign.budget", set="weeks"))
    measure_sum = _calc_hash(Sum(set="weeks", measure="ad_campaign.budget"))
    assert spread != total
    assert total != measure_sum


# ------------------------------------------------- the rest of the refusals --


def test_totalling_a_figure_that_is_not_time_keyed_is_refused() -> None:
    """The shape a reader writes by accident, and the one that used to answer a
    confident nought. A total reads each member at `<member>@<bucket>`; a source
    holding one value per subject is stored under the bare id, so every lookup
    missed and every bucket read 0.0 with nothing thrown.
    """
    with pytest.raises(CheckError) as caught:
        compile_world(MEASURE + SPREAD + '''
# The campaign's whole budget, totalled per week -- which is not a thing.
figure ad_account.flat_total bucketed:
    display "flat"
    depends:
        live = ad_campaign.weeks_left:{ad_account}
    calculate:
        sum(ad_campaign.budget over live)
''')
    assert "one value per subject" in str(caught.value)
    assert "spread(" in str(caught.value), "the message should name the construct meant"


def test_a_name_that_is_both_a_field_and_a_figure_is_refused() -> None:
    """`sum(<dotted>)` can mean a measure, a declared field or a figure, and
    the order between the last two would decide an existing total's meaning --
    except that the collision cannot be declared in the first place.

    This pins the rule the precedence *rests* on rather than the precedence:
    a figure may not take a name its own kind already has as a field. Remove
    that rule and the ordering in `_resolve_field_reads` silently starts
    deciding what `sum(ad_campaign.budget_cents over ...)` means.
    """
    with pytest.raises(CheckError) as caught:
        compile_world('''
# A figure named exactly like a field on its own kind.
figure ad_campaign.budget_cents:
    display "collision"
    unit count
    depends:
        mine = ad_campaign.own_probe:{ad_campaign}
    calculate:
        count(mine)

# The campaign itself.
group ad_campaign.own_probe from ref

# Totalling the ambiguous name.
figure ad_account.ambiguous:
    display "ambiguous"
    unit count
    depends:
        mine = ad_campaign.booked_weeks:{ad_account}
    calculate:
        sum(ad_campaign.budget_cents over mine)
''')
    assert "already has as a field" in str(caught.value)


def test_a_selective_rule_cannot_be_spanned() -> None:
    """A selective rule picks one day a month and is deliberately partial; a
    span enumerates a stretch. The refusal used to be unreachable -- the
    missing-grain rule fired first and told an author who had written `by first
    monday of month` that they had given no grain.
    """
    with pytest.raises(CheckError) as caught:
        compile_world('''
# Nonsense: a partial rule and a stretch.
group ad_campaign.selective from (account_id, starts_at until ends_at by first monday of month in ad_account.timezone)
''')
    assert "selective rule" in str(caught.value)


def test_the_clip_clause_needs_a_span_and_must_name_the_right_grain() -> None:
    """Both halves of the clause's own grammar. The plural is checked against
    the grain rather than accepted as noise: it is the only part a reader can
    use to tell what is being dropped, and `excluding days gone` on a week rule
    reads as a finer promise than the rule can keep.
    """
    from uratori.lang.lex import SyntaxError_

    with pytest.raises((CheckError, SyntaxError_)) as no_span:
        compile_world('''
# A clip with nothing to clip.
group ad_campaign.clip_alone from (account_id, starts_at by week excluding weeks gone in ad_account.timezone)
''')
    assert "only means something for a span" in str(no_span.value)

    with pytest.raises((CheckError, SyntaxError_)) as wrong_plural:
        compile_world('''
# A week rule promising to drop days.
group ad_campaign.clip_mismatch from (account_id, starts_at until ends_at by week excluding days gone in ad_account.timezone)
''')
    assert "excluding weeks gone" in str(wrong_plural.value)


async def test_a_derived_figure_retires_the_buckets_its_source_gave_up() -> None:
    """A figure keyed by a grain with no group of its own -- built on another
    at `:{bucket}` -- has rows exactly where its source has them. When the
    source retires a bucket, the derived row must go too.

    It did not, and it survived a **full** pass: a full pass rebuilds what
    exists rather than removing what does not. The bug was reachable before
    spans (a corrected timestamp refiles a record) and is a certainty with
    them, because a clipped span retires a bucket every period by design. The
    money-in-pence row for a week nobody is working is the shape of it:
    stored, versioned, current, and wrong.
    """
    derived = '''
# The same spend, in whole units -- keyed by its source's buckets and nothing
# else, so it has no group of its own to be retired against.
figure ad_campaign.weekly_units bucketed:
    display "{ad_campaign} units that week"
    unit count
    calculate:
        ad_campaign.weekly_spend:{bucket} / 100
'''
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world(MEASURE + SPREAD + derived)
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    facts.put("t1", "ad_account", "a1", {"name": "A1", "timezone": "UTC"})
    facts.put(
        "t1",
        "ad_campaign",
        "c1",
        {**campaign("a1", AUG_3, SEP_6), "ref": "c1", "budget_cents": 900},
    )
    await engine.run("t1", full=True, at_ms=MID_AUGUST)
    assert set(await rows(store, library, "ad_campaign.weekly_units")) == {
        "c1@2026-W34",
        "c1@2026-W35",
        "c1@2026-W36",
    }

    await engine.run("t1", at_ms=MID_AUGUST + 7 * DAY_MS)
    assert set(await rows(store, library, "ad_campaign.weekly_units")) == {
        "c1@2026-W35",
        "c1@2026-W36",
    }, "a derived row outlived the bucket its source was retired from"


def test_both_ends_of_a_span_are_named_where_a_host_reads_its_dependencies() -> None:
    """The declaration payload tells a host which fields moving would refile a
    grouping's records. A span's far end decides its last bucket exactly as the
    near end decides its first, so listing only the near one described a
    grouping that changing a due date would not affect -- which is the opposite
    of true.
    """
    from uratori.lang.check import _index_fields

    library = compile_world()
    parts = _index_fields(library.indexes["ad_campaign.weeks_left"].spec)
    named = [
        field
        for part in parts
        for field in ((part.field, part.until) if part.until else (part.field,))
    ]
    assert named == ["account_id", "starts_at", "ends_at"]


# ------------------------------------------------- the warm path, downstream --


async def test_a_warm_pass_carries_a_changed_value_into_the_buckets_it_spreads_over() -> None:
    """A budget that doubles must double every week it is spread across, on the
    ordinary pass rather than at the next full reconcile.

    This is the shape a `spread` introduced and nothing else in the language
    has: a **bucketed reader of an unbucketed writer**. The pass marks a
    figure's readers stale in the reader's own subject space, and the two
    shapes it knew were a roster-keyed reader of a coordinate (take the base)
    and a sequenced reader of a sequenced writer (the coordinate passes
    through). Here the writer has one value per campaign and the reader has
    one per campaign-week, so the coordinate has to be *invented* from the
    index -- and marking the bare campaign key instead left every week holding
    the number it was built with.

    A stale value is worse than a missing one on a board whose whole claim is
    that a figure moves when the records behind it do. Nothing was wrong on
    screen: the weeks simply went on saying what they said yesterday, and a
    full reconcile quietly corrected them hours later.
    """
    engine, store, library = await spread_board(
        {"c1": {**campaign("a1", AUG_3, SEP_6, 1000), "ref": "c1"}}, at_ms=EARLY_AUGUST
    )
    before = await rows(store, library, "ad_campaign.weekly_spend")
    assert set(before.values()) == {200.0}, "the fixture: 1000 over five weeks"

    engine._facts.put(
        "t1", "ad_campaign", "c1", {**campaign("a1", AUG_3, SEP_6, 2000), "ref": "c1"}
    )
    await engine.run("t1", written={"ad_campaign": ["c1"]}, at_ms=EARLY_AUGUST)

    assert set((await rows(store, library, "ad_campaign.weekly_spend")).values()) == {400.0}, (
        "the spread kept its old share after the value it divides doubled"
    )


async def test_a_warm_pass_carries_a_spread_into_the_total_above_it() -> None:
    """And on into the account, which is the second broken edge.

    A total over a set is **cross-scope by construction** -- the account's
    figure reads its campaigns' -- so the writer's coordinate (`c1@2026-W33`)
    is not a subject the reader has. Passed through unchanged it named a row
    that does not exist, so the account's own week was never recomputed and
    the two answers disagreed: the campaign's weeks said 400 while the
    account's still totalled 200.

    Asserted separately from the campaign's own weeks above because the two
    are different edges and a fix for one is not a fix for the other -- the
    first cut of this repair corrected the campaign and left the account
    stale, which is the reading a screen actually shows.
    """
    engine, store, library = await spread_board(
        {"c1": {**campaign("a1", AUG_3, SEP_6, 1000), "ref": "c1"}}, at_ms=EARLY_AUGUST
    )
    assert set((await rows(store, library, "ad_account.weekly_spend")).values()) == {200.0}

    engine._facts.put(
        "t1", "ad_campaign", "c1", {**campaign("a1", AUG_3, SEP_6, 2000), "ref": "c1"}
    )
    await engine.run("t1", written={"ad_campaign": ["c1"]}, at_ms=EARLY_AUGUST)

    assert set((await rows(store, library, "ad_account.weekly_spend")).values()) == {400.0}, (
        "the account's weekly total ignored a campaign share that doubled"
    )
