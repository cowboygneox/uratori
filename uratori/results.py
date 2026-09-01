"""The one answer shape, whatever transport carries it.

Every engine answer -- served over HTTP, pushed over a socket, handed to a
listener -- is a `Result`, and the same object on each. A host that carries
these to a browser gets the guarantee the shape was designed for, learned the
hard way by the project this engine grew out of:

**Two parallel maps keyed by the same id.** Its first version returned
`subjects` and `names` separately, so reading one person meant indexing two
objects and hoping they agreed. A `Subject` carries its id, its name and its
value together.

**Three near-identical top-level types**, one per declaration kind, each with a
paragraph explaining how it differed. The differences are data now: what varies
is what sits in `value`, not the envelope around it.

**Three booleans every caller combined differently.** `live`, `stale` and
`populated` had to be reasoned about together to decide whether a number could
be shown, and each caller reasoned slightly differently. `state` is one value
and it says *why* an answer is missing, so a screen that ignores it renders
nothing rather than a fabricated zero.

The structural guarantee underneath all of it: **a response carries no raw
collection a client could reduce over.** Its predecessor intended the browser
not to calculate and lost it anyway, because the payload contained the arrays
and a browser function quietly recomputed a number the server had already
answered. Here there is nothing to recompute from.

pydantic rather than dataclasses, deliberately: a host serving these through
FastAPI (or any OpenAPI emitter) gets client types generated from the schema,
and a generated mirror is the only kind that can fail loudly when it drifts.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, Field

Unit: TypeAlias = Literal["count", "duration", "effort", "share", "days", "level", "moment"]
"""What a value *is*, so a renderer never guesses.

`duration` is wall-clock and `effort` is working time. Both render in hours,
so they part company as the number grows: 144,000 seconds is "1.7d" as a
duration and "40.0h" as an effort, because forty hours of work is a week and
nobody means "one and two-thirds days" by it. They are different quantities,
and the unit travels so a renderer never treats them as one.

(Effort used to render against a working-day dial, so 28,800 seconds read "1d".
Hours say the same thing without a reader having to find out whose working day
the engine had in mind.)
"""

Level: TypeAlias = str
"""A band word from the definition, preserved unchanged.

For `when` ladders: the author's own word (over, warn, at-risk, ...).
For `band` clauses: the engine's word (good, watch, poor) from thresholds.

A renderer maps words to colours. An unrecognized word must render as neutral,
never as good (which a missing `case` would do if green were the default).

A renderer never compares a number to a threshold -- banding in two places is
how a card reads Watch while a sort weighs the same person as Good, with list
order the only symptom."""


class Ok(BaseModel):
    ok: Literal[True] = True


class Unavailable(BaseModel):
    ok: Literal[False] = False
    because: Literal[
        "never-computed",
        "behind-deploy",
        "nothing-collected",
    ]
    """Why there is no answer, in the three ways there can be no answer.

    - `never-computed` -- this tenant has never run this definition. A new
      board, or the window between a deploy and the next sync.
    - `behind-deploy` -- values exist at an older version of the definition. They
      are not shown, because a number computed by a definition that no longer
      exists is worse than a dash.
    - `nothing-collected` -- the definition ran and nothing it reads holds
      anything. A board with no source connected, rather than a team that merged
      nothing.

    The last one matters more than it looks. The backfill writes a measured
    nought for every subject on the roster, so a board with no connection stores
    a complete, confident table of zeroes -- indistinguishable from a team with an
    empty queue unless something says which it is.
    """

    detail: str | None = None


Availability = Annotated[Ok | Unavailable, Field(discriminator="ok")]


class Window(BaseModel):
    """One window of a reading: a span of positions in the source figure's
    own bucket sequence, resolved by the server.

    `span` and `bucket` are the question and the bounds are the answer, and
    both travel: "buckets 31-60" depends on when the tenant's midnight was
    and what the figure's group declared, neither of which a client can
    know. Dates appear in answers, never in questions -- the request said
    `31-60`, and this says which concrete buckets that resolved to.
    """

    span: str
    """The bucket span asked for -- the canonical spelling: `"30"` (the last
    30 buckets, bucket 1 being the one the anchor falls in), `"31-60"` (the
    30 before them). Positions in the sequence `bucket` names, and nothing
    else. What was *asked*, beside what was covered -- an offset bucket
    wearing a label that reads like a trailing span is the failure mode
    this field exists to prevent."""

    bucket: str = "day"
    """What one bucket of the span is: the rule the source figure's group
    clause declared -- `day`, `minute`, `15 minutes`, `hour`, `week`,
    `month`, `quarter`, or a selective rule's own text (`first monday of
    month`). Declared and hashed there, never chosen by the request: an
    argument may narrow which buckets take part and may never change what
    a number means."""

    trailing: int | None = None
    """The span as a plain trailing-days count, kept for what it has always
    meant: present exactly when the span *is* the last N days (`span: "30"`
    over a day-grained figure), `null` for an offset span or any coarser or
    finer sequence rather than a number that would read as one."""

    frm: str | None = None
    to: str | None = None
    """The first (oldest) and last (newest) bucket labels the span resolved
    to, in the sequence's own vocabulary: ISO days, `2026-08` months,
    `2026-Q3` quarters, `2026-W35` weeks, `2026-08-25T14:00` sub-day
    buckets. For a selective rule the edges alone cannot say which sparse
    days were covered -- `buckets` carries the full list there.

    Null when the span resolved to no bucket, which happens only where the
    calendar runs out -- an anchor in year 1, or a span reaching past it.
    An absence, not an empty string: `""` in a date field is a value that
    renders and sorts, and there is no bucket here to name."""

    buckets: list[str] | None = None
    """Every bucket label the span covered, oldest first -- present exactly
    when the sequence is a selective rule (`first monday of month`), whose
    covered buckets are not contiguous: `frm`/`to` edges would claim every
    day between six first-Mondays, most of which no bucket covers. Null for
    the contiguous rules, where the edges say it all and repeating up to
    3,660 day labels per window would tax every response for the sparse
    case's honesty."""

    zone: str | None = None
    """The calendar *this subject's* buckets were cut on, which is a fact
    about the row rather than about the response: a courier in Tokyo and one
    in London have different dates under one heading reading "the last seven
    days", and nothing else on the row would say so.

    Carried on every window, including where the whole board agrees, because
    a row that means "cut in Europe/London" should say so whoever else is on
    the page -- a reader filtering to one subject must not lose the label. It
    is `Result.zone` that collapses: null there means the subjects disagree,
    and a screen prints the per-row answer instead of one heading."""

    mean: float | None = None
    median: float | None = None
    worst: float | None = None
    total: float | None = None
    count: float | None = None
    series: list[float | None] | None = None
    """Per-point values, when the definition asked for them. The one statistic
    that is not a scalar; it exists so a sparkline is a definition's answer
    rather than the client slicing a range into ten and computing ten means."""

    series_scale: list[float | None] | None = None
    """Each series point as a fraction of this window's own largest point,
    0..1, computed here so a screen drawing bars multiplies a served
    fraction by a bar height and composes nothing -- deriving the scale
    client-side means a maximum and a share, the two calculations a client
    must not make. Scaled within the window on purpose, and a screen
    stacking two windows should say so; the raw values ride beside it for a
    client that owns real axes. All-nought (or non-positive) windows scale
    to 0.0 per point: a zero drawn at zero height, never invented relief.
    None exactly when `series` is None."""

    delta: list[float | None] | None = None
    """The change into each bucket, when the definition asked for one -- one
    cell per bucket, positionally aligned with `series` so both draw against
    one axis.

    The oldest cell is **always** absent: it has no predecessor inside the
    range, and the response says so rather than omitting the bucket or
    reaching outside the window for one more value. A hole in the source is
    absent in both directions for the same reason -- differencing across it
    would report a two-bucket movement in a column headed per-bucket.

    Served rather than derived, because a client differencing `series` for
    itself is arithmetic on a screen that no definition claims and no version
    cites -- which is exactly what this field exists to stop."""

    delta_display: list[str | None] | None = None
    """Each delta cell rendered in the reading's own unit, for the reason the
    scalar statistics are: formatting a duration is a division. Signed, so a
    fall reads as one. None exactly where `delta` is None."""

    display: dict[str, str] = Field(default_factory=dict)
    """Each statistic above, rendered.

    **Rendered here because rendering a duration is a division**, and a
    division is a calculation. A client that formatted 61,200 seconds into
    "17h" would be one step from comparing it against a threshold, which is
    the banding-in-two-places failure `level` exists to prevent.

    Beside the numbers rather than instead of them: a screen sometimes needs the
    magnitude (a bar's width) and always needs the text. Keyed by statistic name,
    so a reading that calculates two carries two.
    """

    sample: int = 0
    """How many values took part. For a count figure this is *buckets that
    contributed* (days, for a day-grained one), not records -- a different
    number of similar magnitude, which is why it is named rather than left
    to be inferred."""

    buckets_covered: int = 0
    buckets_requested: int = 0
    """How much of the window has evidence, in buckets of the figure's own
    sequence: `buckets_requested` is how many buckets the span resolved to
    -- the span's width for a contiguous rule (clamped at the calendar's
    edge), the buckets that exist for a selective one (a `fifth monday`
    sequence skips months without one) -- and `buckets_covered` how many of
    those hold a stored value. Separate claims from `sample`, because "the
    queue took no tickets" and "we were not collecting" produce the same
    empty list."""

    level: Level = "unknown"
    unmet: list[str] = Field(default_factory=list)
    """Which requirement fell short, in words. A suppressed mean is otherwise an
    undifferentiated dash whose reason lives in a constant nobody can see."""


class Flag(BaseModel):
    """A sentence a row earned, rendered by the server."""

    name: str
    label: str
    detail: str
    action: str | None = None
    severity: Literal["info", "attention"]


class Row(BaseModel):
    """One record's projected row: named values, and the sentences it earned.

    `values` holds finished numbers and words -- never the records they came
    from. There is nothing here to aggregate, which is the point: a screen that
    wants a count of these rows asks for the summary that counts them.

    **`values` and `display` both travel, for the reason they both travel on a
    `Subject`.** They answer different questions: the number is for anything
    *positional* -- the width of a progress bar, a marker's offset -- and the
    text is what a reader sees. Sending only the text made every value on a row
    a string, which meant the first screen wanting to draw a bar had to parse
    "50.0%" back into a fraction. That is arithmetic in the client, arriving
    through a formatter, which is the one route in that nothing was watching.
    """

    values: dict[str, float | str | None]
    display: dict[str, str]
    """Each value above, rendered. Rendered on the server because rendering a
    quantity is a division, and a client dividing seconds into hours is one
    step from comparing them against a threshold.

    Required rather than defaulted, and there is exactly one constructor
    (`serve.py`'s `_row`), so a default buys nothing and costs something: it
    would let a `Row` reach a client with the text missing, where every reader
    indexes into it and would throw rather than degrade. A generated client
    type declares it required either way, so a default here is only a
    disagreement between the two halves of one contract.
    """

    units: dict[str, Unit]
    flags: list[Flag] = Field(default_factory=list)


class Subject(BaseModel):
    """One row of an answer. Identity and value together, never two maps to join."""

    id: str
    name: str
    """Rendered by the server. For a stored figure this is the name frozen when
    the value was written, because a person renamed next week must not rewrite
    the history of what they moved. For a live answer it is the current one,
    since there is no history to contradict."""

    value: float | str | None = None
    display: str | None = None
    """`value`, rendered. Same reason as `Window.display`.

    Both travel because they answer different questions: the number is for
    anything positional -- a bar, an ordering already decided by the server -- and
    the text is what a reader sees. A screen that renders the number itself is
    formatting, and formatting a duration or an effort is a division against a
    setting it does not have.
    """

    windows: list[Window] | None = None
    row: Row | None = None
    level: Level = "unknown"
    dimension: str | None = None
    """The other half of a pair, when the figure is split across something.
    Present so two rows about one person are told apart by a field rather than
    by a reader noticing."""


class EvidenceMember(BaseModel):
    """One thing a stored value cites: a record, or (for a rollup) a part.

    `display` is this member's own measurement, rendered by the server. A
    `list` figure serves its stored values positionally; a sum or an extreme
    serves each held record as its measure reads it now -- live, like a
    rollup's parts, so a record corrected after the pass visibly disagrees
    with the total instead of agreeing with a number that has stopped being
    right. A count deliberately serves none, because a "1" beside each record
    would be a number nothing computed, printed on the page whose claim is
    that every number was.

    `held` is a separate claim from `title is None`, and `False` means one
    thing: the store was asked for this member and does not have it -- deleted
    at the source, cascaded away, or never collected. The member is listed
    either way, because a list quietly shorter than the value beside it breaks
    the one check this payload exists to enable. `True` also covers "no lookup
    was made" (the mixed-kind fallback), since "not held" is a claim and only
    a lookup can earn it.

    `figure` is set on a part: the source figure whose stored row this is. Two
    operands of one calculation are two different claims, and an unlabelled
    number under a total is not evidence of anything.
    """

    key: str
    title: str | None = None
    url: str | None = None
    held: bool = True
    display: str | None = None
    figure: str | None = None

    dimension: str | None = None
    """A part's day or dimension cell, split off its storage key server-side.
    Only for parts: their titles are frozen subject labels, so twenty-seven
    season cells all read "Seattle Seahawks" with nothing telling them apart.
    A record's key is never split -- a raw fact key may contain the separator
    without it meaning anything."""


class Evidence(BaseModel):
    """The citation behind one stored value, made readable.

    The engine stores every value with the record ids it was computed from
    (`StoredValue.members`); this is that citation joined back to the records,
    so a day of durations can be traced to the records that produced each one.
    Fetched on request rather than carried on `Result`: every row of a served
    figure dragging its members along would make the common read pay for the
    rare check.

    Per the clients-compute-nothing rule, members carry rendered text and no
    numbers -- a list of raw measurements on the wire is something a client
    could reduce over, which is the door `serve.py`'s `_wire` holds shut.

    `parts` says what the members are. A leaf figure cites records of `kind`;
    a rollup cites the stored cells it read -- one row per (member, source
    figure) -- because a total's evidence is its parts, and re-listing the
    records underneath would be re-deriving the number a second way.
    """

    figure: str
    version: str
    subject: str
    state: Availability
    display: str | None = None
    """The stored value as rendered now, so a caller can notice when a rebuild
    landed between reading the table and opening this."""

    note: str | None = None
    """A sentence about this citation the members cannot carry themselves --
    today, that a stored row's measurements and members disagree in length and
    the measurements are therefore withheld. Server-rendered prose, because
    the reason must reach the reader and not stop in a code comment."""

    members: list[EvidenceMember] = Field(default_factory=list)
    parts: bool = False
    source: str | None = None
    kind: str | None = None

    measure: str | None = None
    """The measure the member displays were read through, when there is one --
    the definition that turns these records into the amount above them. Named
    so the panel can say how the rows lead to the value rather than leaving a
    reader to guess. Absent for a count, whose records are the amount; absent
    for parts, whose rows each name their own figure; and withheld whenever
    `note` withholds the measurements, because naming how rows were measured
    above rows deliberately carrying no measurements is a contradiction."""


class Result(BaseModel):
    """Every engine answer, over every transport."""

    kind: Literal["figure", "reading", "projection", "summary"]
    name: str
    version: str
    """The content hash of the definition that produced this. The citation."""

    at: str
    """When the server evaluated it. One instant for the whole response, by
    construction -- a per-row clock produces a list whose oldest entry disagrees
    with itself and an `at` that describes one row of it."""

    zone: str | None = None
    """The one calendar every subject in this answer was cut on, or null where
    they do not agree.

    Not "the tenant's calendar" any more -- there is no such thing. A calendar
    is a field on each subject's own record, so a board can hold couriers in
    two zones and the honest top-level answer is then *none*: each window
    carries its own, and a screen prints those instead of one heading over
    dates that mean different things."""

    unit: Unit
    label: str
    doc: str
    state: Availability
    banded: bool
    """Whether the definition declares a band at all -- a `band:` block on a
    figure, a `band` clause on a reading. A property of the definition, never
    of the data, so it holds its value while the state is unavailable.
    Required rather than defaulted for the reason `_unit` raises rather than
    casts: a serve path that forgot to say would otherwise hide the Band
    column silently, and a missing statement should be a type error.

    Without it "no thresholds declared" and "banded, but nothing to band" are
    the same word on the wire (`level: "unknown"`), and a screen printed a Band
    column of stated absences under definitions that never claimed to band.
    A projection is never banded: the band words it binds travel as row values,
    cited to the figure whose thresholds produced them."""

    statistics: list[str] | None = None
    """For a reading: the statistics its `calculate` block declares, as the
    wire spells them (`sum` travels as `total`, the `Window` field it fills),
    in declaration order. This is the column set a table of windows draws --
    served rather than derived from whichever `display` keys happen to be
    present, because a withheld window carries no values at all and a table
    that unioned present keys would silently narrow itself the day every
    window fell short. `None` for every other kind: statistics are a
    reading's vocabulary, and null here means "does not apply", never
    "empty".

    The catalogue (`DeclarationOut.statistics` on `/definitions`) speaks the
    language instead -- `sum` there, `total` here -- because it describes
    the declaration as written while this list names the fields an answer
    fills. Two vocabularies for two questions; a client binding columns
    binds to THIS one."""

    banded_on: str | None = None
    """For a banded reading: which statistic the band judges, in the same
    wire spelling. On the wire because the band word is a verdict on ONE
    number -- a screen colouring the whole row would band statistics the
    definition never banded, and without this field the screen could only
    guess which column the word belongs beside. `None` wherever `banded` is
    false, and for figures, whose single value leaves nothing to name."""

    subjects: list[Subject] = Field(default_factory=list)
    """In the server's order. A screen does not sort, because sorting is a
    calculation and the sort key is a definition's answer."""

    empty: Subject | None = None
    """What the definition says about a subject it has nothing for. Answered by
    the definition rather than assumed by the caller: "nobody merged anything" is
    a finding, and which requirement that fails is the definition's decision."""

    summary: Row | None = None
    """For a projection, the summary declared over it -- computed at the same
    instant over the same population, which is why it travels with the rows
    rather than on a route of its own.

    For a `kind: "summary"` result -- a bundle's summarise member -- the same
    row travelling *without* the projection's rows: `subjects` stays empty
    and this carries the one row about the population. It is still computed
    over ALL the projection's rows, never the page; only the row payload
    stays home."""


class BundleMemberResult(BaseModel):
    """One member's answer under its slot address.

    `slot` is the name the bundle's definition binds this member to --
    `latency = reading ...` -- so a client addresses `card.latency` and the
    tile's layout is decoupled from definition names. An address and nothing
    more: `result` keeps its own definition's `label` and `doc`, because
    nothing may let a bundle rename what a number is called.
    """

    slot: str
    result: Result


class BundleResult(BaseModel):
    """A bundle's answer: its members' ordinary `Result`s, each under its
    slot address, in declaration order, from one request.

    A wrapper and nothing more. The bundle defines no calculation, so there
    is no `state`, no `unit` and no `subjects` here -- each member carries its
    own, absence reasons included, because "the tile is fine but one number
    is behind a deploy" is a per-member fact the wrapper must not flatten.

    `version` is the bundle's content hash -- the member list, hashed at
    compile time. It exists for the review surface (a changed tile is a
    moved hash in the committed artifact) and appears in no storage key and
    no number's citation: every number inside cites its own member's
    `name@version`, exactly as it would served alone.

    `kind` is a discriminator against `Result`, so the one results route can
    serve either and a typed client branches on a field rather than sniffing
    shapes.
    """

    kind: Literal["bundle"] = "bundle"
    name: str
    version: str
    at: str
    """When the server evaluated the bundle -- the one instant handed to
    every member that takes one, so a tile cannot disagree with itself. The
    `at` anchor is refused for a bundle outright: it moves only a reading's
    windows, and the other members can only be served as they stand, so an
    anchored tile would disagree with itself under a wrapper claiming one
    clock."""

    label: str
    doc: str
    results: list[BundleMemberResult]
    """The members' answers, each under its slot, in the bundle's
    declaration order -- order is substantive too, and a screen may bind to
    either: the position the definition wrote, or the slot it named."""
