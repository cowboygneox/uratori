# 3. The identity hop

> **The question.** *The queue board says nobody on Front Desk is
> overloaded. Mira says she is drowning. Who is right?*

Mira is right, and the board is not lying either. It is answering a subtly
different question from the one everyone thinks it is answering, and the
difference is one word in one line.

## What the ticket actually carries

A ticket does not name Mira. It names a login:

```
desk_ticket "T-129"  { "assigned_login": "mira",        ... }
desk_ticket "T-130"  { "assigned_login": "m.halloran",  ... }
```

Both are Mira. The desk merged two support tools three years ago and nobody
ever consolidated the accounts. The desk's own roster knows this -- it is right
there on the agent record:

```fig
# Somebody who answers tickets. An agent belongs to one team, works to one
# calendar, and carries the two numbers that decide when their queue is too
# full.
fact desk_agent:
    name name
    name as text
    team_id as text
    timezone as text
    in_hand_warn as number
    in_hand_over as number
    closes_target as number
    on_shift as flag
    # The handles this person signs in with. Several, because the desk grew
    # by acquisition and nobody ever merged the accounts.
    many logins:
        handle as text
```

Three of the six agents have two handles: Mira (`mira`, `m.halloran`), Nour
(`nour`, `naziz`) and Sindre (`sindre`, `sv-oncall`).

## The version that looks right

The field on the ticket is `assigned_login`, so the obvious grouping is:

```fig
# Every ticket, filed under the login of whoever is working it.
group desk_ticket.by_login from assigned_login label "filed under the handle on the ticket"
```

Count the open ones per bucket and you get the board the desk has been
running:

| handle | open tickets in hand |
|---|---|
| `m.halloran` | 2 |
| `mira` | 2 |
| `naziz` | 1 |
| `nour` | 0 |
| `pell` | 2 |
| `sindre` | 0 |
| `sv-oncall` | 1 |
| `tomas` | 1 |
| `jules` | 0 |

Nine rows. The largest is 2. Nobody is anywhere near a limit of three or four.
The board is calm, and the board is wrong -- **not in any single number**. Every
one of those nine numbers is correct about the thing it is counting. What is
wrong is the thing it is counting: a *handle* is not a person, and the board
is headed as though it were.

## What it silently produces

Mira is holding four open tickets: T-124 and T-129 under `mira`, T-128 and
T-130 under `m.halloran`. Find them in [the dataset](../tutorial.md#tickets):
four rows, `open` in the resolved column, her two handles in the login column.

The naive board splits her into two people with two tickets each, and **each
half looks comfortable**. That is the shape of this failure and why it survives
so long: it does not produce an obviously silly number, it produces several
plausible small ones. There is no row on that board that a reviewer could look
at and say "that is wrong".

Worse, the *word* beside the number goes the same way. Mira's over-line is
three. At four she is over it. At two-and-two she is not.

## `through` -- the hop

One clause fixes it:

```fig
# Every ticket, filed under the *person* behind the handle on it.
group desk_ticket.handled_by from assigned_login through desk_agent.logins.handle label "handled by this agent"
```

Read `through desk_agent.logins.handle` as: *take the value in
`assigned_login`, go and find the `desk_agent` record that owns that value
somewhere in `logins.handle`, and file the ticket under **that record's
key**.* So T-130's `m.halloran` resolves to `ag-mira`, and T-129's `mira`
resolves to `ag-mira`, and both land in the same bucket.

The buckets that result:

| agent | every ticket, open or not |
|---|---|
| `ag-mira` | 11: T-101, T-104, T-109, T-113, T-116, T-119, T-124, T-125, T-128, T-129, T-130 |
| `ag-nour` | 7: T-106, T-111, T-115, T-120, T-123, T-127, T-132 |
| `ag-pell` | 5: T-102, T-108, T-114, T-122, T-133 |
| `ag-sindre` | 4: T-105, T-110, T-117, T-134 |
| `ag-tomas` | 7: T-103, T-107, T-112, T-118, T-121, T-126, T-131 |

Six agents on the roster, five buckets: `ag-jules` has never been assigned
anything, so there is no bucket for him. That is not an oversight -- it is the
honest answer, and [chapter 4](04-the-first-figure.md) is about what the
engine does with it.

## Three details worth knowing

**The path crosses a `many`, and it flattens on purpose.**
`logins.handle` means *any handle of any login*. That is exactly what you want
here: one person, several doors.

**A handle claimed by two agents lands in both their buckets.** The engine does
not pick a winner. Duplicated identity is a data problem, and a number that
reflects it is a number somebody will notice and fix; a number that silently
resolves it is a data problem nobody ever discovers.

**Moving a handle re-files history, and the engine knows it.** If tomorrow
somebody adds `mira.h` to Mira's record, no *ticket* has changed -- yet every
ticket naming that handle now belongs somewhere new. The engine cannot see
that by watching tickets, so a write to a kind that groups only resolve
*through* makes the next pass a full rebuild rather than an incremental one.
This is handled at the engine's front door, not left to whoever remembered.

## Which one do you keep?

Both, in this tutorial, because the next chapter needs to show you both
boards. In a real file you would keep only `handled_by` -- there is no
question the desk has whose answer is "per login".

But notice what made the wrong one *findable*: it has a name, an explanation,
and a version. Somebody reviewing the file can read
`group desk_ticket.by_login from assigned_login`, ask "is a login a person?",
and the whole thing collapses in one sentence. The equivalent mistake spread
across three queries and a helper function does not collapse; it gets
rediscovered.

---

Next: [4. The first figure](04-the-first-figure.md)
