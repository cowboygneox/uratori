# 8. The target that moves

> **The question.** *Our first-reply promise was two hours until May, when we
> tightened it to one. Were we inside the target that was actually in force in
> March? And in May?*

This is the question that breaks most reporting, and it breaks it in a
particular way: somebody looks up "the target" -- singular, current -- and
judges every month against it. March gets marked against a promise the desk
had not made yet.

## The shape of the data

The desk records **changes**, not states. One record each time somebody moves
a number, and nothing at all in between:

```fig
# One change to one service-level target, by one person, at one moment.
# Nothing is written in the months nobody moves a target.
fact desk_target_change:
    name setting
    team_id as text
    setting as text
    value as number
    set_at as moment
    set_by as text
```

Four records exist, in [the dataset](../tutorial.md#target-changes):

| | team | setting | value | set at |
|---|---|---|---|---|
| tc-1 | tm-front | first_reply_target | 7200 | 2026-03-05 |
| tc-2 | tm-front | resolve_target | 172800 | 2026-03-05 |
| tc-3 | tm-deep | first_reply_target | 3600 | 2026-04-02 |
| tc-4 | tm-front | first_reply_target | 3600 | 2026-05-11 |

The values are in **seconds**, because a duration in this engine is seconds.
7200 is two hours; 3600 is one. Nothing in the record says so -- a `fact` is
structural -- and in a moment a definition will say it.

This shape is everywhere once you look: a price, a staffing level, a quota, a
facility setting. Sparse by nature, and every screen wants the opposite shape.

## Four declarations, each answering exactly one question

```fig
# Just the changes to the first-reply target; the same stream carries the
# resolve target too.
filter desk_target_change.reply_target where setting == "first_reply_target" label "first-reply target changes"

# Every target change, filed under the team it is about and the month it was
# made in.
group desk_target_change.by_month from (team_id, set_at by month in desk_team.timezone) label "set for this team that month"

# The first-reply target in force for this team each month: the last one set
# in the month, carried across every month nobody changed it. Months before
# anybody set one report nothing, because nothing was in force.
figure desk_team.reply_target_month bucketed:
    display "{desk_team} first-reply target"
    unit duration

    depends:
        sets = desk_target_change.by_month:{desk_team} & desk_target_change.reply_target

    calculate:
        latest(desk_target_change.value over sets) carried forward
```

**The factoring is the design**, and it is worth pausing on, because it is how
you will want to model every on-change stream you meet.

- The **group** is deliberately metric-agnostic: one grouping per stream,
  keyed by subject and month. It knows nothing about which setting is which,
  so it serves every setting the stream will ever carry.
- The **filter** owns the narrowing, because deciding which records are in
  play is what a filter is for.
- The **figure's name** is the only place "this setting, monthly" is claimed --
  and it is backed by the two parts it visibly intersects.

Put the setting's name in the group and you need a group per setting. Put the
month in the filter and you have a filter that knows about calendars.

## `latest` reading a field

`latest(desk_target_change.value over sets)` reads **the value the most recent
record in that bucket set it to**. No measure in the way: a
`measure desk_target_change.goal = value in count` would be a second name for
one field, written only to satisfy the grammar.

Two things follow:

- **Which record is "latest" is not a second thing to declare.** The group
  already said `set_at by month`; that field is when the change happened and it
  is the only ordering in sight. Naming it again would be a second place for
  the two to disagree -- and the disagreement would be silent, reporting a
  superseded value with nothing thrown. (Ties break on the record key:
  arbitrary but stable, because two changes stamped at the same instant are a
  data problem, and answering them differently on each pass would be a number
  that moves with nothing behind it.)
- **A field read must declare its unit.** `unit duration` is that declaration,
  and it cannot be derived: `value as number` claims a shape and never a
  meaning, so nothing but this line can say the 7200 is seconds of wall-clock
  time.

> **The trap this closes.** Suppose the desk had recorded its target in
> *minutes* -- `"value": 120` for two hours. The figure would still compile,
> still store, still render: `120` seconds, `2m`. Every month would look
> spectacularly inside target, by a factor of sixty, for ever. The unit
> declaration is the place somebody has to be right, which is why the language
> makes you write it rather than guessing.

## What `carried forward` does

| Month | Front Desk | why |
|---|---|---|
| February | *nothing* | nobody had set anything |
| March | 2.0h | tc-1 landed here -- an **anchor** |
| April | 2.0h | carried |
| May | 1.0h | tc-4 landed here -- a new anchor |
| June | 1.0h | carried |

And for Escalations, whose target was not set until April:

| Month | Escalations |
|---|---|
| March | *nothing* |
| April | 1.0h |
| May | 1.0h |
| June | 1.0h |

**Before the first anchor there is an absence, never a nought.** March at
Escalations is not "a target of zero seconds". Nothing had been promised. And
a nought would sit comfortably under every threshold on every screen, so a
band would have coloured March green for a promise that did not exist.

**Each carried row cites the change it carried from.** June's evidence is
tc-4: its value, when it was set, and who set it. A carried month holds no
records of its own, so a naive chain would cite an empty bucket and dead-end
exactly where the reader started asking.

### Why a carried row may be stored at all

[Chapter 5](05-measuring.md) was firm that a stored value may never read the
clock. A carried row looks like it does -- the sequence grows as time passes --
so it is worth saying why it does not.

Split the question in two. **What June's answer is** follows entirely from the
anchors at or before June; it is the same answer for ever, once June exists.
**Whether June exists yet** is a different question, and it is the one that
moves with the clock. The value is time-invariant; only the extent of the
sequence is not. That split is what makes this legal where `now - opened_at`
is not, and it generalises: before deciding something cannot be written down
because it moves with the clock, split it and check which half actually does.

Three things extend a carried figure, and one implementation serves all three
so the rows are byte-identical whichever asked:

- **A change landing** makes its month an anchor and recomputes forward from
  there. Months *before* it are untouched -- a change entered late but dated in
  April rewrites April onward and leaves March exactly as it was. History is
  never rewritten by a later arrival, which is precisely the property that
  makes these rows safe to store.
- **A pass.** Every pass extends every carried figure up to the month it runs
  in. The pass is the event that notices time; the clock never is one.
- **A read** that finds an unmaterialised month materialises it and serves it,
  so a screen between passes is never told "never computed" about a value that
  has demonstrably been in force for months.

> On your own copy the sequence will run past June into whatever month you run
> it in, at `1.0h` all the way. That is the same rule doing its job: the pass
> noticed time.

## Comparing two sequences

Now the real question. Here is what the desk actually did, month by month:

```fig
# The middle first-reply wait in this team's queue each month, over the
# tickets somebody actually answered.
figure desk_team.median_reply_month bucketed:
    display "{desk_team} typical first reply"

    depends:
        answered = desk_ticket.replied_by_team_month:{desk_team}

    calculate:
        median(desk_ticket.first_reply_seconds over answered)

    band:
        when value > desk_team.reply_target_month:{bucket} then "over target"
        otherwise "within target"
```

| month | Front Desk | band | Escalations | band |
|---|---|---|---|---|
| 2026-03 | 50m | within target | 30m | *no word* |
| 2026-04 | 40m | within target | 1.2h | **over target** |
| 2026-05 | 1.2h | **over target** | 28m | within target |
| 2026-06 | 30m | within target | 3.3h | **over target** |

Read the Front Desk row for May against March. The number went *up* --
50 minutes to 1.2 hours -- and it crossed, but not only because the desk got
slower. The promise tightened in the same month. A screen judging both months
against today's one-hour target would have marked March as fine (it was) and
would have marked April as fine too, by accident: April's 40 minutes beat both
the old target and the new one. The month where the judgement genuinely
differs is what this construct exists to get right.

Then look at Escalations in March: **30 minutes, and no word at all.** The
number is real. There was no target that month, so there is nothing to compare
it against, and **an absent threshold withholds the word** exactly as an absent
value does. A band is a claim, and there is no claim to make here.

### `:{bucket}` -- joining by period, never by position

`desk_team.reply_target_month:{bucket}` reads that other figure **at the same
coordinate**. The subject being evaluated already *is* a coordinate
(`tm-front@2026-05`), so this is a lookup under the same key.

A sequenced figure's bare name is refused in an expression, and the refusal is
doing real work. Written plain it would read like a single static value when
it is a point in time; worse, with two sequences in one expression nothing
would say the arithmetic is per period, so the obvious implementation is to
line the two lists up side by side -- which is right until one of them starts a
month later than the other, and then every number is paired with the wrong
month, plausibly, for ever. Here misalignment is not representable. A month
one side holds and the other does not answers **an absence** there -- never a
nought and never a shift.

```fig
# How far this team's typical first reply ran over or under the target that
# was in force that same month.
figure desk_team.reply_gap_month bucketed:
    display "{desk_team} against target"
    unit duration

    calculate:
        desk_team.median_reply_month:{bucket} - desk_team.reply_target_month:{bucket}
```

| month | Front Desk | Escalations |
|---|---|---|
| 2026-03 | -1.2h | *nothing* |
| 2026-04 | -1.3h | 12m |
| 2026-05 | 15m | -32m |
| 2026-06 | -30m | 2.3h |

Escalations' March is nothing, because one side of the subtraction is nothing.
Not zero. Not "on target".

The same thing happens at the other end, and on your own copy you will see it:
the target carries forward into July, August and September, but nobody
answered a ticket in those months, so the median has no bucket there -- and
the gap reads nothing, month after month, rather than reporting the desk as
suddenly, dramatically inside target.

There is deliberately **no `:{bucket - 1}`** or any other offset. A stored value
whose answer needs a period outside the range in view cannot be checked against
the response that carries it -- a reader is shown a number and, one month back,
nothing to check it against. The change between adjacent periods is a question
for the read, where the range bounds the answer, and
[chapter 9](09-reading-it-back.md) asks it.

## A statistic over a period's own records

`median(...)` inside a `bucketed` figure is allowed, and outside one it is
refused. The difference is whether the population is declared.

Inside, the population is *this month's records for this team*, and the
boundary was written in the group, so "the median first reply at the Front
Desk in May" is a sentence with a population a reader can go and check. That
declared boundary is what turns a statistic into a claim.

Outside a period bucket the population would be everything ever collected, so
the number drifts with the age of the data and nobody can say what it is a
median *of*.

Check May's Front Desk median by hand. Seven Front Desk tickets got their
first reply during May:

| ticket | opened | first reply | wait |
|---|---|---|---|
| T-119 | 05-08 08:50 | 09:05 | 15m |
| T-120 | 05-12 10:30 | 10:50 | 20m |
| T-125 | 05-28 09:30 | 10:30 | 1h |
| T-124 | 05-26 10:00 | 11:15 | **1.25h** |
| T-118 | 05-06 14:00 | 16:30 | 2.5h |
| T-121 | 05-14 11:05 | 15:05 | 4h |
| T-123 | 05-21 13:40 | 05-22 09:40 | 20h |

Seven values, so the middle one is the fourth: **1.25h**, which renders
`1.2h`. The target in force that month was one hour, so the band reads *over
target* -- and the gap figure says by how much: 15 minutes.

Two tickets are missing from that list on purpose. T-135 and T-136 have no
first reply at all, so there is no wait to put in the median. They are not
dragging it up; they are not in it. The number is correct and it is also
incomplete on its own, which is why
[`desk_customer.never_answered`](06-absence-and-age.md) belongs on the same
screen.

---

Next: [9. Reading it back](09-reading-it-back.md)
