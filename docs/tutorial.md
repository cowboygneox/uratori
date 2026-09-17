# A tutorial: the support desk

This is a guided build of a small, real thing. By the end you will have
written every construct the [definition language](language.md) has, against a
world you can hold in your head, and every number in it will be one you can
check by counting rows in a table printed on this page.

It is written for the person who owns the product, not the person who writes
the code. You need no programming background. If you have ever argued with a
spreadsheet, filed a ticket, or asked why two dashboards disagree, you have
all the background this needs.

## What uratori is for

A product shows numbers. Somebody has to be able to say what each one means.

The usual arrangement is that the meaning lives in code -- a query here, a
helper there, a little arithmetic in the browser -- and the only honest answer
to "why did this move overnight?" is "let me go and read three files." uratori
takes the opposite position:

- **The meaning of every number is written down**, in a small language you can
  read, in a file your team reviews like any other change.
- **That written definition is the only thing that computes.** There is no
  second route to the number, so there is nothing to drift out of step.
- **The engine keeps every number current as facts arrive**, including numbers
  built on other numbers, and pushes what moved to whoever is watching.
- **Every answer can be taken apart**, down to the individual records it came
  from.

The rest of this tutorial is that claim, made concrete.

## The world

A customer support desk.

**Agents** answer tickets. Each belongs to a **team**, works to their own
calendar, and carries two numbers on their own record saying when their queue
is getting full.

**Tickets** arrive into a team's queue, get assigned to an agent, get a first
reply, and eventually get resolved. They belong to a **customer**, arrive over
a **channel**, and carry a priority.

**Customers** are on plan tiers, and a plan buys a *grace period* -- how long
one of their tickets may sit before the desk calls it overdue.

Two wrinkles, both deliberate, both the kind of thing a real desk has:

- **The SLA target moves.** The desk promises a first reply within some
  number of minutes. That promise has been changed twice, on two dates, and
  nobody writes anything down in the months between. Every screen wants the
  opposite shape -- a target per month, including the months nobody touched
  it.
- **People have more than one login.** The desk grew by acquisition. Mira
  signs in as `mira` and as `m.halloran`; tickets name the login, not the
  person. Anybody who builds a board straight off the login field turns Mira
  into two half-people, and neither half looks busy.

## How to run it

```bash
cd docs/tutorial
docker compose up
```

Then open <http://localhost:8080/ui/>. That is the engine's own investigation
screen: every definition, every fact, every computed value, and -- the thing
worth the price of admission for this audience -- a *worksheet* for any single
number, showing the calculation step by step with the value of each step
beside it.

[`docs/tutorial/README.md`](tutorial/README.md) has the details, including how
to run it without Docker.

## When "now" is

Most of the numbers here do not move: a count of how many tickets a customer
raised in March is the same answer next year. A few genuinely read the clock
-- how long a ticket has been waiting, which tickets are past their grace
period -- and those are larger on your machine than on this page, because time
has passed.

**Every number quoted in this tutorial was computed as at
2026-06-30 12:00 UTC.** Where a number moves with the clock, the chapter says
so.

## The chapters

Each one starts with a question somebody would actually ask about this desk,
and introduces only what the question needs.

| | Question | Teaches |
|---|---|---|
| [1](tutorial/01-the-world.md) | What is a fact, and what does the desk know? | `fact`, the four types, `name` and `url`, `one` and `many`, absence |
| [2](tutorial/02-which-tickets-count.md) | Which tickets count as open? | `group`, `filter`, `where ==` and `!=`, `label` |
| [3](tutorial/03-the-identity-hop.md) | Why does Mira look half as busy as she is? | `through` -- and the first trap |
| [4](tutorial/04-the-first-figure.md) | How loaded is each agent right now? | `figure`, `depends`, `calculate`, `band`, the three absences |
| [5](tutorial/05-measuring.md) | How long, how much, how many, when? | `measure`, `sum`, `latest`, arithmetic, `max`/`min`, ladders, `unit` |
| [6](tutorial/06-absence-and-age.md) | Which tickets has nobody answered? | `is set`, `""` vs absent, `older than`, thresholds off a record |
| [7](tutorial/07-days-and-dimensions.md) | How many did each agent close in May? | time buckets, `bucketed`, `across`, spans, `spread` |
| [8](tutorial/08-the-target-that-moves.md) | Were we inside the target that was in force *then*? | `carried forward`, comparing two sequences |
| [9](tutorial/09-reading-it-back.md) | Show me the last four months. | `reading`, statistics, `requires`, `band`, live readings |
| [10](tutorial/10-the-queue-as-a-page.md) | Just show me the queue. | `projection`, `field`, `read`, `value`, `flag`, `omit`, `scoped by` |
| [11](tutorial/11-one-row-about-it-all.md) | How bad is the queue, in one line? | `summarise` |
| [12](tutorial/12-what-travels-together.md) | Build me the agent card. | `bundle` |
| [13](tutorial/13-citations-and-evidence.md) | Why did this number move overnight? | versions, evidence, the cascade, tenants |

Everything the chapters write lives, in one compiling file, in
[`docs/tutorial/support.fig`](tutorial/support.fig). The dataset below lives in
[`docs/tutorial/facts.json`](tutorial/facts.json).

---

## The dataset, in full

Eighty records. Small enough to check every number in this tutorial by
counting rows.

The *key* is a record's identity -- the engine knows a record as a **kind** and
a **key**, and nothing else about it is special until a definition says so.
Everything else is a field the desk's own systems happened to write.

Read the columns loosely for now; chapter 1 declares what each one is.

#### teams

| key | name | timezone |
|---|---|---|
| tm-front | Front Desk | Europe/Berlin |
| tm-deep | Escalations | UTC |

#### agents

| key | name | team | calendar | warn at | over at | closes target | on shift | logins |
|---|---|---|---|---|---|---|---|---|
| ag-mira | Mira Halloran | tm-front | Europe/Dublin | 2 | 3 | 3 | yes | `mira`, `m.halloran` |
| ag-tomas | Tomas Beck | tm-front | Europe/Berlin | 3 | 4 | 2 | yes | `tomas` |
| ag-nour | Nour Aziz | tm-front | Europe/Berlin | 3 | 4 | 2 | yes | `nour`, `naziz` |
| ag-pell | Pell Okonkwo | tm-deep | America/New_York | 2 | 3 | 1 | yes | `pell` |
| ag-sindre | Sindre Vik | tm-deep | Europe/Oslo | 2 | 3 | 2 | yes | `sindre`, `sv-oncall` |
| ag-jules | Jules Amari | tm-front | Europe/Berlin | 3 | 4 | 2 | no | `jules` |

#### logins

| key (the handle) | agent |
|---|---|
| `mira` | ag-mira |
| `m.halloran` | ag-mira |
| `tomas` | ag-tomas |
| `nour` | ag-nour |
| `naziz` | ag-nour |
| `pell` | ag-pell |
| `sindre` | ag-sindre |
| `sv-oncall` | ag-sindre |
| `jules` | ag-jules |

#### customers

| key | name | plan | grace_days |
|---|---|---|---|
| cu-northwind | Northwind Traders | enterprise | 1 |
| cu-luma | Luma Health | enterprise | 1 |
| cu-brightpath | Brightpath Logistics | business | 3 |
| cu-kestrel | Kestrel Studios | business | 3 |
| cu-oakline | Oakline Bakery | *absent* | 7 |
| cu-veld | Veld Analytics | `""` | *absent* |
| cu-harrow | Harrow & Sons | starter | 7 |

#### channels

| key | name |
|---|---|
| email | Email |
| chat | Chat |
| phone | Phone |

#### tickets

| key | subject | queue | login | customer | channel | priority | opened (UTC) | first reply (UTC) | resolved (UTC) | reopens | handling s | refund | esc. | tags |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-101 | Invoice PDF will not open | front | mira | northwind | email | normal | 2026-03-03 09:15 | 2026-03-03 10:05 | 2026-03-04 11:20 | 0 | 3600 | *absent* | no | billing |
| T-102 | SSO login loop | deep | pell | luma | email | urgent | 2026-03-05 08:40 | 2026-03-05 09:10 | 2026-03-06 14:00 | 1 | 10800 | *absent* | yes | auth, sso |
| T-103 | Export is missing rows | front | tomas | brightpath | chat | normal | 2026-03-09 13:30 | 2026-03-09 13:52 | 2026-03-10 09:45 | 0 | 5400 | *absent* | no | export |
| T-104 | Password reset email never arrives | front | m.halloran | kestrel | email | normal | 2026-03-11 10:00 | 2026-03-11 11:30 | 2026-03-12 10:15 | 0 | 2700 | *absent* | no | auth |
| T-105 | Webhook retries flooding our endpoint | deep | sindre | northwind | email | urgent | 2026-03-16 09:05 | 2026-03-16 09:35 | 2026-03-18 16:30 | 2 | 14400 | *absent* | yes | webhooks |
| T-106 | Add a seat to our plan | front | nour | oakline | phone | low | 2026-03-18 14:20 | 2026-03-18 14:40 | 2026-03-18 15:30 | 0 | 1800 | *absent* | no | billing |
| T-107 | Report totals look wrong | front | tomas | veld | email | normal | 2026-03-24 11:10 | 2026-03-24 15:10 | 2026-03-26 12:00 | 1 | 7200 | *absent* | no | reports |
| T-108 | Mobile app crashes on open | deep | pell | luma | chat | urgent | 2026-03-27 09:50 | 2026-03-27 10:20 | 2026-03-30 10:00 | 0 | 12600 | *absent* | yes | mobile |
| T-109 | Duplicate charges in March | front | mira | northwind | email | urgent | 2026-04-02 09:30 | 2026-04-02 10:00 | 2026-04-03 13:00 | 0 | 5400 | 480 | no | billing |
| T-110 | API rate limit is unclear | deep | sindre | brightpath | email | normal | 2026-04-07 10:15 | 2026-04-07 12:15 | 2026-04-08 09:20 | 0 | 3600 | *absent* | no | api |
| T-111 | Cannot invite teammates | front | naziz | kestrel | chat | normal | 2026-04-09 13:00 | 2026-04-09 13:18 | 2026-04-09 16:40 | 0 | 2700 | *absent* | no | auth |
| T-112 | Scheduled report stopped running | front | tomas | luma | email | normal | 2026-04-14 08:45 | 2026-04-14 09:30 | 2026-04-15 11:00 | 0 | 3600 | *absent* | no | reports |
| T-113 | Refund for a duplicate seat | front | mira | oakline | email | low | 2026-04-16 11:20 | 2026-04-16 14:20 | 2026-04-17 10:30 | 0 | 1800 | 240 | no | billing |
| T-114 | SAML metadata rejected | deep | pell | luma | email | urgent | 2026-04-21 09:00 | 2026-04-21 09:25 | 2026-04-23 15:45 | 1 | 16200 | *absent* | yes | auth, sso |
| T-115 | Chat widget slow to load | front | nour | veld | chat | normal | 2026-04-23 15:30 | 2026-04-23 16:05 | 2026-04-24 12:10 | 0 | 4500 | *absent* | no | widget |
| T-116 | Data export encoding is garbled | front | m.halloran | brightpath | email | normal | 2026-04-28 10:40 | 2026-04-28 12:40 | 2026-04-30 09:15 | 0 | 5400 | *absent* | no | export |
| T-117 | Bulk import fails at 500 rows | deep | sv-oncall | northwind | email | urgent | 2026-05-04 09:10 | 2026-05-04 09:40 | 2026-05-06 11:30 | 1 | 12600 | *absent* | yes | import |
| T-118 | Cannot change our billing address | front | tomas | kestrel | email | low | 2026-05-06 14:00 | 2026-05-06 16:30 | 2026-05-07 10:00 | 0 | 1800 | *absent* | no | billing |
| T-119 | Dashboard blank after the update | front | mira | luma | chat | urgent | 2026-05-08 08:50 | 2026-05-08 09:05 | 2026-05-08 14:20 | 0 | 5400 | *absent* | no | dashboard |
| T-120 | Two-factor codes rejected | front | naziz | brightpath | phone | urgent | 2026-05-12 10:30 | 2026-05-12 10:50 | 2026-05-13 09:40 | 0 | 4500 | *absent* | no | auth |
| T-121 | Usage numbers disagree with the invoice | front | tomas | northwind | email | normal | 2026-05-14 11:05 | 2026-05-14 15:05 | 2026-05-18 10:00 | 2 | 9000 | 1200 | no | billing, reports |
| T-122 | Latency spikes on the write API | deep | pell | northwind | email | urgent | 2026-05-19 09:20 | 2026-05-19 09:45 | *open* | 1 | 18000 | *absent* | yes | api |
| T-123 | Add a read-only role | front | nour | kestrel | email | low | 2026-05-21 13:40 | 2026-05-22 09:40 | 2026-05-26 11:15 | 0 | 3600 | *absent* | no | auth |
| T-124 | Attachments over 10MB are rejected | front | mira | veld | email | normal | 2026-05-26 10:00 | 2026-05-26 11:15 | *open* | 0 | 2700 | *absent* | no | attachments |
| T-125 | Weekly digest sent twice | front | m.halloran | oakline | email | low | 2026-05-28 09:30 | 2026-05-28 10:30 | 2026-05-29 09:00 | 0 | 1800 | *absent* | no | email |
| T-126 | Invoice tax rate wrong for the EU | front | tomas | luma | email | normal | 2026-06-02 09:00 | 2026-06-02 10:30 | 2026-06-04 11:00 | 0 | 5400 | *absent* | no | billing |
| T-127 | Sandbox data reset unexpectedly | front | nour | brightpath | email | urgent | 2026-06-04 08:30 | 2026-06-04 09:00 | 2026-06-05 16:10 | 0 | 7200 | *absent* | yes | sandbox |
| T-128 | Custom fields missing on export | front | m.halloran | kestrel | email | normal | 2026-06-09 10:20 | 2026-06-09 13:20 | *open* | 0 | 3600 | *absent* | no | export |
| T-129 | Seat count is not updating | front | mira | oakline | chat | normal | 2026-06-11 11:00 | 2026-06-11 11:25 | *open* | 0 | 1800 | *absent* | no | billing |
| T-130 | Login blocked after an email change | front | m.halloran | veld | email | urgent | 2026-06-15 09:40 | 2026-06-15 10:10 | *open* | 1 | 5400 | *absent* | no | auth |
| T-131 | Report scheduler timezone off by one | front | tomas | northwind | email | normal | 2026-06-16 13:15 | 2026-06-17 09:15 | *open* | 0 | 2700 | *absent* | no | reports |
| T-132 | Webhook signature mismatch | front | naziz | luma | email | urgent | 2026-06-18 08:45 | 2026-06-18 09:15 | *open* | 0 | 4500 | *absent* | yes | webhooks |
| T-133 | Read replica lag on reports | deep | pell | brightpath | email | urgent | 2026-06-19 10:00 | 2026-06-19 10:40 | *open* | 0 | 10800 | *absent* | yes | api |
| T-134 | Audit log export times out | deep | sv-oncall | luma | email | normal | 2026-06-22 09:30 | 2026-06-22 15:30 | *open* | 0 | 6300 | *absent* | no | export, audit |
| T-135 | Cannot delete a saved view | front | *unassigned* | kestrel | chat | low | 2026-06-25 14:10 | *absent* | *open* | *absent* | *absent* | *absent* | no | views |
| T-136 | Pricing page 404s from inside the app | front | *unassigned* | oakline | email | normal | 2026-06-26 11:30 | *null* | *open* | *absent* | *absent* | *absent* | no | website |

#### target changes

| key | team | setting | value (s) | set at (UTC) | set by |
|---|---|---|---|---|---|
| tc-1 | tm-front | first_reply_target | 7200 | 2026-03-05 09:00 | Ines Dorn |
| tc-2 | tm-front | resolve_target | 172800 | 2026-03-05 09:05 | Ines Dorn |
| tc-3 | tm-deep | first_reply_target | 3600 | 2026-04-02 08:00 | Ines Dorn |
| tc-4 | tm-front | first_reply_target | 3600 | 2026-05-11 10:00 | Ines Dorn |

#### SLA breaches

| key (the ticket) | reply missed | resolve missed |
|---|---|---|
| T-107 | yes | no |
| T-110 | yes | no |
| T-113 | yes | no |
| T-118 | yes | no |
| T-121 | yes | yes |
| T-123 | yes | yes |
| T-124 | yes | no |
| T-126 | yes | no |
| T-128 | yes | no |
| T-131 | yes | no |
| T-134 | yes | no |
| T-135 | yes | no |
| T-136 | yes | no |

### Things to notice before you start

A few of these rows are shaped the way they are on purpose, and each one
becomes a chapter:

- **Mira and Nour and Sindre each have two logins.** Tickets name the login.
- **`cu-oakline` has no `plan` field at all.** Nobody ever recorded one.
- **`cu-veld`'s plan is an empty string.** Somebody recorded one and then
  cleared it. That is not the same thing as never having said, and chapter 6
  is about why it matters.
- **`cu-veld` has no `grace_days`.** Nobody has drawn the line for them.
- **`cu-harrow` has no tickets at all.** A customer signed last week.
- **`T-135` has no `first_reply_at`; `T-136` has one set to null.** Both mean
  "nobody has answered", written two ways, as two different upstream systems
  really do write it.
- **`T-135` and `T-136` have no `reopens` field**, where every other ticket
  has `0`. Chapter 6 shows the filter that gets this wrong and the one that
  gets it right.
- **`ag-jules` is a new starter** with no tickets and `on_shift: no`.
- **The first-reply target for Escalations was not set until April.** March
  has no target -- not a target of nought.

---

Start at [chapter 1](tutorial/01-the-world.md).
