# 4. The first figure

> **The question.** *How loaded is each agent right now, and who should I
> move work away from?*

A group gives you buckets. A filter gives you a test. Neither is a number. A
**figure** is the thing that produces one -- one value per subject, stored,
and kept current as records move.

## The whole thing

```fig
# How many tickets this agent has in hand right now: open, and assigned to
# one of their handles. A nought here is a measured nought -- an agent with
# an empty queue -- and not a blank.
figure desk_agent.in_hand:
    display "{desk_agent} has {value} tickets in hand"

    depends:
        mine = desk_ticket.handled_by:{desk_agent} & desk_ticket.open

    calculate:
        count(mine)

    band:
        when value >= desk_agent.in_hand_over then "over"
        when value >= desk_agent.in_hand_warn then "warn"
        otherwise "ok"
```

And what it answers, as at 30 June:

| agent | in hand | band |
|---|---|---|
| Mira Halloran | 4 | **over** |
| Pell Okonkwo | 2 | **warn** |
| Nour Aziz | 1 | ok |
| Sindre Vik | 1 | ok |
| Tomas Beck | 1 | ok |
| Jules Amari | 0 | ok |

That is the board the previous chapter's naive version could not produce.

## Line by line

**`figure desk_agent.in_hand:`** -- the prefix is the **scope**: the kind of
thing this is one-value-per. This figure answers per agent. A citation later
reads `desk_agent.in_hand@2776b19457a4`, and the prefix is how a reader knows
what the number is *about* before reading a word of the definition.

**The `#` lines** are required. Not by style guide -- by the compiler, which
refuses a figure with no explanation, in these words: *"a figure nobody can
read is the thing this language exists to prevent."*

**`display`** is the sentence a movement gets reported under. `{desk_agent}`
fills in with the subject's name, `{value}` with the number. Like the
explanation, it is prose, and editing it never recomputes anything.

## `depends` -- which records

```fig
    depends:
        mine = desk_ticket.handled_by:{desk_agent} & desk_ticket.open
```

One line, one named set. Three things are going on:

**`handled_by:{desk_agent}`** -- the `:{...}` part addresses *this subject's
bucket* in that group. The figure is being computed for `ag-mira`, so this is
Mira's bucket: her eleven tickets. Writing the group without the bucket is
refused, because read whole it would look for a bucket keyed by the empty
string, find nothing, and answer zero for everybody, for ever, with nothing
thrown.

**`& desk_ticket.open`** -- `&` is intersection. `|` is union and `-` is
difference. Filters are written *without* a bucket, for the mirror-image
reason: a filter has only one bucket, so scoping it means nothing.

**The result** is Mira's tickets that are also open: four.

### The rule that makes this safe

You cannot write a test in here. There is no
`& where priority == "urgent"`. Anything you want to narrow by must already be
a declared filter, with a name.

This is the language's one genuine safety property, and it is worth
understanding why it exists rather than just obeying it. The engine keeps
these numbers current by watching *which records moved*. It can do that
because `depends` is written entirely in terms of declared memberships -- it can
see, for any record that changes, exactly which figures that record could
possibly affect. A test written inline would narrow the population by reading
record *contents*, which the subscription machinery cannot see. The definition
would then be claiming to depend on things it does not, and the number would
quietly stop updating.

So: **a definition may only narrow by things that are themselves declared.**

## `calculate` -- the number

```fig
    calculate:
        count(mine)
```

`count` of a set is how many records it holds. A count of an empty bucket is a
**real nought** -- a claim that somebody has nothing -- and it is not the same
as no answer at all.

Which brings us to the thing that runs through this whole engine.

## An absence is never a zero

Look at the two bottom rows of that board.

**Jules Amari reads 0.** Jules is on the roster, has no tickets, and the
engine writes a real, measured nought for him. "Jules is carrying nothing" is
a finding. It is also what stops a departed agent from silently keeping their
last count.

But now suppose the whole ticketing sync had failed last night and *no*
tickets had arrived at all. Every agent would read 0, and the board would be a
complete, confident table of zeroes -- indistinguishable from a desk with an
empty queue. Unless something says which it is.

Something does. **Every answer carries a state**, and it has exactly four
values: `ok`, or unavailable for one of three reasons.

| state | what it means |
|---|---|
| `ok` | here is the number |
| `never-computed` | this tenant has never run this definition. A brand-new deployment, or the gap between shipping a definition and the next sync |
| `behind-deploy` | values exist, but at an older version of this definition. They are withheld, because a number computed by a definition that no longer exists is worse than a dash |
| `nothing-collected` | the definition ran and nothing it reads holds anything. No source connected -- not an empty queue |

A screen that ignores the state renders nothing at all, and never a
fabricated zero. A screen that reads it can tell its user the truth: not "0",
but *"not measured, and here is why."*

This is the single idea that most changes how a product feels. You have
probably shipped a dashboard where a dash might mean "nothing happened" or
might mean "the job failed". Nobody can act on that dash. Here there is no
such dash.

## `band` -- the word beside the number

```fig
    band:
        when value >= desk_agent.in_hand_over then "over"
        when value >= desk_agent.in_hand_warn then "warn"
        otherwise "ok"
```

The second thing a figure answers: which of a few states it is in. Rungs are
tested top to bottom, first match wins, and the ladder must end in
`otherwise`.

**The threshold is a fact.** `desk_agent.in_hand_over` is a field on *this
agent's own record* -- Mira's is 3, Tomas's is 4. It costs no declaration; it
is a number somebody typed onto a record, and it is right there in the
evidence when a reader asks why Mira is red and Tomas is not.

What it is emphatically *not* is a setting on a configuration page. On a board
whose whole claim is that every number can be traced to the records behind it,
the one input that decides whether a reader should *worry* must not be the one
input that cannot be traced. If a threshold is computed rather than typed, you
name a **figure** instead (chapter 8 does exactly that). If it genuinely never
varies, you write the number in the definition, where a reader can see it and
where changing it forks the version like any other change of meaning.

Two more properties of a band worth knowing:

- **Two conditions that must both hold are two rungs.** There is no `and`, no
  `or`, no `not`. A ladder of single comparisons reads as a list of claims a
  reader can check one at a time; a boolean expression does not.
- **A ladder stops on an unknown rather than falling through.** If an agent's
  `in_hand_over` had never been filled in, the comparison cannot be decided,
  and the ladder stops -- it does not slide down to `otherwise` and colour them
  green. Banding somebody the engine has not measured as *comfortable* is the
  confident wrong answer this whole design is arranged around avoiding.

## Bands are free to change

A band is evaluated when the figure is *served*, and stored nowhere. Move a
threshold and the board re-colours on the next request; nothing rebuilds. The
band is still part of the definition's version -- a figure that starts banding
differently is a different definition -- and that costs nothing, precisely
because no stored value hangs off it.

## See it

This is the first number in the tutorial with more than one step, so it is the
right one to look at in the UI's **worksheet**:

```
http://localhost:8080/ui/#/work/desk_agent.in_hand/ag-mira
```

That page prints the sentence, the stored value beside its version, and then
the working: the set expression with what each side held and what the
intersection removed, the count, and each band rung with its verdict --
matched, failed, unknown or not reached. Nothing on it is computed by your
browser; it is rendered by the same evaluator that wrote the number.

Two figures the same shape, for practice:

```fig
# How many tickets this customer has open with the desk.
figure desk_customer.open_tickets:
    display "{desk_customer} has {value} tickets open"

    depends:
        theirs = desk_ticket.raised_by:{desk_customer} & desk_ticket.open

    calculate:
        count(theirs)
```

| customer | open |
|---|---|
| Kestrel Studios | 2 |
| Luma Health | 2 |
| Northwind Traders | 2 |
| Oakline Bakery | 2 |
| Veld Analytics | 2 |
| Brightpath Logistics | 1 |
| Harrow & Sons | 0 |

Harrow signed last week and has raised nothing. A measured nought.

---

Next: [5. Measuring](05-measuring.md)
