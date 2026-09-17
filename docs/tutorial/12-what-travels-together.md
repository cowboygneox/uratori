# 12. What travels together

> **The question.** *The agent card on our dashboard shows four things. Right
> now the screen makes four requests and stitches them together. Can the
> definitions say what belongs on that card?*

Yes, and that is the whole of what a **bundle** does. It is the composition
layer: the eight declarations before it answer *how is this computed*; a
bundle answers *what travels together*.

```fig
# Everything the agent card on the desk dashboard shows.
bundle desk_agent.card:
    in_hand  = figure desk_agent.in_hand
    closes   = reading desk_agent.closes_monthly over 1, 1-3
    queue    = projection desk_ticket.queue
    health   = summarise desk_ticket.queue_health
```

One request, four answers, in the written order, each carrying its own
version and provenance exactly as it would if you had asked for it alone.

## A bundle defines no calculation

Members are names plus arguments, and that is all. No `depends`, no
`calculate`, no unit, no band, no arithmetic between members of any kind.

This is a real constraint and it is worth being clear about why it is not an
oversight. A number derived from two members would be a number with no
definition claiming it -- which is the exact thing every chapter of this
tutorial has been arranged against. If the card wants a total of two things,
that total is a figure, with a name and a version, and the card names *it*.

Serving a bundle *triggers* evaluation of its members; every rule that makes a
number trustworthy stays in the member's own definition and its own hash.

## Slots are addresses

```
    in_hand  = figure desk_agent.in_hand
```

The left-hand side is a **slot** -- the address a client reads that member at.
It is required on every member, and it is what decouples the card's layout from
the definitions' names: the screen binds to `card.in_hand`, so renaming the
figure is a one-line change to the card rather than a change to every screen
that shows it.

A slot is an address and **never a display label**. Each member's answer keeps
its own definition's label and explanation, because nothing may let a card
rename what a number is called.

Each member is also written under its own keyword. `figure desk_agent.in_hand`
compiles only if that name really is a figure. Writing `figure` over something
that is actually a projection is refused by what it actually is, because
compiling as "whatever that turns out to be" would make what travels a
surprise.

## Windows belong to the card, not the caller

```
    closes = reading desk_agent.closes_monthly over 1, 1-3
```

Only a windowed reading may carry a window list, and the order is substantive:
a screen may bind to positions, so `over 30, 60` and `over 60, 30` are two
differently-shaped answers and both may exist.

Here the card asks for two windows over one reading: **this month alone**, and
**the last three months pooled**. Two slots may name the same reading when
their window lists differ -- this month beside last quarter is two questions
about one definition -- while the same member over the same windows is refused
as two names for one answer.

**No units ride on the spans.** What a bucket *is* lives in the figure's group
clause, hashed there, so a card serving a monthly figure walks months because
the *figure* says so and never because the card does.

And a caller cannot move them. A tile whose windows the requester could change
would be a different tile under the same version.

## Two shapes a card must not subscribe to

Both refused at build time rather than at serve time:

- **A time-keyed figure** as a `figure` member would drag every stored bucket
  of every subject along on every load. Declare a reading over it and name
  that.
- **A figure split `across` a dimension** serves pairs, not subjects. Name the
  rollup that adds its parts up -- which is exactly what
  [`desk_agent.closed_total`](07-days-and-dimensions.md) is for.

## A summary can travel without its rows

```
    health = summarise desk_ticket.queue_health
```

This is the serving capability a bundle adds. The row payload stays home, and
the summary is **still computed over all the projection's rows**, never the
page -- a summary of a page is a wrong number that reads right.

There is deliberately no "with rows" modifier. A `projection` member means
rows; a `summarise` member means the one row; a card wanting both names both --
in which case the projection is evaluated once and both members are served
from it, so the two cannot disagree. Our card names both, which is why the
queue rows and the headline above them are guaranteed to be about the same
ten tickets.

## One instant, one level, one review token

**Members are served at one instant.** The clock is read once per card request
and handed to every member that takes one, extending the projection's
one-instant rule to the whole card. A page beside a headline evaluated at two
different moments can disagree with itself.

**Composition stays flat.** A bundle may not name another bundle, so there is
no nesting to walk and no cycle to refuse.

**The card's own version cites nothing.** A bundle is versioned like every
other declaration, so a changed card shows as a moved hash in the committed
library -- which is the review surface. But that hash appears in no storage key
and in no number's citation. Nothing on screen ever cites the card; every
number inside it cites its own member.

What is hashed is the member list: slots, keywords, names and window
arguments, in written order. Member *versions* stay out, deliberately -- a
member redefined underneath shows as **that member's** moved hash, on the
artifact and on the wire, while the card's composition, which did not change,
keeps its version.

## What comes back

```
kind:     bundle
name:     desk_agent.card
version:  db6a68299077

  slot in_hand   figure       desk_agent.in_hand @ 2776b19457a4      ok
  slot closes    reading      desk_agent.closes_monthly @ 5c35fd118fcd   ok
  slot queue     projection   desk_ticket.queue @ b73102687941      ok
  slot health    summary      desk_ticket.queue_health @ d035ee3ce254   ok
```

There is no card-level state. "One number is behind a deploy" is a per-member
fact, and flattening it into a single verdict for the card would either hide a
good number or condemn three of them.

---

Next: [13. Citations and evidence](13-citations-and-evidence.md)
