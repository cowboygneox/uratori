# 13. Citations and evidence

> **The question.** *Mira's queue said 3 yesterday and says 4 this morning.
> What changed?*

This is the question the whole engine exists to answer, and it is the one that
usually cannot be. The number came from somewhere; by the time anybody asks,
the code has moved on, the query has been edited, and the honest answer is a
shrug and a re-run.

Here it is answerable in three steps, and each one is a thing you have already
met.

## A version is a citation

Every definition's version is a **content hash of what it means**: the parts
that decide the number, canonicalised and hashed into twelve characters.
`desk_agent.in_hand@2776b19457a4`.

Two properties, both deliberate, both held by tests in this repository:

**Prose does not fork a version.** Rewording an explanation, a display
template or a group's label changes nothing. This matters more than it sounds:
if editing the sentence a customer reads recomputed three hundred stored
values, nobody would ever improve the sentence.

**A changed definition is a new definition, not an edit.** The version is part
of the key every stored value lives under, so changing a definition is a
*cache miss* rather than an invalidation. The new version simply has no rows
yet; recomputing is a fresh write. The old version's values stay exactly where
they were -- which is what lets any history citing
`desk_agent.in_hand@2776b19457a4` show precisely the definition that produced
it, long after the definition has moved on.

What is hashed is everything that could change the answer: the full
specification of every group, filter and measure the definition reads (so
redefining what `desk_ticket.open` *means* moves every figure that counts it,
though their own text is untouched), the calculation, ladders, bands,
`across`, `carried forward`, a reading's statistics and floor, a projection's
fields and flags and `omit`, a summary's projection.

What is not: prose, `label`, `keyed as`, `bucketed`, where a declaration sits
in the file, and the schema itself. The rule behind that list is worth
carrying: **what decides the number is hashed; what decides what the checker
permits is not.**

So when a version moves in your library diff, something about an answer
changed, and when it does not, nothing did. That is the review surface.

## Evidence: the records behind the number

Behind every stored value sit the record ids it was computed from, written
beside it at compute time. Ask for them and the engine joins that citation back
to the records themselves, resolving titles and links through the `name` and
`url` fields [chapter 1](01-the-world.md) declared:

```
GET /tenants/desk/evidence/desk_agent.in_hand?subject=ag-mira
```

Four records: T-124, T-128, T-129, T-130. Each with its subject line and a
link straight into the ticketing tool.

So *"Mira has 4 tickets in hand"* is not a number to be trusted. It is a claim
with its four witnesses attached, and any of them can be opened and argued
with.

It is a separate request rather than a field on every answer, because a board
of forty numbers dragging their members along would make the common read pay
for the rare check.

## The worksheet

Evidence tells you *which records*. The worksheet tells you *how*:

```
http://localhost:8080/ui/#/work/desk_agent.in_hand/ag-mira
```

It is a school-child's "show your work" rather than a flat list. The title
block prints the sentence, the stored value beside its version, and -- when a
live re-derivation disagrees -- the sentence saying so, because a record has
moved since the pass that wrote the row.

Below that, the working: a tree of the calculation **as declared**, each step
with its expression on the left and its value on the right, nested exactly as
deep as the calculation is.

- Sets show what each narrowing removed. Mira's bucket held eleven tickets;
  `& desk_ticket.open` took seven away.
- A sum or an extreme shows every record it read, **including the ones that
  carried no measurement and so contributed nothing** -- present and stated,
  never quietly dropped.
- A ladder shows every rung's verdict in order: matched, failed, unknown, or
  not reached.
- An operand that is itself a stored value opens its own worksheet in place.
  `desk_agent.headroom` reads `desk_agent.in_hand`, so its worksheet contains
  this one.

Nothing on that page is computed by your browser. The tree, the notes and
every rendered value come from the one evaluator the engine itself runs --
which is the only way the explanation can be guaranteed to describe the
calculation rather than to re-implement it.

## The cascade: why the number moved

Now the actual answer to the question at the top.

The engine's ordinary work is incremental. Your systems say which records were
written; the engine:

1. **Re-derives those records' memberships**, and the *diff* of each bucket --
   who was added, who was removed -- is the invalidation signal.
2. **Recomputes the figures whose populations those buckets feed**, for exactly
   the subjects the diff touched.
3. **Cascades.** A figure built on a figure that moved is recomputed too, in
   dependency order.

So T-130 arrives, assigned to `m.halloran`. It lands in Mira's
`handled_by` bucket and in `open`. `desk_agent.in_hand` moves from 3 to 4, and
its band from *warn* to *over*. `desk_agent.headroom`, which never looks at a
ticket at all, moves from 0 to -1, because the figure beneath it moved. So
does `desk_agent.over_by` above that, and `desk_team.spare_seats` above that.

What comes out is a complete change stream: **every value that moved, with both
ends** -- before and after -- and every value that was *removed*, because a
departed subject vanishing silently would leave a screen counting somebody who
is gone. An unchanged recompute reports nothing, because a sync in which
nothing happened filling the log is how the log stops being read. And a failed
run raises rather than reporting an empty list: "nothing changed" is itself
information, and it has to stay distinguishable from "the run did not finish".

The UI's **Activity** screen is that stream, one entry per pass, newest first,
cause before effect: what arrived, and then the movements it caused, each one
`before -> after` in text frozen at the moment it happened.

That is the answer to "what changed overnight?": not a re-run, a record.

### When the engine gives up on being clever

Three situations make a pass rebuild from all the facts rather than
incrementally, and each is a place where the fast path could be silently
wrong:

- **A definition changed.** Each tenant carries a pointer per definition
  saying which version it has computed. After a deploy the pointer no longer
  matches, and the next pass rebuilds that figure -- and only that figure --
  from scratch. Until it does, the answer is `behind-deploy`, which is a
  stated absence rather than a stale number.
- **A pass observed a deletion.** The incremental path honours a deletion
  list, but the cold branch -- the ordinary state between a deploy and the next
  sync -- never reads it, so a deletion arriving in that window would be
  dropped on the floor. Rather than depend on anybody noticing, every deleting
  pass runs full.
- **A record moved in a kind that groupings only resolve *through*.** This is
  the identity hop from [chapter 3](03-the-identity-hop.md) and the calendars
  from [chapter 7](07-days-and-dimensions.md). Add a handle to Mira's record
  and no *ticket* has changed -- but every ticket naming that handle now
  belongs somewhere new. The incremental path cannot see that from the
  tickets, so the pass escalates.

All three happen at the engine's front door, not in anybody's judgement,
because each is a way the fast path can be quietly wrong and none of them
should depend on being remembered.

## Tenants

A tenant is a pure data partition of one world. Facts, memberships, computed
values and pointers are all keyed by tenant. The **schema and the definitions
are not**.

Two tenants running `desk_agent.in_hand` are running *the same definition* --
the content hash says so -- and what differs is only which version each has
computed. There is no per-tenant code path anywhere and, since the settings
went, no per-tenant configuration either.

Which is what makes *"this customer sees a different number"* always answerable
the same way: **different facts.** Not a different build, not a flag, not a
value somebody typed into a form three years ago. Go and look at the records.

## No settings, and why that is the last brick

You have not been asked to configure anything in thirteen chapters. That is
not because this tutorial skipped it.

Every number a definition needs is either a **fact** -- a field on a record, or
a figure computed from the records that set it -- or it is **written in the
definition**, where a reader can see it and where moving it forks the version
like any other change of meaning. A band's threshold, a calculation's, a row
value's, a flag's; the calendar a grouping cuts its buckets on; the line an age
filter draws. All of them.

There used to be four lists of dials, and machinery to make them safe:
fingerprints on every pointer so a moved dial rebuilt exactly what read it, a
boundary where sparse overrides became complete. All of it careful, and all of
it existing to make one thing safe that should not have existed. On a card
where every number can be traced to the records behind it, a dial was the one
input that could not be -- and the number it moved hardest was the one deciding
whether a reader should worry.

So Mira's over-line is `in_hand_over: 3` on Mira's record. It is in the
evidence. It is in the worksheet. If somebody changes it, that change is a
fact arriving, with a time on it, and the board moves through the ordinary
cascade like everything else.

---

## Where next

- [The definition language](../language.md) -- the full reference, and the
  argument behind every rule this tutorial used. Most constructs have an
  explicit paragraph on what the obvious alternative would have done instead.
- [Concepts](../concepts.md) -- the model in one page: facts, schema,
  versions, the cascade, the three absences.
- [The HTTP & websocket API](../http-api.md) -- how records get in and answers
  get out, including the socket that pushes what moved.
- [Setup](../setup.md) -- running it for real.

And [`support.fig`](support.fig) is every definition in this tutorial, in one
file, in order. It compiles. Change something in it, push a fact, and watch
the Activity screen.
