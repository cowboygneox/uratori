# 9. Reading it back

> **The question.** *Show me the last four months. And the four before those.
> And how much of it is real, given half those months are empty?*

[Chapter 7](07-days-and-dimensions.md) stored a value per agent per month. A
**reading** is how you ask a question *over a range of them*.

Everything else about a reading is the same shape as a figure: a required
explanation, a display template, a version that is the hash of what it means.
The claim is the same claim -- this number has a written definition and you
can cite it. The one difference is that a figure is stored and a reading is
worked out when you ask.

## A window is positions, not dates

```fig
# Closes over the window against the target for the same months.
reading desk_agent.closes_monthly(range):
    display "{desk_agent} closes, month by month"

    band on sum:
        when value < desk_agent.closes_target_month then "under"
        otherwise "met"

    depends:
        months = desk_agent.closed_month in range

    calculate:
        sum(months)
        series(months)
```

Asked as `?trailing=4&at=2026-06-30`:

| agent | months | total | per month | band |
|---|---|---|---|---|
| Mira Halloran | 2026-03..06 | 7 | 2, 3, 2, -- | **under** |
| Tomas Beck | 2026-03..06 | 6 | 2, 1, 2, 1 | **under** |
| Nour Aziz | 2026-03..06 | 6 | 1, 2, 2, 1 | **under** |
| Sindre Vik | 2026-03..06 | 3 | 1, 1, 1, -- | **under** |
| Pell Okonkwo | 2026-03..06 | 3 | 2, 1, --, -- | **met** |

**`range` is the only argument a reading can take, and it may only narrow the
population.** It picks which stored months take part. It cannot change the
statistics, the minimum sample or the band -- those are what the number
*means*, so they are written here and hashed here.

**The argument is integers and nothing else.** `4` means the last four buckets
of whatever the source figure's own sequence is: four months here, four days
over a day figure, four quarters over a quarter figure. There are no units, no
dates and no calendar words in the question, because what a bucket *is* was
decided in the group and hashed there.

**Dates appear in answers, never in questions.** The response says which
concrete months `4` resolved to -- `2026-03` to `2026-06` -- and in whose
calendar, because a calendar is a field on each subject's record and two rows
reading "the last four months" can be four different sets of days.

Three spellings exist:

| written | means |
|---|---|
| `4` | the trailing four buckets, pooled into **one** window |
| `5-8` | the four before those, still one window |
| `each 1-12` | twelve **separate** one-bucket windows, in order -- this month against each of the eleven before it |

## The band: a goal read over the same window

```fig
    band on sum:
        when value < desk_agent.closes_target_month then "under"
        otherwise "met"
```

`on sum` says which statistic the verdict is about -- and `desk_agent.closes_target_month`
is a figure, stored per agent per month, holding what each agent is aiming at:

```fig
# The number of tickets this agent is expected to close in a month, carried
# beside the months themselves so a window can compare like with like.
figure desk_agent.closes_target_month bucketed:
    display "{desk_agent} is aiming at {value} that month"
    unit count

    depends:
        done = desk_ticket.closed_by_month:{desk_agent}

    calculate:
        desk_agent.closes_target
```

The rule that makes the comparison honest: **the threshold is read over the
same window, through the same statistic.** `on sum` bands the window's total,
so the goal is totalled across the identical months. Any other rule compares a
span against a point -- four months of closes beside one month of target --
which reads perfectly plausibly and is wrong by the length of the window.

Pell is the interesting row. His target is 1 a month, and he has target months
for March and April (the two months he closed anything). Summed, that is 2. He
closed 3. **Met.** Mira's target is 3 a month over three months: 9, against 7
closed. **Under.**

It follows that the goal must be time-keyed at the source figure's own grain,
and the build refuses anything else: a window is a span of *that figure's*
buckets, so a goal cut a different way would have labels the window never
selects, and every row would band unknown -- which reads as missing data
rather than as a broken definition.

## The statistics, and why it is a closed list

There are nine, and no expression grammar: `mean`, `median`, `worst`, `sum`,
`count`, `per_bucket`, `percentile`, `series`, `delta`. Each is a claim about
a distribution that a reader has to be able to check against the evidence, and
an arbitrary formula is not checkable by anybody who is not already reading
the code.

Several of the rules around them are about the same thing: **not putting two
numbers on a screen that a reader could combine into a third that nothing
claims.**

- **A sum may not sit beside a distribution.** Give somebody a total and a
  mean and they will divide them, and the answer is a number no definition
  ever made a claim about.
- **`count` is live-only.** A windowed reading already reports its sample.
- **One `series` and one `delta` per reading**, because the response carries
  one of each.

### `sum` is the only statistic a count figure gets

`desk_agent.closed_month` stores counts, and a mean of them is refused. This
looks pedantic until you say the sentence out loud: *the mean of those daily
counts* is a mean **per day** wearing a label that says per record. It is a
plausible number of roughly the right magnitude, which is the worst kind of
wrong. A sum of counts is just a count.

### `per_bucket` divides by the window; `mean` divides by the evidence

This is the distinction to get right before reaching for either, because both
are honest, they are usually different numbers, and neither is a default.

Our days are sparse. Nobody closes something every day, and a day with nothing
in it is never written. So over any window, a figure's sequence has holes.

```fig
# Closes per day of the window, counting the days nobody closed anything --
# a different question from the average of the days they did.
reading desk_agent.closes_per_day(range):
    display "{desk_agent} closes per day of the window"

    depends:
        days = desk_agent.closed_day in range

    calculate:
        per_bucket(days)
```

Asked with `?trailing=7&at=2026-06-05` -- the week ending 5 June:

| agent | buckets covered | per_bucket |
|---|---|---|
| Nour Aziz | 1 of 7 | 0.14 |
| Tomas Beck | 1 of 7 | 0.14 |

Each closed one ticket that week. `per_bucket` spreads it across all seven
days the window asked for: *"per day of the week."* A `mean` over the same set
would divide by the one day they were at it and answer `1.0`: *"on a day they
closed something."* Both true, completely different questions, and only the
definition can say which was meant.

The divisor is `buckets_requested`, which the window reports beside
`buckets_covered`, so the division a reader would otherwise have to take on
trust is one they can check against the very response that carried it. It is
also the one distribution statistic allowed over a count figure -- for exactly
the reason the others are not: it says *per bucket* in its name.

### `series` and `delta` draw against one axis

```fig
# How this team's typical first reply moved, month over month.
reading desk_team.reply_trend(range):
    display "{desk_team} first reply, month by month"

    depends:
        months = desk_team.median_reply_month in range

    calculate:
        series(months)
        delta(months)
```

`?trailing=4&at=2026-06-30`:

| | 2026-03 | 2026-04 | 2026-05 | 2026-06 |
|---|---|---|---|---|
| Front Desk, series | 50m | 40m | 1.2h | 30m |
| Front Desk, delta | *none* | -10m | +35m | -45m |
| Escalations, series | 30m | 1.2h | 28m | 3.3h |
| Escalations, delta | *none* | +42m | -45m | +2.9h |

`series` returns one point per bucket of the source figure's own sequence, so
a sparkline is a definition's answer rather than a client slicing a range into
ten and computing ten averages. A bucket that stored nothing is a **hole**,
never a nought.

`delta` is the change *into* each bucket: same order, same count, so the two
draw against one axis. Four buckets produce three changes, and the cell with
none is **the oldest bucket in the range**, which says so rather than being
omitted.

That first empty cell is the whole discipline of the construct, and it is
worth understanding because the "fix" is so tempting. You could fetch February
and difference March against it. You would get a fuller chart and one value in
it that the response cannot account for: a number about a month the response
does not contain, which a reader checking the answer against its own evidence
would come up short on.

A hole breaks the chain in **both** directions too -- the change *into* a
missing bucket and the change *out of* it are equally unknowable. Bridging the
gap (differencing May against March because April is missing) reports a
two-month movement in a column headed per-month, and does it exactly where
collection was patchy. A flat run reads a real nought, which is a finding
("it did not move") and not the same as not knowing.

### `requires` -- withholding rather than misleading

```fig
# How long this agent's customers waited for a first answer over the window.
reading desk_agent.reply_pace(range):
    display "{desk_agent} first-reply wait"

    band on median:
        when value > 2 hours then "slow"
        otherwise "ok"

    depends:
        waits = desk_agent.reply_waits_day in range

    requires:
        at least 3 values in waits

    calculate:
        mean(waits)
        median(waits)
        worst(waits)
        percentile 90 of waits
```

`?trailing=30&at=2026-06-30` -- so, June:

| agent | sample | mean | median | worst | p90 | band |
|---|---|---|---|---|---|---|
| Mira Halloran | 3 | 1.3h | 30m | 3.0h | 3.0h | ok |
| Nour Aziz | 2 | *withheld* | | | | |
| Tomas Beck | 2 | *withheld* | | | | |
| Pell Okonkwo | 1 | *withheld* | | | | |
| Sindre Vik | 1 | *withheld* | | | | |

Four of the five rows come back with no statistics and a stated reason:
*"needs at least 3 values; there are 2."*

`requires` is a precondition on the sample, not a filter on it. When it fails,
**every statistic is withheld together** -- because a worst case printed alone
is, by construction, the outlier -- and the response names which requirement
fell short, rather than showing a dash whose reason lives in a constant nobody
can see.

Write nothing and a windowed reading with a distribution statistic still gets
a floor of *at least 1 value*, injected into the plan and hashed exactly as a
written clause would be, because a floor applied at read time would let two
engines render the same version differently. A `sum` takes no default -- a sum
of nothing is a real nought, and nought renders.

### `percentile` reads as words

`percentile 90 of waits` -- the rank first, then `of`, then the set. Every
other statistic is a plain claim about a whole distribution; this one is a
family of claims indexed by a number, so it is written as a phrase rather than
a call.

The value is the **nearest-rank** value: the sample's values, sorted, at
position `ceil(rank/100 x n)` counting from one. Never an interpolation. A
reader auditing a p90 can point at the one record it came from, where the
interpolated variants some libraries default to answer a number no record
holds.

Mira's three June waits are 25 minutes (T-129), 30 minutes (T-130) and 3 hours
(T-128). Ceiling of 0.9 x 3 is 3, so the p90 is the third: 3 hours -- the same
as the worst, which is exactly right for a sample of three and is the kind of
thing a floor of three values is there to make you notice. Her mean is 1.3h
and her median is 30m, which is the same sample saying two different true
things: one long wait pulls the mean and leaves the middle alone.

### A threshold about time says what scale it is in

```fig
        when value > 2 hours then "slow"
```

The figure being judged answers a duration, which is stored in seconds. A bare
`2` would be two seconds. The scales are `seconds`, `minutes`, `hours`, `days`
and `weeks`, and the clause is required here and refused anywhere the unit
already says what the number is -- `3 days` beside a count of tickets would be
a second claim about what the number measures.

It is a *spelling* rather than a meaning: `2 hours` and `7200 seconds` fold to
the same thing before anything is hashed, so they are one definition with one
version.

## The same records, three ways

Here is the pattern that makes the day/month/quarter split worth it. One event
stream, three declared grains, three readings over them:

| ask | window | answers |
|---|---|---|
| `desk_agent.closes_daily?trailing=7&at=2026-06-05` | 7 days | Tomas: total 1, series `-,-,-,-,-,1,-` |
| `desk_agent.closes_monthly?trailing=4&at=2026-06-30` | 4 months | Tomas: total 6, series `2,1,2,1` |
| `desk_agent.closes_quarterly?trailing=2&at=2026-06-30` | 2 quarters | Tomas: total 6, series `2,4` |

(`at=` anchors the window on that date's end instead of on now, which is how
you reproduce this tutorial's tables on a copy that is running today. It moves
nothing that is stored -- it only decides which stored buckets the window
selects.)

The daily series over that week is five holes and one `1`. Not five zeroes --
Tomas did not close nothing on those days in the sense of a measurement; there
is simply no bucket, because nothing happened to make one.

Quarter boundaries and year straddles are handled by the calendar, because the
labels *are* the calendar's: `2026-Q1`, `2026-Q2`. And all three agree about
which period a ticket is in, because every label comes from the same zoned
local day.

## Live: what is happening right now

The second kind of reading measures records **as they stand at the moment you
ask**, and stores nothing:

```fig
# What is waiting on this agent right now, and how long the worst of it has
# been waiting. Measured against the clock at the moment somebody asks, so
# nothing here is stored.
reading desk_agent.waiting_now():
    display "{desk_agent}'s queue, right now"

    depends:
        waiting = desk_ticket.waiting_seconds over (desk_ticket.handled_by:{desk_agent} & desk_ticket.open)

    calculate:
        count(waiting)
        worst(waiting)
```

This is the one construct that may name a clock measure, and it is safe for
one reason: nothing is stored. Whether a ticket is still open is decided by
things that *happened*; only the wait itself runs to `now`. Splitting those two
halves is the same trick `carried forward` used in the last chapter.

**A live reading takes no argument**, and the empty `()` is what says so. There
is no range because nothing is stored to pick from. The argument list and the
source form encode the same fact twice on purpose: written `(range)` over a
live source, a reading would accept a window, ignore it, and answer today's
number under a heading saying thirty days.

> **Honesty note.** Live readings compile, are checked and are versioned, but
> the engine does not serve them yet: asking for one answers a stated
> *not-yet*, not a wrong number. The construct is in this tutorial because it
> is part of the language and its rules are enforced today. The serving path is
> the missing half. The same question, answered from stored values, is
> [the projection in chapter 10](10-the-queue-as-a-page.md).

## One more rule

**A reading may only read a figure, never another reading.** Composing them is
how a team number becomes a mean of means, weighting each person equally
instead of each record -- which is a genuinely different number, always looks
reasonable, and is nobody's intended question.

## See it

A reading's page in the UI draws one column per **declared** statistic -- the
column set is the definition's, stable even when a window is withheld -- with
the band column sitting beside exactly the statistic the band judges, and a
declared `series` drawn as bars in a column of its own. Where the answer has
no single calendar, each window's date bounds carry whose calendar they are
in.

---

Next: [10. The queue as a page](10-the-queue-as-a-page.md)
