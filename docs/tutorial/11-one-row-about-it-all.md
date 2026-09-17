# 11. One row about it all

> **The question.** *I do not want to read ten rows. Tell me in one line how
> bad the queue is.*

[Chapter 10](10-the-queue-as-a-page.md) said a projection aggregates nothing:
no counting its own rows, no totalling a column. That is not an omission -- it
is the answer to "how many ways may this product compute a number?", and the
answer is one. The headline about a page is its own declaration, with its own
name and its own version.

```fig
# The open queue in one row: how big it is, how much of it is past grace,
# and how much of it nobody has answered.
summarise desk_ticket.queue_health over desk_ticket.queue:
    count tickets
    count tickets_overdue where overdue == 1
    count tickets_unanswered where answered == 0
    total waiting_total in days = waiting_days

    value:
        verdict =
            when tickets_overdue > 0 then "attention"
            otherwise "clear"

    flag queue-past-grace when tickets_overdue > 0:
        label "Past grace"
        detail "{tickets_overdue} of {tickets} open {tickets|ticket is:tickets are} past grace."
        action "Work the overdue rows first."
        severity attention
```

As at 30 June:

| | |
|---|---|
| tickets | 10 |
| tickets_overdue | 7 |
| tickets_unanswered | 1 |
| waiting_total | 182d |
| verdict | attention |

> **Past grace** -- 7 of 10 open tickets are past grace. *Work the overdue rows
> first.*

> Three of those five numbers move with the clock, because the page they
> summarise does. On your own copy `tickets_overdue` is **8** -- T-136 has
> crossed its seven-day grace since 30 June -- and `waiting_total` is far
> larger. `tickets` and `tickets_unanswered` are the same, because neither
> reads a clock.

## Everything arrives through the projection

`over desk_ticket.queue` is the only route in. A summary cannot read a record,
a fact or a figure directly, and it cannot summarise another summary. One
population, one route to it.

Which means the counts inherit everything the page decided. Eleven tickets are
open; `tickets` says **10**, because T-135 was omitted from the page and **an
omitted row is out of the summary too**. And `tickets_overdue` uses the exact
same `overdue` column the flags on the rows used, so a row cannot be flagged
while the headline disagrees.

It also **aggregates all of the projection's rows, never the page.** Sort and
limit belong to the page; a summary of the first fifty rows under a heading
naming the whole queue is a wrong number that reads as a right one.

## `count` is a floor; `total` is not

Two opposite decisions, stated in both places, for the same reason.

**`count <name> [where <condition>]` -- an unknown does not count.** A row the
engine has not measured is not evidence that the thing being counted is true.
So a count is a floor: at least this many.

That is exactly what happens here. Veld's two tickets have no `grace_days`, so
their `overdue` column reads 0 via the ladder's first rung -- and had that rung
not been written, the comparison would have been undecidable and those rows
would simply not have counted. Either way the headline never claims more than
it can show.

**`total <name> in <unit> = <column>` -- an absent contribution makes the whole
total absent.** Deliberately the opposite. A sum that skipped the unmeasured
rows is arithmetic over a population nobody chose; it reads low, plausibly, and
then repairs itself later when the data fills in, which is the sawtooth
signature you have probably seen on a chart and blamed on something else. Rows
a `where` excludes contribute nothing and are not absences.

The unit is required rather than inherited from the column, and the reason is
a small trap worth naming: a *share* per row totals to a **quantity of
shares**, which is not a share of anything.

> `waiting_total` reads `182d` where the ten ages in the chapter 10 table sum
> to 181. The ages are each rounded for display; the total is the sum of the
> unrounded ones. Every number here travels twice -- the magnitude, for
> anything positional, and the text a reader sees -- and the rendering is done
> once, on the server, precisely so that nobody downstream re-derives it
> differently.

## `value` -- over the counts, and nothing else

```fig
    value:
        verdict =
            when tickets_overdue > 0 then "attention"
            otherwise "clear"
```

A summary value may read the counts and totals above it, and nothing else. In
particular it may **not** read a row value: a row value is one number per
record, and the summary is holding hundreds of them, so the expression would
have no single thing to be about.

A count or total may not reuse a name the projection already binds, either.
One word would mean one ticket in one line and the whole queue in the next.

## Flags, again

Same construct as a projection's, over the summary's own bindings:

```
detail "{tickets_overdue} of {tickets} open {tickets|ticket is:tickets are} past grace."
```

`{tickets|ticket is:tickets are}` picks a form from the same binding it prints.
That coupling is the whole design of the plural: a sentence physically cannot
pluralise on one number and print another.

## One more property

**The summary hashes its projection's version.** Rename what `overdue` means,
or change `omit`, or change `from`, and every count that reads them moves to a
new version -- because they are counting something different now, and a
citation that did not move would be a lie.

---

Next: [12. What travels together](12-what-travels-together.md)
