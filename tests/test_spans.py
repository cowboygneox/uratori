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
    budget_cents as number

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
):
    facts = MemoryFactStore()
    store = MemoryEngineStore()
    library = compile_world()
    engine = Uratori(schema=WORLD, library=library, store=store, facts=facts)
    for key, zone in accounts.items():
        facts.put("t1", "ad_account", key, {"name": key.upper(), "timezone": zone})
    for key, body in campaigns.items():
        facts.put("t1", "ad_campaign", key, body)
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
    across = "2026-08-02T13:00:00Z"
    _e, store, library, _f = await board(
        {"a1": "Europe/London", "a2": "Pacific/Auckland"},
        {
            "c1": campaign("a1", across, "2026-08-05T09:00:00Z"),
            "c2": campaign("a2", across, "2026-08-05T09:00:00Z"),
        },
    )
    found = await rows(store, library, "ad_account.running")
    assert found.get("a1@2026-W31") == 1.0, f"London's Sunday start was lost: {found}"
    assert found.get("a1@2026-W32") == 1.0, f"London's span is short: {found}"
    assert "a2@2026-W31" not in found, f"Auckland was given a week it never ran: {found}"
    assert found.get("a2@2026-W32") == 1.0, f"Auckland's span is wrong: {found}"


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
    """The rule an age filter is already behind. Membership that moves with the
    clock is as fresh as the last reconcile, and no pointer covers a group only
    a `from` reads -- so the page would change under a reader with nothing to
    rebuild it."""
    with pytest.raises(CheckError) as caught:
        compile_world('''
# A page of campaigns, which may not take its population from a span.
projection ad_campaign.sheet:
    from ad_campaign.weeks_left
    field:
        ref = ref as text
''')
    assert "weeks_left" in str(caught.value)


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
    assert "ad_campaign.booked_weeks" not in outcome.reindexed


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
        at_ms=1_785_758_400_000.0,  # 2026-08-02, before the span begins
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
        at_ms=1_785_758_400_000.0,
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
        {
            "c1": {
                "ref": "c1",
                "account_id": "a1",
                "starts_at": AUG_3,
                "ends_at": SEP_6,
            }
        },
        at_ms=1_785_758_400_000.0,
    )
    found = await rows(store, library, "ad_campaign.weekly_spend")
    assert set(found) == {
        "c1@2026-W32",
        "c1@2026-W33",
        "c1@2026-W34",
        "c1@2026-W35",
        "c1@2026-W36",
    }
    assert all(v is None for v in found.values()), f"an unknown budget became a number: {found}"


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
        at_ms=1_785_758_400_000.0,
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
    """
    _e, store, library = await spread_board(
        {
            "c1": {**campaign("a1", AUG_3, "2026-08-14T09:00:00Z"), "ref": "c1", "budget_cents": 200},
            "c2": {
                **campaign("a1", "2026-08-17T09:00:00Z", "2026-09-04T09:00:00Z"),
                "ref": "c2",
                "budget_cents": 900,
            },
        },
        at_ms=1_785_758_400_000.0,
    )
    found = await rows(store, library, "ad_account.weekly_spend")
    # c1: 200 over W32-W33 -> 100 each. c2: 900 over W34-W36 -> 300 each.
    assert found == {
        "a1@2026-W32": 100.0,
        "a1@2026-W33": 100.0,
        "a1@2026-W34": 300.0,
        "a1@2026-W35": 300.0,
        "a1@2026-W36": 300.0,
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
    source."""
    with pytest.raises(CheckError) as caught:
        compile_world(MEASURE + SPREAD + '''
# No `bucketed`, so there is no sequence to divide across.
figure ad_campaign.flat:
    display "flat"
    depends:
        weeks = ad_campaign.my_weeks:{ad_campaign}
    calculate:
        spread(ad_campaign.budget over weeks)
''')
    assert "bucketed" in str(caught.value)
