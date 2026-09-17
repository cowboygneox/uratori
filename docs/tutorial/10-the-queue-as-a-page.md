# 10. The queue as a page

> **The question.** *Stop aggregating. Just show me the queue: every open
> ticket, the oldest first, with who it is for and how long it has been
> sitting -- and tell me which ones I should be worried about.*

Everything so far has produced *one value per subject*. A **projection**
produces **one row per record**, assembled at the instant you ask, stored
nowhere.

That last part is what lets it do two things nothing else in the language may:
**read the clock**, and **produce prose**.

## The page

```fig
# Every ticket still open, one row each, oldest first -- with the customer's
# plan, the grace their plan buys them, and the sentence a row earns when it
# has run past it.
projection desk_ticket.queue:
    from desk_ticket.open
    sort by waiting_days descending
    limit 50

    field:
        ticket = subject as text
        opened = opened_at as date
        replied = first_reply_at as date
        priority = priority as text
        escalated = escalated as flag
        reporter = reporter.email as text
        customer = name from customer_id through desk_customer.id as text
        plan = plan from customer_id through desk_customer.id as text
        grace = grace_days from customer_id through desk_customer.id as number

    value:
        waiting_days in days = days from opened to now
        answered in count =
            when replied is nothing then 0
            otherwise 1
        overdue in count =
            when grace is nothing then 0
            when waiting_days > grace then 1
            otherwise 0
        parked in count =
            when escalated == true then 0
            when answered == 1 then 0
            when priority == "low" then 1
            otherwise 0

    flag ticket-overdue when overdue == 1:
        label "Past grace by {waiting_days}"
        detail "{customer} has waited {waiting_days}, and their plan buys {grace}."
        action "Answer {ticket} or move it to Escalations."
        severity attention

    flag ticket-unanswered when answered == 0:
        label "Never answered"
        detail "Nobody has replied to {customer} on this one yet."
        severity attention

    omit when parked == 1
```

Asked as at 30 June:

| ticket | customer | plan | grace | waiting | answered | overdue | flags |
|---|---|---|---|---|---|---|---|
| Latency spikes on the write API | Northwind Traders | enterprise | 1 | 42d | 1 | 1 | overdue |
| Attachments over 10MB are rejected | Veld Analytics | `""` | *nothing* | 35d | 1 | 0 | |
| Custom fields missing on export | Kestrel Studios | business | 3 | 21d | 1 | 1 | overdue |
| Seat count is not updating | Oakline Bakery | *nothing* | 7 | 19d | 1 | 1 | overdue |
| Login blocked after an email change | Veld Analytics | `""` | *nothing* | 15d | 1 | 0 | |
| Report scheduler timezone off by one | Northwind Traders | enterprise | 1 | 14d | 1 | 1 | overdue |
| Webhook signature mismatch | Luma Health | enterprise | 1 | 12d | 1 | 1 | overdue |
| Read replica lag on reports | Brightpath Logistics | business | 3 | 11d | 1 | 1 | overdue |
| Audit log export times out | Luma Health | enterprise | 1 | 8d | 1 | 1 | overdue |
| Pricing page 404s from inside the app | Oakline Bakery | *nothing* | 7 | 4d | 0 | 0 | never answered |

Ten rows, from eleven open tickets. One was dropped; we will come to it.

> **This page moves with the clock, and that is the point.** Run it yourself
> and every age is larger, and the last row -- four days old here -- will have
> crossed its seven-day grace and picked up the overdue flag. Nothing is stored,
> so nothing is stale.

## `from` -- the population, written where a reader can check it

```fig
    from desk_ticket.open
```

Without it, every record of the kind gets a row. With it, the page *is* a
declared population, and it speaks the same set language a figure's `depends`
speaks -- `&`, `|`, `-` over declared names.

It has rules of its own, because there is no subject here. `from` decides
which records *become* rows, so nothing exists yet to scope a bucket by:

- **Only declared filters**, never a bare name.
- **Only predicate and presence filters** -- a single bucket, read whole.
- **No age filters.** This is the one worth understanding, because the reason
  is subtle and the consequence is the sort of thing that survives for a year.
  Age buckets are resolved against the clock when the engine last rebuilt its
  groupings. A page filtered through one would show a population as old as the
  last pass -- and moving a customer's `grace_days` would change who is on the
  page with nothing rebuilding it. So `from` is stored state only, and
  clock-dependent narrowing has its own tool: `omit`, below.

## `field` -- values off the record

`<name> = <path> as <type>`, and **the type is required**. A string is mute in
the way an integer is: `date` is what lets a span know a value is an instant;
`flag` is what lets a condition test a boolean without comparing it against the
word `"true"`. Inferring the type from the shape of one record's value would
classify a ticket with no reply differently from one with a reply, under the
same definition.

**`reporter.email`** crosses the `one` block from chapter 1. It resolves to one
value, so it can be a column. A path crossing `many tags:` could not -- a row's
cell holds one value.

**A join** reads a path off a related record:

```fig
        customer = name from customer_id through desk_customer.id as text
```

*Find the `desk_customer` whose `id` matches this ticket's `customer_id`, and
read `name` off it.* Byte for byte the same `through` phrase a group and an age
filter use.

This is why `desk_customer` and `desk_team` each carry an `id` field back in
chapter 1 that looks like it just repeats the record's own key: `through`
matches against a *declared field*, and there is no syntax for matching a
record's own key directly. Without it, nothing here could look a customer or
a team back up from the id a ticket names.

**Anything other than exactly one match is nothing.** A group resolves a
relation to *every* owner on purpose; a field holds one value, so the choice is
between picking a winner and admitting there is no answer -- and picking the
first in sorted order would be perfectly stable and still a fabrication, about
the wrong record.

Two columns in the table above show absence doing its job:

- Veld's **plan** reads `""` and Oakline's reads *nothing*. The same
  distinction chapter 6 made in a filter, arriving in a row, because a row's
  text field is the same value read the same way.
- Veld's **grace** is *nothing*, because nobody set one.

## `value` -- derived per row

The same expression language a figure calculates with -- arithmetic, `max` and
`min`, ladders -- over this row's own bindings, plus one construct legal only
here:

```fig
        waiting_days in days = days from opened to now
```

`days from A to B` is **signed calendar days** between two instants, either of
which may be `now`. Days rather than seconds because a written threshold about
an age is written in days -- "fourteen" is what somebody says -- and the first
definition that forgot to divide by 86,400 would compare seconds against days
and read as never crossing. Signed, so "overdue by three" and "three days
left" are one expression.

Notice the units. A value declares its unit before the `=`, for the same
reason arithmetic in a figure does. A ladder returning **words** must not (its
unit is worked out); a ladder returning **numbers** must, or the renderer
prints "77.5 late" where the definition meant "78d".

And notice the ladders' first rungs:

```fig
        overdue in count =
            when grace is nothing then 0
            when waiting_days > grace then 1
            otherwise 0
```

`is nothing` and `is something` are the presence tests, and they answer
*before* the unknown guard -- "is there a value at all" is never itself
unknown. Without that first rung, Veld's two tickets would hit
`waiting_days > grace` with nothing on the right-hand side, the comparison
could not be decided, and the ladder would **stop**: no `overdue` value at all,
and therefore no flag either. With it, the definition says out loud what a
customer with no line gets. They are words rather than an operator because
`grace == null` would put a value into the language that is not a value.

## A threshold here is a column, never a dial

The `overdue` ladder compares against `grace`, which is a **bound column**
read off the customer's record. If the rule varied by something computed
instead, you would bind that with `read:` and compare against the column.

A number that does not vary is written in the definition, where a reader can
see it. What there is nowhere to put is a setting on a page: a dial moved
which rows earned a flag with nothing in the row to say so.

## `flag` -- the sentence a row earns

Half of what a status screen produces is not numbers but **conditional prose**,
rendered from the same values the bands read. Leaving it in the host would put
a row's *reason* somewhere its *number* is not.

```fig
    flag ticket-overdue when overdue == 1:
        label "Past grace by {waiting_days}"
        detail "{customer} has waited {waiting_days}, and their plan buys {grace}."
        action "Answer {ticket} or move it to Escalations."
        severity attention
```

Which renders, for T-122:

> **Past grace by 42d** -- Northwind Traders has waited 42d, and their plan
> buys 1. *Answer Latency spikes on the write API or move it to Escalations.*

What keeps this a template rather than a second language: **substitution, and
one plural form.** `{name}` prints a bound value, rendered by the server in the
value's own unit. `{count|change is:changes are}` picks a form from the same
binding it prints, so a sentence can never pluralise on one number and print
another. There are no expressions inside a placeholder and no formatting
directives -- anything a sentence needs computed is a `value`, named and
checkable beside the flag that reads it. A placeholder naming nothing the
projection binds is a build failure.

The `when` is one comparison or presence test, never a conjunction. **A
condition over an unknown does not fire the flag**, because a flag is a claim.

`label`, `detail` and `severity` (`info` or `attention`) are required; `action`
is optional, because most flags have nothing to ask for, and inventing an
imperative puts a to-do on a page whose whole value is that every row is
actionable.

One thing is different about flag templates from every other piece of prose in
this language: **they are part of the version hash.** A figure's display
describes a number that did not move; a flag's sentence *is* the content of
that row.

## `omit` -- the narrowing `from` cannot do

```fig
    omit when parked == 1
```

Eleven tickets are open; ten rows came back. T-135 -- *Cannot delete a saved
view* -- is not on the page. It is low priority, nobody has answered it, and
nobody escalated it: the desk has parked it, and parked work does not belong
on the queue the team works from.

`omit` exists for the one narrowing `from` cannot express: a population whose
membership moves with the clock. It reads the row's own computed values, at
the same single instant every other value on the page reads, so it can never
be stale.

Look at how `parked` is written:

```fig
        parked in count =
            when escalated == true then 0
            when answered == 1 then 0
            when priority == "low" then 1
            otherwise 0
```

Three judgements, and the gate tests the one word they produce. The condition
grammar refuses conjunctions, so the judgement lives in a ladder whose rungs a
reviewer can argue with one at a time. A raw `priority == "low"` gate would be
the shape review rejects on sight -- it would have hidden any low-priority
ticket, including one that had been escalated.

Two more rules:

- **An omitted row is off the page *and out of the summary*.** The counts are
  over the rows a reader can see. The gate runs before the summary, the sort
  and the limit.
- **A condition the engine cannot answer keeps the row.** A flag's unknown does
  not fire, because a flag is a claim; a gate's unknown does not drop, because
  dropping on the absence of evidence narrows a population by a cheap path, and
  a page quietly one row short corrects itself never.

## `sort` and `limit`

```fig
    sort by waiting_days descending
    limit 50
```

Sorting is a calculation, so the server does it and the answer arrives in
order. Rows with no value for the sort key go **last in either direction** --
written as a constant rank they would sort first descending and push real rows
off a limited page.

**`limit` is refused without `sort`.** A limit with no order returns an
arbitrary subset that looks like a complete list and changes between runs for
reasons no reader can see.

Both are applied *after* any summary is computed, so a summary is always about
the whole population and never the page.

## `read` -- a stored figure, per row

A projection cannot aggregate -- no counting its own rows, no averaging a
column. Those are figures, and offering them here would be a second way to
compute a number this engine claims has exactly one. But it can **read** a
figure that already exists:

```fig
# One row per agent on shift: the count, the word beside it, and the room
# they have left.
projection desk_agent.roster:
    from desk_agent.rostered
    sort by in_hand descending
    limit 20

    field:
        who = name as text
        team = name from team_id through desk_team.id as text
        over_at = in_hand_over as number

    read:
        in_hand = desk_agent.in_hand
        in_hand_word = band of desk_agent.in_hand

    value:
        spare in count = over_at - in_hand

    flag agent-over-line when spare < 1:
        label "Queue is full"
        detail "{who} is holding {in_hand} and their line is {over_at}."
        action "Move something off {who}."
        severity attention
```

| who | team | in hand | word | spare | flag |
|---|---|---|---|---|---|
| Mira Halloran | Front Desk | 4 | over | -1 | Queue is full |
| Pell Okonkwo | Escalations | 2 | warn | 1 | |
| Nour Aziz | Front Desk | 1 | ok | 3 | |
| Sindre Vik | Escalations | 1 | ok | 2 | |
| Tomas Beck | Front Desk | 1 | ok | 3 | |

Jules is not on the page: `from desk_agent.rostered` is `on_shift == true`,
and he is still in training. (The three agents on 1 are tied on the sort key,
so their order among themselves is not guaranteed and will vary between
runs -- a tie is a tie, and the server does not invent a second key to break
it with.)

**`band of desk_agent.in_hand`** binds the *word* that figure's own band
answers, derived at serve time from the value and the thresholds that band
names. It is a second spelling rather than something that appears
automatically beside every read, because a name in scope that appears nowhere
in the text is exactly what this language is arranged against. Asking for the
band of a figure that declares none is a build failure -- otherwise every rung
testing it would stop and every flag gated on it would silently never fire.

A read's figure must share the projection's kind. A projection over agents
asking for a figure scoped to customers would look every row up under an id
from a different space and find nothing: a column of dashes, for ever.

## `scoped by` -- one bucket, asked for

`from` can never carry a scope, because it decides which records *become*
rows. But a caller asking for "the urgent tickets Front Desk took in May"
already knows the subject and the period before a single row exists. That is a
different question, and it has its own clause:

```fig
# Every ticket, filed under the team queue it landed in and the month it
# arrived -- the group a scoped request resolves against.
group desk_ticket.opened_by_team_month from (queue_team_id, opened_at by month) label "arrived in this queue that month"

# The urgent tickets one team took in one month, five at a time.
projection desk_ticket.urgent_by_month scoped by desk_ticket.opened_by_team_month:
    from desk_ticket.urgent
    sort by ticket ascending
    limit 5

    field:
        ticket = subject as text
        opened = opened_at as date
        customer = name from customer_id through desk_customer.id as text
```

Asked as `?subject=tm-front&trailing=1&at=2026-06-30` -- Front Desk, the
month the request is anchored in:

| ticket | opened | customer |
|---|---|---|
| Login blocked after an email change | 2026-06-15 | Veld Analytics |
| Sandbox data reset unexpectedly | 2026-06-04 | Brightpath Logistics |
| Webhook signature mismatch | 2026-06-18 | Luma Health |

And `?subject=tm-front&trailing=2-2&at=2026-06-30` -- the month before:

| ticket | opened | customer |
|---|---|---|
| Dashboard blank after the update | 2026-05-08 | Luma Health |
| Two-factor codes rejected | 2026-05-12 | Brightpath Logistics |

The request names **one** subject and **exactly one** bucket. `trailing=1` is
the period the request is anchored in, `trailing=2-2` the one before it. A bare
`trailing=3` means *the last three* everywhere else in this language, and it
means the same here -- which is three pages, so it is refused rather than
quietly served as one. Six weeks of comparison is six requests, one bucket
each.

The window and `from` simply intersect: urgent **and** in that bucket.

One detail: a scoped page's period part is cut in UTC and may not read a
subject's own calendar, because a bare `?subject=` id is not a stored record
yet -- there is nothing to read a zone off before a bucket has even been
resolved.

## See it

A record's page in the UI shows this ticket's row exactly as the page serves
it -- or says why it is not on it, which is where an `omit` becomes visible
rather than mysterious.

---

Next: [11. One row about it all](11-one-row-about-it-all.md)
