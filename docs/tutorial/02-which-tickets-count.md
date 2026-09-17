# 2. Which tickets count

> **The question.** *Two screens disagree about how many tickets are open.
> Both say "open". Where does that word actually get decided?*

In most products it gets decided in several places at once, slightly
differently each time, and the disagreement is discovered by a customer. Here
it gets decided in exactly one, and that place has a name you can cite.

## Two verbs, two questions

Reaching a population of records is the whole of the foundation, and there are
exactly two ways to do it. They answer different questions, so they get
different keywords.

**A `group` fans out.** It files every record of a kind into a bucket, one
bucket per value of a field. "One per customer", "one per team", "one per
agent".

```fig
# Every ticket, filed under the customer who raised it.
group desk_ticket.raised_by from customer_id label "raised by this customer"
```

**A `filter` narrows.** It is a single bucket holding whatever passes a test.

```fig
# Still in hand: nobody has recorded a resolution.
filter desk_ticket.open where resolved_at is not set label "still open"
```

Both are named `<kind>.<name>`, and the prefix is the kind whose records they
file. Both take an optional `label`, which is prose: how to say the thing in a
sentence. The label never affects what is computed, and changing it never
recomputes anything.

Groups and filters are the *only* way a definition reaches records. There is
no third route, no ad-hoc query, no "just this once".

## `desk_ticket.open` is a definition, not a convention

Look at what got decided in that one line:

- **Open means "nobody wrote a resolution time".** Not "status is not
  Closed", not "resolved_at is in the future". If somebody later wants open to
  mean something else, they edit this line, the whole team reviews the edit,
  and every number built on it moves together. There is no second screen
  quietly using the old rule, because there is no second place the rule could
  live.

Its two buckets, over our thirty-six tickets:

| filter | holds |
|---|---|
| `desk_ticket.open` | 11 tickets: T-122, T-124, T-128, T-129, T-130, T-131, T-132, T-133, T-134, T-135, T-136 |
| `desk_ticket.resolved` | the other 25 |

Count them in [the dataset](../tutorial.md#tickets). Eleven rows say *open* in
the **resolved** column.

## The `where` forms you need today

```fig
# Finished: somebody recorded a resolution.
filter desk_ticket.resolved where resolved_at is set label "resolved"

# The tickets the desk treats as urgent.
filter desk_ticket.urgent where priority == "urgent" label "urgent"

# The tickets somebody thought were worth calling out to Escalations.
filter desk_ticket.escalations where escalated == true label "escalated"

# Anything filed under the billing tag.
filter desk_ticket.billing where tags.label == "billing" label "about billing"
```

Note the quoting, because it is carrying a claim:

- `== "urgent"` -- a quoted value is **a word a text field holds**.
- `== true` -- bare, so it is **a flag's value**. `"true"` in quotes would be
  asking whether a piece of text spells out the word.
- Numbers are written bare too.

`desk_ticket.billing` crosses a `many` (a ticket has several tags), and that
means *any element*: a ticket is in the bucket if any of its tags is
`billing`. Eight tickets are: T-101, T-106, T-109, T-113, T-118, T-121, T-126,
T-129.

## The first trap: absent satisfies `!=`

Here is a question a product owner asks every week: *how many customers are
not on enterprise?*

The obvious definition:

```
filter desk_customer.not_enterprise where plan != "enterprise"
```

It compiles. It runs. It answers **5**: Brightpath, Harrow, Kestrel, Oakline
and Veld.

Now look at those five in [the customers table](../tutorial.md#customers).
Three of them are genuinely on a non-enterprise plan. **Oakline has no `plan`
field at all**, and **Veld's plan is an empty string** -- somebody filled it in
and then cleared it. Neither is a customer anybody has said is not on
enterprise. They are customers nobody has said anything about.

This is not a bug; it is the language being consistent, and the rule is worth
memorising: **an absent value satisfies `!=`.** A record with no `plan` is not
on the `"enterprise"` plan, which is true, and is very often not what the
person writing the filter meant.

The general shape of the mistake: `!= <something>` is a *good* way to exclude
a known value and a *bad* way to ask whether a field has been filled in at
all. That second question has its own form, and it is the subject of
[chapter 6](06-absence-and-age.md), where the same trap costs the desk a real
number.

For now, the working habit: **when you write `!=`, ask yourself what the
records with nothing in that field should do.** If the answer is "not be in
this bucket", `!=` is the wrong tool.

## See it

Open a filter's own page in the UI and it shows you exactly which records it
holds for the chosen tenant, so "which tickets count as open" is a list you
can read rather than a rule you have to trust. A group's page shows one chosen
bucket's members first and its other buckets below.

---

Next: [3. The identity hop](03-the-identity-hop.md)
