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
and which settings dials a definition may read. Source reaches the engine
over `PUT /definitions` (see [the HTTP API](http-api.md)), where a definition
that does not compile is refused whole, in the checker's own words.

---

## A first definition

A world of couriers and orders. Facts arrive as plain JSON records, each with
an identity -- a *kind* and a *key* -- and a body of fields
([Concepts](concepts.md) carries the full model, over this same dataset):

```
shop_courier "c1"  { "name": "Aki" }
shop_courier "c2"  { "name": "Bo" }

shop_order "o1"    { "ref": "A-1", "courier_id": "c1", "status": "riding" }
shop_order "o2"    { "ref": "A-2", "courier_id": "c1", "status": "riding" }
shop_order "o3"    { "ref": "B-7", "courier_id": "c2", "status": "delivered" }
```

The correlations are already in the data -- each order's `courier_id` names a
courier's key -- but nothing yet says what those fields *mean*. That is
what a definition declares. The host has told the engine the two kinds,
`shop_order` and `shop_courier`, and one dial, `limits.carrying.over`:

```
index shop_order.carried_by from courier_id
index shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{shop_courier} has {value} orders in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

    band:
        when value >= limits.carrying.over then "over"
        otherwise "ok"
```

Reading it back: the first index puts every order in a bucket per courier --
`o1` and `o2` land in Aki's, `o3` in Bo's; the second holds whichever orders
are not yet delivered, which excludes `o3`. The figure intersects the two for
each courier and counts what is left, so Aki reads 2 and Bo reads 0 -- a
measured nought, not a blank -- and the band turns each count into a word by
comparing it against a dial the host declared. The `#` line above the header
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

Keywords are **not reserved**. `index`, `figure`, `depends` and the rest come
out of the lexer as ordinary names and the parser matches on their text -- a
reserved word list is a thing that grows and then collides with somebody's
index called `display`.

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
  number is cited. Every figure, reading, projection and summarise is refused
  without one, in these words: *"figure shop_courier.carrying has no
  explanation. Write `#` comment lines directly above the declaration -- they
  are the customer-facing definition, rendered wherever the number is cited,
  and a figure nobody can read is the thing this language exists to
  prevent. (A `# ----` rule line is a file banner, not prose.)"* An index or
  a measure may carry one and is not made to. Three
  rules decide which lines belong. The run is unindented `#` lines, and may
  sit at most **one blank line**
  above the header -- the origin project measured what strict contiguity
  cost, and a third of its declarations rendered bare with their explanation
  stranded a single blank line up. **Two blanks** is a detached paragraph,
  and adopting it would attach prose the author never aimed here. And a
  **banner** -- a line of dashes (`# ----------`), or a dash rule naming a
  file (`# ---- board.fig ----`) -- is a file rule, not prose, and ends the
  run: it is
  what a concatenating build writes between two `.fig` files, and without the
  exception the first declaration in every file would adopt the previous
  file's banner as its explanation. (`# --- see the note below` is prose --
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
  literals; a negative threshold lives in a settings dial, and a negative
  value comes out of subtraction.
- **Names** may contain dots, and the dot is meaningful. Every declaration is
  named `<fact kind>.<name>` -- `shop_courier.carrying` -- because a citation
  is `name@version` and the prefix says what the definition is about. Inside
  an expression, a *dotted* name is a settings path or an index, and a *bare*
  name is something the definition bound above; nothing else can produce
  either shape, so a typo is reported with the list of what was actually
  bound.

**One namespace covers all six declaration kinds.** Two definitions sharing a
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

## Six declarations

| | Answers | Stores |
|---|---|---|
| `index` | which records are in which set | buckets |
| `measure` | a quantity read off one record | nothing |
| `figure` | one value per subject | values |
| `reading` | a statistic over stored days, or over records right now | nothing |
| `projection` | one row per record | nothing |
| `summarise` | one row about a whole population | nothing |

Only a **figure** stores anything, and that single fact decides which
constructs may read a clock: a stored value computed from `now` is stale the
instant it is written and nothing would ever recompute it, because the clock
is not an event. Every number would be real exactly once. So `now` is fenced
by where the result may go -- a figure may never name it, and the constructs
that may name it store nothing.

---

## `index` -- which records

```
index shop_order.carried_by from courier_id
index shop_order.open where status != "delivered"
index work_issue.sized where estimate_seconds is set
index work_issue.stuck where status_changed_at older than thresholds.longWipDays
index work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
index code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
index code_review.approved keyed as code_change where was_approved == true
```

An index is named `<fact kind>.<name>`; the prefix is the kind whose records
it buckets. There are two shapes, because they answer different questions. A
**field** index (`from`) buckets every record by the value of one field, so it
*fans a figure out* -- one bucket per courier, per person, per repository. A
**predicate** index (`where`) is a single bucket holding whatever matches, so
it *narrows*. Intersecting the two is how a per-subject figure gets both at
once, as `carrying` did above.

An optional trailing `label "still open"` names how to say the index in a
sentence. It is prose: the fallback is mechanical and ugly, which is the
prompt to write one, and it never touches the version hash.

### `through` -- the identity hop

```
index work_issue.assigned_to from assignee_account_id through team_person.accounts.account_id
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

**Age**: `where updated_at older than thresholds.staleChangeDays` (and
`younger than`). Membership is decided by how long ago a moment was, in whole
days, against a dial. This is **the one clock a stored figure may read**, and
it is fenced differently from a clock measure rather than by the same rule: a
clock measure decays every second, but membership does not -- a record crosses
this line once, on a knowable day, and until it does the answer is unchanged.
The question is not "is this value decaying" but "how long may the crossing go
unnoticed", and the answer is *until the next full pass*. That only holds
because every dial these clauses may name is in days; the unit is fixed
precisely so the unsafe version cannot be written. A record whose moment
cannot be read is in **no** age bucket -- an absent timestamp is not evidence
of age.

The dial an age clause names must be one of the schema's **bucket settings**
(see [Settings dials](#settings-dials) below), because turning it re-buckets a
tenant's whole history.

### Composite indexes: time buckets and pairs

```
index code_change.merged_by_day from (author_account_id through team_person.accounts.account_id, merged_at by day in tenant.timezone)
index code_change.authored_in   from (author_account_id through team_person.accounts.account_id, connection_id)
```

A parenthesised `from` keys each bucket `<subject>@<rest>`; the parts can be
several, but a figure may only fan out of an index carrying at most two -- a
subject and one more is all a value key can hold, and the checker says so at
the figure, not the index. This is what lets a figure be per-person *and* per-day, or
per-person *and* per-source, without the engine learning a second scope
dimension. Because `@` joins the halves, a key value containing `@` is refused
outright rather than encoded -- a subject that decomposes into the wrong pair
is silent, and a refusal is not.

`by day` truncates an instant to a calendar day. `in tenant.timezone` names
*whose* calendar: the **name** of a bucket setting, never its value, because
one compiled plan is shared by every tenant. Naming the dial is also what
makes the dependency derivable, so turning it re-buckets automatically rather
than on the day somebody remembers. A bare `by day` means UTC, and that is a
choice a definition makes rather than a default it falls into -- two figures
on one card, one cut by UTC days and one by the tenant's, would be two rows
headed "30d" measuring two different months.

Two sub-day grains exist beside the day: `by minute` and `by 15 minutes`. A
sub-day label is local time truncated to the grain -- `2026-08-25T14:30` --
in the calendar the definition names, exactly as a day key is the local date.
Because the calendar is applied when the bucket is written, grouping a label
into its hour or its day at read time is prefix truncation: no zone
arithmetic happens on a read, and a grouped point can never disagree with the
day a `by day` index would have filed the same event under. When the clocks
go back, the repeated hour's two passes share their labels and their records
share a bucket -- the honest answer about a quarter-hour that occurred twice;
keying by UTC instead would put every local midnight mid-bucket in most of
the world's zones, a constant error to avoid a twice-a-year merge. The
spring-forward hour stays a run of holes, which is true -- those wall-clock
minutes never happened.

**The stored grain is the finest the definition claims, and every coarser
view is derived at read time.** `week` and `month` are deliberately absent as
truncations: a range over days can produce either, and storing the coarser
grain as well would be two answers to one question. That argument points
downward too -- `hour` is refused as a truncation because a range over
quarter-hours produces it, so it exists only as a [series
grouping](#statistics). Other minute counts wait for a definition to ask,
exactly as `percentile` does. The grain is in the version hash the way every
index spec is: minutes and quarter-hours store values that mean different
things, and reusing them across the change would file a quarter's count under
a minute's key.

Whether the second part is a time bucket or a dimension decides what the
figure over the index must declare -- see [time-keyed
figures](#time-keyed-figures) and
[`across`](#across----a-second-dimension).

### `keyed as` -- a second kind sharing an id space

Some worlds store two fact kinds about one underlying thing -- a change and
the review timeline rebuilt from its events, say -- keyed by the same id, so
their sets can be intersected and the answer means something. `keyed as`
declares that sharing:

```
index code_review.approved keyed as code_change where was_approved == true
```

Declared rather than inferred, because the failure it guards is silent:
intersecting ids that mean different things yields the empty set, and an
empty set is a figure reading zero for everybody rather than an error anybody
sees. Every index over one kind must agree about that kind's id space --
otherwise the guard could be defeated by writing a second index and leaving
the clause off, which is the quietest possible way to lose it.

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
default of `effort` prints a tally of reopens as `5d`, and neither throws.
Note that **`effort` is not a synonym for `duration`**: a duration is
wall-clock, so 28,800 seconds is eight hours; effort is working time,
rendered against the tenant's working day, so the same 28,800 seconds is one
day. Both renderings are right about their own quantity and one number cannot
have both. A field holding several numbers, or a numeric *string*, reads as
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
        when value >= thresholds.wip.over then "over"
        when value >= thresholds.wip.warn then "warn"
        otherwise "ok"
```

A figure is named `<fact kind>.<name>`, and the prefix is its **scope** -- the
kind of subject it is one-value-per. It requires an explanation (the `#` lines
above it -- the definition a reader is shown), a `display` template (the
sentence a movement is reported under), exactly one of `depends` or `combine`,
and a `calculate` block. `unit` and `band` are optional.

The `display` template is prose, written by convention with the scope kind,
the `across` kind for a split figure, and `{value}` as placeholders --
`"{team_person} open in {data_connection}"`. Like the explanation it is stored
with the definition, where a host's data screen can show it, and like the
explanation it is deliberately outside the version hash.

### `depends` -- sets of record ids

Each line binds a name to a set expression over indexes:

```
    depends:
        mine = work_issue.assigned_to:{team_person} & work_issue.active
```

- `index:{scope}` addresses a field index's bucket for this subject. Writing
  a field index *without* the bucket is refused: read unbucketed it looks for
  a bucket keyed by the empty string, finds nothing, and the figure reads
  zero for everybody. Addressing a predicate index *with* one is refused for
  the mirror-image reason -- it has only the one bucket.
- `&`, `|` and `-` are intersection, union and difference. They share one
  precedence level and associate left to right; parenthesise anything you
  would have to think about.
- A bare name refers to a set bound on an earlier line.

There is **no filter here, on purpose**. `depends` may narrow only by set
membership, never by record contents, because a predicate over contents
cannot narrow what the figure subscribes to and would therefore be a
declaration that lies. This is the language's one safety property, and the
checker upholds it: anything a figure wants to filter by must be an index,
where the subscription machinery can see it.

Two structural rules, both refused with the failure named:

- **One id space per set.** Intersecting `team_person`-bucketed work items
  with `code_change` ids is empty, for everybody, for ever, with nothing
  thrown. Use `keyed as` if two kinds genuinely share ids.
- **Exactly one index addressed by `:{scope}`** across all the sets. With
  none the figure has no subjects at all and would compute a board-wide total
  attributed to nobody; with two, a value would be keyed by two different
  things.

### `calculate`

The expression language, whose leaves a sentence can always name: a set's
size, a measure over a set, a bound figure, a literal, a settings dial.

- `count(mine)` -- how many records the set holds. A count of an empty bucket
  is a real nought, not an absence.
- `sum(work_issue.estimate over mine)` -- a field measure totalled across the
  set. Only a field measure may be summed: totalling a *duration* is a
  quantity nobody asked for (a list is what a span reads), and totalling
  moments is a date in the future.
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
- A dotted name (`thresholds.wip.over`) reads a settings dial declared in the
  schema's **figure settings**; a bare name reads a binding from `depends` or
  `combine`.

### The `when` ladder

```
    calculate:
        when wip >= thresholds.wip.over then "over"
        when wip >= thresholds.wip.warn then "warn"
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

### `combine` -- a figure reading another figure

```
# Open changes, all sources together.
figure team_person.open_mrs:
    display "{team_person} open changes"

    combine:
        sources = team_person.open_mrs_by_source over data_connection

    calculate:
        sum(sources)
```

`combine` binds another figure's stored values, so that **a total and its
parts cannot disagree**: there is one count, and this adds it up. Agreement by
construction is worth more than two independent counts held together by a
test.

`over <kind>` present reads the *parts* of a figure split `across` that kind;
`over` absent reads the one value the source holds for this same subject
(read it by its bare name in `calculate`, or feed it to a ladder or
arithmetic). Neither mistake fails loudly -- a bare read of a dimensioned
figure would take whichever part sorts first, and a rollup of an
undimensioned one would total a single value and look right for ever -- so
the checker refuses both by name, and also checks `over` against the source's
own `across`, so a source later split across something else fails the build
here rather than quietly changing what this is a total of.

The rules around it:

- A figure has `depends` **or** `combine`, never both: two populations
  arriving at one calculation with no rule for how they relate.
- The source must share this figure's scope -- different scopes are different
  id spaces, so every lookup would miss.
- The source must be declared **earlier in the source**. That is also why a
  figure cannot read itself.
- A rollup may total at most one dimensioned figure, and may not read a
  `level` figure (a word has no arithmetic) or a time-keyed one (it has one
  value per bucket, not one per subject) as a single value.

### Time-keyed figures

A figure whose scope index ends in a `by day`, `by minute` or `by 15 minutes`
part stores one value per subject *per bucket*:

```
# How long each change merged that day had been open.
figure team_person.time_to_merge:
    display "{team_person} time to merge"

    depends:
        merged = code_change.merged_by_day:{team_person}

    calculate:
        list(code_change.open_seconds over merged)
```

Time-keyed is what makes a figure readable over a range -- it is the property
a windowed [reading](#reading----evaluated-never-stored) requires of its
source. A bucket of time is not a dimension: it has no roster and no name, so
a time-keyed figure cannot also declare `across`, cannot be read by a
projection, and cannot be combined as a single value.

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

When a composite index's second part is a dimension rather than a date, the
figure must say so with `across <fact kind>`. A dimension is not a new kind of
thing -- it is a **second subject**, with a roster, a name field and an id
space the checker already knows how to reason about (the kind must have a
name field declared, or every row would be headed by a raw id, and it may not
equal the scope). Underneath is the same composite key a time-keyed figure
uses; what `across` adds is the *declaration*, without which every reader
downstream is silently wrong -- the display template renders the variable as
literal text and a generated sentence describes the whole population beside a
number that is a slice of it.

**A pair's roster is the index, never a cross product.** Crossing every
person with every source would write a nought against pairs that
categorically cannot hold a record. The consequence: a pair reads a real `0`
once it has *ever* appeared, and is absent until then.

### `band:` -- the word beside the number

```
    band:
        when value >= thresholds.wip.over then "over"
        otherwise "ok"
```

The second thing a figure answers: which of a few states its number is in.
It is part of the figure rather than a `level` figure of its own, because a
word on screen sourced from a definition the page never named -- found by
scanning the library for whatever banded this one -- is exactly the
untraceable number this engine exists to end.

Three rules keep it a band rather than a second calculation hiding in the
same block, and one property makes it cheap:

- The only binding in scope is **`value`** -- this figure's own answer.
  Reading a set or a source here would be a second calculation sharing the
  first one's name and version.
- Every rung must answer a **word**. A rung answering a number would reach a
  screen as an unexplained integer in a column of words.
- A figure whose value is a word or a list **cannot be banded** -- there is
  nothing to compare, so every subject would fall through to the bottom rung,
  silently.
- It is **evaluated when the figure is served and stored nowhere**: the
  ladder is pure over the value and the tenant's dials, so turning a
  threshold re-bands the board on the next request instead of forcing a
  rebuild. It *is* in the version hash -- a figure that starts banding
  differently is a different definition -- and that costs nothing, because no
  stored value hangs off it.

The dials a band reads must be figure settings, and they are tracked
separately from the calculation's dials, so a dial only the band reads never
forces a rebuild of values it did not affect.

---

## `reading` -- evaluated, never stored

A figure is stored; a reading is evaluated on demand. Everything else about
them is deliberately the same shape -- a mandatory explanation, a display
template, a version that is the hash of the semantics -- because the claim is
the same claim: this number has a written definition and you can cite it.

There are two kinds, told apart by what `depends` binds.

### Windowed: over stored days

```
# How long this person's changes took to merge.
reading team_person.to_merge(range):
    display "{team_person} time to merge"
    band low against flow.leadTimeDays

    depends:
        merged = team_person.time_to_merge in range

    requires:
        at least 3 values in merged

    calculate:
        mean(merged)
        worst(merged)
```

`<figure> in range` summarises a **time-keyed** figure's stored values over a
window of days. The source is not a set expression on purpose: the figure
already decided the population at write time, so the only narrowing left is
temporal, and offering set arithmetic here would let a definition re-litigate
membership at read time using values instead of ids.

The rule that keeps a reading a definition rather than a function: **an
argument may narrow the population and may never change the calculation.**
`range` -- the only argument a reading can take -- picks which stored days
take part; the statistics, the minimum sample and the band decide what the
number *means*, so they are written here and hashed here. The engine resolves
the range to actual dates against the tenant's calendar and reports them on
the answer, so "the last 30 days" is never the client's guess.

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
    band low on count against flow.pendingReviews

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
index addressed by `:{scope}`, and the measure may not be a field measure.

> **Honesty note.** Live readings compile, are checked, and are versioned,
> but the standalone engine does not yet serve them: `answer()` raises
> `NotImplementedError` for a live reading, and the bulk results surface
> skips them. The construct is documented because it is part of the language
> and its rules are enforced today; the serving path is the missing half.

### Statistics

A closed vocabulary, not an expression grammar: `mean`, `median`, `worst`,
`sum`, `count`, `series`. Each is a claim about a distribution that a reader
has to be able to check against the evidence, and an arbitrary formula is not
checkable by anybody not already reading the code. Each line of `calculate`
is one statistic over one bound set.

- **`sum` is why a count figure can be read at all.** The distribution
  statistics are refused over daily counts, because a mean of them is a mean
  per *day* wearing a label that says per record -- a plausible number of
  roughly the right magnitude, which is the worst kind of wrong. A sum of
  nothing is nought, and nought renders; a mean of nothing is unknown.
- **`count` is live-only**: a windowed reading already reports its sample,
  and for a count figure the two are *different numbers* -- the sample is
  the buckets that contributed, not records.
- **`series`** returns the per-point values rather than a scalar. It exists so
  a sparkline is a definition's answer rather than the client slicing a range
  into ten and computing ten means.
- **`series(...) by <grain>`** groups a sub-day figure's buckets --
  `15 minutes`, `hour` or `day`. The grain is declared and hashed rather than
  chosen by the caller, because an hourly series and a daily one differ in
  what every point *means*. It attaches to `series` alone: the scalar
  statistics run over the window's raw values whatever the grain is, so a
  grain on `mean` would either do nothing or make it a mean of group means,
  weighting each hour equally instead of each record. Over a sub-day source
  the grain is **required** -- a bare series over ninety days of quarter-hours
  is 8,640 points under a line that reads like any other sparkline, and that
  payload choice belongs in the definition. Over a day-keyed source it is
  **refused** -- a bare series already is the day series, and a second
  spelling of one thing is a first place for two to disagree. One reading
  declares one series -- two grains are two readings. A grouped point follows
  the day series' own rule at the new grain: the mean of a `list` figure's
  records, the sum of a `count` figure's buckets -- which is also why grouping
  is defined only for counts and list figures: any other stored scalar would
  have to be *summed* across buckets, and four quarter-hours at 0.5 becoming
  an hour at 2.0 is arithmetic no definition claims. A **minute** series is
  deliberately absent though the minute is a storable grain: over a sparse
  figure a minute group holds one record, so the point *is* the record -- the
  raw collection the payload exists to withhold -- and refusing the word is
  also what makes a finer-than-stored series unwritable, since the finest
  series grain equals the coarsest sub-day stored grain.
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
    band low against flow.leadTimeDays
    band low on count against flow.pendingReviews
    band low against ack.responseMinutes in minutes
```

Which dial decides whether the number reads good, watch or poor, and which
direction is good: `low` means lower is better (a wait, a latency), `high` is
for a share. Declared in the definition rather than applied by the reader,
because banding in two places is how a card reads one word while a sort
weighs the same subject differently, with list order the only symptom. The
dial must be one of the schema's **reading settings**, and its value is a
two-edged band, `{good, poor}`.

`on <statistic>` names which statistic is coloured -- the mean when
unwritten, and the named one must be one the reading actually calculates
(including the default: a band over a worst-only reading colours nothing, and
a permanently grey row reads as missing data rather than a broken
definition).

`in <unit>` is what the *threshold* is written in -- `minutes`, `hours` or
`days`, days when unwritten. It matters the first time a healthy value is
single digits of minutes: in days the tightest threshold anybody would type
is 1, so every row bands good and the column is decoration. It is in the
version hash, because the same numbers read in minutes are a band 1,440
times tighter, and a colour change under a version claiming nothing moved is
the one thing a content-addressed version must not do. `on count` bands the
count directly and refuses a time unit -- a count of things has no time in
it; left to the duration path, a count of 3 becomes 3/86400 against a
threshold in days and every queue bands good for ever.

There is no `work_hours` unit. It once existed, resolved to exactly 3,600
seconds, and was therefore a synonym for `hours` with documentation claiming a
working day mattered and nothing that made it -- removed rather than left as
a lie. Doing it properly is a working calendar, not a scale factor.

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
            when age_days >= thresholds.longWipDays then 1
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
it -- so which records get a row is part of the projection's version, index
specs included: redefining an index a population reads moves the projection's
version (and its summary's), the same way a live reading's indexes move its.
A population hashed by index names alone would let two different pages cite
identically.

It speaks the set language a figure's `depends` speaks -- `&`, `|`, `-` over
index names -- with rules of its own, because there is no subject here:
`from` decides which records *become* rows, so nothing exists yet to scope a
bucket by, and there is no depends block for a bare name to refer to. Each
refusal is a case that would otherwise resolve to the empty set, and an empty
population is not an error anybody sees -- it is a page with no rows that
looks like a complete one:

- **Only indexes**, never bare names.
- **Only predicate and presence indexes** -- a single bucket, read whole. A
  fan-out index read whole looks for a bucket keyed by the empty string and
  finds nothing; a scoped bucket (`:{...}`) has no row to be scoped by.
- **No age indexes.** Age buckets are resolved against the clock at reindex
  time, and no figure pointer covers an index only a `from` reads -- moving
  the dial it names would change who is on the page with nothing rebuilding
  it.
- **The index's id space must be the projection's kind.** Ids from another
  space match no record of this kind, so every row would be filtered away
  with nothing thrown.

The buckets a `from` filters through are stored state, so serving is gated
the way a figure's pointer gates it: the engine records which index set a
tenant last bucketed under, only after the rebuild actually ran, and a
projection whose population was bucketed under a different index set (or
never) answers `behind-deploy` (or `never-computed`) rather than an `ok` page
with records silently missing. (A kind with no records at all still answers
`nothing-collected` first -- that is a claim about the sync, and it comes
before any question about buckets.) The recorded version covers the library's
whole index set, so a deploy that changes *any* index holds every `from`
page in that state until the tenant's next pass -- a short, honest absence,
traded deliberately against a page that cannot silently be wrong. A population that matches nothing is served
`ok` with no rows -- records were collected, and the empty page is the
population's truthful answer.

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
an index uses, so a reader who has learned one has learned both. **Anything
other than exactly one match is nothing**: an index resolves a relation to
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
as a `level`, derived at serve time from the value and the live dials. It is
a second spelling rather than a binding that appears automatically beside
every read, because a name in scope that appears nowhere in the text is the
thing this language is arranged against -- and it is refused over a figure
that declares no band, where every rung testing it would stop and every flag
gated on it would silently never fire.

### `value` -- derived per row

The figure expression language again -- arithmetic, `max`/`min`, ladders,
settings dials (the schema's **projection settings**) -- over the row's own
bindings, plus the one construct legal only here:

```
        age_days in days = days from status_changed to now
```

`days from <moment> to <moment>` is signed calendar days between two
instants, either of which may be `now`. It is the clock, and the rule the
clock has always had is that a *stored* value may not read one; a projection
stores nothing, so the question does not arise. Days rather than seconds
because every calendar dial is in days, and the first definition that forgot
to divide by 86,400 would compare seconds against days and read as never
crossing. Signed, so "overdue by three" and "three days left" are one
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
stale between reindexes -- which is why age indexes are refused there. The
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

## Settings dials

A definition never contains a tenant's numbers. It names **dials** --
`thresholds.wip.over`, `tenant.timezone`, `flow.leadTimeDays` -- and the
host's schema declares which dials exist, their defaults, and *where* a
definition may read each one. The name, never the value, is compiled into the
plan: one plan is shared by every tenant, and naming the dial is what makes
the dependency derivable, so turning it recomputes exactly what read it.

The schema splits the dials into **four lists, by what turning the dial
costs** (see [Concepts](concepts.md) for the host's side of this):

| List | Named by | Turning it costs |
|---|---|---|
| bucket settings | an index's `by day in ...` and `older/younger than ...` | re-bucketing a tenant's whole history |
| figure settings | a figure's `calculate` and `band:` | recomputing one value per subject |
| reading settings | a reading's `band ... against ...` | nothing -- a reading stores nothing |
| projection settings | a projection's or summary's values and flag conditions | nothing |

The lists exist so a definition cannot write a dial into a position the
engine cannot afford to honour -- and so the expensive lists stay short and
deliberate. The checker refuses a dial named from the wrong position, with
the list of what is allowed.

Two shapes of dial: a scalar, and a **band** -- `{good, poor}`, one dial with
two edges, read whole by `band ... against ...`. One path is reserved rather
than declared: `tenant.hoursPerDay`, which the renderer divides by to print
an effort as days; a host that renders efforts carries it in its defaults.

A dial a definition names that resolves to nothing **raises** rather than
falling back to something plausible -- the definition said which dial it
wanted, so there is nothing to guess, and a fallback would produce values,
numbers and evidence all about the wrong thing. A dial *set to nought* is a
value somebody chose, distinct from an unset one; the invalidation machinery
honours that distinction, so zeroing a threshold rebuilds what read it.

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
  index labels are all out: rewording an explanation never forks a version
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
| the full spec of every index a definition reads | changing what `code_change.open` *means* changes what the figure counts, though the figure's own text is untouched |
| the full spec of every measure it names | the same integer reading "5d" or "5" is a scale change |
| the calculation, ladders and all | it is the number |
| `across` | one value per subject becomes one per pair; the stored values mean something else |
| a figure's `band:` ladder and the dials it names | the band is one of the answers the figure gives -- and it costs nothing to move, since no value is stored under it |
| a rollup's **source version** | redefine the parts and the total must rebuild, or it reads a number derived from a definition that no longer exists |
| a reading's statistics and `requires` floor, written or defaulted | a floor applied at read time would let two engines render one version differently |
| a band's direction, dial, `on` (unless the default mean) and unit (unless the default days) | the same threshold in minutes is a band 1,440 times tighter |
| a projection's fields, joins, reads (including whether a read is `band of`), values, sort and limit | a join decides *which record* a path is read off; `band of X` and `X` are different columns off one figure |
| a projection's flag templates | a flag's sentence is the whole content of that row |
| a summary's counts, totals, values, flags and its **projection's version** | rename what a row value means and every count moves |

What deliberately is not: explanations, `display` templates, index labels,
`keyed as` (it decides what the checker permits, not what the arithmetic
produces), declaration positions, and the schema itself -- a version is the
hash of a definition's semantics, and the same definition under two hosts is
the same definition.

One asymmetry worth knowing: a **windowed reading hashes its source figure's
name, not its version**. A reading is not stored, so which figure version it
read is a fact about the evaluation and travels on the response -- a reading
citing a moved figure is visible rather than silently re-versioned. A *live*
reading is the exception: it has no source on the wire, so its own version is
the only provenance token there is, and it hashes the resolved index and
measure specs.

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
  namespace across all six kinds.
- *"there is no index called ... Declared: ..."* / *"...is not a set defined
  in depends. Defined: ..."* -- a typo, answered with what was actually
  bound.
- *"...buckets by ..., so it needs a bucket: write `X:{kind}`"* and *"...is a
  predicate index, so it has a single bucket and cannot be addressed per
  subject"* -- the two ways to mis-address an index, each of which would
  otherwise read a bucket keyed by the empty string and answer zero for
  everybody.
- *"set ... combines indexes over A and B. A set is a set of ids..."* -- two
  id spaces in one expression; the intersection is empty for ever. `keyed as`
  is the fix when the sharing is real.
- *"...applies M, which reads K, to a set holding K2 ids. Every lookup would
  miss..."* -- a measure over the wrong kind: the same silence one layer
  along.
- *"...names no index addressed by `{scope}`, so it has no subjects"* /
  *"...is fanned out by more than one index"* -- exactly one index fans a
  figure out.
- *"...has both a depends and a combine block"* -- two populations, one
  calculation, no rule for how they relate.
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
  one; an index or a measure may go unexplained, because neither is served to
  a reader on its own.
- *"there is no figure called ... A figure may only read one declared before
  it..."* -- ordering, and the cycle guard.
- *"...reads X as a single value, but X is split across ..."* / *"...adds up
  X over Y, but X is not split across anything"* -- `over` must match the
  source's `across`, in both directions.
- *"reading ... summarises stored values, so it must declare (range)"* /
  *"reading ... measures records as they stand, so it takes no arguments"* --
  the windowed/live distinction, encoded twice on purpose.
- *"...is not time-keyed -- its scope index must end in a `by day`, `by
  minute` or `by 15 minutes` part..."* -- only a time-keyed figure has a
  range to read over.
- *"the figure under ... stores a count, so mean(...) is a mean per day
  wearing a label that says per record"* -- only `sum` over counts.
- *"...calculates both a sum and a distribution"* -- two numbers a reader can
  divide produce a third no definition claims.
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
- *"...is not a setting an index may name. Those are: ..."* (and the
  calculation, band and projection variants) -- the four settings lists, each
  enforced at the position that pays its cost.

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
| a filter in `depends` | it would let a declaration lie about what it reads |
| `work_hours` as a threshold unit | as shipped it was a synonym for `hours`; doing it honestly is a working calendar |
| negative literals | a negative threshold is a dial; a negative value is a subtraction |
