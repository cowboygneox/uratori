# Concepts

uratori answers one question, repeatedly and defensibly: *what is this number,
and why?* A host application pushes plain records at it, declares once what
its world looks like, and writes definitions -- small, versioned programs --
that say what every number means. The engine computes those numbers
incrementally as records move, and serves each one back with the version that
computed it and the evidence behind it.

This page is the model in full, starting where the engine starts: with the
facts. It uses one running example throughout: a courier dispatch service,
whose world is orders and couriers, and whose screens want to know how loaded
each courier is. The other pages assume this one:
[the definition language](language.md) for writing `.fig`,
[Setup](setup.md) for running the service, and
[the HTTP & websocket API](http-api.md) for driving it.

## Facts

A fact is a plain JSON record with an identity -- a *kind* and a *key* -- and
a body of fields the host chose. Here is the courier world in full: two
couriers, three orders.

```
shop_courier "c1"  { "name": "Aki" }
shop_courier "c2"  { "name": "Bo" }

shop_order "o1"    { "ref": "A-1", "courier_id": "c1", "status": "riding" }
shop_order "o2"    { "ref": "A-2", "courier_id": "c1", "status": "riding" }
shop_order "o3"    { "ref": "B-7", "courier_id": "c2", "status": "delivered" }
```

The correlations are already in the bodies. `courier_id` is how an order
relates to a courier: `"o1"` and `"o2"` carry `"c1"`, so they are Aki's, and
`"o3"` is Bo's. `status` says which orders are still in hand -- two riding,
one delivered. Nothing in the records marks those fields as special.
*Declaring* that `courier_id` correlates orders to couriers, and that
undelivered means open, is exactly what a group and a filter do
([Definitions](#definitions-and-the-compile), below); read that way, these
five records already hold the page's number: Aki is carrying two orders, Bo
none.

That is the whole of what the engine knows. It does not fetch facts from
anywhere, does not interpret them beyond what definitions read, and never
writes them -- how records arrive, from which provider, on what schedule, is
entirely the host's business. The engine's side of the contract is narrow on
purpose: given every record of a kind (or some records by key), compute what
the definitions say. There are no filters, no orderings, no projections on
the read path, because every convenience a fact source grows is a way a
calculation could start depending on where the records live -- rule 4 of
[the four rules](#the-four-rules), at the storage boundary.

Facts carry their own names. Aki's record has a `name` field, and the schema
(next section) says so; when the engine writes a computed value it freezes
the subject's rendered name alongside it, so a courier renamed next week does
not rewrite the history of what they carried.

## The schema

A `Schema` is the one declaration of the host's world, handed over twice --
when the definitions are compiled, and when the engine is constructed -- and
never threaded through individual calls, because two call sites disagreeing
about the world is a class of bug the object exists to make unwritable.

```json
{
  "kinds": ["shop_courier", "shop_order"],
  "name_fields": {"shop_courier": "name", "shop_order": "ref"},
  "url_fields": {"shop_order": "url"},
  "figure_settings": ["limits.carrying.over"],
  "defaults": {"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}}
}
```

(As JSON because that is the wire: this exact document is what `PUT /schema`
takes. `GET /schema` answers it back with every absent list made explicit --
`bucket_settings: []` and friends -- so a diff against what you sent compares
shapes, not omissions.)

Four things live here, and each is a decision the host owns:

- **Kinds** are the closed set of record kinds a definition may name. Closed
  because kinds are compile-time: they are written into definitions and
  hashed into versions. A kind must lex as one identifier in the definition
  language -- `code_review-request` is refused outright, because `-` is the
  set-difference operator and such a kind could never be written at all.
- **Name fields** say which field of a record carries its human-facing name,
  per kind. A kind with no name field renders as its raw id: honest, and ugly
  enough that the checker refuses to split a figure across such a kind.
  **Url fields** are the same decision for a record's link -- evidence
  members carry one so a reader can walk from a cited record to the source
  system. Declared rather than guessed, because a field that happens to be
  called `url` is a host convention the engine was never taught.
- **Settings lists** declare which dials definitions may read -- four lists,
  split by cost. More on these [below](#settings-dials).
- **Defaults** are the shipped settings document, the base every tenant's
  sparse overrides are completed against.

One dial path is reserved rather than declared: `tenant.hoursPerDay`, which
the renderer divides by to print an *effort* (seconds of working time) as
days. A host that renders efforts must carry it in `defaults`.

### Or declare the facts in the language

Kinds, name fields and url fields can instead be declared where the
definitions live, as `fact` declarations -- the courier world above, written
that way:

```
# An order in the shop, as the provider last showed it.
fact shop_order:
    name ref
    url url
    ref as text
    url as text
    courier_id as text
    status as text
    handling_seconds as number

# A courier on the road.
fact shop_courier:
    name name
    name as text
```

The schema document then carries **settings and defaults alone**, and the
compile refuses a schema that also declares kinds: one world, one door.
What the fuller declaration buys -- field existence and type checks on every
path a definition reads, verification of every record that arrives, and a
schema a reader can trace a number back to -- is
[the language guide's to tell](language.md); records themselves are still
plain JSON, and a schema-taught world keeps working exactly as this page
describes.

## Definitions and the compile

Definitions are written in a small language (`.fig` files, by convention) and
compiled against the schema into a `Library`. Eight kinds of declaration
exist; the courier world uses four:

```
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

measure shop_order.effort = handling_seconds in effort

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

# Whether a courier is over the carrying limit.
figure shop_courier.load_band:
    display "{value}"

    combine:
        carrying = shop_courier.carrying

    calculate:
        when carrying >= limits.carrying.over then "over"
        otherwise "ok"
```

The `#` lines above each figure are its *explanation*. The compile attaches
that comment run as the declaration's customer-facing doc, served wherever
the number is cited, and refuses a figure, reading, projection or summary
that arrives without one -- a number nobody explained is a number nobody can
defend. Groups, filters and measures may carry one but are not required to:
they are plumbing, not numbers a reader meets.

- **Groups** bucket records by a field, one bucket per value: `carried_by`
  files each order under its courier. **Filters** hold the records matching a
  test: `open` holds the undelivered ones. Together they are the only way a
  definition reaches a population.
- **Measures** read quantities off records, with a declared unit, so a number
  is never a bare float of unknown meaning.
- **Figures** are stored, per-subject values, recomputed incrementally as
  facts move. `carrying` counts each courier's open orders; `load_band` is a
  figure built *on* a figure (the `combine` block), banding the count against
  a settings dial.
- **Readings** summarise a time-keyed figure over trailing windows -- "orders
  delivered over the last 30 days" is a reading over a per-day deliveries
  figure.
- **Projections** assemble live rows at the instant they are asked -- a
  worklist, with per-row values, sentences and a server-decided order.
- **Summaries** (`summarise ... over ...`) put a projection's population into
  one row: counts and totals *defined* as being over the rows, so there is no
  second route that could count them differently.

Compilation is a real checker, not a parser with opinions kept to itself. A
definition that names an unknown kind, reads a dial from a position the
schema did not grant, or composes statistics into something no definition
claims (a `sum` beside a `mean` is two numbers a reader can divide) is
refused, with a line number and the reason in the checker's own words. The
full language, and the reasoning behind each refusal, is in
[the definition language](language.md).

The schema is a compile-time input exactly as the source is -- but it is
deliberately **not** hashed into versions. A version is the hash of a
definition's semantics, and the same definition under two hosts is the same
definition.

## The four rules

Everything on this page is in service of four rules. They are inherited from
the project this engine grew out of -- a standup board whose whole premise
was that no number on screen is unexplained -- and each one was learned by
watching it fail.

1. **One calculation system.** A number means a definition. There is no second
   place arithmetic can live: no helper on the way out, no mapping function in
   a client, no "quick count" beside the real one. Two places computing one
   number is how they come to disagree, and the disagreement is always
   discovered by a reader.

2. **Clients compute nothing.** Values arrive rendered beside their
   magnitudes, and the payload carries no raw collection to recompute from.
   The predecessor *intended* the browser not to calculate and lost it anyway,
   because the arrays were in the payload and a client function quietly
   reduced over them. Here there is nothing to reduce.

3. **An absence is never a zero.** Missing means *not computed*, and every
   response says why. A dash that might mean "nothing happened" or might mean
   "nothing was measured" is a dash nobody can act on.

4. **A cheap path may not narrow a population** -- neither the one a
   calculation runs over nor the one it is reported over. The incremental
   path exists to be fast, not to be a different calculation; a scoped run
   must leave exactly the state a full run would.

These are not aspirations. Each is enforced somewhere concrete: rule 1 by the
checker refusing every second route to a number (a projection may not
aggregate -- that is a figure's or a reading's job -- and a `sum` may not sit
beside a `mean` a reader could divide it by), rule 2 by the `Result` shape
carrying no numeric collections, rule 3 by `state` being a required
discriminated value rather than a nullable number, rule 4 by escalation to
full passes whenever the warm path cannot be proven equivalent (see
[the cascade](#the-incremental-cascade), below).

## Versions

Every definition's version is a content hash of its semantics -- the parts
that decide the number, canonicalised and hashed. This has two properties,
both deliberate and both tested:

- **Prose does not fork a version.** Rewording the explanation above a
  declaration must not recompute three hundred stored values. Only what
  changes the answer changes the hash.
- **A changed definition is a new definition, not an edit.** The version is
  part of the key every stored value lives under, so changing a definition is
  a *cache miss*, never an invalidation: the new version simply has no rows
  yet, and recomputing is a fresh write. The old version's values stay
  intact, which is what lets any history that cites `shop_courier.carrying @
  7a65feeb434b` show exactly the formula that computed it, even after the
  definition has moved on.

The version travels on every answer. It is the citation: a screen showing a
number can always say which written definition produced it, and a reviewer
diffing a definitions file sees precisely which versions fork -- semantic
changes move hashes, prose does not.

## The incremental cascade

The engine's ordinary work is the warm path. The host says which records were
written (`written={"shop_order": ["o2"]}`), and the engine:

1. Re-derives the changed records' index memberships. The *diff* of each
   bucket -- who was added, who was removed -- is the invalidation signal.
2. Recomputes the figures whose populations those buckets feed, for exactly
   the subjects the diff touched.
3. Cascades: a figure built on a figure that moved is recomputed too, in
   dependency order. When the courier's third order arrives, `carrying` moves
   from 2 to 3, and `load_band` -- which never looks at orders at all --
   moves from `ok` to `over`, because the figure beneath it moved.

What comes out is a complete change stream: every value that moved, with both
ends (`before`, `after`), and every value that was *removed* -- a departed
subject that vanished silently would leave a screen counting somebody who is
gone. An unchanged recompute reports nothing, because a sync in which nothing
happened filling the log is how the log stops being read. And a failed run
raises rather than reporting an empty list: "nothing changed" is itself
information, and a run that moved nothing must stay distinguishable from a
run that did not finish.

The warm path is held to rule 4 by test, not by hope: a scoped run must leave
byte-for-byte the state a full run would, and the suite carries a control -- a
deliberately narrowed run that *disagrees* -- to prove the assertion can fail.

### Full passes, and when the engine escalates

Some situations rebuild a figure from all facts rather than incrementally:

- **A cold pointer.** Each tenant carries a pointer per definition: which
  version it has computed, under which settings fingerprint. After a deploy
  ships a changed definition, or a dial moves, the pointer no longer matches,
  and the next pass rebuilds that figure -- and only that figure -- from
  scratch.
- **A pass that observed a deletion runs full, whatever shape it had.** The
  warm path honours a deletion list, but the cold branch -- the ordinary
  state between a deploy and the next sync -- never reads it. A deletion
  arriving while any pointer is stale would be dropped on the floor, and the
  departed record would keep its index memberships until the next full pass.
  Rather than depend on the caller noticing that window, the engine escalates
  every deleting pass to full: correct in every branch by construction, and a
  full recompute is a fair price on the rare pass that reports something
  gone.
- **A moved record of a kind groups only resolve *through* runs full**, for
  the same reason from the other direction. Consider
  `group shop_order.carried_by from courier_ref through shop_courier.handles`:
  orders name a courier by handle, and the courier record is how handles map
  to couriers. No group or filter is over `shop_courier` itself, so when a courier's
  handle changes, the warm path sees the write and rebuilds nothing -- and
  every order that resolved through the old handle stays filed under the old
  answer. The origin project shipped exactly this bug. These moves are rare
  (operator actions, identity changes); the recompute is the same fair price.

Both escalations happen at the engine's front door, not in the caller's
judgement, because each is a way the warm path can be silently wrong and
neither should depend on anyone remembering.

## Tenants

A tenant is a pure data partition of one world. Facts, index memberships,
computed values, pointers and settings are all keyed by tenant; the schema
and the definitions are not. Two tenants running the same definition are
running *the same definition* -- the content hash says so -- and what differs
per tenant is only which version it has computed and what its dials are set
to. There is no per-tenant code path anywhere, which is what makes "this
tenant sees a different number" always answerable by data: different facts,
or a different dial.

## Settings dials

Definitions read named dials -- `limits.carrying.over` in the banding figure
above -- and tenants set them. Two design decisions carry the weight here.

**A tenant's stored settings are sparse; completion happens once, at the
boundary.** A tenant's document contains only what an operator changed. Every
calculation needs a complete document, so the engine merges the tenant's
document over `schema.defaults` exactly once, at its front door. Everything
below assumes a complete document and *raises* on a missing dial: a
definition named the dial, so there is nothing to guess, and a fallback would
band every subject against a number nobody chose. (Two layers each applying
defaults is also how an evaluation and an invalidation come to disagree about
what a dial is set to -- one boundary means one truth.)

**Dials are declared in four lists, split by what turning one costs:**

| List | Read from | Moving it invalidates |
|---|---|---|
| `bucket_settings` | a group's `by day in ...` and a filter's `older/younger than ...` | the tenant's whole bucketed history -- day boundaries moved, so every membership is suspect |
| `figure_settings` | figure calculations | one stored value per subject, for each figure naming the dial |
| `reading_settings` | reading statistics and bands | nothing stored -- the next read evaluates under the new dial |
| `project_settings` | projection values and flags | nothing stored -- rows are assembled when asked |

The split is not bookkeeping: merging any two lists would let a definition
write a dial into a position the engine cannot honour. And the pointer each
tenant carries includes a *fingerprint* of the dials that definition names,
so moving `limits.carrying.over` rebuilds `shop_courier.load_band` and
nothing else -- and a dial explicitly set to its default fingerprints
identically to an unset one, so a no-op save costs no rebuild.

## Results

Every answer -- served over HTTP or pushed over the websocket -- is one
`Result`, and the same object on either transport. The envelope carries the definition's `version` (the citation),
`at` (one evaluation instant for the whole response), the tenant's calendar
`zone`, the `unit` (so a renderer never guesses whether 28,800 seconds is
"8h" of wall-clock or "1d" of working time), and `subjects`: one row per
subject, id, frozen name and value together, in the server's order -- a
screen does not sort, because sorting is a calculation and the sort key is a
definition's answer.

Numbers travel twice, deliberately: `value` (the magnitude, for anything
positional -- a bar's width, a marker's offset) and `display` (the text a
reader sees, rendered by the server). Rendering is on the server because
rendering a duration or an effort is a *division*, against a dial the client
must not hold -- a client that knows `hoursPerDay` is one step from banding
against a threshold. Band levels are the same story: a `level` is a word from
the definition, mapped to a colour by the renderer and never re-derived from
a number, because banding in two places is how a card reads Watch while a
sort weighs the same person as Good.

Behind every stored value sits its **evidence**: the record ids the value was
computed from, written beside it at compute time. `GET
/tenants/{t}/evidence/{figure}?subject=...` joins that citation back to the
records -- titles and links resolved through the schema's name and url fields
-- so "2 orders in hand" is traceable to the two orders. It is a separate
fetch rather than a field on `Result`, because every row dragging its members
along would make the common read pay for the rare check.

### The four absences

`state` is a discriminated value: `ok`, or unavailable with one of exactly
four reasons.

- **`never-computed`** -- this tenant has never run this definition. A new
  board, or the window between a deploy and the next sync.
- **`behind-deploy`** -- values exist, at an older version of the definition.
  They are not shown, because a number computed by a definition that no
  longer exists is worse than a dash.
- **`setting-moved`** -- a dial the definition names has changed and the
  rebuild has not finished. The stored values describe the old dial.
- **`nothing-collected`** -- the definition ran and nothing it reads holds
  anything. A board with no source connected, rather than a courier fleet
  with an empty queue.

The last one is why rule 3 is load-bearing rather than pedantic. The engine
writes a measured nought for every subject on the roster -- "Aki is carrying
0 orders" is a finding, and a departed courier must not silently keep their
last count. So a tenant with no facts connected stores a complete, confident
table of zeroes, indistinguishable from a fleet with nothing in hand *unless
something says which it is*. `nothing-collected` is that something. A screen
that ignores `state` renders nothing -- never a fabricated zero -- and a
screen that reads it can tell its user the truth: not "0", but "not
measured, and here is why".

## Where next

- Writing definitions: [the definition language](language.md).
- Running the service: [Setup](setup.md), then
  [the HTTP & websocket API](http-api.md).
