# The definition language

Every number uratori serves comes from a definition written in this language.
There is no second way to compute one -- that is the doctrine the engine
inherits: one calculation system, clients that compute nothing, absences that
are never zeroes, and no cheap path that narrows a population.

This document is an authoring guide and a reference for `.fig` source: enough
to go from an empty file to correct definitions, and enough to review somebody
else's. Each section says what a construct does and, where it matters, what
the obvious alternative would have done instead -- almost every rule here
exists because the alternative compiles, runs, and puts a plausible number on
a screen.

Definitions compile against a [`Schema`](concepts.md) -- the host's one-time
declaration of which fact kinds exist, which field carries a record's name,
and which field carries its link. Source reaches the engine
over `PUT /definitions` (see [the HTTP API](http-api.md)), where a definition
that does not compile is refused whole, in the checker's own words.

---

## A first definition

A world of couriers and orders. Facts arrive as plain JSON records, each with
an identity -- a *kind* and a *key* -- and a body of fields
([Concepts](concepts.md) carries the full model, over this same dataset):

```
shop_courier "c1"  { "name": "Aki", "max_orders": 3 }
shop_courier "c2"  { "name": "Bo",  "max_orders": 1 }

shop_order "o1"    { "ref": "A-1", "courier_id": "c1", "status": "riding" }
shop_order "o2"    { "ref": "A-2", "courier_id": "c1", "status": "riding" }
shop_order "o3"    { "ref": "B-7", "courier_id": "c2", "status": "delivered" }
```

The correlations are already in the data -- each order's `courier_id` names a
courier's key -- but nothing yet says what those fields *mean*. That is what a
definition declares, starting with the world itself:

```
# Somebody who carries orders, and how many they are cleared for.
fact shop_courier:
    name name
    name as text
    max_orders as number

# One order, from pickup to doorstep.
fact shop_order:
    name ref
    ref as text
    courier_id as text
    status as text

group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{shop_courier} has {value} orders in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value > shop_courier.max_orders then "over"
        otherwise "ok"
```

Reading it back: the group puts every order in a bucket per courier --
`o1` and `o2` land in Aki's, `o3` in Bo's; the filter holds whichever orders
are not yet delivered, which excludes `o3`. The figure intersects the two for
each courier and counts what is left, so Aki reads 2 and Bo reads 0 -- a
measured nought, not a blank -- and the band turns each count into a word by
comparing it against `max_orders` **on that courier's own record**. Aki is
cleared for three and holding two; Bo is cleared for one and holding none.

That threshold is a fact, and it costs no declaration: it is a field on a
record, read for the subject the value is about. A threshold that is computed,
or that has history worth keeping, is a **figure** the band names instead. What
it is never is a control living outside the data -- the number that decides
whether a reader should worry is the last one that should be unciteable.

The `#` line above the header
is the figure's **explanation** -- attached by the compile, served wherever
the number is cited, and required. Push another order at the engine and Aki's
count moves; push `o1`'s delivery and it moves back. Nothing else needs
writing -- no invalidation, no recomputation schedule. The definition *is*
the behaviour.

Everything below is that example, taken apart and extended.

---

## The shape of a file

Indentation is significant, Python-style, because the point of the language is
that somebody who does not write code can read a definition and say whether it
is right. Braces would cost a line of noise per block for no gain.

Keywords are **not reserved**. `group`, `filter`, `figure`, `depends` and the
rest come out of the lexer as ordinary names and the parser matches on their
text -- a reserved word list is a thing that grows and then collides with
somebody's filter called `display`.

There is **no line continuation**. Every statement is one line. A set
expression that wants to wrap should be split into named sets instead.

Blank lines are free, between declarations and inside blocks alike. Directive
order inside a block is free too -- each directive may appear at most once,
except `flag` anywhere and a summary's `count` and `total`, and the parser
accepts them in any order -- so the house
style spends that freedom on legibility: single-line directives (`display`,
`unit`, `sort`, `limit`) first and adjacent, then a blank line before each
directive that owns an indented body.

The pieces:

- **Comments** run from `#` to the end of the line. A `#` inside a string is
  part of the string -- `label "waiting #1"` is not truncated.
- **Explanations** are comments doing the language's most important job: the
  run of `#` lines directly above a declaration is attached by the compile as
  that declaration's explanation -- the customer-facing definition of the
  number, a product surface rather than a code comment, served wherever the
  number is cited. Every `fact`, `figure`, `reading`, `projection` and
  `summarise` is refused without one, in these words: *"figure shop_courier.carrying has no
  explanation. Write `#` comment lines directly above the declaration -- they
  are the customer-facing definition, rendered wherever the number is cited,
  and a figure nobody can read is the thing this language exists to
  prevent. (A `# ----` rule line is a file banner, not prose.)"* A group, a
  filter or a measure may carry one and is not made to. Three
  rules decide which lines belong. The run is unindented `#` lines, and may
  sit at most **one blank line**
  above the header -- the origin project measured what strict contiguity
  cost, and a third of its declarations rendered bare with their explanation
  stranded a single blank line up. **Two blanks** is a detached paragraph,
  and adopting it would attach prose the author never aimed here. And a
  **banner** -- a line of dashes (`# ----------`), a dash rule naming a
  file (`# ---- board.fig ----`), or a section rule with a dash run at both
  ends (`# ------------- reviews --`) -- is a rule line, not prose, and ends
  the run: the first is
  what a concatenating build writes between two `.fig` files, the last is
  what a long file draws between its own regions, and without the
  exception the declaration under either would adopt the rule above it as
  its explanation. (`# --- see the note below` is prose --
  the pattern is a *rule*, not any comment that starts with dashes.) The
  explanation is deliberately outside
  the version hash: rewording one must not fork a version and recompute every
  stored value. (The old spelling -- a triple-quoted docstring inside the
  block -- is refused at lex time with directions: *"a docstring. A
  declaration's explanation is written as `#` comment lines directly above
  it, not inside the block"*.)
- **Strings** are double-quoted, on one line, with `\` escaping the next
  character.
- **Numbers** are unsigned literals like `3` or `0.25`. There are no negative
  literals, because `-` is already two operators here -- subtraction and set
  difference -- and `mine -3` would be a difference or a literal depending on
  a space. A negative value comes out of a subtraction; a negative threshold
  is written the other way up (`when value + 100 < 0`).
- **Names** may contain dots, and the dot is meaningful. Every declaration is
  named `<fact kind>.<name>` -- `shop_courier.carrying` -- because a citation
  is `name@version` and the prefix says what the definition is about. Inside
  an expression, a *dotted* name is a group, a filter, another figure, or a
  field on the subject's own record, and a *bare* name is something the
  definition bound above; nothing else can produce either shape, so a typo is
  reported with the list of what was actually bound.

**One namespace covers all nine declaration kinds.** Two definitions sharing a
name would make a citation ambiguous, so the checker refuses the second
whatever kind it is.

**Declaration order matters in one place**: a figure may only read a figure
declared before it. A cycle has no line number, and on a cold build the wrong
order stores a nought for everybody and never revisits it. The engine compiles
one source string; a host that keeps definitions in several `.fig` files
concatenates them (the library's `read_definitions` helper does so in file
name order), so which file sorts first decides what can be built on what --
a real constraint on naming, better stated than discovered.

---

## Nine declarations

| | Answers | Stores |
|---|---|---|
| `fact` | what a record of this kind carries | nothing -- it gates what may land |
| `group` | which records belong to which subject | one bucket per value |
| `filter` | which records pass a test | one bucket |
| `measure` | a quantity read off one record | nothing |
| `figure` | one value per subject, or one per bucket when `bucketed` | values |
| `reading` | a statistic over stored buckets, or over records right now | nothing |
| `projection` | one row per record | nothing |
| `summarise` | one row about a whole population | nothing |
| `bundle` | which answers travel together in one request | nothing -- it composes |

Only a **figure** stores anything, and that single fact decides which
constructs may read a clock: a stored value computed from `now` is stale the
instant it is written and nothing would ever recompute it, because the clock
is not an event. Every number would be real exactly once. So `now` is fenced
by where the result may go -- a figure may never name it, and the constructs
that may name it store nothing.

---

## `fact` -- what a record is

```
# An order in the shop, as the provider last showed it.
fact shop_order:
    name ref
    url link
    ref as text
    link as text
    # Which courier holds it; absent until assigned.
    courier_id as text
    status as text
    placed_at as moment
    delivered_at as moment
    weight_grams as number
    rush as flag
    one dropoff:
        street as text
    many events:
        kind as text
        at as moment
```

The world, declared where the definitions that read it are declared. A host
may still teach kinds through the schema document instead ([Concepts --
The schema](concepts.md)); what it may not do is both -- a source that
declares facts refuses a schema that also declares kinds, because two
declarations of one world is where they drift. With facts declared, three
things happen that a kind list cannot do:

- **Every path a definition reads is checked.** A group bucketing by a field
  that does not exist, a measure subtracting a text, a projection binding
  `as date` over something that is not a moment, a filter testing a flag
  against a word it can never hold -- each was a silently empty bucket or a
  column of dashes in production, and each is now a build failure that names
  the field and lists what the record actually carries. (For predicates the
  quoting carries the claim: a bare `true` names a flag's value, a quoted
  `"true"` names a word a text field holds, and numbers are written bare so
  they compare in the bucket key's own spelling.)
- **A record must match the declaration to land.** The facts route verifies
  every written body; an undeclared field or a wrong type refuses the batch
  whole, by kind, key and field (see [the HTTP API](http-api.md)).
- **A reader can walk a number back to the schema.** Each fact is versioned
  and served with its fields and their prose, so the trace that starts at a
  figure bottoms out on a declaration instead of on arbitrary JSON.

### The shape

Named **bare** -- `fact shop_order:` -- where every other declaration is
`<fact kind>.<name>`, so a kind can never collide with a definition name.
There is **no `field` keyword**: a fact's body is its fields, so the word
would be noise on every line. A line whose second token is `as` is a field;
`name <field>` and `url <field>` are the two directives; `one x:` and
`many x:` open nested blocks. A field literally called `name` is still
writable (`name as text`) -- nothing here is a reserved word, in keeping with
the lexer's own rule.

### Structural only, on purpose

Four types -- `text`, `number`, `flag`, `moment` -- and nothing else, because
a fact describes the record as the provider shows it and every
*interpretation* already has a home. A correlation (`courier_id` names a
courier) is a group's `through` claim; what a number *means* (`effort`,
`count`) is the measure's `in` clause. Writing either on the fact was
considered and refused: nothing in the records marks those fields as special,
and a `key of shop_courier` here would be the correlation declared twice.
`moment` survives that test because "this string is an instant" is a claim
about the value's form -- it is what makes `delivered_at - placed_at`
checkable and what an age filter needs.

`name` and `url` are directives pointing at fields rather than markers on the
field line, because they are rendering, not data: fields are hashed, these
are not, and a rendering choice written inline would look like part of the
field's semantics. Both are optional -- a kind with no `name` renders raw ids
(honest, and the checker still refuses to split a figure `across` it), and a
kind with no `url` serves linkless evidence.

### `one` and `many` -- nesting is cardinality

Blocks nest by plain recursion, and what they declare is not the provider's
payload shape but a **cardinality the checker can reason about**: a path
crossing only `one` blocks yields one value; a path crossing a `many` yields
every element. That single property is what each downstream rule branches on
-- a group may cross a `many` (bucketing flattens deliberately: any
account_id of any account), a measure may not (it reads one value, and across
a list it would silently skip or first-win), a projection field may not (a
row's field holds one value), and an age filter may not (it reads one
instant). A predicate or an `is set` over a `many` is allowed and means *any
element*: `==` asks whether any element matches, which makes `!=` "no element
does" -- and, absent-satisfies-`!=` being the standing rule, a record with an
empty list passes a `!=` exactly as a record with no field does. A deeply
nested fact is usually the host
transcribing the provider instead of mapping it; allowed, and the flatter
mapping is house style.

**A list of scalars is not declarable.** No construct can read one -- a
predicate compares one field against one literal and cannot test membership
-- and a declared-but-unreadable field would be a construct nobody has
checked. The same goes for an open JSON bag: the body a host pushes is the
body it authored, and what the provider sends but nothing reads is left out
of the mapping. That is the write-boundary spelling of "a field that is not
on a record here is a field not on disk".

### An absence is never an error

The declaration's claim is **known/unknown, not required/optional**. A record
may omit any field, or carry it as an explicit null -- absence means "nobody
said", exactly as it does everywhere else in this engine. What must not
happen is a *present* value the declaration cannot account for.

### Versioned, but downstream of nothing

A fact's version hashes its fields and their shapes -- not its prose, not
`name` or `url`. **No definition's version reads it**, for the reason
`keyed as` is not hashed: a fact schema decides what the checker permits and
what the write boundary accepts, never what the arithmetic produces. That is
the property that makes adoption safe -- a host that moves its kinds into
`fact` declarations moves no figure's hash and rebuilds no tenant's history,
and the test that pins this compiles the same definitions both ways and
compares every version.

---

## `group` and `filter` -- which records

```
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
filter work_issue.sized where estimate_seconds is set
filter work_issue.stuck where status_changed_at older than 14 days
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in team_person.timezone)
filter code_review.approved keyed as code_change where was_approved == true
```

Both are named `<fact kind>.<name>`; the prefix is the kind whose records
they bucket. Two keywords, because they answer different questions. A
**group** (`from`) buckets every record by the value of one field, so it
*fans a figure out* -- one bucket per courier, per person, per repository. A
**filter** (`where`) is a single bucket holding whatever matches, so it
*narrows*. Intersecting the two is how a per-subject figure gets both at
once, as `carrying` did above. One keyword (`index`) covered both for a
while, and the word answered neither question -- each shape now wears the
verb it performs, and writing one shape under the other's keyword is refused
with directions.

An optional trailing `label "still open"` names how to say the declaration in
a sentence. It is prose: the fallback is mechanical and ugly, which is the
prompt to write one, and it never touches the version hash.

### `through` -- the identity hop

```
group work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
```

A record often carries a foreign id rather than the id of the subject a board
cares about: a work item names an account, and a person owns one or more
accounts. `through <kind>.<path>` buckets by the id of the record that *owns*
the value rather than by the value itself. Without the hop, a figure scoped to
a person would bucket by account and split anybody with two logins into two
half-people -- the exact failure an identity join exists to prevent,
reintroduced one layer up.

The path is walked with lists flattened, so `accounts.account_id` means "any
account_id of any account". A value claimed by more than one owner lands in
every owner's bucket, deliberately: duplicated identity is a data problem a
figure should reflect, not silently pick a winner for.

### The `where` forms

**Equality**: `where state == "open"`, `where active == true`,
`where retries != 0`. The value is a quoted string, a number, `true` or
`false`.

An **absent value satisfies `!=`**, deliberately: a record with no `state` is
not in state `"merged"`. That is right, and it has a consequence worth
memorising -- `where estimate_seconds != "0"` matches every *unsized* record
rather than none of them. When the question is whether a field has been
filled in at all, that question has its own form:

**Presence**: `where estimate_seconds is set` (and `is not set`). Membership
is decided by whether anybody has *said* something, and two decisions are
folded into it. **Nought counts as absent** -- providers routinely write `0`
and `null` into the same field for the same state, so honouring the
difference would move a coverage figure when an operator cleared a box rather
than when anybody estimated anything. A boolean `false` counts as **present**,
because somebody answered.

**Age**: `where updated_at older than 3 days` (and `younger than`).
Membership is decided by how long ago a moment was, in whole days. This is
**the one clock a stored figure may read**, and it is fenced differently from
a clock measure rather than by the same rule: a clock measure decays every
second, but membership does not -- a record crosses this line once, on a
knowable day, and until it does the answer is unchanged. The question is not
"is this value decaying" but "how long may the crossing go unnoticed", and the
answer is *until the next full pass*. That only holds because the threshold is
in days; the unit is fixed precisely so the unsafe version cannot be written.
A record whose moment cannot be read is in **no** age bucket -- an absent
timestamp is not evidence of age.

Where the line is not the same for every record, it comes off a record --
either the one being filtered, or its **owner**:

```
filter code_change.stale where updated_at older than grace_days
filter code_change.idle  where updated_at older than stale_days from repo_id through code_repo.id
```

The first reads a column the record is already holding. The second is the
same join a projection field writes, one construct along: each repository
declares its own staleness, and the threshold is a fact like every other
number.

The bare form exists because the join was the only one, and reaching a
column on the record itself meant writing a *self-join* -- `older than
grace_days from ref through code_change.ref`, pairing a record with itself.
The other two positions a threshold appears in read a bare field off the
subject already; this one demanded ceremony to reach a number that was
already there.

This is the position a dial was hardest to remove from, and it is worth saying
why the answer is a join rather than a figure. Everywhere else a threshold
that varies is a figure looked up by subject; a filter has no subject. It runs
over records *before* anything buckets them by person or site, so there is
nothing to look a goal up by -- and a figure that could feed a filter would be
a cycle, since the filter decides the population the figure is computed over.
What a record does have is its owner.

**A record with no line on it is in no filter**, and neither is one holding
two -- the same rule the bare field read has everywhere else. A record nobody
has drawn a line for is not a record whose line is nought.

**A join that matches no record is not in the filter.** Never a default and
never the whole population: an owner nobody has collected yet is a threshold
nobody has stated. The same for a record naming two owners -- two staleness
rules and no way to choose, which is the projection join's rule one construct
along.

Moving the owner's record moves the line for every record under it, and the
incremental path cannot see that from the records themselves -- so a write to
a kind an age filter reads through **escalates the pass to a full one**,
exactly as a write to an identity-hop kind does.

### Composite groups: time buckets and pairs

```
group code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in team_person.timezone)
group code_change.authored_in   from (author_account_id through team_person.accounts.account_id, connection_id)
```

A parenthesised `from` keys each bucket `<subject>@<rest>`; the parts can be
several, but a figure may only fan out of a group carrying at most two -- a
subject and one more is all a value key can hold, and the checker says so at
the figure, not the group. This is what lets a figure be per-person *and*
per-day, or
per-person *and* per-source, without the engine learning a second scope
dimension. Because `@` joins the halves, a key value containing `@` is refused
outright rather than encoded -- a subject that decomposes into the wrong pair
is silent, and a refusal is not.

`by day` truncates an instant to a calendar day. `in ...` names *whose*
calendar, and it takes either shape a threshold does:

- **`in team_person.timezone`** -- a field on the subject's record, read for
  the subject a bucket belongs to. This is the one to reach for whenever
  subjects genuinely differ, and everything below is about it.
- **`in "Asia/Tokyo"`** -- one calendar for every subject, written where a
  reader can see it. The argument is the one that keeps a literal threshold
  legal: a value in the definition is not a control outside it, because the
  reader can see it and moving it forks the version. It is refused at compile
  time if it is not a real zone -- unlike a record's, where an unusable value
  is a fact about one subject and the board goes on without them.

A bare `by day` means UTC, and that is a choice a definition makes rather than
a default it falls into -- two figures on one card, one cut by UTC days and
one by somebody's own, would be two rows headed "30" measuring two different
months.

It used to be a tenant dial, and a dial meant one calendar for a whole board:
a courier in Tokyo and one in London had their days cut on somebody else's
midnight, and the number under "yesterday" was about a period neither of them
worked. The written form is not that dial coming back. A dial varied
invisibly, from a form, with nothing on the card to cite; this is in the
source, in the version, and in the trace.

For a board that shares one calendar the field form was a real cost, and the
showcase paid it: thirty-two franchises each carrying `"America/New_York"`,
with a comment saying the league keeps one clock -- so a team missing the
value fell into no bucket, and a typo on one record was a one-record way to
empty a figure.

**Subject-scoped rather than record-scoped**, and that is the load-bearing
choice. Read off the record being bucketed, a subject's sequence could mix
calendars -- some of a courier's days cut in Tokyo, some in Berlin, depending
on where each order came from -- and a reading walks that sequence counting
back positions, so its window would be a span of no particular calendar. Read
off the subject, every bucket in one subject's sequence is cut the same way.

Three things follow, and the first is the price:

- **One record shared by two subjects lands on two different dates.** An
  08:00 UTC merge is the 25th in Tokyo and the 24th in London, and it is not
  the engine's place to pick. So a composite group is no longer a plain cross
  product of its parts: the time part is computed against the subject the
  bucket is being filed under.
- **A subject with no calendar recorded is in no bucket.** Never UTC as a
  fallback: a subject nobody has said a calendar for has no calendar, and
  defaulting one files their history under days they never worked, with
  nothing on the board to say so. A value that is not a usable zone name
  counts as none -- `"PST"`, a typo, an empty template. As a dial this was one
  value with one write door to validate at; as a field it is uncontrolled
  provider data on every roster record, and `timezone as text` accepts
  anything.
- **Moving a subject's calendar refiles their whole history**, and the
  records themselves do not move -- so the change stream over them says
  nothing. The kind carrying a calendar is treated exactly as a kind a group
  resolves *through*: a write to one escalates the pass to a full one.

The consequence a reader sees is on the [reading](#reading----evaluated-never-stored)
side: "the last thirty days" covers a different thirty dates for each
subject, so each window reports its own bounds and its own calendar, and the
response carries one over the top only where every subject agrees.

### The bucket rules

The `by` clause is **the one place calendar vocabulary belongs**, because it
is a declaration: the rule decides how many values a figure has and which
bucket an event lands in -- it decides what a stored number *means* -- and
only a declaration, hashed into a version, may do that. A
[reading's](#windowed-over-a-figures-stored-buckets) window argument is then
just integer positions in whatever sequence was declared here.

Seven grains, sub-day through quarter:

```
by minute        2026-08-25T14:30      by week     2026-W35  (ISO weeks)
by 15 minutes    2026-08-25T14:30      by month    2026-08
by hour          2026-08-25T14:00      by quarter  2026-Q3   (calendar quarters)
by day           2026-08-25
```

**A span** names two moments instead of one, and the record joins every
bucket between them -- both ends inclusive, so a stretch that begins and ends
inside one week is in that week rather than in none:

```
# Every week an item is planned to be worked in.
group work_issue.planned_weeks from (container_id, start_date until due_date by week in "UTC")
```

That is the right question about a *commitment*, where the single-instant
rule is the right question about an event. Work planned from August to
October did not happen in August; it occupies eleven weeks, and a chart of
what is in flight each week needs it in all eleven. Both ends are truncated in
the same calendar as any other label. A span missing either end is in no
bucket -- never one silently running to now or to for ever, because a plan
with no end is one nobody has scheduled -- and ends in the wrong order produce
nothing rather than being reversed.

Adding `excluding weeks gone` drops the buckets whose period has already
passed, which turns the rule from a fact about a plan into a claim about the
future: an item half way through occupies the weeks it has *left*. The
period in progress is kept; dropping it would make the near future, the only
part anybody can still act on, the one part missing. The plural must match the
grain, because it is the only part of the clause a reader can use to tell what
is being dropped.

This is the second place membership moves with the clock (an
[age filter](#filter) is the first), and it costs the same thing: **the filing
is as fresh as the last pass.** The engine notices for itself -- a clipped
span's stored buckets carry the day they were built on, so the grouping
rebuilds on the first pass of a new day and everything over it recomputes
through the ordinary cascade. The cron a forward chart needs is the pass that
already runs. Fenced to day grain and coarser, for the reason
`carried forward` is: a pass is the only clock membership has.

A label is the *local* instant reduced to the grain, in the calendar the
definition names: the zone is applied once, to find the local time, and a
coarser label is calendar arithmetic on the local day -- so a month figure
and a day figure over one event can never disagree about which month the
event's day was in. When the clocks go back, the repeated hour's two passes
share their labels and their records share a bucket -- the honest answer
about a quarter-hour that occurred twice; keying by UTC instead would put
every local midnight mid-bucket in most of the world's zones, a constant
error to avoid a twice-a-year merge. The spring-forward hour stays a run of
holes, which is true -- those wall-clock minutes never happened. Other
minute counts than 15 wait for a definition to ask, exactly as `percentile`
does.

**A coarser view is its own declaration.** Monthly deliveries beside daily
deliveries is two groups and two figures -- two names, two explanations, two
hashes -- and each buckets the records *directly*: a month bucket holds
every record of the month, never a rollup of day buckets. An earlier edition
refused `week` and `month` here on the ground that a range over days can
produce either; that argument was aimed at *unnamed* read-time truncation --
one figure quietly serving two grains, with nothing written saying which a
number was -- and declaring the coarser grain under its own name is the
opposite: each grain is a written, versioned question, and a reader can cite
either. The grain is in the version hash the way every group and filter spec
is: months and days store values that mean different things, and reusing
them across the change would file a month's count under a day's key.

The **selective rules** are the same family, deliberately partial:

```
group shop_order.first_monday_drops from (courier_id, delivered_at by first monday of month in shop_courier.timezone)
```

`by <ordinal> <weekday> of month` -- `first` through `fifth`, `monday`
through `sunday` -- buckets an instant under its own day *when that day is
the rule's day of its month*, and under **nothing at all** otherwise. The
filter falls out of the function being undefined off-rule, the same doctrine
as `is set`: there is no separate narrowing step a cheap path could skip.
The stored buckets are sparse day labels, one per month at most -- a fifth
Monday exists in some months only, and those months simply contribute no
bucket to the sequence.

Whether the second part is a time bucket or a dimension decides what the
figure over the group must declare -- see [time-keyed
figures](#time-keyed-figures) and
[`across`](#across----a-second-dimension).

### `keyed as` -- a second kind sharing an id space

Some worlds store two fact kinds about one underlying thing -- a change and
the review timeline rebuilt from its events, say -- keyed by the same id, so
their sets can be intersected and the answer means something. `keyed as`
declares that sharing:

```
filter code_review.approved keyed as code_change where was_approved == true
```

Declared rather than inferred, because the failure it guards is silent:
intersecting ids that mean different things yields the empty set, and an
empty set is a figure reading zero for everybody rather than an error anybody
sees. Every group and filter over one kind must agree about that kind's id
space -- otherwise the guard could be defeated by writing a second declaration
and leaving the clause off, which is the quietest possible way to lose it.

`keyed as` is deliberately **not** in the version hash: it decides what the
checker permits, not what the arithmetic produces.

---

## `measure` -- a quantity on one record

```
measure code_change.open_seconds = merged_at - created_at         # a duration
measure work_issue.estimate = estimate_seconds in effort          # a field
measure work_issue.moved = moment updated_at                      # an instant
measure code_review_request.waiting_seconds = now - requested_at  # a clock
```

A measure decorates the members of a set; it never decides which records are
in one. It is deliberately **not** arithmetic -- one field, or one gap between
two moments. An expression grammar here would let a definition read record
contents in ways the checker cannot reason about.

**A duration** (`later - earlier`) is the seconds between two moments, by
construction, so it needs no unit.

**A field measure** reads whatever number the record carries, and must say
what that number *is*: `in effort` for seconds of working time, `in count`
for a tally. Required rather than defaulted, because the same integer means
different things -- a default of `count` prints an estimate as `144000`, a
default of `effort` prints a tally of five reopens as `0.0h`, and neither
throws. Note that **`effort` is not a synonym for `duration`**: a duration is
wall-clock and effort is working time. Both render in hours, so they agree at
eight and part company above a day -- 144,000 seconds is `1.7d` as a duration
and `40.0h` as an effort, because a working week is forty hours and nobody
means "one and two-thirds days" by it. One number cannot be both. A field holding several numbers, or a numeric *string*, reads as
nothing -- first-wins would answer with whichever value the provider happened
to order first.

**A moment** (`moment <field>`) names a single instant. It is its own kind of
measure, not a field measure with a third unit, because an instant is not a
quantity: subtracting two of them is milliseconds and totalling a column of
them is a date in the future -- both compile as arithmetic, neither throws,
and each puts a plausible figure on a screen. Moments exist for `latest` and
`earliest`, and the checker refuses them everywhere else. There is no clock
spelling here: after `moment`, the word `now` is just a field name, and a
record without a field called `now` measures as nothing -- a stored instant
that tracked the clock would be wrong one millisecond after it was written.

**`now`** is the clock, and it only means something subtracted from:
`now - requested_at`. A measure that reads it is a *clock measure*, and the
fence is on where the result may go -- a figure may never name one; only a
live reading may, and a live reading stores nothing.

---

## `figure` -- one value per subject

```
# In progress -- how many work items this person has actively underway.
figure team_person.wip:
    display "{team_person}'s work in progress"

    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active

    calculate:
        count(mine)

    band:
        when value >= team_person.wip_over then "over"
        when value >= team_person.wip_warn then "warn"
        otherwise "ok"
```

A figure is named `<fact kind>.<name>`, and the prefix is its **scope** -- the
kind of subject it is one-value-per. It requires an explanation (the `#` lines
above it -- the definition a reader is shown), a `display` template (the
sentence a movement is reported under) and a `calculate` block. A figure over
records also declares `depends`; one built only on other figures needs no
group at all, and takes its subjects from what it reads. `unit` and `band`
are optional.

The `display` template is prose, written by convention with the scope kind,
the `across` kind for a split figure, and `{value}` as placeholders --
`"{team_person} open in {data_connection}"`. Like the explanation it is stored
with the definition, where a host's data screen can show it, and like the
explanation it is deliberately outside the version hash.

### `depends` -- sets of record ids

Each line binds a name to a set expression over groups and filters:

```
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
```

- `group:{scope}` addresses a group's bucket for this subject. Writing a
  group *without* the bucket is refused: read unbucketed it looks for
  a bucket keyed by the empty string, finds nothing, and the figure reads
  zero for everybody. Addressing a filter *with* one is refused for
  the mirror-image reason -- it has only the one bucket.
- `&`, `|` and `-` are intersection, union and difference. They share one
  precedence level and associate left to right; parenthesise anything you
  would have to think about.
- A bare name refers to a set bound on an earlier line.

There is **no inline predicate here, on purpose**. `depends` may narrow only
by set membership, never by record contents, because a predicate over
contents cannot narrow what the figure subscribes to and would therefore be a
declaration that lies. This is the language's one safety property, and the
checker upholds it: anything a figure wants to narrow by must be a declared
filter, where the subscription machinery can see it.

Two structural rules, both refused with the failure named:

- **One id space per set.** Intersecting `team_person`-bucketed work items
  with `code_change` ids is empty, for everybody, for ever, with nothing
  thrown. Use `keyed as` if two kinds genuinely share ids.
- **Exactly one group addressed by `:{scope}`** across all the sets. With
  none the figure has no subjects at all and would compute a board-wide total
  attributed to nobody; with two, a value would be keyed by two different
  things.

### `calculate`

The expression language, whose leaves a sentence can always name: a set's
size, a measure over a set, a bound figure, another figure, a literal.

- `count(mine)` -- how many records the set holds. A count of an empty bucket
  is a real nought, not an absence.
- `sum(work_issue.estimate over mine)` -- a field measure totalled across the
  set. A *duration* measure may not be summed -- that is a quantity nobody
  asked for, and a list is what a span reads -- nor may moments, since
  totalling dates is a date in the future.
- `sum(work_issue.weekly_effort over live)` -- another **figure's** values,
  totalled across the records in this bucket. What a measure cannot give: a
  number some other definition worked out, where no column holds it. Each
  member is read at the bucket being answered for, so the source must be
  keyed at the same grain, and **an absent contribution makes the whole total
  absent** -- adding up whichever members happened to have a number is
  arithmetic over a population nobody chose.
- `spread(work_issue.remaining over weeks)` -- one value shared **evenly**
  across the buckets the subject occupies. The counterpart a span makes
  necessary: membership in five weeks means a quantity counts in full in all
  five, which is right for a count of what is running and five times the
  truth for an amount of money. Evenly rather than by overlap, so the ends of
  a span round up -- a bounded error at two buckets, in the direction that
  never hides a peak, against a model that does not know which day the work
  lands on anyway. The divisor is the buckets the subject occupies *now*, so
  a clipped span divides what is left over what is left.
- `list(code_change.open_seconds over merged)` -- the measure's value for
  every member, in evidence order, with unmeasurable records left out of both
  the values and the evidence. **`list` does not aggregate**: averaging at the
  figure's own grain throws away the only thing a range needs, and which
  statistic a reader wants is a question for the read, answered by a reading
  and hashed into *its* version. Only a duration measure may be listed.
- `latest(work_issue.moved over children)` / `earliest(...)` -- the most or
  least recent instant in a population. The measure must be a moment measure;
  there is deliberately no general maximum over a column of numbers. The
  empty set answers nothing -- the latest of nothing would be 1 January 1970,
  an epic created this morning reading as untouched for fifty-six years --
  and a record whose timestamp cannot be read is skipped rather than counted.
- Arithmetic: `+`, `-`, `*`, `/`, with the usual precedence and parentheses,
  over numbers only. **Division by nought answers nothing** -- never infinity
  and never nought, because a confident 0% over an epic with six of seven
  stories closed is the wrong answer this engine exists to avoid. Any absent
  operand makes the result absent.
- `max(a, b)` / `min(a, b)` -- the larger or smaller of two values. **An
  absence propagates** here too. The tempting alternative -- an absence does
  not compete, so `max(a, nothing)` is `a` -- is wrong in this engine, because
  a missing value means *not computed*, never "the subject has none of it";
  the engine writes a real nought for anybody who genuinely has none.
- A dotted name is **another figure**, or a **field on the subject's own
  record**, in that order. Both are `<kind>.<name>`, which is why a figure may
  not take a name one of its scope's fields already has -- one spelling
  answering two things is what this language exists to refuse, and it is
  refused where the collision is made rather than where it is read. A bare
  name reads a binding from `depends`.

  **`shop_courier.max_orders`** -- the field -- is the short road, and the one
  most thresholds want: a number somebody typed onto a record. It costs no
  declaration. The record is always the subject's, because that is the only
  record there is a key for; another kind's field is refused rather than
  resolved by picking one. It reads only in a **fact-taught** world, where the
  field is declared and the checker can see it -- an unchecked field read
  would be a silent absence for every subject.

  **`team_person.wip`** -- the figure -- is for a threshold that is computed,
  or that has history worth keeping. The named figure must be declared
  earlier and share this figure's scope, and a sequenced one is read at its
  coordinate (`goal:{bucket}`). It becomes an ordinary **read**: this figure
  is rebuilt when the one it names moves, and sorts after it.

  Either way a calculation can now reach a number it did not compute itself,
  which the two blocks did not cover between them: `depends` gives a
  population and cannot reach a value, and a rollup totals parts and cannot
  count records. "How much room is left before this courier's limit" was
  unwritable.

### The `when` ladder

```
    calculate:
        when wip >= team_person.wip_over then "over"
        when wip >= team_person.wip_warn then "warn"
        otherwise "ok"
```

Rungs are tested in written order, first match wins, and the ladder must end
in `otherwise` -- without it a value can be missing because a case was never
written, which renders exactly like a value missing because the definition
says so, and only one of those is a claim.

**Six comparison operators and no `and`, `or` or `not`.** A ladder of single
comparisons explains as a list of clauses a reader can check; a boolean
expression does not. Two conditions that must both hold are two rungs.

**A ladder stops on an unknown** rather than falling through. `otherwise` is
the bottom of the band, and banding a subject the engine has never computed
as *comfortable* is the confident wrong answer everything here is arranged
around avoiding. To say something *about* the unknown, use the two presence
tests -- `when planned is nothing then "unscheduled"`, and `is something` --
which answer before the null guard: "is there a value at all" is never itself
unknown. They are words rather than an operator because `x == null` would put
a value into the language that is not a value.

**A figure's ladder must return words, and the same shape from every rung.**
One returning numbers would carry an absence out under a numeric unit, where
nothing downstream can hold it; one mixing the two would store a value every
reader has to branch on. The words a ladder can return are the only place
arbitrary text exists in the language -- the set of words a figure can
produce is finite and listed in its own definition, because a figure that
could return any string would be a template engine with a version hash.

### Units

A figure's unit is **derived** wherever anything can derive it: a count is a
count, a sum of an effort measure is an effort, a list of a duration measure
is a duration, a ladder returns a `level`, an extreme returns a `moment`.
Arithmetic is the one shape where nothing can tell: `delivered / committed`
and `committed - delivered` are the same two operands producing a share and a
quantity, and 0.6 renders as "60%" or as "0.6" with no way to know which was
meant. So arithmetic (and `max`/`min`) **must** declare a unit --

```
    unit share
```

-- from `share`, `days`, `effort`, `count`, `duration`, and everything else
must **not**: a second place to write it is a first place for the two to
disagree. `level` and `moment` cannot be written at all; each comes from
exactly one construct and is always derived.

A figure built on another figure inherits its unit from **the binding it
actually reads**, not from whichever binding happens to carry an inheritable
one.

### `sum(<figure>)` -- adding up a split figure's parts

```
# Open changes, all sources together.
figure team_person.open_mrs:
    display "{team_person} open changes"

    calculate:
        sum(team_person.open_mrs_by_source)
```

A rollup totals the parts of a figure split `across` a dimension, so that **a
total and its parts cannot disagree**: there is one count, and this adds it
up. Agreement by construction is worth more than two independent counts held
together by a test.

This was a block, and the block was four lines and a name for one operation
whose only legal consumer was the `sum` immediately below it:

```
    combine:
        sources = team_person.open_mrs_by_source over data_connection

    calculate:
        sum(sources)
```

Neither half said anything the calculation could not. `over <kind>` restated
what the source already declares -- and a second place to write it is a first
place for the two to disagree. The binding was an alias for a value used once,
immediately. So a rollup is written where every other operation is written,
and the block is refused with the line it becomes.

Both mistakes around it still fail quietly, so both are still refused by name.
A bare read of a dimensioned figure would take whichever part sorts first; a
rollup of an undimensioned one would total a single value and look right for
ever. And a figure may roll up one source: a rollup's members are addresses
carrying no figure name, so two would be indistinguishable once stored.

The rules around it:

- A figure has `depends` **or** a rollup, never both: two populations
  arriving at one calculation with no rule for how they relate. Reading one
  figure's *value* beside a population is fine -- that is a number, not a
  second population.
- The source must share this figure's scope -- different scopes are different
  id spaces, so every lookup would miss.
- The source must be declared **earlier in the source**. That is also why a
  figure cannot read itself.
- A rollup may total at most one dimensioned figure, and may not read a
  `level` figure (a word has no arithmetic) or a time-keyed one (it has one
  value per bucket, not one per subject) as a single value.

### Time-keyed figures

A figure whose scope group ends in a time bucket part -- any of the seven
grains, or a selective rule -- stores one value per subject *per bucket*:

```
# How long each change merged that day had been open.
figure team_person.time_to_merge bucketed:
    display "{team_person} time to merge"

    depends:
        merged = code_change.merged_by_day:{team_person}

    calculate:
        list(code_change.open_seconds over merged)
```

`bucketed` is required, and refused where there is no sequence. The grain is
written in the group and implied by the name, so the keyword is bare -- a
figure restating the grain would be a second place for the two to disagree,
leaving the checker to decide which wins. What the word adds is the
*declaration*, exactly as [`across`](#across----a-second-dimension) does one
key-part along: unsaid, every reader downstream is wrong in its own way -- a
projection binds a column that never resolves, a bundle subscribes to every
stored bucket of every subject, a rollup totals a sequence as though it were
one number.

It is deliberately **not in the version hash**. It mirrors the group's spec,
and that spec is already hashed into every definition that reads it, so
hashing it here would store a second copy of a fact already recorded --
`keyed as` exactly: a construct that decides what the checker permits, never
what the arithmetic produces. Staying out is also what made it introducible.
Hashed, making it required would have moved every sequenced figure's version
in every deployment and rebuilt every tenant's history to store
byte-identical numbers.

Time-keyed is what makes a figure readable over a range -- it is the property
a windowed [reading](#reading----evaluated-never-stored) requires of its
source. A bucket of time is not a dimension: it has no roster and no name, so
a time-keyed figure cannot also declare `across` and cannot be read by a
projection. It may be read by another *bucketed* figure, one coordinate at a
time -- see [on-change data](#on-change-data-carried-forward) below.

### `across` -- a second dimension

```
# Open changes per source.
figure team_person.open_mrs_by_source across data_connection:
    display "{team_person} open in {data_connection}"

    depends:
        mine = code_change.authored_in:{team_person} & code_change.open

    calculate:
        count(mine)
```

When a composite group's second part is a dimension rather than a date, the
figure must say so with `across <fact kind>`. A dimension is not a new kind of
thing -- it is a **second subject**, with a roster, a name field and an id
space the checker already knows how to reason about (the kind must have a
name field declared, or every row would be headed by a raw id, and it may not
equal the scope). Underneath is the same composite key a time-keyed figure
uses; what `across` adds is the *declaration*, without which every reader
downstream is silently wrong -- the display template renders the variable as
literal text and a generated sentence describes the whole population beside a
number that is a slice of it.

**A pair's roster is the group, never a cross product.** Crossing every
person with every source would write a nought against pairs that
categorically cannot hold a record. The consequence: a pair reads a real `0`
once it has *ever* appeared, and is absent until then.

### `band:` -- the word beside the number

```
    band:
        when value >= team_person.wip_limit then "over"
        otherwise "ok"
```

The second thing a figure answers: which of a few states its number is in.
It is part of the figure rather than a `level` figure of its own, because a
word on screen sourced from a definition the page never named -- found by
scanning the library for whatever banded this one -- is exactly the
untraceable number this engine exists to end.

**The threshold is a fact.** A rung compares against a literal or against
another **figure**, and never against a tenant dial. A dial was a number the
host set on a settings page, outside the fact stream, cited by nothing -- so
on a card where every figure could be traced to the records behind it, the
one part that decided whether a reader should worry could not be. Naming a
figure makes the threshold an answer like any other: computed from the
records that set it, carried forward across the buckets nobody moved it in
(see [on-change data](#on-change-data-carried-forward)), versioned, and
citable. A literal stays legal because a number written in the definition is
not a control outside it: the reader can see it, and moving it forks the
version like any other semantic change.

The rules that keep it a band rather than a second calculation hiding in the
same block:

- The only *binding* in scope is **`value`** -- this figure's own answer.
  Reading a set or a rollup's source here would be a second calculation
  sharing the first one's name and version.
- A threshold figure must **share this figure's scope, dimension and
  grain**, and answer in the **same unit**. The join is subject-key equality:
  a monthly number is stored under `c1@2026-07` and so is a monthly goal, so
  the comparison lands on the same coordinate by construction. Days compare
  against days and months against months, and a mismatch is refused at
  compile time rather than left to produce keys that never meet -- which
  would band every row unknown, reading as missing data rather than as a
  wrong definition. The unit rule is the same argument one layer along: both
  sides are numbers by the time the ladder sees them, so a duration judged
  against a count runs and is wrong by 86,400.
- A **sequenced** threshold is named at the coordinate -- `goal:{bucket}` --
  and an unsequenced one bare. Bare over a sequence would have to pick a
  bucket, and whichever it picked would be a fabrication.
- Every rung must answer a **word**. A rung answering a number would reach a
  screen as an unexplained integer in a column of words.
- A figure whose value is a word or a list **cannot be banded** -- there is
  nothing to compare, so every subject would fall through to the bottom rung,
  silently.
- **An absent threshold withholds the word**, exactly as an absent value
  does. A month before anybody set a goal has no goal, and a nought sits
  comfortably under every comparison.
- It is **evaluated when the figure is served and stored nowhere**: the
  ladder is pure over the value and the goals beside it, so a goal moving
  re-bands the board on the next request instead of forcing a rebuild. It
  *is* in the version hash -- a figure that starts banding differently is a
  different definition -- and that costs nothing, because no stored value
  hangs off it.

The figures a band names are tracked separately from the ones a calculation
reads, and the split is what makes the cheapness real: a goal moving
**re-serves** this figure without **rebuilding** it, and a host asking "what
must recompute when this figure moves?" must not be told about a band.

---

## On-change data: `carried forward`

Some of the world is recorded as **changes** rather than as states. A goal, a
staffing level, a price, a facility setting: one record each time somebody
moves the value, and nothing at all in between. The records are sparse by
nature -- a month nobody touched it has no record -- and every screen wants
the opposite shape, a value per month including the months nobody changed
anything.

Nothing below is about goals in particular. The pattern applies to any
on-change fact, and the four declarations each answer exactly one question.

```
# One change to one setting, at one site, by one person.
fact setting_change:
    site_id as text
    setting as text
    value as number
    set_at as moment
    set_by as text

filter setting_change.target where setting == "target_minutes"
group setting_change.by_month from (site_id, set_at by month in site.timezone)

# The target in force each month: the latest one set within the month,
# carried across the months where nobody changed it.
figure site.target_month bucketed:
    display "{site} target"
    unit duration

    depends:
        sets = setting_change.by_month:{site} & setting_change.target

    calculate:
        latest(setting_change.value over sets) carried forward
```

**The factoring is the design.** The group is *metric-agnostic* -- one group
per fact stream, keyed by subject and month -- so one group serves every
setting the stream carries. The filter owns the narrowing, because deciding
which records are in play is what a filter is for. And the figure's **name**
is the only place "this setting, monthly" is claimed, backed by the two parts
it visibly intersects. Putting the setting's name in the group would mean a
group per setting; putting the month in the filter would mean a filter that
knows about calendars.

### Reading a declared field

`latest(setting_change.value over sets)` reads the field the most recent
record in the bucket set it to, and `sum(shop_order.weight over mine)` adds a
field up across a set. There is deliberately no measure in the way:
`measure setting_change.goal = value in count` would be a second name for one
field, written only to satisfy the grammar, in a language that refuses a
second place to write one thing everywhere else.

A **measure** is for a quantity a record does not already carry -- a duration
between two moments, an instant, a wait against the clock -- or for one
meaning several figures share, declared once and named three times rather than
a unit repeated at each reader. `sum` demanded one anyway until the asymmetry
was noticed: only `latest` needs an *ordering*, which is why only `latest` is
confined to a `bucketed` figure, and a total has no ordering to get wrong.

`latest` therefore covers two operations, and they do not blur. Over a
**moment measure** it answers *when* -- the most recent instant in a
population. Over a **declared field** it answers *what the value was then*.
Which one was meant follows from what the dotted name resolves to, and a
measure wins where both could match, so declaring one can never silently
change what an existing figure computes.

**Which record is "latest" is not a second thing to declare.** The group
already said `set_at by month`; that field is when the change happened, and
it is the only ordering in sight. Naming it again would be a second place for
the two to disagree, and the disagreement is silent -- order by a different
field than you bucketed by and a bucket reports a superseded value with
nothing thrown. Ties break on the record key: arbitrary, but stable, because
two changes stamped at the same instant are a data problem and answering them
differently on each pass would be a figure that moves with nothing behind it.

A field read must declare its `unit`. That is the unit rule in its sharpened
form -- **declare only what cannot be derived** -- and nothing can derive
this one: a count is a count and a sum of an effort measure is an effort
because the *construct* says what the number is, where a field read says only
that a record carries a number. The fact layer is structural on purpose, so
`value as number` claims a shape and never a meaning. A redundant declaration
stays refused everywhere it always was.

### What `carried forward` does

The buckets nobody changed anything in report the last change. With a target
of 30m set in February and 25m in June, and August the present month:

| Month | Value | Why |
|---|---|---|
| January | *absent* | nothing had ever been set |
| February | 30m | a change landed here -- an **anchor** |
| March, April, May | 30m | carried |
| June | 25m | a new anchor |
| July, August | 25m | carried |

**Before the first anchor there is an absence, never a nought.** Nothing had
been set, so there is nothing to report -- and a nought sits comfortably
under every threshold, so a band would have coloured January green for a
target that did not exist.

**Each carried row cites the change it carried from.** July's evidence is the
June record: its value, its `set_at`, its `set_by`. A carried bucket holds no
records of its own, so a naive chain would cite the empty bucket and dead-end
exactly where the reader started asking.

### Why these rows may be stored

The standing rule is that a stored value may never read a clock: the clock is
not an event, so nothing would ever recompute what it produced, and the value
would be real exactly once.

A carried row does not read one. Its contents follow entirely from the
anchors at or before its own bucket, so September's answer is the same answer
for ever once September exists -- **deterministic and time-invariant**. What
*is* clock-dependent is only how far the sequence has been extended, which is
an existence question rather than a value one. Splitting those two halves is
what makes materialising legal here where `now - requested_at` is refused,
and it generalises: before deciding something cannot be stored because it
moves with the clock, split it and check which half actually does.

Three events extend a carried figure, and **one implementation serves all
three**, so the rows are byte-identical whichever asked:

- **A fact landing.** The bucket it lands in becomes an anchor, and the carry
  is recomputed from that bucket *forward*. Buckets before it are untouched:
  a change entered late but dated in April rewrites April onward and leaves
  March exactly as it was. History is never rewritten by a later arrival --
  which is precisely the property that makes these rows safe to store. The
  one exception is a bucket the anchors no longer justify at all: re-date
  the *first* change from February to May and the rows before May are
  retired, because nothing carried into them any more and they would
  otherwise report a value that was never in force.
- **A pass.** Every pass extends every carried figure to the bucket
  containing the pass's own instant. The pass is the event that notices time;
  the clock never is one.
- **A read.** A read that finds an unmaterialised bucket materialises it in
  place and then serves it, so a screen between passes is never told "never
  computed" about a value that has demonstrably been in force for months.
  Concurrent first-readers race benignly: both compute the same rows, and an
  insert-or-nothing write decides which one counts as having created each.

Three code paths writing these rows would be three chances to disagree, and
every row would look plausible. A pass's **run report** names the figures it
carried, so "the pass ran and moved nothing" stays a different finding from
"the pass never reached this figure" -- which, for a construct whose whole
job is filling buckets nobody wrote records into, is otherwise impossible to
tell apart from outside. (The persisted activity log does not keep it yet;
the report on the response does.)

**Materialisation never runs past the present bucket**, whatever asks --
including a caller's `?at=`, which may narrow what is *reported* and never
moves what is stored. The sequence is resolved by the same resolver a
window's `over 1-6` uses, counted back from the present, so it cannot name a
future bucket, and a change dated next month is carried into nothing until
next month. (The bucket the future-dated change itself lands in is an
ordinary anchor and is computed like any other; what the carry declines to
do is fill the months between.)

`carried forward` needs records to anchor on, so it is refused on a figure
built from other figures: there is no group, no bucket somebody changed
something in, and nothing for the carry to start from.

**A carried grain finer than a pass can honour is refused.** Extension is the
pass noticing time, so a figure owing a new bucket every minute would get one
per sync and read as an absence in between. Fenced the way an age filter is
fenced to whole days: the unenforceable version is not written, rather than
written and left to disappoint. Carry at day grain or coarser.

`carried forward` **is** in the version hash, where `bucketed` beside it is
not. The asymmetry is the rule rather than an inconsistency: one mirrors a
spec already hashed, the other changes what the stored values *are*. The same
records under the same calculation give two buckets without it and twelve
with it.

### Comparing two sequences

A bucketed figure may read another at the same coordinate:

```
# The median job length each month.
figure site.actual_month bucketed:
    display "{site} actual"

    depends:
        done = job.by_month:{site}

    calculate:
        median(job.length over done)

# How far each month ran over or under the target in force that month.
figure site.gap_month bucketed:
    display "{site} gap"
    unit duration

    calculate:
        site.actual_month:{bucket} - site.target_month:{bucket}

    band:
        when value > 0 then "over"
        otherwise "ok"
```

**A sequenced figure's bare name is refused in expression position.** Written
plain it reads like a static declaration when it is a point-in-time value --
and, worse, with two sequences in one expression nothing says the arithmetic
is per coordinate, so the obvious implementation is a positional zip: right
until one source starts a bucket later than the other, and then every number
is paired with the wrong month, plausibly, for ever.

The semantics are **join by bucket key, never by position**. The subject
being evaluated already *is* a coordinate, so reading a source at the same
coordinate is a lookup under the same key and misalignment is
unrepresentable. A coordinate one side holds and the other does not answers
an **absence** there -- never a nought, and never a shift.

The same selector works in a `band:` rung, comparing this figure's value
against another figure at the same coordinate. Bare means one value per
subject and a selector means one per coordinate, so the two shapes are
visually distinct by construction.

**There is no `:{bucket - 1}`.** Offsets are refused in the grammar, because
there is nothing to explain about a construct that does not exist. A stored
value whose answer needs a bucket outside the population in view cannot be
checked against the response that carries it: a reader is shown the number
and, one coordinate back, nothing to check it against. It is the refusal
[`delta`](#statistics)'s oldest cell gets, one layer down. If a definition
wants the change between adjacent buckets, that is `delta`, where the range
in view bounds the answer.

### Statistics over a bucket

A `bucketed` figure may take a distribution statistic over the records of its
own bucket -- `median(job.length over done)` above. The standing refusal is
against a statistic over **aggregates**: a mean of daily counts is a mean per
*day* wearing a label that says per record. Here the population is the
bucket's own records and the boundary is declared in the group, so "the
median job length in August" is a sentence with a population a reader can
check. The declared boundary is what turns the statistic into a claim.

Outside a bucketed figure it is refused -- the population would be everything
ever collected, so the number drifts with the data's age and nobody can say
what it is a median *of*. Over a rollup it is refused as the mean of means it
would be.

### Reading it back

```
# How the target and the work moved, month by month.
reading site.pace(range):
    display "{site} pace"

    depends:
        t = site.target_month in range

    calculate:
        median(t)
        delta(t)
```

Nothing here is special to carried data: a carried figure is an ordinary
bucketed figure whose buckets happen to be dense, so every reading, window
and bundle rule applies unchanged.

---

## `reading` -- evaluated, never stored

A figure is stored; a reading is evaluated on demand. Everything else about
them is deliberately the same shape -- a mandatory explanation, a display
template, a version that is the hash of the semantics -- because the claim is
the same claim: this number has a written definition and you can cite it.

There are two kinds, told apart by what `depends` binds.

### Windowed: over a figure's stored buckets

```
# How long this person's changes took to merge.
reading team_person.to_merge(range):
    display "{team_person} time to merge"

    band:
        when value > team_person.lead_time_goal then "over"
        otherwise "ok"

    depends:
        merged = team_person.time_to_merge in range

    requires:
        at least 3 values in merged

    calculate:
        mean(merged)
        worst(merged)
```

`<figure> in range` summarises a **time-keyed** figure's stored values over a
window of its buckets. The source is not a set expression on purpose: the
figure already decided the population at write time, so the only narrowing
left is temporal, and offering set arithmetic here would let a definition
re-litigate membership at read time using values instead of ids.

The rule that keeps a reading a definition rather than a function: **an
argument may narrow the population and may never change the calculation.**
`range` -- the only argument a reading can take -- picks which stored
buckets take part; the statistics, the minimum sample and the band decide
what the number *means*, so they are written here and hashed here.

At serving time a range is a **span of integer positions in the source
figure's own bucket sequence**, counted back from the anchor -- bucket 1 is
the bucket the anchor falls in, both ends inclusive. The sequence is
whatever the figure's group declared: `over 30` on a day-grained figure is
the trailing thirty days, on a month-grained one the last thirty months,
on a `first monday of month` one the last thirty first-Mondays. The
argument grammar is integers and nothing else -- **no units, no dates, no
calendar words** -- because the bucket rule changes what the number means,
and by the law above an argument may never do that. (Unit-suffixed spans
-- `1-48h`, `over ... in hours` -- shipped briefly and were retired: the
same reading meant two different things under two spellings of one
request. They now refuse with directions to the group clause.) Dates
appear in **answers**, never in questions: the engine resolves each span
to the concrete buckets it covered against the tenant's calendar and
reports them on the window -- edges for a contiguous rule, the full bucket
list for a sparse one -- so "the last 6 months" is never the client's
guess.

The span forms, at the HTTP door and in a bundle member's `over` list
alike:

- `30` -- the trailing span, buckets 1-30 pooled into **one window**;
- `31-60` -- an offset span, the thirty before them, still one window;
- `each 1-12` (HTTP: `each:1-12`), or the bare `each 12`, which means
  `each 1-12` exactly as `12` means `1-12` -- sugar for the twelve one-bucket
  windows `1, 2-2, ..., 12-12`, **one window per bucket in order** (the
  nearest is spelled `1`, since a span starting at bucket 1 canonicalises
  to its bare bound), so a
  per-bucket comparison -- this month against each of the eleven before it
  -- is not twelve enumerated spans. It expands at the door: the sugared
  and enumerated spellings are indistinguishable downstream, duplicate
  check and hash included.

Every declared rule applies per window unchanged: the floor withholds a
thin window's statistics with the reasons named, the band bands each
window, the series returns each window's own points.

### The worked example: day, month, quarter

The driving shape is one event stream cut at three calendar grains -- an
order lands in its day, its month and its quarter -- and each cut is its
own declaration:

```
group shop_order.drops_by_day     from (courier_id, delivered_at by day in shop_courier.timezone)
group shop_order.drops_by_month   from (courier_id, delivered_at by month in shop_courier.timezone)
group shop_order.drops_by_quarter from (courier_id, delivered_at by quarter in shop_courier.timezone)

# Deliveries per courier per day.
figure shop_courier.daily_drops bucketed:
    display "{shop_courier} deliveries that day"
    depends:
        done = shop_order.drops_by_day:{shop_courier}
    calculate:
        count(done)

# Deliveries per courier per calendar month -- the month's own records,
# not a rollup of days.
figure shop_courier.monthly_drops bucketed:
    display "{shop_courier} deliveries that month"
    depends:
        done = shop_order.drops_by_month:{shop_courier}
    calculate:
        count(done)

# Deliveries per courier per calendar quarter.
figure shop_courier.quarterly_drops bucketed:
    display "{shop_courier} deliveries that quarter"
    depends:
        done = shop_order.drops_by_quarter:{shop_courier}
    calculate:
        count(done)

# The month-by-month picture a goals screen compares against.
reading shop_courier.drops_by_month(range):
    display "{shop_courier} deliveries, month by month"
    depends:
        months = shop_courier.monthly_drops in range
    calculate:
        sum(months)
        series(months)
```

`?trailing=30` on the day reading is the last thirty days;
`?trailing=each:1-12` on the month reading is a year of month windows, each
summed, floored and banded on its own; `?trailing=each:1-4` on the quarter
reading is the last four quarters, year straddles handled by the calendar
because the labels are the calendar's (`2025-Q4`, `2026-Q1`). The three
figures agree by construction about which bucket an order is in, because
every label is derived from the same zoned day.

The source figure must share the reading's scope, be time-keyed, and store a
number -- an effort figure is refused (the reading path renders count or
duration, so an effort would be banded as wall-clock and printed as raw
seconds), as are a word and a moment. And **a reading may only read a
figure**, never another reading: composing them is how a team number becomes
a mean of means, weighting each person equally instead of each record.

### Live: over records, right now

```
# Review asks waiting on this person right now.
reading team_person.pending_reviews():
    display "{team_person} pending reviews"

    band on count:
        when value > team_person.queue_limit then "over"
        otherwise "ok"

    depends:
        waiting = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)

    calculate:
        count(waiting)
        worst(waiting)
```

`<measure> over <set expression>` measures records as they stand at the
moment of asking. This is the one construct that may name a clock measure
(`now - requested_at`), and it is safe because nothing is stored: whether an
ask is outstanding is decided by things that *happened*, and only the wait
itself runs to `now`. Splitting those two halves is the trick, and it
generalises -- before deciding something cannot be a definition because it
moves with the clock, split it and check which half actually does.

A live reading takes **no argument**: there is no range because nothing is
stored to pick from, and `()` is what says so. The argument list and the
source form encode the same fact twice, deliberately -- written `(range)`
over a live source, a reading would accept a window, ignore it, and return
today's answer under a heading saying thirty days, so the checker requires
the two to agree. The set expression follows a figure's rules: exactly one
group addressed by `:{scope}`, and the measure may not be a field measure.

> **Honesty note.** Live readings compile, are checked, and are versioned,
> but the standalone engine does not yet serve them: `answer()` raises
> `NotImplementedError` for a live reading, and the bulk results surface
> skips them. The construct is documented because it is part of the language
> and its rules are enforced today; the serving path is the missing half.

### Statistics

A closed vocabulary, not an expression grammar: `mean`, `median`, `worst`,
`sum`, `count`, `series`, `delta`. Each is a claim about a distribution that a
reader has to be able to check against the evidence, and an arbitrary formula
is not checkable by anybody not already reading the code. Each line of
`calculate` is one statistic over one bound set.

- **`sum` is why a count figure can be read at all.** The distribution
  statistics are refused over stored counts, because a mean of them is a
  mean per *bucket* wearing a label that says per record -- a plausible
  number of roughly the right magnitude, which is the worst kind of wrong.
  A sum of nothing is nought, and nought renders; a mean of nothing is
  unknown.
- **`count` is live-only**: a windowed reading already reports its sample,
  and for a count figure the two are *different numbers* -- the sample is
  the buckets that contributed, not records.
- **`series`** returns the per-point values rather than a scalar, **one
  point per bucket of the figure's own sequence** -- days of a day figure,
  months of a month figure, first-Mondays of a selective one. It exists so
  a sparkline is a definition's answer rather than the client slicing a
  range into ten and computing ten means. A bucket's point follows the
  bucket's shape: a `list` bucket's point is the mean of its own records,
  a `count` bucket's point is the count. A bucket that stored nothing is a
  hole, never a nought. One reading declares one series. (An earlier
  `series(...) by <grain>` clause regrouped stored buckets on the way out
  -- read-time truncation under the reading's name; with the coarser
  grains declarable, the coarser view is its own figure and the clause is
  retired, with directions.) A **minute-grain figure refuses `series`
  outright**: over a sparse figure a minute bucket holds one record, so
  the point *is* the record -- the raw collection the payload exists to
  withhold.
- **`delta`** is the change into each bucket of the range: one cell per
  bucket, in the same order and count as a series, so the two draw against
  one axis. n buckets produce n-1 changes, and the cell that has none is the
  **oldest bucket in the range** -- it has no predecessor inside the window,
  and the answer says so rather than omitting the bucket. That absence is
  the whole discipline of the construct. The tempting fix is to fetch the
  bucket *before* the window and difference against it, which produces a
  fuller chart and a number the response cannot account for: one of its
  values would be about a bucket the response does not contain, and a reader
  checking the answer against its own evidence would come up short. It is
  the same refusal an offset selector gets, one layer out.

  A hole breaks the chain in **both** directions: the change into a missing
  bucket and the change out of it are equally unknowable. Bridging the gap
  -- differencing March against January because February is missing --
  reports a two-bucket movement in a column headed per-bucket, and does it
  exactly where collection was patchy. A flat run reads a real nought, which
  is a finding ("it did not move") and not the same as not knowing.

  It is allowed over a **count** figure, where the distribution statistics
  are not: a mean of daily counts is a mean per day wearing a per-record
  label, but the change from one day's count to the next is a claim about
  days, which is what the buckets are. It is refused over a **live** reading
  (nothing is stored to difference, so it would answer an empty list under a
  heading promising a trend), twice in one reading (a response carries one),
  and under a **band** -- a band compares one number and a delta is one cell
  per bucket, so every row would band unknown for ever, which reads as
  missing data rather than as a broken definition. It is refused over a figure keyed by
  **minute or 15 minutes**, for the reason a minute-grain series is: a
  delta's cells *are* its source's buckets, and it has no grain to group
  them to, so ninety days of quarter-hours would be 8,640 cells on the wire
  -- the raw collection the payload exists to withhold. There are
  deliberately no stride or offset variants either; each would be a second
  spelling of a question nothing has asked.

  `delta` is what closed this language's oldest gap. A trend had been listed
  as missing for as long as there were readings, on the reasoning that the
  server could compute one over a reading's answer. That reasoning was wrong
  in the usual way: the number still reaches a screen, it just arrives with
  no definition claiming it and no version citing it.
- **A sum may not sit beside a distribution**: two numbers a reader can
  divide produce a third that no definition claims.

### `requires`

```
    requires:
        at least 3 values in merged
```

A precondition on the sample, not a filter on it. When it fails, **every
statistic is withheld together** -- a worst case printed alone is by
construction the outlier -- and the response names which requirement fell
short, instead of a dash whose reason lives in a constant nobody can see.

Unwritten, a windowed reading with a distribution statistic gets a default of
**at least 1 value**, injected into the plan and hashed exactly as a written
clause would be -- a floor applied at read time would let two engines render
the same version differently. So an unwritten minimum withholds only over an
empty window, and still says so. A sum takes no default, because a sum of
nothing is a real nought; a live reading takes none, because an empty queue
is a real count of nought pending, not a shortfall. Write the clause to raise
the floor.

### `band`

```
    band:
        when value > site.target_month then "over"
        otherwise "ok"

    band on sum:
        when value < site.quota_month then "under"
        when value < site.stretch_month then "met"
        otherwise "beaten"
```

The same ladder a figure bands with, one stratum along -- `value` is the
statistic being judged, and the thresholds are figures or literals. Declared
in the definition rather than applied by the reader, because banding in two
places is how a card reads one word while a sort weighs the same subject
differently, with list order the only symptom.

`on <statistic>` names which statistic the verdict is about -- the mean when
unwritten, and the named one must be one the reading actually calculates
(including the default: a band over a worst-only reading colours nothing, and
a permanently wordless row reads as missing data rather than a broken
definition). It cannot name `series` or `delta`, which are a cell per bucket
rather than a number.

**A threshold figure is read over the same window, through the same
statistic.** `on sum` bands the window's total, so the goal is totalled
across the identical buckets; `on mean` compares mean against mean. Any other
rule compares a span against a point -- six months of deliveries beside one
month of target -- which reads plausibly and is wrong by the length of the
window. It follows that the goal must be **time-keyed at the source figure's
own grain**, and the checker refuses anything else: a window is a span of
that figure's buckets, so a goal cut a different way has labels the window
never selects. A **live** reading has no window at all, so its threshold is
one value per subject and a sequenced figure is refused there.

There is no direction: it was a second vocabulary for what the comparison
operator already says.

**A threshold about a span of time says what scale it is written in.** Where
the figure being judged answers a `duration` or an `effort` -- both stored in
seconds -- a rung writes `when value >= 6.5 days`, or, reading a record,
`when value >= shop_courier.max_minutes minutes`. The scales are `seconds`,
`minutes`, `hours`, `days` and `weeks`, each a plain multiple of a second.

This is the one clause the retired `band low against <dial> in minutes` had
that was worth keeping, and it was removed with the dial on the argument that
a figure carries its own unit for the checker to compare. That is true when
the threshold *is* a figure, and false of both roads that replaced the dial
for the common case: a fact field is structural by design (`max_minutes as
number` claims a shape and never a meaning) and a literal claims nothing at
all. So a duration in seconds could be judged against a number meaning
minutes and be wrong by sixty for ever -- the dial's exact failure, on the
path this guide recommends.

The scale is refused on any other unit, by the rule the rest of the language
follows: declare what a reader downstream would otherwise get silently wrong,
never what the declaration already says. A count of deliveries is a tally,
and `3 days` beside it is a second claim about what the number measures. It
is also a *spelling* rather than a semantic -- folded to seconds before
anything is hashed -- so `168 hours` and `7 days` are one definition with one
version.

What is hashed is the ladder itself, and `on` unless it is the default.

---

## `projection` -- one row per record

```
# One row per work item: its key, its age, and the sentence it earns.
projection work_issue.item:
    sort by age_days descending
    limit 300

    field:
        key = key as text
        status_changed = status_changed_at as date
        active = active as flag
        epic_start = start_date from container_id through work_container.id as text

    value:
        age_days in days = days from status_changed to now
        stuck in count =
            when active == 0 then 0
            when age_days >= 14 then 1
            otherwise 0

    flag issue-long-wip when stuck == 1:
        label "Stuck {age_days}"
        detail "Has not changed status in {age_days}."
        action "Pick {key} up or put it down."
        severity attention
```

The only construct that may read a clock **and** produce prose, and it is
safe with both for one reason: **it stores nothing**. A row is assembled when
somebody asks, at one instant passed in by the caller -- never "the current
time" read per row -- so one instant reaches every row and a page cannot
disagree with itself.

**It aggregates nothing.** No counting its own rows, no averaging a column --
`count`, `sum`, `list` and the extremes are all refused here. Those are
figures, and offering them in a projection would be a second way to compute a
number this engine claims has exactly one. One row about the population is a
[summary](#summarise----one-row-about-the-population).

### `from` -- the population

Without it, every record of the projection's kind gets a row. With it, the
page is a declared population:

```
# Everything in flight, plus anything sized enough to plan.
projection work_issue.board:
    from work_issue.active | work_issue.sized

    field:
        key = key as text
```

`from` is the definition of *on the page*, written where a reader can check
it -- so which records get a row is part of the projection's version, filter
specs included: redefining a filter a population reads moves the projection's
version (and its summary's), the same way a live reading's groups and filters
move its. A population hashed by filter names alone would let two different
pages cite identically.

It speaks the set language a figure's `depends` speaks -- `&`, `|`, `-` over
declared names -- with rules of its own, because there is no subject here:
`from` decides which records *become* rows, so nothing exists yet to scope a
bucket by, and there is no depends block for a bare name to refer to. Each
refusal is a case that would otherwise resolve to the empty set, and an empty
population is not an error anybody sees -- it is a page with no rows that
looks like a complete one:

- **Only declared filters**, never bare names.
- **Only predicate and presence filters** -- a single bucket, read whole. A
  group read whole looks for a bucket keyed by the empty string and
  finds nothing; a scoped bucket (`:{...}`) has no row to be scoped by.
- **No age filters.** Age buckets are resolved against the clock at reindex
  time, and no figure pointer covers a filter only a `from` reads -- so the
  page would show a population as old as the last pass, and moving the
  threshold its owner declares would change who is on the page with nothing
  rebuilding it.
- **The filter's id space must be the projection's kind.** Ids from another
  space match no record of this kind, so every row would be filtered away
  with nothing thrown.

The buckets a `from` narrows through are stored state, so serving is gated
the way a figure's pointer gates it: the engine records, per group and
filter, which spec version a tenant's buckets were built under -- recorded
only after that grouping's rebuild actually ran -- and a projection whose
population was bucketed under a different definition (or never) answers
`behind-deploy` (or `never-computed`) rather than an `ok` page with records
silently missing. (A kind with no records at all still answers
`nothing-collected` first -- that is a claim about the sync, and it comes
before any question about buckets.) Only the groupings the population
actually reads are compared, so a deploy that changes an unrelated group or
filter holds nothing: each `from` page waits exactly when its own groupings
moved, until the tenant's next pass -- a short, honest absence, traded
deliberately against a page that cannot silently be wrong. A population that
matches nothing is served `ok` with no rows -- records were collected, and
the empty page is the population's truthful answer.

### `field` -- values off the record

`<name> = <path> as <type>`, where the type is `text`, `date`, `number` or
`flag`. **The type is required**, because a string is mute in the way an
integer is: `date` is what lets a span know a value is a moment, `flag` is
what lets a condition test a boolean without comparing against the string
`"true"`. Inferring from the shape of one record's value would classify an
epic with no due date differently from one with a date, under the same
definition.

**A join** reads a path off a related record:
`epic_start = start_date from container_id through work_container.id as text`
-- find the `work_container` whose `id` matches this record's `container_id`,
and read `start_date` off it. The `through` phrase is byte for byte the one
a group uses, so a reader who has learned one has learned both. **Anything
other than exactly one match is nothing**: a group resolves a relation to
every owner on purpose, but a field holds one value, so the choice is between
picking a winner and admitting there is no answer -- and picking the first in
sorted order would be stable and still a fabrication, about the wrong record.

### `read` -- stored figures, per row

```
# One row per person: the number, and the word beside it.
projection team_person.card:
    field:
        name = display_name as text

    read:
        wip = team_person.wip
        wip_band = band of team_person.wip
```

`<name> = <figure>` binds a stored figure's value for this row's subject --
which is why the figure's scope must be the projection's kind: a projection
over one kind asking for a figure scoped to another would look every row up
under an id from a different space and find nothing, a column of dashes for
ever. Day-keyed and `across` figures are refused for the same
one-value-per-row reason.

`band of <figure>` binds the *word* the figure's own `band:` block answers,
as a `level`, derived at serve time from the value and the goals that
figure's band names. It is
a second spelling rather than a binding that appears automatically beside
every read, because a name in scope that appears nowhere in the text is the
thing this language is arranged against -- and it is refused over a figure
that declares no band, where every rung testing it would stop and every flag
gated on it would silently never fire.

### `value` -- derived per row

The figure expression language again -- arithmetic, `max`/`min`, ladders --
over the row's own bindings, plus the one construct legal only here:

```
        age_days in days = days from status_changed to now
```

**A threshold here is a `read:`**, not a dial. A row value or a flag
condition comparing against a number that varies binds the figure holding it
above and compares against that column; a number that does not vary is
written where the reader can see it. A dotted name that names no figure is
refused with the rewrite, for the reason a band's was: a dial moved which
rows earned a flag with nothing in the row to say so.

`days from <moment> to <moment>` is signed calendar days between two
instants, either of which may be `now`. It is the clock, and the rule the
clock has always had is that a *stored* value may not read one; a projection
stores nothing, so the question does not arise. Days rather than seconds
because a written threshold about an age is written in days -- "fourteen" is
what somebody says -- and the first definition that forgot to divide by 86,400
would compare seconds against days and read as never crossing. Signed, so "overdue by three" and "three days left" are one
expression. Both ends must be moments -- a `date` field, a `moment` read, or
`now`.

A value declares its unit before the `=` (`in days`, `in count`, ...) for the
reason arithmetic in a figure does. A ladder returning **words** must not
declare one -- its unit is worked out -- and a ladder returning **numbers**
must, or the renderer prints "77.5 late" where the definition meant "78d". A
ladder's rungs may sit on the following lines, indented.

### `flag` -- a sentence a row earns

```
    flag issue-long-wip when stuck == 1:
        label "Stuck {age_days}"
        detail "Has not changed status in {age_days}."
        action "Pick {key} up or put it down."
        severity attention
```

The construct this language was most reluctant to add. Half of what a status
screen produces is not numbers but *conditional prose*, rendered from the
same values the bands read, and leaving it in the host would mean a row's
*reason* lived somewhere its *number* did not.

What keeps it a template rather than a language: substitution, and one plural
form. `{name}` prints a bound value, rendered by the server in the value's
own unit; `{count|change is:changes are}` picks a form from the same binding
it prints, so a sentence cannot pluralise on one number and print another.
No expressions inside a placeholder, no formatting directives -- anything a
sentence needs computed is a `value`, named and checkable beside the flag
that reads it. A placeholder naming nothing the projection binds is refused
at compile time.

The `when` condition is one comparison or presence test, never a conjunction
-- two conditions that must both hold are a ladder `value` tested here by its
word. A condition over an unknown does not fire the flag. The flag's name is
hyphenated (`issue-long-wip`); it is the kind a screen groups by. `label`,
`detail` and `severity` (`info` or `attention`) are required; `action` --
what to do, in the imperative -- is optional, because most flags have nothing
to ask for and inventing an imperative puts a to-do on a page whose value is
that every row is actionable. A projection may declare several flags.

Unlike every other piece of prose in the language, **a flag's templates are
in the version hash**: a figure's display describes a number that did not
move, but a flag's sentence is the whole content of that row.

### `omit` -- a row-level gate

```
    omit when parked == 1
```

`omit when <comparison>` drops a row when the condition holds, at the moment
the row is assembled. It exists for the one narrowing `from` cannot say: a
population whose membership moves with the clock. `from` filters through
stored buckets, and a bucket resolved against the clock at reindex time goes
stale between reindexes -- which is why age filters are refused there. The
gate reads the row's own computed values instead, at the same single instant
every value reads, so "starts more than thirty days out" is decided fresh on
every ask and can never be stale.

The example names a `value`, not a raw span, on purpose. A gate is usually
several judgements -- too far out, *and* nobody working it, *and* nothing
delivered under it -- and the condition grammar refuses conjunctions, so the
judgement lives in a ladder `value` (`parked`) whose rungs a reader can
argue with one at a time, and the gate tests its word. A raw
`days_until_start > 30` is the shape review rejects on sight: a start pushed
to next quarter with the due date left behind is the common data error, and
a start-only gate hides exactly the row that needs attention.

An omitted row is off the page *and out of the summary* -- the counts are
over the rows a reader can see, for the same reason `from` removes its
records from them. The gate runs before the summary, the sort and the limit.

The condition is one comparison or presence test, never a conjunction, like
a flag's `when`: two conditions that must both hold are a ladder `value`
tested here by its word. **A condition the engine cannot answer keeps the
row.** A flag's unknown does not fire because a flag is a claim; the gate's
unknown does not drop because dropping on the absence of evidence would
narrow the population by a cheap path, and a page quietly short one row
corrects itself never.

`omit` is the definition of *on the page* as surely as `from` is, so it is
part of the projection's version hash: a projection that starts omitting
rows cites differently, and its summary follows.

### `sort` and `limit`

`sort by <binding> ascending|descending`, over anything bound except a moment.
Rows with no value for the sort key go last in either direction -- written as
a constant rank they would sort first descending and push real rows off a
limited page. **`limit` is refused without `sort`**: a limit with no order
returns an arbitrary subset that looks like a complete list and changes
between runs for reasons no reader can see. An order with no limit is fine;
that is just a sorted list.

Sort and limit are applied *after* any summary is computed, so a summary is
always about the whole population and never the page.

> A previous edition of this page warned that `from` was accepted and hashed
> but not honoured by the serving path. That has changed: the population is
> filtered through its stored buckets on every serve, gated by the index-set
> version so a page whose buckets were built under a different library
> answers `behind-deploy` rather than serving rows silently missing.

---

## `summarise` -- one row about the population

```
# The backlog, in one row.
summarise work_issue.backlog over work_issue.item:
    count items
    count items_stuck where stuck == 1
    total days_waiting in days = age_days where stuck == 1

    value:
        verdict =
            when items_stuck > 0 then "attention"
            otherwise "clear"

    flag backlog-stuck when items_stuck > 0:
        label "Stuck work"
        detail "{items_stuck} {items_stuck|item is:items are} sat still."
        severity attention
```

A separate declaration rather than a block inside `projection`, because a
projection answers "one row per record" and this answers "one row about the
population" -- two definitions, two names, two versions. Folding them would
give one name two answers, which is exactly what `name@version` exists to
prevent.

It aggregates **all** of one projection's rows, never the page: sort and
limit belong to the page, and a summary of the first three hundred rows under
a heading naming the whole population is a wrong number that reads as a right
one. It cannot read a record, a fact or a figure directly -- everything
arrives through the projection, so there is one population and one route to
it -- and it cannot summarise another summary, for the reason a reading may
not read a reading.

- **`count <name> [where <condition>]`** -- how many rows. Absent `where`
  means every row. **An unknown does not count, so a count is a floor**: a
  row the engine has not measured is not evidence that the thing being
  counted is true.
- **`total <name> in <unit> = <column> [where <condition>]`** -- a row value
  added up. Only a number may be summed -- a word would concatenate and a
  date is not in the numeric namespace at all. **An absent contribution makes
  the whole total absent** -- deliberately the opposite decision to a count,
  and stated in both places: a sum that skipped the unmeasured rows is
  arithmetic over a population nobody chose; it reads low, plausibly, and
  repairs itself later, which is the sawtooth signature. Rows a `where`
  excludes contribute nothing and are not absences. The unit is required
  rather than inherited from the column, because a share per row totals to a
  *quantity of shares*, which is not a share of anything.
- **`value:`** -- ladders and arithmetic over the counts and totals above,
  and over nothing else. A summary value may not read a row value: a row
  value is one number per record and the summary holds hundreds of them.
- **`flag`** -- exactly as in a projection, over the summary's own bindings.
- A count or total may not reuse a name the projection already binds -- one
  word would mean one row in one line and the whole population in the next.
- A `where` over a row value the projection does not bind is refused. A
  comparison that cannot be decided for a row -- an unknown, or an ordered
  test over something with no order -- does not put that row in the count,
  which is the floor rule again rather than a guess.

The summary hashes its projection's **version**, so renaming what a row value
means moves every count that reads it.

---

## `bundle` -- what travels together

```
# Everything the person card shows.
bundle team_person.card:
    merge_pace = reading team_person.to_merge over 7, 14, 30
    wip        = figure team_person.wip
    items      = projection work_issue.item
    backlog    = summarise work_issue.backlog
```

The composition stratum. The eight declarations above answer *how is this
computed*; a bundle answers *what travels together* -- a precalculated
dashboard tile, requestable by name on the results surface as one request.
The response is the members' ordinary answers, in the written order (order is
substantive: a screen binds to positions the definition wrote), each carrying
its own version and provenance exactly as it would served alone.

**A bundle defines no calculation.** Members are names plus arguments,
nothing else -- no `depends`, no `calculate`, no unit, no band, no
cross-member arithmetic of any kind. A number derived from two members is a
rollup figure's job; a trend inside one window is the reading's own
`delta`. Serving a bundle *triggers* evaluation of its
members, and every rule that makes a number trustworthy stays in the member's
own definition and hash.

### The members

Each line binds a **slot** -- the address a client reads that member at --
to a declaration keyword, a name, and (for a windowed reading alone) an
optional window list. The binding is required on every member: a client
addresses `card.merge_pace`, so the tile's layout is decoupled from
definition names and a renamed reading is a one-line change to the tile
rather than a change to every screen. A slot is a bare word, unique within
its bundle, and an *address only* -- each member's answer keeps its own
definition's label and doc, because nothing may let a bundle rename what a
number is called. Two slots may name the same member when their window
lists differ (this month beside last month is two questions about one
reading); the same member over the same window list is refused as two
names for one answer. The list, in its order -- window order is
substantive, a screen may bind to positions -- so `over 30, 60` and
`over 60, 30` are two differently-shaped answers and both may exist.

- **`<slot> = reading <name> [over 7, 14, 30]`** -- the bucket spans to
  serve the reading over, unwritten meaning the serving default. A span is
  integer positions in the source figure's own sequence, counted back from
  the anchor, bucket 1 being the bucket the request is anchored in:
  `over 30` is the trailing thirty buckets, `over 1-30, 31-60` two exact,
  non-overlapping windows, and `over each 1-12` a window per bucket --
  twelve one-bucket windows, in order, exactly the sugar the HTTP
  parameter's `each:1-12` is. **No units ride on the spans** (`in hours`
  shipped briefly and was retired): what a bucket is lives in the figure's
  group clause, hashed, so a tile serving a month figure walks months
  because the *figure* says so, never because the tile does. The same
  ceilings the request doors apply are applied here, at compile time, so a
  tile cannot commit to a request the server would refuse on every load: a
  span covers at most 3660 buckets and reaches at most 3660 days back
  through the figure's own rule (121 positions is six days of hours and
  thirty years of quarters), and one member asks for at most 366 windows --
  `over each 1-3660` is inside the span ceiling and is still 3,660 answers
  per subject. Only a *windowed* reading
  may carry the list; a live one is named bare, mirroring the language's
  rule that the argument list and the source form encode liveness twice,
  loudly -- written `over 7` a live member would accept a window, ignore
  it, and answer today under a heading saying seven days. A duplicate span
  is refused as the typo it is -- compared canonically, so `over 30, 1-30`
  is the same question twice, and an `each` expansion collides with the
  enumerated windows it stands for. (Honesty note: a bare live member
  compiles, and until live readings are servable a tile naming one answers
  the same not-yet refusal the reading's own route gives -- whole, never
  one member short.)
- **`<slot> = figure <name>`** -- its current value per subject. The same two shapes
  the bulk results surface declines to push are refused here at compile
  time: a **time-keyed** figure member would drag every stored bucket of
  every subject along on every request (declare a reading over it and name
  that), and a figure **split across** a dimension serves pairs, not
  subjects (name the rollup that adds its parts up).
- **`<slot> = projection <name>`** -- the page: rows, with the projection's own sort
  and limit, and its summary attached as always.
- **`<slot> = summarise <name>`** -- **the population row alone, without the
  projection's rows.** This is the serving capability the bundle adds: the
  row payload stays home, but the summary is still computed over ALL the
  projection's rows, never the page -- a summary of a page is a wrong number
  that reads right. There is deliberately no `with rows` modifier: a
  projection member means rows, a summarise member means the one row, and a
  tile wanting both names both -- in which case the projection is evaluated
  once and both members are served from it, so the two cannot disagree.

Every member must resolve to an existing declaration of its keyword's kind --
a name that is really something else is refused by what it actually is,
because `figure work_issue.item` compiling as "whatever that is" would make
what travels a surprise. A slot bound twice is refused, and so is the same
member under the same windows -- two names for one answer -- while two slots
over one reading with different spans are two questions and welcome. And
**a bundle may not name another bundle**: composition
stays flat -- one level of bundles over the declarations that compute -- so
there is no nesting to walk and no cycle to refuse.

Members are served at **one instant**: the clock is read once per bundle
request and handed to every member that takes one, extending the projection's
one-instant rule to the tile -- a page beside a headline evaluated at two
different moments can disagree with itself.

### A hash that cites nothing

A bundle is versioned like every other declaration -- so a changed tile shows
as a moved hash in the committed library artifact, which is the review
surface -- but the hash exists for that surface *only*: it appears in **no
storage key and no number's citation**. Nothing on screen ever cites the
bundle; every number inside cites its own member's `name@version`.

What is hashed is the member list -- slots, kinds, names and window
arguments, in written order (slots are structural: clients couple to them
as addresses, so a renamed slot is a changed tile; window arguments hash as
canonical spans, so `over 30`, `over 1-30` and `over 30 in days` are one
question with one hash). Prose stays out, like everywhere else. Member
*versions* stay
out too, deliberately -- the same asymmetry a windowed reading has with its
source figure: a member redefined underneath shows as that member's own moved
hash, on the artifact and on the wire, while the tile's composition -- which
did not change -- keeps its version.

---

## No settings

A definition never contains a tenant's numbers. Where a number varies it is a
**fact** -- a figure computed from the records that set it. Where it does not,
it is written in the definition, visible to the reader and hashed into the
version.

**Nothing reads a dial, and there is nowhere left to set one.** The lists a
schema declares are accepted and dropped, so a host need not empty them to
deploy; `PUT /tenants/{t}/settings` is gone. Each list held one kind of
number, and each of those is a fact now:

| List | Held | Reads it now |
|---|---|---|
| `figure_settings` | the numbers a calculation compared against | a figure named outright, or a literal |
| `reading_settings` | a band's threshold | a figure named in the ladder, or a literal |
| `project_settings` | a row value's or a flag's threshold | a figure bound with `read:`, or a literal |
| `bucket_settings` | a calendar, and an age filter's threshold | a field on the subject's record; a field on the owner's, or a number of days |

A dial named from any of those positions is refused, with the rewrite in the
message.

No path is reserved any more. `tenant.hoursPerDay` was the last one: the
renderer divided an effort by it to print "1d" instead of "8h", which made
the length of a working day something a tenant set from a form and every
effort on every board move when they did. An effort renders in hours, which
needs nothing from anybody.

---

## Versions

A definition's version **is** the hash of its semantics -- the first twelve
hex characters of a SHA-256 over a canonical JSON form of the compiled plan
-- and it is part of the storage key. That is what makes a definition change
a cache miss rather than an invalidation: the new version has no values, so
recomputing is a fresh write, and the old version's values stay intact to
explain any history that cites them. It is also the review surface: a host
that commits its compiled library sees every meaningful change as a moved
version in the diff.

Two properties, both held by construction and by test:

- **Prose does not change the hash.** Explanations, display templates and
  group and filter labels are all out: rewording an explanation never forks a version
  and never recomputes a stored value.
- **Neither does anything incidental.** Keys are sorted at every depth, line
  numbers are excluded, and absent optional parts are dropped -- so a
  refactor that builds the same plan in a different order, and a new optional
  keyword nobody uses, both leave every existing version where it was.
  Absent-unless-declared holds at every depth, which is what let bands be
  added to figures without rebuilding a single board.

What *is* hashed, and why each one had to be:

| In the hash | Because |
|---|---|
| the full spec of every group and filter a definition reads | changing what `code_change.open` *means* changes what the figure counts, though the figure's own text is untouched |
| the full spec of every measure it names | the same integer reading "5d" or "5" is a scale change |
| the calculation, ladders and all | it is the number |
| `across` | one value per subject becomes one per pair; the stored values mean something else |
| a figure's `band:` ladder, thresholds and all | the band is one of the answers the figure gives -- and it costs nothing to move, since no value is stored under it. A named figure hashes as a figure, distinctly from a subject-field read of the same spelling, so the two can never be swapped under a version claiming nothing moved |
| a rollup's **source version** | redefine the parts and the total must rebuild, or it reads a number derived from a definition that no longer exists |
| a reading's statistics and `requires` floor, written or defaulted | a floor applied at read time would let two engines render one version differently |
| a reading's band ladder and its `on` (unless the default mean) | the band is the verdict, and a reading that starts wording it differently is a different definition |
| a projection's fields, joins, reads (including whether a read is `band of`), values, sort and limit | a join decides *which record* a path is read off; `band of X` and `X` are different columns off one figure |
| a projection's flag templates | a flag's sentence is the whole content of that row |
| a summary's counts, totals, values, flags and its **projection's version** | rename what a row value means and every count moves |
| a bundle's member list -- kinds, names and window arguments, in written order | the response preserves that order, so a re-ordered tile is a different tile. Member *versions* are out (a moved member is its own moved hash, on the artifact and on the wire), and the bundle's hash is review-only: it appears in no storage key and no number's citation |

What deliberately is not: explanations, `display` templates, group and
filter labels,
`keyed as` (it decides what the checker permits, not what the arithmetic
produces), declaration positions, and the schema itself -- a version is the
hash of a definition's semantics, and the same definition under two hosts is
the same definition. A `fact` declaration is the same case as `keyed as`,
one construct up: it has a version of its own (fields and shapes; prose and
the name/url pointers are out), but **no downstream version reads it** --
declaring facts over a previously schema-taught world moves nothing and
rebuilds nothing.

One asymmetry worth knowing: a **windowed reading hashes its source figure's
name, not its version**. A reading is not stored, so which figure version it
read is a fact about the evaluation and travels on the response -- a reading
citing a moved figure is visible rather than silently re-versioned. A *live*
reading is the exception: it has no source on the wire, so its own version is
the only provenance token there is, and it hashes the resolved group, filter
and measure specs.

---

## The checker

The parser rejects only what it cannot represent; every rule that needs to
name a rule lives in the checker, which runs at compile time -- a definition
that does not compile is a build failure (or a 422 from
[`PUT /definitions`](http-api.md)), never a blank tile in front of a reader.
Every refusal names the rule and, wherever it can, what the mistake would
have *done*, because a message that only says "invalid" sends somebody
looking for a typo.

The ones most worth recognising, in the checker's own words:

- *"...is not a fact kind. Those are: ..."* -- the name's prefix is not in
  the host's schema. Kinds are closed and compile-time, so the fix is either
  a spelling or a schema change, and the message lists the candidates.
- *"...needs a prefix: a figure is named `<fact kind>.<what>`..."* -- every
  declaration carries its kind, because a citation is `name@version`.
- *"...is already a figure. A reading needs its own name..."* -- one
  namespace across all nine kinds.
- *"there is no group or filter called ... Declared: ..."* / *"...is not a
  set defined in depends. Defined: ..."* -- a typo, answered with what was
  actually bound.
- *"...buckets by ..., so it needs a bucket: write `X:{kind}`"* and *"...is a
  filter, so it has a single bucket and cannot be addressed per
  subject"* -- the two ways to mis-address a group or a filter, each of which
  would otherwise read a bucket keyed by the empty string and answer zero for
  everybody.
- *"set ... combines record sets over A and B. A set is a set of ids..."* -- two
  id spaces in one expression; the intersection is empty for ever. `keyed as`
  is the fix when the sharing is real.
- *"...applies M, which reads K, to a set holding K2 ids. Every lookup would
  miss..."* -- a measure over the wrong kind: the same silence one layer
  along.
- *"...names no group addressed by `{scope}`, so it has no subjects"* /
  *"...is fanned out by more than one group"* -- exactly one group fans a
  figure out.
- *"...names a population in `depends` and rolls up another figure's parts
  in the same calculation"* -- two populations with no rule for how they
  relate.
- *"...has a combine block, and there is no such block any more"* -- with the
  one line it becomes.
- *"...adds up X, which is not split across anything"* -- a rollup of an
  undimensioned figure totals a single value and looks right for ever.
- *"...takes a name that <kind> already has as a field"* -- both are read as
  `<kind>.<name>`, so one spelling would answer two things.
- *"...lists M, which is measured to now"* / *"...measures days from ... to
  ..., which reads the clock"* -- a stored value may not read a clock; a live
  reading or a projection may.
- *"...produces a number nothing can name. Add `unit <...>`"* and *"...its
  calculation already says what the number is"* -- arithmetic must declare a
  unit; everything else must not.
- *"...returns a number from its when ladder"* -- a figure's ladder answers
  words.
- *"a `when` ladder must end in `otherwise`"* (from the parser) -- falling
  off the end and stopping on an unknown both render as a dash, and only one
  of them is a claim.
- *"figure ... has no explanation. Write `#` comment lines directly above the
  declaration..."* (from the parser) -- the explanation is the
  customer-facing definition, so the four rendered kinds are refused without
  one; a group, a filter or a measure may go unexplained, because none is
  served to a reader on its own.
- *"there is no figure called ... A figure may only read one declared before
  it..."* -- ordering, and the cycle guard.
- *"...reads X as a single value, but X is split across ..."* / *"...adds up
  X over Y, but X is not split across anything"* -- `over` must match the
  source's `across`, in both directions.
- *"reading ... summarises stored values, so it must declare (range)"* /
  *"reading ... measures records as they stand, so it takes no arguments"* --
  the windowed/live distinction, encoded twice on purpose.
- *"...is not time-keyed -- the group that fans it out must end in a time
  bucket part (`by day`, `by month`, `by first monday of month`, ...)"* --
  only a time-keyed figure has a sequence to read over.
- *"the figure under ... stores a count, so mean(...) is a mean per *day*
  wearing a label that says per record"* -- only `sum` over counts.
- *"`series(...) by <grain>` was retired. A coarser view is its own
  declaration..."* -- the read-time regrouping, refused toward an
  hour-or-coarser group under its own name.
- *"...takes a series over a figure keyed by the minute, and a series'
  points are the stored buckets..."* -- the point would be the record, the
  raw collection the payload exists to withhold.
- *"...calculates both a sum and a distribution"* -- two numbers a reader can
  divide produce a third no definition claims.
- *"...takes a delta, and it measures records as they stand"* -- a delta is
  the change between adjacent stored buckets, and a live reading stores
  none.
- *"...takes a delta over a figure keyed by 15 minutes"* -- a delta's cells
  are its source's own buckets and there is no grain to group them to, so a
  sub-day source would put the raw collection on the wire. Left to compile
  it did not fail: it answered an empty list, with no reason attached.
- *"...bands on delta(...) / series(...), which is one cell per bucket rather
  than one number"* -- a band compares a single value; left to compile, every
  row would band unknown for ever, which reads as missing data rather than as
  a broken definition. The `series` half of this refusal is newer than the
  construct: banding a series compiled for as long as series existed, and
  coloured nothing.
- *"...bands on the mean, by default, which it does not calculate"* -- a band
  must colour a statistic the reading computes.
- *"...is a reading, and a reading may only read a figure"* -- no means of
  means.
- *"projection ... reads F, which is one value per <other kind>"* -- a read's
  figure must share the projection's kind.
- *"projection ... reads the band of F, which declares no band"* -- `band of`
  needs a `band:` on the figure.
- *"...counts something. A projection aggregates nothing"* -- one row per
  record; the aggregate is a summary.
- *"...takes N rows and does not say in what order"* -- `limit` needs `sort`.
- *"flag ... interpolates `{x}`, which nothing here binds"* -- a placeholder
  naming nothing would print the word undefined in front of a reader.
- *"summary ... binds 'x', which is already a value of ..."* / *"Only a
  number may be summed"* / *"...reads 'x', which nothing binds"* -- the
  summary's namespace and typing rules.
- *"...reads its calendar from "x", which is not a fact kind"* -- a calendar
  is a field on the subject's own record, so it names a kind and a field.
- *"...is fanned out by P, but G reads its calendar from Q.field"* -- the
  same rule at the figure, which is the only place that knows which kind the
  group's subject keys belong to.
- *"... is a dotted name, so it reads a tenant dial"* on an age filter --
  with both answers: read the threshold off the record's owner, or write the
  number of days.
- *"...band compares against "x", which is not a figure declared before it"*
  (a reading says *"which is not a figure"*) -- a threshold outside the fact
  stream is the one number on a card nothing can cite. The refusal carries
  the rewrite, because an author staring at a definition that worked
  yesterday needs to be told what to write instead.
- *"...band compares against F, which holds one value per day where this
  figure holds one per month"* -- a band and the number it judges share a
  bucketing. Left to compile, the two subject keys never meet and every row
  bands unknown.
- *"...answers a share and its band compares against F, which answers a
  count"* -- both are numbers by the time the ladder sees them, so the
  comparison runs and is wrong by whatever the units differ by.
- *"...band compares against F bare, and it holds one value per month"* --
  name the coordinate: `F:{bucket}`.
- *"a band is a ladder now, not a direction and a dial"* -- the retired
  `band low against <dial> in minutes` clause, refused by the parser with the
  rewrite, on a figure and on a reading alike.
- *"...answers a duration, which is stored in seconds, and its band compares
  against 561600 with no scale"* -- and the message does the division, because
  the whole point is that nobody reads 561600 as six and a half days.
- *"...band compares against 3 days, and the figure answers a count"* -- the
  same rule from the other side.
- *"bundle ... names X as a figure, but it is a projection"* -- a member is
  written under its own keyword, so what travels is never a surprise.
- *"bundle ... names X, which is time-keyed... declare a reading over it"* /
  *"...which is split across... Name the rollup"* -- the two figure shapes a
  tile must not subscribe to, refused at compile time rather than at serve
  time.
- *"a bundle may not name another bundle"* -- composition stays flat.
- *"bundle ... gives X windows, and X measures records as they stand"* -- a
  live member is named bare, the liveness-encoded-twice rule one level up.

---

## Deliberately absent

Each of these was considered and refused, and the refusals are as much a part
of the language as the grammar. A construct with no definition using it is a
construct nobody has checked.

| Not here | Why |
|---|---|
| `and` / `or` / `not` | a list of clauses is checkable; a boolean expression is a transliteration |
| `week` / `month` truncation | a range over days produces either; storing the coarser grain is two answers to one question |
| `hour` truncation | the same argument pointed downward: a range over quarter-hours produces it, so it is a series grouping at read time |
| numeric minute counts other than 15 | a truncation is a product decision about grain; nothing has asked for them (the one-minute grain is spelled `by minute`) |
| a minute-resolution series | over a sparse figure the point *is* the record -- the raw collection the payload exists to withhold |
| `percentile` | nothing has asked for it |
| aggregation in a projection | those are figures, and this engine claims one way to compute a number |
| a general `max(<measure> over <set>)` | `latest` / `earliest` are the narrow case a definition wanted |
| arbitrary text | a figure that could return any string would be a template engine with a version hash |
| an inline predicate in `depends` | it would let a declaration lie about what it reads; narrowing is a declared filter's job |
| `key of <kind>` on a fact field | the correlation is the group's `through` claim; on the fact it would be declared twice, and nothing in a record marks a field as special |
| a unit on a fact field | what a number means is the measure's claim; a fact is structural, and grams filed as a `count` would compile |
| a list of scalars in a fact | no construct can read one -- a predicate cannot test membership -- and a declared-but-unreadable field is a construct nobody has checked |
| `work_hours` as a scale | as shipped it was a synonym for `hours` -- 3,600 seconds, with a docstring claiming a working day mattered and nothing that made it. Doing it honestly is a working calendar, and doing it dishonestly is a dial wearing a keyword |
| negative literals | `-` already means subtraction *and* set difference, so `mine -3` would parse two ways depending on a space. A negative value comes out of a subtraction; a negative threshold is written the other way up |
| `:{bucket - 1}` and other offsets | an answer needing a bucket outside the population in view cannot be checked against the response that carries it; the change between adjacent buckets is `delta`, where the range bounds it |
| a unit inherited from a fact field | the fact layer is structural, so there is nothing there to inherit; a declared-field read names its unit because nothing can derive it |
