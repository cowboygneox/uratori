# 5. Measuring

> **The question.** *Counting tickets only tells me so much. How long are
> customers waiting? How much time is each agent actually holding? How much
> have we refunded? When did each agent last finish anything?*

Counting answers "how many". Everything else needs a **measure**: a quantity
read off one record.

## Seven measures, seven questions

```fig
# How long the customer waited for the first human answer.
measure desk_ticket.first_reply_seconds = first_reply_at - opened_at

# Working time an agent has logged against this ticket.
measure desk_ticket.handling = handling_seconds in effort

# How many times this ticket came back after somebody called it finished.
measure desk_ticket.reopen_count = reopens in count

# Money credited back to the customer over this ticket.
measure desk_ticket.refund = refund_amount in amount

# When this ticket was finished.
measure desk_ticket.closed_at = moment resolved_at

# When this ticket arrived.
measure desk_ticket.raised_at = moment opened_at

# How long this ticket has been waiting, as at the moment somebody asks.
measure desk_ticket.waiting_seconds = now - opened_at
```

A measure **decorates** records; it never decides which records are in a set.
It is deliberately not a calculator: one field, or one gap between two
moments, and nothing more. An expression language here would let a definition
read record contents in ways nothing downstream could reason about.

### A duration is a gap between two moments

`first_reply_at - opened_at` is the seconds between two instants, by
construction. It needs no unit, because there is nothing else it could be.

### A field measure must say what the number *means*

`handling_seconds` holds `10800`. Ten thousand eight hundred of what? The
record does not say -- [chapter 1](01-the-world.md) was explicit that a `fact`
describes shape and never meaning. So the measure says it, and it must:

| clause | for |
|---|---|
| `in effort` | seconds of **working time** |
| `in count` | a tally |
| `in amount` | a quantity where magnitude matters more than precision: money, bytes, requests |

Required, never defaulted, because the same integer means different things and
**neither wrong guess throws**. Default it to `count` and a piece of working
time prints as `144000`. Default it to `effort` and a tally of five reopens
prints as `0.0h`. Both look like numbers. Both are wrong.

**`effort` is not a synonym for "duration".** A duration is wall-clock; an
effort is working time. Both render in hours, so they agree at eight hours and
part company above a day: 144,000 seconds is `1.7d` as a duration and `40.0h`
as an effort, because a working week is forty hours and nobody means "one and
two-thirds days" by it. One number cannot be both, so one number has to say
which it is.

**`amount`** renders compact: `155` stays `155`, `1234.5` prints `1.2k`,
`13412000` prints `13M`. It carries no currency symbol and no unit word at all
-- what the number *is* is your domain knowledge, and a screen wanting a `$`
puts one there itself.

### A moment is not a quantity

`moment resolved_at` names a single instant. It is its own kind of measure
rather than a third unit on a field measure, and the reason is arithmetic:
subtracting two instants gives milliseconds and totalling a column of them
gives a date in the future. Both would compile. Neither would throw. Each
would put a plausible number on a screen. So moments exist for exactly two
operations -- "the most recent" and "the earliest" -- and the compiler refuses
them everywhere else.

### `now` is the clock, and it is fenced

`now - opened_at` is how long something has been waiting *at the moment you
ask*. A measure that reads it is a **clock measure**, and there is one hard
rule about it:

> **A stored value may never read the clock.**

The reason is mechanical rather than philosophical. The engine recomputes
things when facts move. The clock is not a fact and never moves anything, so a
stored number computed from `now` would be correct for exactly one instant and
then sit there being wrong for ever, with nothing to make it recompute.

So `now` is allowed only where nothing is stored: a live reading
([chapter 9](09-reading-it-back.md)) and a projection
([chapter 10](10-the-queue-as-a-page.md)). Try to name one from a figure and
the build fails, saying so.

## Putting measures to work

### `sum` over a measure

```fig
# Working time this agent is currently holding: the hours logged against
# every ticket still in their hands.
figure desk_agent.effort_in_hand:
    display "{desk_agent} is holding {value} of work"

    depends:
        mine = desk_ticket.handled_by:{desk_agent} & desk_ticket.open

    calculate:
        sum(desk_ticket.handling over mine)
```

| agent | holding |
|---|---|
| Pell Okonkwo | 8.0h |
| Mira Halloran | 3.8h |
| Sindre Vik | 1.8h |
| Nour Aziz | 1.2h |
| Tomas Beck | 0.8h |
| Jules Amari | 0.0h |

Check Mira: her four open tickets are T-124 (2700s), T-128 (3600), T-129
(1800), T-130 (5400). That is 13,500 seconds, and 13,500 / 3,600 = 3.75, which
renders `3.8h`.

Notice what the figure did **not** have to declare. No `unit` line. The
calculation already says what the number is: a sum of an effort measure is an
effort. The rule across the whole language is **declare only what cannot be
derived**, because a second place to write something is a first place for the
two to disagree. Write `unit effort` here and the build refuses it.

Two tickets, T-135 and T-136, have no `handling_seconds` at all. They are open
and unassigned, so they are in nobody's bucket -- but had they been, a record
with nothing to measure simply contributes nothing to a sum, and a sum over a
set where nobody has logged anything is a real `0.0h`.

### An amount, and a count

```fig
# Money this customer has been credited back, over every ticket they raised.
figure desk_customer.refunds:
    display "{desk_customer} has been credited {value}"

    depends:
        theirs = desk_ticket.raised_by:{desk_customer}

    calculate:
        sum(desk_ticket.refund over theirs)
```

| customer | credited |
|---|---|
| Northwind Traders | 1.7k |
| Oakline Bakery | 240 |
| everyone else | 0 |

Northwind's two refunds are 480 (T-109) and 1200 (T-121): 1,680, printed
compactly as `1.7k`.

### `latest` and `earliest`

```fig
# When this agent last finished anything.
figure desk_agent.last_close:
    display "{desk_agent} last closed a ticket at {value}"

    depends:
        mine = desk_ticket.handled_by:{desk_agent} & desk_ticket.resolved

    calculate:
        latest(desk_ticket.closed_at over mine)
```

| agent | last close |
|---|---|
| Nour Aziz | 2026-06-05 |
| Tomas Beck | 2026-06-04 |
| Mira Halloran | 2026-05-29 |
| Sindre Vik | 2026-05-06 |
| Pell Okonkwo | 2026-04-23 |
| Jules Amari | *nothing* |

Jules is the row that matters. He has closed nothing, so the set is empty, and
**the latest of nothing is nothing**. Not 1 January 1970 -- which is what a
naive maximum over an empty list gives you, and which would have this page
report that a new starter has been idle for fifty-six years.

The mirror image reads the other end:

```fig
# The arrival time of the oldest thing still in this agent's hands.
figure desk_agent.oldest_open:
    display "{desk_agent}'s oldest open ticket arrived at {value}"

    depends:
        mine = desk_ticket.handled_by:{desk_agent} & desk_ticket.open

    calculate:
        earliest(desk_ticket.raised_at over mine)
```

Pell's is 2026-05-19 -- T-122, the latency spike, which has been open for six
weeks.

## Arithmetic, and the unit rule biting

```fig
# How many more tickets this agent could take before crossing their own
# over-line. A negative number means they are already past it.
figure desk_agent.headroom:
    display "{desk_agent} has room for {value} more"
    unit count

    calculate:
        desk_agent.in_hand_over - desk_agent.in_hand
```

| agent | headroom |
|---|---|
| Jules Amari | 4 |
| Nour Aziz | 3 |
| Tomas Beck | 3 |
| Sindre Vik | 2 |
| Pell Okonkwo | 1 |
| Mira Halloran | **-1** |

Three things here.

**There is no `depends` block.** A figure built only on other figures and
record fields needs no group of its own; it takes its subjects from what it
reads.

**Two different kinds of dotted name, side by side.** `desk_agent.in_hand_over`
is a **field on this agent's own record**. `desk_agent.in_hand` is the
**figure** from the last chapter. The engine resolves a figure first and a
field second, which is exactly why a figure may not take a name one of its
scope's fields already has -- one spelling answering two things is what this
language exists to refuse, and it refuses it where the collision is *made*,
not where it is read.

Reading the figure makes this one a **dependant**: when `in_hand` moves,
`headroom` is rebuilt, automatically, in the right order.

**`unit count` is required here, and only here.** Arithmetic is the one shape
where nothing can work out the answer: `a - b` and `a / b` over the same two
operands produce a quantity and a share, and `0.6` renders as "60%" or as
"0.6" with no way to tell which was meant. So arithmetic must declare; almost
everything else must not.

### `max`, `min`, and what an absence does

```fig
# How far past their own over-line this agent is, and nought when they are
# under it.
figure desk_agent.over_by:
    display "{desk_agent} is {value} past the line"
    unit count

    calculate:
        max(0 - desk_agent.headroom, 0)
```

Mira reads `1`; everybody else reads `0`. (`0 - headroom` rather than a
negative literal, because `-` already means both subtraction and set
difference in this language, and `mine -3` would parse two ways depending on a
space.)

**An absence propagates through `max` and `min`.** The tempting alternative --
"a missing value does not compete, so `max(a, nothing)` is `a`" -- is wrong
here, because a missing value means *not computed*, never "this subject has
none of it". The engine writes a real nought for anybody who genuinely has
none, so the two are already distinguishable, and blurring them would throw
that away.

You can watch it happen:

```fig
# The grace this customer's tickets actually get: what their plan buys them,
# capped at the three days the desk promises everybody. A customer nobody has
# recorded a grace for has no answer here -- an absence does not lose a
# comparison, it propagates through it.
figure desk_customer.effective_grace:
    display "{desk_customer} tickets get {value} of grace"
    unit days

    depends:
        theirs = desk_ticket.raised_by:{desk_customer}

    calculate:
        min(desk_customer.grace_days, 3)
```

| customer | grace_days on record | effective |
|---|---|---|
| Northwind Traders | 1 | 1d |
| Luma Health | 1 | 1d |
| Brightpath Logistics | 3 | 3d |
| Kestrel Studios | 3 | 3d |
| Harrow & Sons | 7 | 3d |
| Oakline Bakery | 7 | 3d |
| Veld Analytics | *absent* | *nothing* |

Veld does not get `3`. Nobody has drawn a line for Veld, so there is no line,
and `min` says so.

## Division, and the answer to nothing

```fig
# The share of this customer's tickets that came back after being closed.
figure desk_customer.reopen_rate:
    display "{value} of {desk_customer}'s tickets came back"
    unit share

    calculate:
        desk_customer.reopened_tickets / desk_customer.tickets
```

| customer | reopened | tickets | rate |
|---|---|---|---|
| Northwind Traders | 4 | 7 | 57.1% |
| Veld Analytics | 2 | 4 | 50.0% |
| Luma Health | 2 | 8 | 25.0% |
| Brightpath Logistics | 0 | 6 | 0.0% |
| Kestrel Studios | 0 | 6 | 0.0% |
| Oakline Bakery | 0 | 5 | 0.0% |
| Harrow & Sons | 0 | 0 | *nothing* |

(`desk_customer.reopened_tickets` counts the tickets that came back.
[Chapter 6](06-absence-and-age.md) writes it, because asking "has this come
back" correctly needs the next chapter's tools -- get it wrong and this whole
column reads 25% high.)

Harrow is the whole point of the row. **Division by nought answers nothing** --
never infinity, and never nought. A confident `0.0%` beside a customer who has
never raised a ticket is a claim nobody made, and it sits comfortably under
every threshold on every screen. The three 0.0% rows above are different: those
customers raised tickets and none came back. That is a finding.

## Adding up another figure

`sum` can also total another figure's values across the records in a bucket:

```fig
# Room left across the whole team: every member's headroom, added up.
figure desk_team.spare_seats:
    display "{desk_team} has room for {value} more"

    depends:
        ours = desk_agent.on_team:{desk_team}

    calculate:
        sum(desk_agent.headroom over ours)
```

Front Desk: Mira -1, Tomas 3, Nour 3, Jules 4, so **9**. Escalations: Pell 1,
Sindre 2, so **3**.

This is the operation that reaches a number no column holds. A measure reads
what is on a record; this reads what some other definition worked out. And
there is a rule attached that is the opposite of the one for measures: **if
any member has no value, the whole total is absent.** Adding up whichever
members happened to have a number is arithmetic over a population nobody
chose; it reads low, plausibly, and repairs itself later.

## A ladder that answers a word

```fig
# The word a queue screen puts beside an agent's name.
figure desk_agent.queue_state:
    display "{desk_agent}'s queue is {value}"

    calculate:
        when desk_agent.in_hand >= desk_agent.in_hand_over then "over"
        when desk_agent.in_hand >= desk_agent.in_hand_warn then "filling"
        when desk_agent.in_hand == 0 then "clear"
        otherwise "ok"
```

| agent | queue |
|---|---|
| Mira Halloran | over |
| Pell Okonkwo | filling |
| Nour Aziz | ok |
| Sindre Vik | ok |
| Tomas Beck | ok |
| Jules Amari | clear |

Same ladder shape as a band, with two rules of its own. **A figure's ladder
must answer words**, and the same shape from every rung -- one returning
numbers would carry an absence out under a numeric heading, where nothing
downstream can hold it. And the words it can produce are listed, right there,
in its own definition. That finite, visible list is the only place arbitrary
text exists in this language, because a figure that could return any string
would be a template engine with a version number.

---

Next: [6. Absence, presence and age](06-absence-and-age.md)
