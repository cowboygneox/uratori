"""The shape a definition parses into.

Deliberately close to the source text: the parser's job is to say what was
written, and every decision about whether it *means* anything belongs in
`check.py`. Keeping them apart is what lets the checker produce errors that
name a rule ("no group or filter called code_change.opne") rather than a position.

Every node is a frozen dataclass and every union is closed. `check.py` and
`evaluate.py` match over them with `assert_never` in the default arm, so adding
a construct here and forgetting to handle it somewhere is a type error rather
than a figure that silently answers nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from ..windows import WindowSpec

# ----------------------------------------------------------------- fact --

FactFieldType: TypeAlias = Literal["text", "number", "flag", "moment"]
"""What a fact field structurally is -- the value's form, never its reading.

Four types and no more, because the fact layer describes the record as the
provider shows it and interpretation lives in the definitions that read it: a
correlation is a group's `through` claim, and a number's meaning (`effort`,
`count`) is the measure's. `moment` survives that test where `key of <kind>`
and `in <unit>` did not -- "this string is an instant" is a claim about the
value's form, and it is what makes `merged_at - created_at` checkable.
"""


@dataclass(frozen=True)
class FactField:
    """One field of a fact -- `placed_at as moment`, or a nested block.

    There is no `field` keyword: a fact's body *is* its fields, so the word
    would be noise on every line. A line whose second token is `as` is a
    field; `name <field>` and `url <field>` are the two directives; `one x:`
    and `many x:` open nested blocks. A field literally called `name` is
    still writable (`name as text`), so nothing here is a reserved word.
    """

    name: str
    type: FactFieldType | None = None
    """None exactly when this is a nested block (`one` / `many`)."""

    many: bool = False
    """`many events:` -- a list of objects. `one dropoff:` when false with
    children. Declared rather than inferred from data, because cardinality is
    semantics: a path crossing a `many` yields every element, which is right
    for bucketing and a fabrication for a measure."""

    children: tuple[FactField, ...] = ()
    doc: str = ""
    """The `#` run above the field -- the customer-facing description of what
    the provider writes here. Never hashed."""

    line: int = 0


@dataclass(frozen=True)
class FactDecl:
    """`fact shop_order:` -- what a record of this kind is.

    Named bare, no dot: every other declaration is `<fact kind>.<name>`, so a
    kind name cannot collide with a definition name and the one-namespace rule
    still holds. Structural only -- see `FactFieldType` for what was refused.
    """

    name: str
    doc: str
    fields: tuple[FactField, ...]
    name_field: str | None = None
    """`name ref` -- which field carries a record's human-facing name. A
    directive pointing at a field rather than a marker on the field line,
    because it is rendering, not data: fields are hashed, this is not, and
    folding it onto the field would make a rendering choice look like part of
    the field's semantics."""

    url_field: str | None = None
    line: int = 0


# ---------------------------------------------------------------- index --

Truncation: TypeAlias = Literal[
    "minute", "15 minutes", "hour", "day", "week", "month", "quarter"
]
"""`mergedAt by day` -- how a raw value is reduced before it becomes a key.

The one place calendar vocabulary is welcome, because it is a declaration:
a truncation decides how many values a figure has and which one an event
lands in -- it changes what a number *means* -- and the language's law is
that only a declaration, hashed into a version, may do that. An argument
(`over 1-6`) is positions in the sequence this grain declares, and nothing
else.

Seven grains, sub-day through quarter. A coarser figure beside a finer one
-- monthly deliveries beside daily deliveries -- is **two declarations**:
two names, two explanations, two hashes, each computed directly from the
records (a month bucket holds every record of the month, not a rollup of
day buckets), so each is citable on its own and neither is a hidden
re-slicing of the other. An earlier edition refused `week` and `month`
here on the ground that a range over days produces either; that argument
was aimed at *unnamed* read-time truncation -- one name quietly serving
two grains -- and declaring the coarser grain under its own name is the
opposite of that. Other minute counts wait for a definition to ask,
exactly as `percentile` does.

A sub-day label is local time truncated to the grain -- `2026-08-25T14:30`
-- in the calendar the definition names, exactly as a day key is the local
date, a month key the local month (`2026-08`), a quarter `2026-Q3` and a
week the ISO week (`2026-W35`). When the clocks go back the repeated
hour's two passes share their labels and their records share a bucket,
which is the honest answer about a quarter-hour that occurred twice;
keying by UTC instead would put every local midnight mid-bucket, a
constant error to avoid a twice-a-year merge.
"""


@dataclass(frozen=True)
class Through:
    """`through team_person.accounts.accountId` -- bucket by the id of the
    record that owns this value rather than by the value itself.

    A merge request carries a provider account id, and a person is one or more
    accounts. Without the hop, a figure scoped to a person would bucket by
    account and split anybody with a Jira login and a GitLab login into two
    half-people -- the exact failure the identity join exists to prevent,
    reintroduced one layer up.

    The path is walked with lists flattened, so `accounts.accountId` means "any
    accountId of any account".
    """

    kind: str
    """A fact kind. Not an alias for one: aliases hid the only check that
    mattered, because they validated the alias and never the kind it resolved
    to."""

    path: str


@dataclass(frozen=True)
class Zone:
    """Which record carries the calendar, and which of its fields.

    `kind` is a fact kind, and the record is the one keyed by the bucket's
    **subject** -- part one of a composite, resolved through its hop where it
    has one. For a group with no subject part (a bare `from set_at by day`)
    it is the record being bucketed, and the kind must be that record's own.

    Written as `<kind>.<field>` so a reader can see which record without
    tracing the group's first part, and so the checker can refuse the
    mismatch: naming a kind the subject part does not resolve to would look
    up every key in the wrong table and answer nothing, which reads as a
    board with no buckets rather than as a wrong declaration.

    A record with no value there is in **no bucket**. Never UTC as a
    fallback: a subject nobody has recorded a calendar for has no calendar,
    and defaulting one files their history under days they never worked.
    """

    kind: str
    field: str


@dataclass(frozen=True)
class IndexField:
    """One component of an index key.

    Split out so a composite can reuse it verbatim. A one-part composite and a
    field index are the same thing; both spellings exist because
    `group code_change.by_author from authorAccountId` should not have to be
    written as a tuple.
    """

    field: str
    through: Through | None = None
    truncate: Truncation | None = None
    select: str | None = None
    """`by first monday of month` -- a *selective* calendar rule, canonical
    text. Where a truncation is total (every instant has a day and a month),
    an ordinal weekday-of-month rule is deliberately partial: an instant on
    the first Monday of its month buckets under that day's date, and every
    other instant lands in **no bucket** -- the filter falls out of the
    function being undefined there, the same doctrine as `is set`. The
    stored buckets are sparse day labels, one per month at most (a fifth
    Monday exists in some months only), and a reading's window walks them
    as the sequence they are: `over 1-6` is the last six first-Mondays.

    Mutually exclusive with `truncate`; hashed as its own key, so every
    spec written before the family existed keeps its version."""

    zone: Zone | None = None
    """`by day in team_person.timezone` -- whose calendar decides which day.

    A **field on the subject's record**, not a tenant dial. It used to be a
    dial, and a dial made one calendar for a whole board: a courier in Tokyo
    and one in London had their days cut on somebody else's midnight, and the
    number under "yesterday" was about a period neither of them worked.

    Subject-scoped rather than record-scoped, and that is the load-bearing
    choice. Read off the record being bucketed, a subject's sequence could
    mix calendars -- some of a courier's days cut in Tokyo, some in Berlin,
    depending on where each order came from -- and a reading walks that
    sequence counting back positions, so its window would be a span of no
    particular calendar. Read off the subject, every bucket in a subject's
    sequence is cut the same way. The price is that one record shared by two
    subjects lands on two different dates, which is the honest answer to
    "which day was this, for them": an 08:00 UTC merge is the 25th in Tokyo
    and the 24th in London, and it is not the engine's place to pick.

    Absent means UTC, and that is a choice a definition makes rather than a
    default it falls into.
    """


@dataclass(frozen=True)
class ByField:
    """`from authorAccountId` -- one bucket per distinct value. Fans a figure
    out per subject."""

    part: IndexField


@dataclass(frozen=True)
class ByPredicate:
    """`where state == "open"` -- a single bucket holding whatever matches.
    Narrows rather than fans out."""

    field: str
    op: Literal["==", "!="]
    value: str
    quoted: bool = False
    """Whether the value was written in quotes. Evaluation cannot tell --
    bucket keys are strings either way -- but the checker must: against a
    declared world, a bare `true` names a flag's value and a quoted `"true"`
    names a word a text field holds, and a rule that could not tell them
    apart refused the quoted spelling while advising the author to quote it.
    Deliberately not hashed: it never changes what the arithmetic produces."""


@dataclass(frozen=True)
class ByPresence:
    """`where estimateSeconds is set` -- membership decided by whether the
    record carries the field at all.

    Its own arm rather than a third operator on `ByPredicate`, because it is the
    one question that arm structurally cannot ask: an absent value satisfies
    `!=` by design -- a record with no `state` is not in state "merged" -- so
    `where estimateSeconds != "0"` matches every *unsized* story rather than
    none of them. That is an over-count, silently, in the one number whose whole
    job is to say how much of an epic is guesswork.

    **Nought counts as absent**, and that is a decision this arm makes rather
    than inherits. Jira writes `0` and `null` into the same field for the same
    state -- nobody sized this -- so a presence test that honoured the
    difference would move when an operator cleared a box rather than when
    anybody estimated anything. Hence `is set` rather than `exists`: the
    question is whether somebody has *said* something.
    """

    field: str
    negated: bool = False


@dataclass(frozen=True)
class ByAge:
    """`where updated_at older than 3 days`, or `older than stale_days from
    repo_id through code_repo.id` -- membership decided by how long ago a
    moment was, in whole days.

    **The one place a stored figure may read a clock**, and it is fenced
    differently from a clock measure rather than by the same rule. A clock
    measure is refused to a figure because the number would be stale the instant
    it was written and nothing would ever recompute it: a wait grows every
    second, so every value would be real exactly once. Membership does not do
    that. A record crosses this line once, on a knowable day, and until it does
    the answer is unchanged.

    So the question is not "is this value decaying" but "how long may the
    crossing go unnoticed", and the answer is **until the next full reconcile**.
    That holds only because the threshold is in days, which is why the unit is
    fixed here rather than written: making it configurable would make the
    unsafe version writable.

    The threshold used to be a dial, and this was the hardest position to take
    one out of: a filter narrows records *before* anything buckets them by
    subject, so there is no subject whose goal figure could be looked up. What
    there is instead is the record's own owner. `stale_days from repo_id
    through code_repo.id` reads the number off the joined record, so each
    repository declares its own staleness -- the same join a projection field
    writes, one construct along.

    **A join that matches no record is not in the filter.** Never a default
    and never the whole population: an owner nobody has collected yet is a
    threshold nobody has stated, and guessing one would file records under a
    staleness rule nobody wrote.

    `days` is the other arm: a number in the definition, for a threshold that
    does not vary. It is not the dial by another name -- a reader can see it,
    and moving it forks the version.
    """

    field: str
    direction: Literal["older", "younger"]
    days: float | None = None
    """A literal threshold, in days."""

    read: str | None = None
    """The field to read the threshold off, on the joined record."""

    local: str | None = None
    """The field on *this* record naming its owner -- `repo_id`."""

    through: Through | None = None
    """Which record to read it off: the owner's kind, and the path its key is
    matched on. Exactly one of `days` and (`read`, `local`, `through`) is
    set."""


@dataclass(frozen=True)
class ByComposite:
    """`from (authorAccountId through team_person.accounts.accountId, mergedAt by day)`
    -- one bucket per combination, keyed `<subject>@<rest>`.

    This is what lets a figure be per-person *and* per-day without the engine
    learning about a second scope dimension. A record is in the cross product of
    its parts' values, the "exactly one scope index" rule still holds, and
    invalidation stays the single lookup it already was.
    """

    parts: tuple[IndexField, ...]


IndexBy: TypeAlias = ByField | ByPredicate | ByPresence | ByAge | ByComposite


@dataclass(frozen=True)
class IndexDecl:
    name: str
    kind: str
    """The fact kind part of the name: `code_change` in `code_change.open`."""

    spec: IndexBy
    keyed_as: str | None = None
    """`keyed as code_change` -- these records use another kind's ids.

    A merge request is two facts: the change itself and the review timeline
    rebuilt from its event stream. They are separate because the timeline is
    expensive to collect and immutable once closed, but both are keyed by the
    same id -- so the two id spaces can be intersected and the answer means
    something.

    Declared rather than inferred, because the failure it guards is silent:
    intersecting ids that mean different things yields the empty set, and an
    empty set is a figure reading zero rather than an error anybody sees.

    Deliberately **not** in the version hash. It decides what the checker
    permits, not what the arithmetic produces.
    """

    label: str | None = None
    """How to say this index in a sentence: "authored by", "still open".

    Optional, and the fallback is mechanical -- "whose author_account_id is
    ...". The fallback is correct and unreadable, which is the right default: a
    missing label degrades to something true rather than to nothing, and the
    ugliness is the prompt to write one.
    """

    line: int = 0


# ------------------------------------------------------------------ set --


@dataclass(frozen=True)
class BucketScope:
    """`code_change.authored_in:{team_person}` -- one bucket per subject."""

    variable: str


@dataclass(frozen=True)
class BucketAll:
    """`code_change.open` -- the single bucket holding everything that matches."""


Bucket: TypeAlias = BucketScope | BucketAll


@dataclass(frozen=True)
class SetIndex:
    index: str
    bucket: Bucket
    line: int = 0


@dataclass(frozen=True)
class SetRef:
    name: str
    line: int = 0


@dataclass(frozen=True)
class SetOp:
    op: Literal["intersect", "union", "difference"]
    left: SetExpr
    right: SetExpr
    line: int = 0


SetExpr: TypeAlias = SetIndex | SetRef | SetOp
"""A set of record ids.

There is no filter here on purpose: `depends` may narrow only by set
membership, because a predicate over record *contents* cannot narrow what the
figure subscribes to and would therefore be a declaration that lies.
"""


# -------------------------------------------------------------- measure --

MeasureUnit: TypeAlias = Literal["effort", "count"]
"""What a *field* measure's number is, because nothing else can tell.

A duration measure needs no unit: it is the seconds between two moments by
construction. A field measure reads whatever the record carries, and the same
integer means different things -- `estimateSeconds` is an amount of work,
`reopens` is a tally.

`effort` is not a synonym for `duration`. A duration is wall-clock; effort is
*working* time. Both render in hours, so they agree at eight and part company
above a day: 144,000 seconds is "1.7d" as a duration and "40.0h" as an effort,
because a working week is forty hours and nobody means "one and two-thirds
days" by it. One number cannot be both, and filing effort under duration puts
the two readings on two screens -- the disagreement this engine exists to end.
"""


@dataclass(frozen=True)
class DurationMeasure:
    """`measure code_change.open_seconds = mergedAt - createdAt` -- seconds
    between two moments."""

    name: str
    kind: str
    later: str
    """The later moment, or the literal `now` when `clock` is set."""

    earlier: str
    clock: bool = False
    """`now - requestedAt`. The one place the engine may read a clock, fenced by
    where the result may go: a figure may not name one, because a stored value
    computed against `now` is stale the instant it is written and nothing would
    ever move it. Only a live reading may, and a live reading stores nothing.

    `now` is therefore reserved in the later position: a fact kind carrying a
    field genuinely called `now` cannot be subtracted from. Nothing has one, and
    a sigil would be a spelling for a case that does not exist.
    """

    line: int = 0


@dataclass(frozen=True)
class FieldMeasure:
    """`measure work_issue.estimate = estimateSeconds in effort` -- a number the
    record already carries.

    Still not arithmetic: one field, no expression. The unit is **required**
    rather than defaulted, and that is why this is a separate variant rather
    than an optional attribute. A default of `count` prints an estimate as
    `144000`; a default of `effort` prints a tally of five reopens as `0.0h`.
    Neither throws, both render, and the wrong one is a plausible number in
    the wrong scale.
    """

    name: str
    kind: str
    field: str
    unit: MeasureUnit
    line: int = 0


@dataclass(frozen=True)
class MomentMeasure:
    """`measure work_issue.moved = moment updatedAt` -- a single instant.

    The third measure kind, and the one producing something a figure could not
    previously hold. It exists for `latest` and `earliest` and the checker
    refuses it everywhere else, which is what keeps an instant out of the
    arithmetic: subtracting two of them is milliseconds and totalling a column
    of epochs is a number with no meaning, and both compile.

    Not a `FieldMeasure` with a third unit. A field measure reads a *number*,
    and every provider writes a timestamp as an ISO string -- so the unit would
    have had to change what the read does as well as what the number means.

    **No `now`.** `moment now` is a stored value that would be wrong one
    millisecond after it was written, with nothing to move it. The parser does
    not accept the word.
    """

    name: str
    kind: str
    moment: str
    line: int = 0


MeasureDecl: TypeAlias = DurationMeasure | FieldMeasure | MomentMeasure


# ----------------------------------------------------------- calculate --

Comparison: TypeAlias = Literal[">=", ">", "<=", "<", "==", "!="]
"""How a `when` clause tests one value against another.

Six operators and no boolean combinators -- no `and`, no `or`, no `not`. A
ladder of single comparisons explains as a list of clauses, which a reader can
check; a boolean expression does not, and the moment one is allowed the
generated sentence stops being a description and becomes a transliteration. Two
conditions that must both hold are two rungs.
"""

AbsenceTest: TypeAlias = Literal["nothing", "something"]
"""`when planned_days is nothing then "unscheduled"`.

The two tests that ask about the *absence* of a value rather than its size. A
ladder stops on an unknown rather than falling through to `otherwise`, which is
correct -- banding somebody the engine has never computed as comfortable is the
confident wrong answer this product is arranged around avoiding -- and it left a
definition with no way to say anything *about* the unknown.

Words rather than an operator: `x == null` would put a value into the language
that is not a value, and every comparison against it would then need a rule.
"""

ArithOperator: TypeAlias = Literal["+", "-", "*", "/"]

DeclaredUnit: TypeAlias = Literal["share", "days", "effort", "count", "duration"]
"""What an arithmetic value *is*, because nothing else can tell.

`delivered / committed` is mute. It could be a share, and `breakdown -
delivered` over the same two operands is seconds of effort. Deriving it would
mean dimensional analysis over a language whose leaves include another figure
of unknown unit; guessing puts "144000" or "0.6d" on a screen.

So arithmetic **must** declare one and every other calculation must **not** --
for those it stays derivable, and a second place to write it is a first place
for the two to disagree.
"""

FigureUnit: TypeAlias = DeclaredUnit | Literal["level", "moment"]
"""Every unit a stored value can be in.

The two nothing can declare come from exactly one construct each and are always
derived: `level` is the word a ladder returns, `moment` is the instant an
extreme picks out. Neither is writable as `in <unit>` -- the parser refuses both
by name rather than by omission, because "unknown unit" is a worse error than
"that one is worked out for you".

A moment is a number on disk and is not a quantity, which is why it needs a unit
of its own rather than passing as a count: 1.77e12 rendered as a count is
unreadable, and subtracted from another moment it is milliseconds.
"""


@dataclass(frozen=True)
class Count:
    set: str
    line: int = 0


@dataclass(frozen=True)
class ListOf:
    """`list(<measure> over <set>)` -- the measure's value for every member, in
    the order of the stored evidence.

    No aggregation, because averaging at the figure's own grain throws away the
    only thing a span needs. Which statistic a reader wants is a question for
    the read, and answering it here would freeze one choice into a version hash.
    """

    measure: str
    set: str
    line: int = 0


@dataclass(frozen=True)
class Sum:
    """`sum(<parts>)` adds a subject's cells up; `sum(<measure> over <set>)`
    adds a field up across records.

    One word for two bindings, distinguished by `over`. The operation genuinely
    is addition in both cases, and a reader who has to remember which noun takes
    which verb is being taxed for the compiler's convenience. What that borrows
    is an obligation: neither mistake fails loudly, so the checker refuses each
    by name. Left to fall through, one would total nothing and the other would
    total a set's *ids*.
    """

    set: str
    measure: str | None = None
    line: int = 0


@dataclass(frozen=True)
class Part:
    """`total` -- a figure bound in `combine` with no `over`, read as one value.

    The scalar counterpart of `sum`. Where that adds a subject's parts up, this
    reads the one value the source holds for this subject, so a figure can be
    built on a number another definition already computed rather than on a
    second count of the same records.
    """

    name: str
    line: int = 0


@dataclass(frozen=True)
class Number:
    value: float
    line: int = 0


@dataclass(frozen=True)
class Text:
    """`"over"` -- an enumeration member, and the reason a value may be a word.

    Deliberately not a general string type. The only place text can be written
    is the result of a ladder rung, so the set of words a figure can produce is
    finite, listed in its own definition and printable in the generated
    sentence. A figure that could return arbitrary text would be a template
    engine with a version hash.
    """

    value: str
    line: int = 0


@dataclass(frozen=True)
class Setting:
    """`thresholds.openChanges.warn` -- a dial from the tenant's settings.

    The name, never the value, for the reason an index's zone is. What makes it
    safe is that the dependency is *derivable*: the checker walks the
    calculation, so moving the dial makes this figure pending and rebuilds it,
    and a definition that starts reading a new one cascades the day it is
    written.
    """

    path: str
    line: int = 0


@dataclass(frozen=True)
class FigureRef:
    """Another figure, named outright in a band rung -- the threshold as a fact.

    A band's threshold used to be a **dial**: a number the host set per tenant,
    outside the fact stream, cited by nothing. So the one part of a card that
    decides whether a reader should worry was the one part no evidence could
    explain, and "why is this red" ended at a settings page rather than at a
    record. Naming a figure instead makes the threshold an answer like every
    other answer -- computed from records, versioned, and citable.

    Produced by the checker rather than the parser: a dotted name parses as a
    `Setting` and the checker rewrites it once it knows the name resolves to a
    figure. That keeps the grammar free of a lookahead it cannot do and puts
    the resolution in the one place that holds the declarations.

    The bare spelling reads one value per subject. A *sequenced* source is read
    at a coordinate instead -- see `Coord` -- because bare over a sequence
    would have to pick a bucket, and whichever it picked would be a
    fabrication.
    """

    name: str
    line: int = 0


@dataclass(frozen=True)
class SubjectField:
    """`shop_courier.max_orders` -- a number read off the record the value is
    *about*.

    The shortest thing in the language, and it exists because its absence was
    the most expensive. A threshold that varies per subject is usually a
    number somebody typed onto that subject's record: a courier's allowance, a
    site's target, a repository's staleness window. Reaching it used to mean a
    group pairing a record with itself, a measure renaming one field, and a
    figure summing a set of one -- three declarations of ceremony before
    anything could name the number. A group had been reading the same record
    with none of that (`by day in shop_courier.timezone`); nothing else could.

    The record is the subject's, always, and the kind is written out so a
    reader sees which record without tracing the figure's scope. Another
    kind's field is refused: there is no key to pick a record of it by, and
    picking one would be a fabrication.

    **A figure is still the other route**, and it is the right one whenever
    the threshold is computed from something, or has history worth keeping --
    a target set in June that June's rows should still be judged against.
    This is for the case where the number is simply written down.

    No unit travels with it. A field is structural (`max_orders as number`)
    and the fact layer claims a shape, never a meaning -- so the comparison
    is what fixes the quantity, exactly as it does for a literal. That is the
    one thing this shares with the dial it replaces, and the reason it is
    tolerable here is the reason a literal is tolerable: the number is
    visible to the reader, on a record they can open.
    """

    kind: str
    field: str
    line: int = 0


@dataclass(frozen=True)
class Rung:
    left: CalcExpr
    op: Comparison | AbsenceTest
    then: CalcExpr
    right: CalcExpr | None = None
    """Absent for `is nothing` / `is something`, which are about the left side
    alone. Optional rather than a placeholder node, so nothing can mistake a
    dummy for a comparison somebody wrote."""

    line: int = 0


@dataclass(frozen=True)
class Ladder:
    """Tested in written order, first match wins, and it must end in
    `otherwise`.

    That guarantees something narrower than "the calculation is total". It can
    still answer nothing, and deliberately does when a value it tests is
    missing. What the last rung removes is the *other* way to get nothing:
    falling off the end because a case was never written. Both render as a dash
    and only one of them is a claim the definition makes.
    """

    rungs: tuple[Rung, ...]
    otherwise: CalcExpr
    line: int = 0


@dataclass(frozen=True)
class Arith:
    """`done / measured`, `progress - expected`.

    Every operand is still something a sentence can name: a set's size, a stored
    figure, a literal, a dial, or another arithmetic node over those. There is
    no escape hatch that reads a record field, so `depends` remains the only
    thing that decides membership.

    **Division by nought answers nothing, never infinity and never nought.** An
    epic with no sized work has no progress that can be reported. Infinity
    renders as a number nobody can act on; nought is a confident 0% over an epic
    with six of seven stories closed. Any absent operand makes the result
    absent, for the same reason.
    """

    op: ArithOperator
    left: CalcExpr
    right: CalcExpr
    line: int = 0


@dataclass(frozen=True)
class Pick:
    """`max(breakdown, own)` -- the larger of two values, or the smaller.

    A call rather than an operator because there is no infix spelling anybody
    would guess, and it is here rather than in a general function namespace
    because there is no general function namespace: every addition is a
    construct somebody has to be able to read in a generated sentence, and "the
    larger of" is one.

    An absence propagates. The tempting alternative -- an absence does not
    compete, so `max(a, nothing)` is `a` -- is wrong in this engine, because a
    missing value means **not computed**, never "the subject has none of it".
    The backfill walks the roster, so a real nought is written for anybody who
    genuinely has none.
    """

    which: Literal["max", "min"]
    left: CalcExpr
    right: CalcExpr
    line: int = 0


@dataclass(frozen=True)
class DaysBetween:
    """`days from start to now` -- signed calendar days between two moments.

    Legal only in a projection. It is the clock, and the rule the clock has
    always had is that a *stored* value may not read one. A projection stores
    nothing, so the question does not arise.

    It lives on this union rather than in a grammar of its own because a
    projection's value block is otherwise exactly the figure expression
    language, down to the precedence and the ladder. Two parsers producing two
    nearly-identical trees is the drift this codebase keeps finding.

    Days rather than seconds: every calendar dial here is in days, so seconds
    would mean every definition dividing by 86,400, and the first one that
    forgot would compare a span of seconds against a dial of days and read as
    never crossing it. Signed, so "overdue by three" and "three days left" are
    one expression.
    """

    frm: str
    to: str
    line: int = 0


@dataclass(frozen=True)
class Extreme:
    """`latest(moved over children)` / `earliest(moved over children)` -- the
    most or least recent instant in a population.

    `count` says how many, `sum` says how much, `list` says which, and none of
    them says *when*.

    Deliberately narrow, and each narrowing is a refusal the checker makes by
    name: the measure must be a moment measure, so there is no
    `latest(estimate over children)` returning the largest number in a column;
    the result may not be summed, banded, compared or arithmetic'd; and the
    whole calculation is the extreme or none of it.

    **Why it is a stored figure and not a projection value.** It reads no clock:
    the latest of a set of recorded instants moves when a record moves and at no
    other time, which is exactly the event-shaped thing a stored value is for.
    The *span* from it to now is the clock-dependent half, and that lives in the
    projection.

    **The empty set answers nothing, and a record whose timestamp cannot be read
    is skipped rather than counted.** Both differ from `sum`, and both for the
    same reason: nought is a real instant. The latest of nothing would be 1
    January 1970 -- an epic created this morning reading as untouched for
    fifty-six years -- and so would one all of whose children carry an
    unreadable timestamp, which is the case that makes the skip load-bearing
    rather than tidy.

    v1 shipped `latest` alone and recorded the reason: the two directions
    disagree about what an unreadable timestamp means, so shipping both meant
    shipping one tested direction and one untested trap. Both are here because
    both now have a definition asking for them, and the disagreement is a test
    rather than a paragraph.
    """

    which: Literal["latest", "earliest"]
    measure: str
    set: str
    line: int = 0


@dataclass(frozen=True)
class FieldTotal:
    """`sum(shop_order.weight over mine)` -- a field, added up across a set.

    The same argument `FieldPick` makes, at the other statistic. A measure
    decorates a record with a quantity a definition *names* -- a duration
    between two moments, a field re-labelled with what its number means --
    and where the field already is the number, a measure is a second name
    for one thing.

    `latest` had always been allowed to read a field directly and `sum` had
    not, which was an asymmetry with no argument behind it: the rule that
    confines `latest` to a `bucketed` figure is about *ordering* -- which
    record came last -- and a total has no ordering to get wrong.

    A measure is still the right declaration for a quantity that is computed
    (`merged_at - created_at`), or one whose meaning several figures share:
    declared once, named three times, rather than a unit repeated at each
    reader. What it is no longer is the toll on adding up a column somebody
    typed.

    The unit is the figure's to declare, because nothing here can derive it:
    a fact field claims a shape (`weight as number`) and never a meaning.
    """

    kind: str
    field: str
    set: str
    line: int = 0


@dataclass(frozen=True)
class FieldPick:
    """`latest(setting_change.value over sets)` -- the value the most recent
    record in the set carries.

    The construct on-change data needs, and it is deliberately not a measure.
    A measure decorates a record with a *quantity a definition names*: a
    duration between two moments, or a field re-labelled with what its number
    means. Here there is nothing to name -- `value` is already the value, and
    a `measure setting_change.goal = value in count` would be a second name
    for one field, written only to satisfy the grammar. The language refuses
    a second place to write one thing everywhere else; this is that rule
    reaching the calculate block.

    **Not the same word twice.** `latest` over a *moment measure* answers
    *when* -- the most recent instant in a population. This answers *what*
    the value was at that instant. They are told apart by what the dotted
    name resolves to, which is decidable at compile time and is why the
    checker rewrites one into the other rather than leaving the evaluator to
    guess.

    `ordered_by` is not written in the definition. The group that fans the
    figure out already said `set_at by month`, and that field is when the
    change happened -- the only ordering in sight. Making the definition name
    it again would be a second place for the two to disagree, and the
    disagreement is silent: order by the wrong field and the bucket reports a
    superseded value with nothing thrown. Ties break on the record key, which
    is arbitrary but *stable*: two changes stamped at the same instant are a
    data problem, and answering them differently on each pass would be a
    figure that moves with nothing behind it.
    """

    which: Literal["latest", "earliest"]
    kind: str
    field: str
    set: str
    ordered_by: str = ""
    line: int = 0


@dataclass(frozen=True)
class BucketStat:
    """`median(job.length over done)` -- a distribution statistic over the
    records of one bucket.

    Legal **only in a `bucketed` figure**, and only over a set of records --
    never over another figure's stored values. Both halves are the same rule
    the readings enforce, arriving one stratum down.

    A mean of daily counts is a mean *per day* wearing a label that says per
    record: a plausible number of roughly the right magnitude, which is the
    worst kind of wrong. That refusal is about a statistic over
    **aggregates**. Here the population is the bucket's own records and the
    bucket boundary is declared in the group, so "the median job length in
    August" is a sentence with a checkable population behind it -- the
    boundary is what turns the statistic into a claim.

    Outside a bucketed figure the population is "everything ever", and a
    median over that drifts with the data's age: it moves when nothing
    happened, and nobody can say what it is a median *of*. Over a combined
    figure it is a mean of means, weighting each bucket equally instead of
    each record, which is the very thing `list` exists to defer to the read.
    """

    fn: Literal["mean", "median", "worst"]
    measure: str
    set: str
    line: int = 0


@dataclass(frozen=True)
class Coord:
    """`goal:{bucket}` -- a sequenced figure read at the coordinate this
    calculation is already at.

    A sequenced figure holds one value per bucket, so its **bare name is
    refused in expression position**: written plain it reads like a static
    declaration when it is a point-in-time value, and with two sequences in
    one expression nothing says the arithmetic is per coordinate. The
    obvious implementation of the bare form is a positional zip, which is
    right until one source starts a month later than the other -- and then
    every number is paired with the wrong month, silently, for ever.

    The selector makes the join explicit and the semantics are
    **join-by-bucket-key, never positional**: misalignment is not merely
    unlikely, it is unrepresentable. A coordinate present on one side and
    absent on the other yields an **absence** at that coordinate -- never a
    nought, and never a shift that would slide every later value one place.

    It reads the *same* coordinate and no other. There is deliberately no
    `:{bucket - 1}`: a stored value whose answer needs a bucket outside the
    population in view cannot be audited from the response carrying it, and
    a reader shown the number would have nothing to check it against. That
    is the refusal `delta`'s oldest cell gets, one layer down.

    Bare means one value per subject and a selector means one per coordinate,
    so the two shapes are visually distinct by construction and a reader never
    has to look up which kind of thing a name is.

    `name` is a `combine` binding in a figure's `calculate`, and a figure
    name in a `band:` rung, because those are the two places a second
    sequenced value can appear at all.
    """

    name: str
    line: int = 0


CalcExpr: TypeAlias = (
    Count | ListOf | Sum | Part | Number | Text | Setting | FigureRef | SubjectField | Ladder
    | Arith | Pick | DaysBetween | Extreme | FieldPick | FieldTotal | Coord
    | BucketStat
)


# --------------------------------------------------------------- figure --


@dataclass(frozen=True)
class Combine:
    """`sources = team_person.open_mrs_by_source over data_connection`

    One figure read by another. Its own block rather than an entry in `depends`,
    because the two bind different kinds of thing: a `depends` set is a set of
    *record ids* combined with `&`, `|` and `-`; this is a set of *stored
    values*, and no operation means anything applied to both. Sharing the block
    would also make the grammar ambiguous where it is currently decidable -- both
    are dotted names, so the parser would have to guess which namespace was
    meant and a typo in an index name would be reported as a missing figure.

    **`over` absent means the source is read as a single value**, not as a set of
    parts: the one value under this same subject. The two are one keyword apart
    and mean entirely different things, so which is which is decided by the
    source's own shape rather than by the reader's expectation. Neither mistake
    fails loudly -- a bare read of a dimensioned figure takes whichever part
    sorts first, and a rollup of an undimensioned one totals a single value and
    looks right for ever -- so the checker refuses both by name.

    `over` is deliberately redundant with the source's own `across`, so the two
    can be checked against each other: a source later split across something else
    fails the build here rather than quietly changing what this is a total of.
    """

    name: str
    figure: str
    over: str | None = None
    line: int = 0


@dataclass(frozen=True)
class NamedSet:
    name: str
    expr: SetExpr
    line: int = 0


@dataclass(frozen=True)
class FigureDecl:
    name: str
    doc: str
    display: str
    calculate: CalcExpr
    across: str | None = None
    """`across data_connection` -- a second dimension, one value per pair.

    A fact kind, exactly as the scope is, and it binds a second variable of its
    own name. That symmetry is the design: a dimension is not a new kind of
    thing, it is a second subject, so it has a roster, a name field and an id
    space the checker can already reason about.

    Underneath it is the composite index a day-keyed figure already uses, so
    nothing new happens in bucketing or invalidation. What `across` adds is the
    *declaration* that the second part is a dimension rather than a date --
    which nothing could previously say, and without which every reader
    downstream is silently wrong: the display template renders the variable as
    literal text and the generated sentence describes the whole population
    beside a number that is a slice of it.

    In the version hash. A figure that gains a dimension is a different figure
    with the same arithmetic, and reusing the stored values would leave a
    person-keyed number under a name that now means a pair.
    """

    bucketed: bool = False
    """`figure site.target_month bucketed:` -- this figure holds one value per
    subject *per bucket* of its group's declared sequence.

    A bare keyword, with no grain repeated after it. The grain is already
    written in the group and implied by the name, and a figure that restated
    it would be a second place for the two to disagree -- the checker would
    then have to decide which one wins, which is a question with no good
    answer.

    **Required when the group makes a sequence, refused when it does not.**
    Both directions are the same argument `across` makes one key-part along:
    what the keyword adds is the *declaration*, and without it every reader
    downstream is silently wrong in its own way -- a projection binds a
    column that never resolves, a bundle subscribes to every stored bucket of
    every subject, a rollup totals a sequence as though it were one number.

    **Not in the version hash.** It is a checker-verified mirror of the
    group's spec, and that spec is already hashed into every definition that
    reads it, so hashing it here would add nothing but a second copy. The
    `keyed as` rule exactly: a construct that decides what the checker
    permits, never what the arithmetic produces, stays out. It also has to
    stay out to be introducible at all -- hashed, making it required would
    have moved every sequenced figure's version in every deployment, and
    rebuilt every tenant's history to store byte-identical values.
    """

    carried: bool = False
    """`latest(...) carried forward` -- a step function over the figure's own
    bucket sequence.

    Sparse facts, dense buckets. One record exists each time somebody changed
    the value, so a month nobody touched it has no record -- and the answer
    for that month is not an absence but the same value, still in force. The
    suffix says the buckets between changes carry the last change forward.

    **In the version hash**, unlike `bucketed` beside it, and the difference
    is exactly the rule: `bucketed` mirrors something already hashed, while
    this changes what the stored values *mean*. The same records under the
    same calculation produce two months of values without it and twelve with
    it, so reusing a version across the change would serve carried numbers
    from a definition that never claimed them.
    """

    unit: DeclaredUnit | None = None
    sets: tuple[NamedSet, ...] = ()
    combines: tuple[Combine, ...] = ()
    """A figure has `depends` or `combine`, never both. Reading indexes *and*
    another figure would be two populations arriving at one calculation with no
    rule for how they relate."""

    band: Ladder | None = None
    """The second thing this figure answers: which band its number falls in.

    **A figure declared its number and a *separate* figure declared the band
    over it, and that was one concept written as two declarations.** The board
    printed a Band column beside every count, sourced by scanning the library at
    serve time for a `level` figure that combined this one -- so the word on
    screen came from a definition the page never named, and the page showing the
    formula did not contain it. Reported as *"where the hell is band coming from
    and why isn't it in the figure definition"*, which is the whole argument.

    Three things follow from it living here rather than in a figure of its own:

    - **It is evaluated on read and stored nowhere.** The ladder is pure over
      this figure's value and the tenant's dials, so there is nothing to keep in
      step. Turning a threshold re-bands the board on the next request instead of
      marking a figure pending, rebuilding every subject's value, and withholding
      the band in between -- which was a visible unknown on every card for the
      length of a rebuild, in exchange for storing a word derivable from a number
      sitting next to it.
    - **It is in the version hash.** The band is an answer this figure gives, so
      a definition that starts banding differently is a different definition. It
      costs nothing to move it: no value is stored under it.
    - **It reads `value`, which is this figure's own answer.** There is no other
      binding in scope, and that is what keeps it a *band* rather than a second
      calculation -- a ladder that could read anything else would be a way to
      compute a second number here, which is the thing `combine` and a separate
      figure are for.
    """

    line: int = 0


# -------------------------------------------------------------- reading --


@dataclass(frozen=True)
class WindowedSource:
    """`merged = team_person.time_to_merge in range`

    Deliberately not a set expression. A figure's `depends` composes buckets
    with set arithmetic, and every one of those operations is about *which
    records* are in play. A windowed reading is downstream of that question --
    the figure already decided the population -- so the only narrowing left is
    temporal, and offering set arithmetic here would let a definition
    re-litigate membership at read time using values instead of ids.
    """

    figure: str
    line: int = 0


@dataclass(frozen=True)
class LiveSource:
    """`waiting = code_review_request.waiting_seconds over (asked:{p} & pending)`

    What a live reading reads: records, right now, with a measure over them.

    A set expression here and not above, and the asymmetry is the whole
    distinction between the two: a windowed reading summarises values a figure
    already wrote, so membership was decided at write time. A live reading has
    no such figure -- it *is* the figure, evaluated on demand -- so membership is
    its own to declare, in exactly the grammar a figure uses.
    """

    measure: str
    set: SetExpr
    line: int = 0


StatisticFn: TypeAlias = Literal[
    "mean", "median", "worst", "sum", "count", "series", "delta"
]
"""A closed vocabulary, not an expression grammar.

Each is a claim about a distribution that a reader has to be able to check
against the evidence, and an arbitrary formula is not checkable by anybody who
is not already reading the code. `percentile` is the obvious next one and is
deliberately absent until something asks for it.

`sum` is the odd one and is why a `count` figure can be read at all. The
distribution statistics are refused over daily counts, because a mean of them is
a mean per *day* wearing a label that says per record.

`count` is live-only: a windowed reading already reports the sample, so a count
beside it would be one quantity under two names -- and the two would not even
agree, because for a count source the sample is days that contributed.

`series` returns the per-bucket values rather than a statistic over them, and
`delta` the change into each bucket. They are the two that are not scalars, and
each exists because the alternative was the browser doing the arithmetic: a
sparkline sliced out of a range, and a trend differenced out of a response.

`delta` closes the language's oldest gap. A trend had been listed as missing
for as long as there were readings, on the reasoning that the server could
compute one over a reading's answer -- which is rule 2 with extra steps, a
number on a screen that no definition claims and no version cites. What makes
it declarable is that the window already fixes the population: n buckets in
range produce n-1 changes, and the oldest bucket's cell is a stated absence
rather than a quiet fetch of the bucket before the window. There are
deliberately no stride or offset variants; each would be a second spelling of
a question nothing has asked.
"""


@dataclass(frozen=True)
class Statistic:
    fn: StatisticFn
    set: str
    line: int = 0
    """A series' points are one per bucket of the source figure's own
    sequence -- the grain its group declared -- so there is nothing here to
    configure. `series(...) by <grain>` existed while coarser grains were
    not declarable; now that an hourly view of quarter-hour events is its
    own hour-grouped figure, a read-time regrouping would be a second,
    unnamed spelling of that declaration -- and under integer windows its
    edge points could straddle the window, claiming time the span does not
    cover. A minute-grain figure refuses `series` outright: a minute bucket
    of a sparse figure holds one record, so the point *is* the record --
    the raw collection the payload exists to withhold."""


@dataclass(frozen=True)
class Requirement:
    """`at least 3 values in merged`

    A precondition on the sample, not a filter on it. When it fails every
    statistic is withheld together and the reading says which requirement fell
    short -- as opposed to a suppressed mean being an undifferentiated dash whose
    reason lives in a constant nobody can see from the screen.

    Unwritten on a windowed reading with a distribution statistic, the checker
    injects a default of one value, hashed like a written clause -- see the
    comment in `check.py` for why sum and live readings are exempt.
    """

    count: int
    set: str
    line: int = 0


@dataclass(frozen=True)
class ReadingBand:
    """`band on sum:` and an indented ladder -- the same ladder a figure bands
    with, one stratum along.

    It used to be a clause: `band low against flow.leadTimeDays in minutes`, a
    direction and a two-edged dial that the engine turned into good, watch or
    poor. Three things were wrong with it and only the last is fatal.

    The direction was a second vocabulary -- `low` versus `high` -- for
    something a comparison operator already says. The unit existed because a
    dial is a bare number with no idea what it measures, so the definition had
    to say whether 5 meant minutes or days, and getting it wrong banded every
    row good for ever at 1,440 times the intended threshold. And the dial
    itself was a control outside the fact stream: the one number on the card
    that no evidence could explain.

    A ladder fixes all three at once. The operator carries the direction, the
    threshold is a figure that knows its own unit, and the threshold is a fact.

    `on` names which statistic the word judges -- the mean when unwritten,
    named rather than inferred from whichever statistics happen to be present,
    because inference silently re-colours a row the day a second one is added.
    It also decides how a **figure named in the ladder is reduced**: the
    reference is read over the same window and through the same statistic as
    the number being banded, so a window's total is judged against the total of
    the goal across those same buckets. Any other rule compares a span against
    a point -- six months of deliveries against one month of target -- which
    reads plausibly and is wrong by a factor of the window's length.
    """

    ladder: Ladder
    on: StatisticFn | None = None
    line: int = 0


@dataclass(frozen=True)
class ReadingSet:
    name: str
    windowed: WindowedSource | None = None
    live: LiveSource | None = None
    line: int = 0


@dataclass(frozen=True)
class ReadingDecl:
    """A figure is stored; a reading is evaluated.

    Everything else about them is the same shape on purpose -- a mandatory
    explanation, a display template, a version that is the hash of the semantics --
    because the claim being made is the same claim: this number has a written
    definition and you can cite it.

    `range` is the one argument, and the rule that keeps a reading a figure
    rather than a function is that **an argument may narrow the population and
    may not change the calculation**. A range picks which stored values take
    part. A statistic, a minimum or a band would decide what the number *means*,
    so they are written here and hashed here.

    A live reading takes **no argument**: there is no range because nothing is
    stored to pick from, and `()` is what says so. The argument list and the
    source form encode the same fact twice and the checker requires them to
    agree -- deliberate redundancy, because it is the one thing that makes the
    mistake loud. `(range)` over a live source would compile into a reading that
    accepts a window, ignores it, and returns today's answer under a heading
    saying thirty days.
    """

    name: str
    doc: str
    display: str
    args: tuple[str, ...]
    sets: tuple[ReadingSet, ...]
    calculate: tuple[Statistic, ...]
    requires: tuple[Requirement, ...] = ()
    band: ReadingBand | None = None
    line: int = 0


# ----------------------------------------------------------- projection --

FieldType: TypeAlias = Literal["text", "date", "number", "flag"]
"""What a field binding holds, because a string is mute in the way an integer is.

`date` is what lets `days from start to now` know that `start` is a moment
rather than a word. `flag` lets a band test a boolean without the definition
comparing a string to `"true"`.

Required with no default, for the reason a field measure's unit is: inferring
from the shape of one record's value would classify an epic with no due date
differently from one with a due date, under the same definition.
"""


@dataclass(frozen=True)
class Join:
    """`queue_name = name from queueId through support_queue.id as text`

    The phrase after the path is byte for byte the one an index key uses, and
    that is the whole reason it is spelled this way: a reader who has learned
    how an author resolves to a person has already learned this, and a second
    spelling of one relation would be a second thing to keep in step.

    **Anything other than exactly one match is nothing.** An index resolves a
    relation to *every* owner on purpose -- an account claimed by two people is a
    data problem a figure should reflect rather than silently pick a winner for.
    A row cannot do that: a field holds one value, so the choice is between
    picking one and admitting there is no answer, and it admits there is no
    answer. Picking the first in sorted order would be stable and would still be
    a fabrication, and worse here than for a plain field: this picks among
    several *records*, so the row would name something with nothing to do with
    the subject and nothing anywhere would say a second candidate existed.
    """

    field: str
    kind: str
    path: str


@dataclass(frozen=True)
class FieldDecl:
    name: str
    path: str
    type: FieldType
    join: Join | None = None
    line: int = 0


@dataclass(frozen=True)
class ReadDecl:
    """`progress = work_container.progress` -- a stored figure, for this row.

    The subject is the record's own id, which is why a projection's fact kind
    must be the figure's scope: a projection over one kind asking for a figure
    scoped to another would look every row up under an id from a different space
    and find nothing -- a column of dashes, on every row, for ever.
    """

    name: str
    figure: str
    band: bool = False
    """`wip_band = band of team_person.wip` -- the figure's band, not its number.

    Explicit rather than implied by the binding's name, and explicit rather than
    bound automatically alongside the value. A read that silently produced two
    bindings would put a name in scope that appears nowhere in the text, which is
    the thing this whole language is arranged against: every name a rung reads is
    a name somebody wrote.
    """

    line: int = 0


@dataclass(frozen=True)
class ValueDecl:
    """`expected in share = elapsed / planned` -- one derived value.

    The unit is declared for the reason a figure's is, and absent for a ladder
    returning *words*, which has a unit nothing needs to be told. A ladder
    returning *numbers* is a different thing wearing the same syntax and does
    need one, or the renderer prints "77.5 late" where the definition meant
    "78d".

    Written before the `=` rather than after the expression, because a ladder's
    value is on the following lines and a trailing clause would have nowhere to
    sit that a reader would find.
    """

    name: str
    expr: CalcExpr
    unit: DeclaredUnit | None = None
    line: int = 0


@dataclass(frozen=True)
class Condition:
    """One comparison, never a ladder and never a conjunction.

    A flag fires or it does not; a row is in a population or it is not. Two
    conditions that must both hold are a `value` that is a ladder, tested here by
    its word -- the same answer the figure language gives for the absence of
    `and`, and for the same reason.
    """

    left: CalcExpr
    op: Comparison | AbsenceTest
    right: CalcExpr | None = None


@dataclass(frozen=True)
class FlagDecl:
    """A sentence a row earns.

    The construct this language was most reluctant to add. Half of what a
    roadmap screen produces is not numbers but *conditional prose*, rendered
    from the same values the bands read, and leaving it in the host language
    would mean a row's *reason* lived somewhere its *number* did not. A reader
    who can trace the number and not the sentence beside it has half of what
    this product claims.

    What keeps it a template rather than a language: substitution, and one
    plural form. No expressions inside a placeholder, no formatting directives,
    no conditionals beyond the `when` that decides whether it fires. Anything a
    sentence needs computed is a `value` -- named, checkable, and visible beside
    the flag it feeds.

    `action` is optional, and both halves of that are deliberate. Most flags have
    nothing to ask for, and inventing an imperative puts a to-do on a page whose
    whole value is that every row is actionable. It is a template rather than a
    constant in the host language because the *sentence telling somebody what to
    fix* is the content of the row.
    """

    name: str
    when: Condition
    label: str
    detail: str
    severity: Literal["info", "attention"]
    action: str | None = None
    line: int = 0


@dataclass(frozen=True)
class SortDecl:
    name: str
    direction: Literal["ascending", "descending"]
    line: int = 0


@dataclass(frozen=True)
class ProjectDecl:
    """One row per record: its key, its link, the figures about it, and the
    sentences a reader is owed.

    Its own declaration rather than a fifth kind of figure because everything
    else here produces a scalar, and that constraint is what makes a figure
    checkable -- one value, one unit, one sentence, one version. It *reuses*
    rather than reimplements: `from` takes the same set expression a figure's
    `depends` does, `value` the same expression language, a band the same ladder.

    **It aggregates nothing.** No counting its own rows, no averaging a column.
    Those are figures, and offering them here would be a second way to compute a
    number this product claims has exactly one.
    """

    name: str
    doc: str
    frm: SetExpr | None = None
    fields: tuple[FieldDecl, ...] = ()
    reads: tuple[ReadDecl, ...] = ()
    values: tuple[ValueDecl, ...] = ()
    flags: tuple[FlagDecl, ...] = ()
    omit: Condition | None = None
    """A row-level gate over the computed values, decided when the row is
    assembled -- which is what `from` cannot say: a population is stored
    buckets, and membership that moves with the clock goes stale between
    reindexes. The gate may read the clock for the same reason a value may:
    a projection stores nothing. A condition the engine cannot answer keeps
    the row -- dropping on the absence of evidence would narrow a population
    by a cheap path."""

    sort: SortDecl | None = None
    limit: int | None = None
    """Refused without a sort. A limit with no order returns an arbitrary subset
    that looks like a complete list and changes between runs for reasons no
    reader can see. An order with no limit is fine -- that is a sorted list -- so
    the rule is one-directional."""

    line: int = 0


# ------------------------------------------------------------ summarise --


@dataclass(frozen=True)
class CountDecl:
    """`count overdue where flight_health == "overdue"` -- how many rows.

    Absent `where` means every row, written as the ordinary case rather than a
    special one: demanding `where 1 == 1` would be noise.

    **An unknown does not count, so a count is a floor.** A row the engine has
    not measured is not evidence that the thing being counted is true. The
    opposite decision to a total, deliberately, and stated in both places.
    """

    name: str
    when: Condition | None = None
    line: int = 0


@dataclass(frozen=True)
class TotalDecl:
    """`total open_children in count = open_children where on_plan == 1`

    Only a number may be summed: a word would concatenate, a date is not in the
    numeric namespace so it would answer nothing for every row, and an unbound
    name is a column of nothing -- three failures, none of them loud.

    **An absent contribution makes the whole total absent.** Not "the sum of the
    rows that had a number", which is real arithmetic over a population nobody
    chose: it reads low, plausibly, and recovers on its own, which is the
    sawtooth signature. Rows the `where` excludes contribute nothing and are not
    absences.

    The unit is required rather than inherited from the column. Inheriting would
    be right most of the time and silently wrong for the case this construct
    exists to serve: a share per row is a *quantity of shares* in total, which is
    not a share of anything.
    """

    name: str
    of: str
    unit: DeclaredUnit
    when: Condition | None = None
    line: int = 0


@dataclass(frozen=True)
class SummariseDecl:
    """One row about the population a projection describes.

    Not a block inside `project`, and the rule that a projection aggregates
    nothing is not relaxed by this. A projection answers "one row per record" and
    a summary answers "one row about the population", and those are two
    definitions with two names, two explanations, two versions and two entries on
    the Data screen. Folding the second into the first would give one name two
    answers, which is exactly what `name@version` exists to prevent.

    The objection a projection's header makes does not apply here. It refuses
    aggregation on the grounds that "those are figures", which is true of
    everything a figure can express -- and a health band moves with the wall
    clock, so counting by it can never be a stored figure. The choice was never
    one way or two; it was a definition or code nobody can read.

    It aggregates *all* of one projection's rows. Sort and limit are the page and
    a summary is about the population, so a summary of the first three hundred
    under a heading naming the whole is refused by construction rather than by a
    rule.

    It cannot read a record, a fact or a figure -- everything arrives through the
    projection, so there is one population and one route to it. It cannot
    summarise another summary, for the reason a reading may not read a reading: a
    mean of means weights each group equally instead of each member, and nothing
    at run time can see it.
    """

    name: str
    over: str
    doc: str
    counts: tuple[CountDecl, ...] = ()
    totals: tuple[TotalDecl, ...] = ()
    values: tuple[ValueDecl, ...] = ()
    """Arithmetic and ladders over the counts and totals above, and over nothing
    else. A summary value may not read a *row* value: a row value is one number
    per record and the summary holds hundreds of them, so a name that resolves
    per row has no single value here. Binding only what this block and the two
    above declare makes the mistake an unbound name rather than a number taken
    from whichever row sorted last."""

    flags: tuple[FlagDecl, ...] = ()
    line: int = 0


# --------------------------------------------------------------- bundle --


@dataclass(frozen=True)
class BundleMember:
    """One line of a bundle: a slot binding, a declaration's kind and name,
    and (for a windowed reading) the windows to serve it over.

    Names plus arguments and nothing else, by design. There is no `depends`,
    no `calculate`, no cross-member arithmetic: a bundle defines no
    calculation, so every rule that makes a member's number trustworthy stays
    in the member's own definition and hash. A number derived from two
    members is a `combine` figure's job; a trend across windows is the
    server's job inside the reading's own response.
    """

    slot: str
    """`latency = reading ...` -- the address a client reads this member at,
    required on every member so a screen couples to the tile's layout rather
    than to definition names. An address, never a display label: the
    member's answer keeps its own definition's label and doc, and nothing
    lets a bundle rename what a number is called."""

    kind: Literal["figure", "reading", "projection", "summarise"]
    name: str
    windows: tuple[WindowSpec, ...] | None = None
    """`over 7, 14, 30`, `over 1-30, 31-60`, `over each 1-12` -- which
    bucket spans the reading serves: bare positions in the source figure's
    own sequence, whatever grain its group declared, because an argument
    may never say what a bucket is. `each a-b` expands at parse time to the
    one-bucket windows `a-a ... b-b`, indistinguishable downstream from the
    enumerated spelling. Only a *windowed* reading may carry a list,
    mirroring the language's rule that the argument list and the source
    form encode liveness twice, loudly: a live member is named bare the way
    a live reading declares `()`. Absent on a windowed reading, the serving
    default decides -- the same default an unqualified request to the
    results surface gets."""

    line: int = 0


@dataclass(frozen=True)
class BundleDecl:
    """`bundle team_person.card:` -- a named composition of definitions,
    served as one request.

    The composition stratum: the other declarations answer "how is this
    computed", a bundle answers "what travels together" -- a precalculated
    dashboard tile. Serving one *triggers* evaluation of its members; it
    computes nothing of its own, which is why its hash appears in no storage
    key and no number's citation.

    Member order is substantive -- the response preserves it -- so the
    members are a tuple in written order, and the hash covers that order.
    """

    name: str
    doc: str
    members: tuple[BundleMember, ...]
    line: int = 0


Decl: TypeAlias = (
    FactDecl
    | IndexDecl
    | MeasureDecl
    | FigureDecl
    | ReadingDecl
    | ProjectDecl
    | SummariseDecl
    | BundleDecl
)


@dataclass
class Document:
    """Everything one parse produced, in declaration order."""

    decls: list[Decl] = field(default_factory=list)
