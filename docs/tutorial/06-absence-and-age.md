# 6. Absence, presence and age

> **The question.** *How many of our tickets have never been answered? How
> many have come back after we closed them? And which ones are past the time
> we promised that customer?*

Every one of these is a question about a field being *filled in*, and every
one of them has an obvious wrong answer that ships a plausible number.

## Three different questions that look like one

Take `reopens`, the count of times a ticket came back. Thirty-four of our
thirty-six tickets carry it, most of them as `0`. Two -- T-135 and T-136, the
ones nobody has touched -- have no `reopens` field at all.

Now: *which tickets have been reopened?*

### The version that looks right

```
filter desk_ticket.reopened_wrong where reopens != 0
```

It answers **10**: T-102, T-105, T-107, T-114, T-117, T-121, T-122, T-130,
**T-135, T-136**.

The last two are wrong, and this is [chapter 2](02-which-tickets-count.md)'s
trap arriving with real consequences. T-135 and T-136 have never been
reopened; nobody has *said* anything about them. Absent satisfies `!=`, so the
filter swept them in, and every number built on it -- a reopen rate, a quality
score, a per-customer trend -- is now 25% too high, quietly, in the direction
that makes the desk look worse than it is.

### The version that is right

```fig
# Tickets that came back after somebody called them finished.
filter desk_ticket.reopened where reopens is set label "reopened at least once"
```

It answers **8**: the same list without T-135 and T-136. Count the `reopens`
column in [the dataset](../tutorial.md#tickets): eight rows hold 1 or 2.

`is set` (and `is not set`) asks **has anybody said anything here**, and it
folds in two decisions that are exactly what a product owner wants:

- **Nought counts as absent.** Providers routinely write `0` and `null` into
  the same field for the same state. Honouring the difference would move a
  coverage number when an operator cleared a box rather than when anybody
  filled one in.
- **A boolean `false` counts as present**, because somebody answered.

So `reopens is set` means "this ticket has a non-zero reopen count", which is
the English question, and it is not something you would guess from the words.
It is worth learning once.

Two figures over that bucket, answering two questions people routinely
confuse:

```fig
# How many of this customer's tickets came back after being closed.
figure desk_customer.reopened_tickets:
    display "{desk_customer} has had {value} tickets reopened"

    depends:
        theirs = desk_ticket.raised_by:{desk_customer} & desk_ticket.reopened

    calculate:
        count(theirs)

# How many times, in total, this customer's tickets have come back -- a
# ticket reopened twice counts twice, where reopened_tickets counts it once.
figure desk_customer.reopens_total:
    display "{desk_customer}'s tickets have come back {value} times"

    depends:
        theirs = desk_ticket.raised_by:{desk_customer}

    calculate:
        sum(desk_ticket.reopen_count over theirs)
```

| customer | tickets reopened | times reopened |
|---|---|---|
| Northwind Traders | 4 | 6 |
| Luma Health | 2 | 2 |
| Veld Analytics | 2 | 2 |
| everyone else | 0 | 0 |

Northwind's four are T-105, T-117, T-121 and T-122, and two of them came back
*twice*: 4 and 6. Notice that the second figure needs no filter at all -- it
sums the `reopens` column over every ticket the customer raised, and a ticket
with nothing in that column contributes nothing. Both are true; they are not
the same number; and each says in its own name which one it is.

## The empty string is a value

Now the other half. Look at the customers:

| customer | `plan` | |
|---|---|---|
| Northwind Traders | `enterprise` | somebody said |
| Luma Health | `enterprise` | somebody said |
| Brightpath Logistics | `business` | somebody said |
| Kestrel Studios | `business` | somebody said |
| Harrow & Sons | `starter` | somebody said |
| Oakline Bakery | *no field at all* | nobody ever said |
| Veld Analytics | `""` | somebody said, then cleared it |

Three filters, three different answers:

```fig
# Customers somebody has recorded a plan tier for.
filter desk_customer.plan_stated where plan is set label "plan recorded"

# Customers whose plan tier was filled in and then cleared.
filter desk_customer.plan_cleared where plan == "" label "plan cleared"
```

| filter | holds |
|---|---|
| `plan is set` | 5: Brightpath, Harrow, Kestrel, Luma, Northwind |
| `plan == ""` | 1: Veld |
| `plan != "enterprise"` | 5: Brightpath, Harrow, Kestrel, Oakline, Veld |

**Veld is in the second but not the first.** An empty string *is a value* --
somebody cleared the field, which is a thing that happened -- so `== ""` finds
it, and `!= ""` therefore no longer would. But `is set` is asking a different
question ("has anybody said anything") and reads `""` the way it reads a
missing field: as nobody having said.

That is not inconsistency, it is two questions. The reason it matters is that
different upstream systems write the same state differently: one writes
`null`, the next writes `""`, and a coverage number that honoured the
difference would move when nothing about the world did.

The working rule:

| you want to ask | write |
|---|---|
| has anybody filled this in? | `is set` / `is not set` |
| did somebody specifically clear it? | `== ""` |
| is it something other than this value? | `!= "..."` -- and remember the blanks come with it |

## Never answered

With that settled, the question at the top of the chapter is one line:

```fig
# Tickets nobody has answered at all.
filter desk_ticket.unanswered where first_reply_at is not set label "never answered"
```

**Two**: T-135 and T-136. And notice that they are absent in two different
ways -- T-135 simply has no `first_reply_at` field, and T-136 has one set to
`null`. Both mean nobody said. Both are in the bucket.

```fig
# Tickets from this customer nobody has answered yet.
figure desk_customer.never_answered:
    display "{desk_customer} is waiting on {value} first replies"

    depends:
        theirs = desk_ticket.raised_by:{desk_customer} & desk_ticket.unanswered

    calculate:
        count(theirs)
```

Kestrel 1, Oakline 1, everybody else a measured 0.

> **A thing worth noticing, as a product owner.** In
> [chapter 8](08-the-target-that-moves.md) the desk computes its typical
> first-reply time, and it looks healthy. It is computed over the tickets
> somebody *answered*. The two tickets nobody has answered are not in that
> median dragging it up; they are not in it at all, because there is no
> first-reply time to put in it. That is the correct arithmetic and it is also
> a blind spot, which is exactly why this filter exists beside it. The
> engine will not invent a number for an unanswered ticket, so the question
> "how many did we never answer" has to be asked separately, and put on the
> same screen.

## Age: the one clock a stored number may read

[Chapter 5](05-measuring.md) said a stored value may never read the clock.
There is one exception, and it is fenced carefully enough to be safe.

```fig
# Tickets that have been sitting a fortnight.
filter desk_ticket.stale where opened_at older than 14 days label "open over 14 days"
```

Why is this allowed where `now - opened_at` is not? Because **membership does
not decay**. A clock measure changes every second. A record crosses this line
*once*, on a knowable day, and until it does the answer is unchanged. The
question is not "is this value decaying" but "how long may the crossing go
unnoticed", and the answer is *until the next pass* -- which only holds because
the threshold is in whole days. The unit is fixed at days precisely so that
the unsafe version cannot be written.

One rule: **a record whose timestamp cannot be read is in no age bucket.** An
absent `opened_at` is not evidence of age.

> This one genuinely moves. As at 30 June it holds 30 tickets -- everything
> opened on or before 16 June. On your own copy it holds more, because more
> time has passed. It is the only number in this tutorial that behaves that
> way on purpose.

## A line that is different for every customer

The desk does not promise everybody the same thing. An enterprise customer's
ticket is overdue after a day; a starter's after a week. The line is on the
customer's record:

```fig
# Tickets past the grace period their own customer's plan buys them.
filter desk_ticket.past_grace where opened_at older than grace_days from customer_id through desk_customer.id label "past this customer's grace"
```

Read the clause as: *find the `desk_customer` whose `id` matches this ticket's
`customer_id`, read `grace_days` off it, and use that as this ticket's line.*
The `through` phrase is byte for byte the one a group's identity hop uses, so
learning one is learning both.

Three refusals fall out, and all three are the same principle:

- **A customer with no `grace_days` gives their tickets no line at all**, and a
  ticket with no line is in no filter. Veld has no `grace_days`, so Veld's
  tickets -- T-107, T-115, T-124, T-130 -- are never in this bucket. Never a
  default of nought, never a default of "everybody". A customer nobody has
  drawn a line for is not a customer whose line is zero.
- **A ticket naming two customers** would have two lines and no way to choose,
  so it is in no bucket either.
- **Moving a customer's `grace_days` moves the line for every ticket of
  theirs**, and the tickets themselves have not changed -- so a write to a kind
  an age filter reads through escalates the next pass to a full rebuild, the
  same way the identity hop does.

### Why a *join* rather than a threshold figure?

Everywhere else in this language, a threshold that varies is a figure looked
up by subject. A filter cannot do that, and the reason is worth a sentence,
because it is the kind of thing that saves an afternoon: **a filter has no
subject.** It runs over records *before* anything files them by person or
customer, so there is nothing to look a number up by. And a figure that could
feed a filter would be a circle -- the filter decides the population the figure
is computed over.

What a record *does* have is an owner. So the threshold comes off the owner.

## Putting it together

```fig
# Tickets in this agent's hands that are past their customer's grace period.
figure desk_agent.past_grace:
    display "{desk_agent} has {value} tickets past grace"

    depends:
        late = desk_ticket.handled_by:{desk_agent} & desk_ticket.open & desk_ticket.past_grace

    calculate:
        count(late)
```

| agent | past grace |
|---|---|
| Mira Halloran | 2 |
| Pell Okonkwo | 2 |
| Nour Aziz | 1 |
| Sindre Vik | 1 |
| Tomas Beck | 1 |
| Jules Amari | 0 |

Three sets intersected, and each one is a named, reviewable claim: hers, open,
past grace. Note that `past_grace` on its own holds resolved tickets too -- an
age filter does not know or care whether something finished -- so the `& open`
is doing real work.

Mira's two are T-128 (Kestrel, 3 days of grace, open 21 days) and T-129
(Oakline, 7 days, open 19 days). Her other two open tickets are Veld's, and
Veld has no line.

## When somebody else already decided

Sometimes the verdict is not yours to compute. The desk runs an SLA tool that
stamps a record against any ticket that missed something:

```fig
# The SLA tool's verdict on one ticket, keyed by the ticket it is about. A
# record exists only where something was missed.
fact desk_breach:
    ticket_id as text
    reply_breached as flag
    resolve_breached as flag
```

Thirteen such records exist, and each one is **keyed by the ticket key it is
about**. That is a deliberate arrangement and it needs declaring, because the
engine is about to be asked to intersect two sets of ids from two different
kinds:

```fig
# The tickets the SLA tool recorded a missed first reply against.
filter desk_breach.reply_missed keyed as desk_ticket where reply_breached == true label "first reply missed"
```

```fig
# First replies the SLA tool recorded as missed against this agent.
figure desk_agent.reply_breaches:
    display "{desk_agent} missed {value} first replies"

    depends:
        missed = desk_ticket.handled_by:{desk_agent} & desk_breach.reply_missed

    calculate:
        count(missed)
```

| agent | missed first replies |
|---|---|
| Tomas Beck | 5 |
| Mira Halloran | 3 |
| Sindre Vik | 2 |
| Nour Aziz | 1 |
| Pell Okonkwo | 0 |
| Jules Amari | 0 |

(Two of the thirteen breaches, T-135 and T-136, are on unassigned tickets, so
they belong to nobody: 5 + 3 + 2 + 1 + 0 + 0 + 2 = 13.)

Without `keyed as`, that intersection is refused at build time. **That refusal
is the whole feature**, because the failure it prevents is silent: intersecting
ids that mean different things yields the empty set, and an empty set here is
a figure reading `0` for every agent, for ever, with nothing thrown and
nothing to notice. Declaring the sharing makes it a claim somebody reviewed
rather than a coincidence somebody relied on. (The same clause works on a
group.)

---

Next: [7. Days and dimensions](07-days-and-dimensions.md)
