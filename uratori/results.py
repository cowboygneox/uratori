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

`duration` is wall-clock and `effort` is working time -- the same 28,800 seconds
is "8h" under one and "1d" under the other, and both are right about their own
quantity. Filing one under the other is the disagreement this engine exists to
end.
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
        "setting-moved",
        "nothing-collected",
    ]
    """Why there is no answer, in the four ways there can be no answer.

    - `never-computed` -- this tenant has never run this definition. A new
      board, or the window between a deploy and the next sync.
    - `behind-deploy` -- values exist at an older version of the definition. They
      are not shown, because a number computed by a definition that no longer
      exists is worse than a dash.
    - `setting-moved` -- a dial the definition names has changed and the rebuild
      has not finished. The stored values describe the old dial.
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
    """One trailing window of a reading, resolved to actual days by the server.

    `trailing` is the question and `frm`/`to` is the answer, and both travel:
    "the last 30 days" depends on when the tenant's midnight was, which a client
    cannot know.
    """

    trailing: int
    frm: str
    to: str
    zone: str | None = None

    mean: float | None = None
    median: float | None = None
    worst: float | None = None
    total: float | None = None
    count: float | None = None
    series: list[float | None] | None = None
    """Per-point values, when the definition asked for them. The one statistic
    that is not a scalar; it exists so a sparkline is a definition's answer
    rather than the client slicing a range into ten and computing ten means."""

    series_by: Literal["15 minutes", "hour", "day"] | None = None
    """What one series point spans, when the definition grouped a sub-day
    figure. Absent for a day-keyed source, where a point has always been a
    day. On the wire so a series of 168 points says what it is, rather than
    leaving the reader to divide -- and a closed union rather than a string,
    so a new grain is a compile error in a typed client instead of a silently
    unhandled word."""

    display: dict[str, str] = Field(default_factory=dict)
    """Each statistic above, rendered.

    **Rendered here because rendering a duration is a division**, and a division
    is a calculation. 61,200 seconds is "17h" as wall-clock and, against a
    tenant's working day, something else entirely -- so a client that formatted
    it would need `hoursPerDay`, and a client that knows `hoursPerDay` is one
    step from banding against a threshold.

    Beside the numbers rather than instead of them: a screen sometimes needs the
    magnitude (a bar's width) and always needs the text. Keyed by statistic name,
    so a reading that calculates two carries two.
    """

    sample: int = 0
    """How many values took part. For a count figure this is *buckets that
    contributed* (days, for a day-keyed one), not records -- a different number
    of similar magnitude, which is why it is named rather than left to be
    inferred."""

    days_covered: int = 0
    days_requested: int = 0
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
    """Each value above, rendered. Rendered on the server because rendering an
    effort is a division against the tenant's working day, and a client that
    knows the working day is one step from banding against a threshold.

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
    """The tenant's calendar, so a screen can say *when* rather than printing an
    ISO instant verbatim. A client cannot work this out: it knows its own
    timezone and the board belongs to a team that may not share it."""

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
    rather than on a route of its own."""
