# 1. What the desk knows

> **The question.** *Before anybody can ask "how many tickets are open?", the
> engine has to know what a ticket is. What does it know, and who told it?*

## Records arrive; nothing is inferred

uratori holds **facts**. A fact is a plain record with two pieces of identity
-- a **kind** (`desk_ticket`) and a **key** (`T-101`) -- and a body of fields
whoever pushed it chose:

```
desk_ticket "T-101" {
  "id": "T-101",
  "subject": "Invoice PDF will not open",
  "queue_team_id": "tm-front",
  "assigned_login": "mira",
  "customer_id": "cu-northwind",
  "opened_at": "2026-03-03T09:15:00Z",
  "first_reply_at": "2026-03-03T10:05:00Z",
  "resolved_at": "2026-03-04T11:20:00Z",
  "reopens": 0
}
```

That is the whole of what arrives. The engine does not fetch anything, does
not guess what a field means, and never writes a record of its own. Your
ticketing tool, your CRM, your spreadsheet export: something on your side
pushes records in, on whatever schedule suits you.

Notice what is *already true* in that record and *not yet said*:
`customer_id` holds `"cu-northwind"`, which is the key of a customer record.
The correlation is in the data. Nothing has declared that it means anything.
Declaring is what the rest of this tutorial does.

## Declaring the world

A record can land, but until something declares the shape of its kind, nobody
can check anything about it. That declaration is a `fact`:

```fig
# A company whose people raise tickets. `plan` is the tier they bought;
# `grace_days` is how long a ticket of theirs may sit before the desk calls
# it overdue.
fact desk_customer:
    name name
    id as text
    name as text
    plan as text
    grace_days as number
```

Read it downwards:

- `fact desk_customer:` names the kind. This is the only declaration in the
  language named bare -- everything else is named `<kind>.<something>`, so a
  kind can never collide with a definition.
- `name name` says **which field carries the human-facing name**. The first
  word is the directive; the second is the field. When the engine stores a
  computed number about Northwind Traders, it freezes that rendered name
  beside the value, so renaming the customer next week does not rewrite the
  history of what they raised.
- The remaining lines are the fields. There is no `field` keyword, because a
  fact's body *is* its fields and the word would be noise on every line. A
  line whose second word is `as` is a field.

The comment lines above the declaration are not a comment in the usual sense.
They are the declaration's **explanation** -- the sentence a reader is shown
wherever this thing is cited. The compiler attaches them, and it refuses a
figure, a reading, a projection, a summary or a fact that arrives without one.
This is the single most product-facing thing in the language, and it is
enforced by the build.

## Four types, and no fifth

```
text      a string
number    a number
flag      true or false
moment    an instant in time
```

That is the entire vocabulary, and the shortness is the point. A `fact`
describes **the record as the provider showed it** -- its shape, nothing more.

It is tempting to want more. You might want to write, on `customer_id`,
"this names a customer". You might want to write, on `handling_seconds`, "this
is working time, in seconds". Both are refused, and both have a home
elsewhere:

- *"`customer_id` names a customer"* is a **correlation**. It is what a group's
  `through` clause declares (chapter 3). Writing it here would declare it
  twice, in two places that can disagree.
- *"`handling_seconds` is working time"* is what a number **means**. It is
  what a measure's `in` clause declares (chapter 5). Nothing in the record
  marks the field as special; a bare integer is a bare integer.

`moment` survives the cut because "this string is an instant" is a claim about
the value's *form*, not its meaning -- and it is what lets the engine check
that `resolved_at - opened_at` is a sensible thing to write at all.

## Here is the ticket, declared

```fig
# One request for help, as the ticketing tool last showed it. Every timestamp
# is absent until it happens: a ticket nobody has answered has no
# `first_reply_at`, and one nobody has finished has no `resolved_at`.
fact desk_ticket:
    name subject
    url url
    id as text
    subject as text
    url as text
    queue_team_id as text
    # The handle of whoever is working it -- absent while unassigned.
    assigned_login as text
    customer_id as text
    channel as text
    priority as text
    opened_at as moment
    first_reply_at as moment
    resolved_at as moment
    reopens as number
    handling_seconds as number
    refund_amount as number
    escalated as flag
    one reporter:
        name as text
        email as text
    many tags:
        label as text
```

Two new things.

**`url url`** is the second directive, the mate of `name`. It says which field
holds the address of this record in the system it came from, so that when the
engine hands you the evidence behind a number, each cited record carries a
link you can follow. Both directives are optional. A kind with no `name`
renders raw ids -- honest, and ugly enough to be a prompt.

**`one` and `many` are cardinality, not payload shape.** `one reporter:` opens
a block whose fields resolve to exactly one value per ticket.
`many tags:` opens a block whose fields resolve to a *list* per ticket. That
single distinction is what everything downstream branches on:

| | crossing a `one` | crossing a `many` |
|---|---|---|
| grouping by it | fine | fine, and deliberately flattening: *any* tag of *any* element |
| measuring it | fine | refused -- a measurement reads one value, and over a list it would silently pick one |
| a column in a row | fine | refused -- a row's cell holds one value |
| testing it | fine | fine, and it means *any element*: `== "billing"` asks whether any tag is billing, so `!= "billing"` means *no* tag is |

One thing you cannot declare is a bare list of words -- `many tags:` with no
field inside it. Nothing in the language could read such a thing (a test
compares one field against one value; there is no "is it in the list"), and a
field nothing can read is a field nobody has checked.

## An absence is never an error

Look at `T-135` in the dataset. It has no `assigned_login`, no
`first_reply_at`, no `resolved_at`, no `reopens`, no `handling_seconds`. It
lands anyway, and nothing complains.

That is deliberate. A field declaration here is a claim about
**known/unknown**, never about **required/optional**. A record may leave any
field out, or send it as an explicit `null` -- `T-136` does exactly that with
`first_reply_at` -- and both mean the same thing: *nobody has said*.

What is refused is the other direction: a record carrying a field the
declaration does not know about, or carrying a word where the declaration says
a number. That batch is rejected whole, naming the kind, the key and the
field, because a record that does not match its declaration is a record no
definition can safely read.

The consequence runs through everything that follows. When you ask "what is
Mira's first-reply time on T-135?", the answer is not zero and not an error.
It is *nothing*, and the engine will say so in those words.

## What declaring facts buys you

Three things a bare list of kind names cannot:

1. **Every path a definition reads is checked at build time.** Group by a
   field that does not exist, subtract two pieces of text, put a list in a
   column: each of those used to be a silently empty screen and is now a build
   failure naming the field and listing what the record actually carries.
2. **Every record that arrives is verified.** A batch with an undeclared field
   is refused at the door, before anything is computed from it.
3. **A number can be traced back to the schema.** When you follow a figure
   down through its evidence to the records, the trail ends on a declaration
   with prose on it, not on anonymous JSON.

## See it

In the UI at <http://localhost:8080/ui/>, the **Facts** screen lists every kind
and how many records the engine holds of it -- and a kind somebody declared but
nobody has pushed shows at zero, because "nothing collected" is a finding
rather than a blank. Click into `desk_ticket`, then into `T-101`, and you get
that record's page: the stored document, and (once you have read a few more
chapters) every grouping it landed in and every number it counted toward.

---

Next: [2. Which tickets count](02-which-tickets-count.md)
