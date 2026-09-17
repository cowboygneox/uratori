# 7. Days and dimensions

> **The question.** *How many did each agent close in May? And how does that
> compare with March? And is the phone queue different from email?*

Everything so far has answered *right now*. A figure held one value per
subject and that value moved as facts moved. History was not kept, because
nothing asked for it.

These questions do ask for it, and they ask for it in a particular way:
**cut into periods**.

## A bucket of time is declared, never chosen at read time

```fig
# The same records, cut into calendar months.
group desk_ticket.closed_by_month from (assigned_login through desk_agent.logins.handle, resolved_at by month in desk_agent.timezone) label "closed by this agent that month"
```

The parentheses make this a **composite** grouping: each bucket is keyed by
two things at once, written `ag-mira@2026-05`. The first part is the subject,
exactly as before, identity hop and all. The second part is a moment reduced
to a period.

Seven grains exist, from sub-day to quarter:

```
by minute        2026-05-08T14:30      by week     2026-W19  (ISO weeks)
by 15 minutes    2026-05-08T14:30      by month    2026-05
by hour          2026-05-08T14:00      by quarter  2026-Q2   (calendar quarters)
by day           2026-05-08
```

The reason the grain is written *here*, in a declaration, rather than passed
in with the request, is the most important structural decision in this part of
the language. The grain decides **what a stored number means**. "Mira closed
2" is a different claim about a day than about a month, and only a
declaration -- reviewed, named, versioned -- may make that kind of claim. A
request may narrow *which* buckets you see. It may never change what one is.

## Whose calendar?

`in desk_agent.timezone` reads the calendar off **the subject's own record**.
Mira is in Dublin, Pell in New York, Sindre in Oslo.

This is not decoration. A ticket Pell closed at 02:00 UTC was closed
*yesterday* in New York, and filing it under the UTC date puts it in a day
Pell did not work. A single desk-wide calendar would do that to everybody
outside it, and the number under "yesterday" would be about a period nobody
had.

Three consequences follow, and the first is a cost:

- **One record shared by two subjects can land on two different dates.** An
  08:00 UTC event is the 25th in Tokyo and the 24th in London, and it is not
  the engine's place to pick. So the time part is computed against whichever
  subject the bucket is being filed under.
- **A subject with no calendar recorded is in no bucket at all.** Never UTC as
  a fallback. A person nobody has stated a calendar for has no calendar, and
  defaulting one files their history under days they never worked with nothing
  on the board to say so. A value that is not a real zone name -- `"PST"`, a
  typo, an empty template -- counts as none.
- **Moving somebody's calendar re-files their whole history**, and no ticket
  has changed, so the engine escalates that pass to a full rebuild. Same
  reasoning as the identity hop.

You can also write `in "Europe/Berlin"` -- one calendar for everybody, checked
at build time against the real zone list. A bare `by month` with no `in` at
all means UTC, and that is a choice a definition makes rather than a default
it falls into: two figures on one card, one cut in UTC and one in somebody's
own, would be two rows headed "30" measuring two different months.

## `bucketed` -- a figure with a sequence

```fig
# Tickets closed that calendar month, counted per agent -- the month's own
# records, never a rollup of the days.
figure desk_agent.closed_month bucketed:
    display "{desk_agent} closed {value} that month"

    depends:
        done = desk_ticket.closed_by_month:{desk_agent}

    calculate:
        count(done)
```

`bucketed` is the declaration that this figure stores one value per subject
**per period** rather than one per subject. It is bare -- it does not restate
the grain -- because the grain is already in the group, and a second place to
write it is a first place for the two to disagree.

What it stores:

| agent | Mar | Apr | May | Jun |
|---|---|---|---|---|
| Mira Halloran | 2 | 3 | 2 | -- |
| Tomas Beck | 2 | 1 | 2 | 1 |
| Nour Aziz | 1 | 2 | 2 | 1 |
| Pell Okonkwo | 2 | 1 | -- | -- |
| Sindre Vik | 1 | 1 | 1 | -- |

Go and count. Mira's April closes are T-109 (3 Apr), T-113 (17 Apr) and T-116
(30 Apr): three.

**The dashes are absences, not zeroes, and they are stored as nothing at all.**
A bucket with nothing in it is never written. Mira closed nothing in June, so
there is no June bucket for Mira -- and that turns out to matter a great deal
in [chapter 9](09-reading-it-back.md), where averaging over "the months she
worked" and "the months in the window" give genuinely different answers.

## A coarser view is its own declaration

You might expect to store days and let a request roll them up into months.
That is refused, and the replacement is: declare the coarser grain too.

```fig
group desk_ticket.closed_by_day from (assigned_login through desk_agent.logins.handle, resolved_at by day in desk_agent.timezone)
group desk_ticket.closed_by_month from (assigned_login through desk_agent.logins.handle, resolved_at by month in desk_agent.timezone)
group desk_ticket.closed_by_quarter from (assigned_login through desk_agent.logins.handle, resolved_at by quarter in desk_agent.timezone)
```

Three groupings, three figures, three names, three explanations, three
versions. And **each one buckets the records directly**: the month figure holds
every ticket closed that month, not a sum of thirty day buckets.

The gain is that each grain is a written question a reader can cite, and a
number on a screen can say which one it is. The cost is three declarations
instead of one, which is a fair price for never having to ask "is this figure
showing me days or months?".

They cannot disagree about which period something is in, either, because every
label is derived from the same local instant: the zone is applied once to find
the local time, and a coarser label is calendar arithmetic on that local day.

Quarterly, then, over the same twenty-five closes:

| agent | 2026-Q1 | 2026-Q2 |
|---|---|---|
| Mira Halloran | 2 | 5 |
| Tomas Beck | 2 | 4 |
| Nour Aziz | 1 | 5 |
| Pell Okonkwo | 2 | 1 |
| Sindre Vik | 1 | 2 |

## `list` -- keeping the numbers rather than averaging them

Counting is not the only thing a period bucket can hold.

```fig
# Every first-reply wait this agent recorded that day, kept as the list of
# waits rather than averaged -- which statistic a reader wants is a question
# for the read.
figure desk_agent.reply_waits_day bucketed:
    display "{desk_agent} first replies that day"

    depends:
        answered = desk_ticket.replied_by_day:{desk_agent}

    calculate:
        list(desk_ticket.first_reply_seconds over answered)
```

`list` does not aggregate, and the refusal to aggregate here is the point.
Reduce each day to its average and you have thrown away the only thing a
range needs: you can no longer ask for the median across three months,
because a median of daily averages is not a median of anything real. So the
figure keeps the measurements, and *which statistic you want* becomes a
question for the read -- asked in [chapter 9](09-reading-it-back.md), and
hashed into that reading's own version.

Records with nothing to measure are left out of both the numbers and the
evidence, so a ticket with no first reply is not a zero in the list.

## `across` -- a second dimension that is not a date

The second half of a composite key does not have to be a period. It can be
another *thing*:

```fig
# Every ticket, filed under the agent who worked it and the channel it came
# in on. The second part is a dimension, not a date.
group desk_ticket.worked_in from (assigned_login through desk_agent.logins.handle, channel) label "worked by this agent on this channel"

# Tickets this agent closed, split by the channel they came in on.
figure desk_agent.closed_by_channel across desk_channel:
    display "{desk_agent} closed {value} on {desk_channel}"

    depends:
        done = desk_ticket.worked_in:{desk_agent} & desk_ticket.resolved

    calculate:
        count(done)
```

| agent | email | chat | phone |
|---|---|---|---|
| Mira Halloran | 6 | 1 | -- |
| Tomas Beck | 5 | 1 | -- |
| Nour Aziz | 2 | 2 | 2 |
| Pell Okonkwo | 2 | 1 | -- |
| Sindre Vik | 3 | -- | -- |

`across desk_channel` is the declaration that this figure holds one value per
*pair*. It is required, and without it every reader downstream would be
silently wrong in its own way: the display template would render
`{desk_channel}` as literal text, and a sentence describing the whole
population would sit beside a number that is a slice of it.

**Look at the dashes again.** Mira has never closed a phone ticket, so there is
no Mira-and-phone pair at all -- not a zero. The roster of pairs is *the group*,
never a cross product of every agent against every channel, because crossing
them would write a confident nought against combinations that categorically
cannot hold a record. A pair reads a real `0` once it has ever appeared, and
is absent until then.

## A total that cannot disagree with its parts

```fig
# Every ticket this agent closed, all channels together -- the parts of
# closed_by_channel, added up, so a total and its parts cannot disagree.
figure desk_agent.closed_total:
    display "{desk_agent} closed {value} tickets"

    calculate:
        sum(desk_agent.closed_by_channel)
```

| agent | closed |
|---|---|
| Mira Halloran | 7 |
| Tomas Beck | 6 |
| Nour Aziz | 6 |
| Pell Okonkwo | 3 |
| Sindre Vik | 3 |
| Jules Amari | 0 |

Twenty-five, and they add up across the rows of the table above because
**there is one count and this adds it up**. The alternative -- a second figure
counting closed tickets independently -- would be two numbers held together by
a test, and every product owner reading this has been in the meeting where
those two numbers stopped agreeing.

## Work that spans several periods

An event lands in one period. A *commitment* occupies a run of them, and a
ticket that was open for two weeks is the second kind of thing.

```fig
# Every ticket, filed under itself and under every week it was open in --
# both ends inclusive, so a ticket opened and closed inside one week is in
# that week rather than in none.
group desk_ticket.its_open_weeks from (id, opened_at until resolved_at by week) label "open during this week"
```

`A until B by week` puts the record in **every** bucket between the two ends,
both inclusive. T-123 opened on 21 May (week 21) and closed on 26 May (week
22), so it is in two.

A span missing either end is in no bucket at all -- never one silently running
to now or for ever, because a commitment with no end is one nobody has
scheduled. So our eleven open tickets are not in this grouping.

Now, the question a capacity conversation actually asks: *what did each week
of work cost?* If a ticket took three hours over two weeks, it is not three
hours in each -- that is twice the truth.

```fig
# A ticket's working time, shared evenly across the weeks it was open.
figure desk_ticket.effort_by_week bucketed:
    display "{desk_ticket} effort that week"

    depends:
        weeks = desk_ticket.its_open_weeks:{desk_ticket}

    calculate:
        spread(desk_ticket.effort over weeks)
```

`spread` shares one value **evenly** across the buckets the subject occupies.
T-123's 3,600 seconds over two weeks is 1,800 in each. Evenly rather than
proportionally to the overlap, deliberately: the ends of a span round up, a
bounded error at two buckets, in the direction that never hides a peak -- and
against a model that does not know which day inside the span the work actually
landed on anyway.

(`desk_ticket.effort` is a figure scoped to the ticket itself. A figure answers
one value *per subject*, so a figure about one ticket needs a grouping that
files each ticket under its own id -- which is what `group desk_ticket.itself
from id` is for. It is a small piece of plumbing and it is honest about being
one.)

Roll that up per team and you have a chart of what each week cost:

```fig
# What each week cost the team: every ticket that was open in that week, at
# the share of its working time the week carries.
figure desk_team.effort_by_week bucketed:
    display "{desk_team} effort that week"

    depends:
        running = desk_ticket.team_open_weeks:{desk_team}

    calculate:
        sum(desk_ticket.effort_by_week over running)
```

| week | Front Desk | Escalations |
|---|---|---|
| 2026-W10 | 1.0h | 3.0h |
| 2026-W11 | 2.2h | -- |
| 2026-W12 | 0.5h | 4.0h |
| 2026-W13 | 2.0h | 1.8h |
| 2026-W14 | 1.5h | 1.8h |
| 2026-W15 | 0.8h | 1.0h |
| 2026-W16 | 1.5h | -- |
| 2026-W17 | 1.2h | 4.5h |
| 2026-W18 | 1.5h | -- |
| 2026-W19 | 2.0h | 3.5h |
| 2026-W20 | 2.5h | -- |
| 2026-W21 | 1.8h | -- |
| 2026-W22 | 1.0h | -- |
| 2026-W23 | 3.5h | -- |

Escalations' weeks 13 and 14 are both 1.8h, and they are the same ticket:
T-108 ran from 27 to 30 March, straddling the week boundary, and its 12,600
seconds were split in half.

Both groupings cut their weeks in UTC. That is not an oversight -- it is
required, because the two figures have to agree about which week is which for
the `sum` to land on the same buckets, and a ticket has no calendar of its own
to read.

> There are two more clauses a span can take -- `excluding weeks gone`, which
> drops periods that have already passed so a forward chart shows only what is
> left; and `carrying overdue weeks`, which parks work that is entirely late
> into the current period so it does not drop silently off the chart. Neither
> is used here, because this desk's spans are all in the past. Both are in
> [the language guide](../language.md).

## See it

A `bucketed` figure's rows carry the period in the UI, and every one of them
has its evidence one click away. On a ticket's own page, the **upward** view
shows every stored value that cites this record -- and every leaf figure that
counts records of this kind says whether it counted this one, so *"it did not
count it"* is stated rather than inferred.

---

Next: [8. The target that moves](08-the-target-that-moves.md)
