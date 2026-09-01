"""What the checker produces: plans the engine runs, and nothing else.

Nothing downstream ever parses source again. A plan is JSON-serialisable by
construction, because the compiled library is committed as JSON so that a change
to a definition shows up in a diff as a moved version -- which is the part of a
definition change worth reviewing, and the part that decides whether stored
values are reused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from ..windows import WindowSpec
from .ast import (
    CalcExpr,
    Condition,
    FieldType,
    FigureUnit,
    FlagDecl,
    IndexBy,
    Join,
    Ladder,
    MeasureUnit,
    Requirement,
    SetExpr,
    SortDecl,
    Statistic,
    StatisticFn,
)

Value: TypeAlias = float | str | list[float | None] | None
"""What a stored value can be.

A number, a word (from a ladder), nothing (the engine could not tell), or a list
(the members of a `list` figure, in evidence order).

**Nothing is not nought.** A missing value means *not computed*, never "the
subject has none of it" -- the backfill walks the roster and writes a real
nought for anybody who genuinely has none. Every reader has to decide what an
absence means for it, and none of them may coerce.
"""


@dataclass(frozen=True)
class CompiledFactField:
    name: str
    type: str | None = None
    """text | number | flag | moment, or None for a nested block."""

    many: bool = False
    doc: str = ""
    children: tuple[CompiledFactField, ...] = ()


@dataclass(frozen=True)
class CompiledFact:
    """What a record of this kind is -- the world, as one declaration of it.

    Versioned like everything else, and the version is the hash of the fields
    and their types alone: prose, the name field and the url field are
    rendering. **No downstream version reads it.** A fact schema decides what
    the checker permits and what the write boundary accepts -- like `keyed
    as`, it never changes what the arithmetic produces, so a host adopting
    fact declarations moves no figure's hash and rebuilds nothing.
    """

    name: str
    fields: tuple[CompiledFactField, ...]
    name_field: str | None = None
    url_field: str | None = None
    doc: str = ""
    version: str = ""


@dataclass(frozen=True)
class CompiledIndex:
    name: str
    kind: str
    """The fact kind whose records this buckets."""

    id_space: str
    """Whose ids the members are. Equal to `kind` unless `keyed as` says
    otherwise. A figure may only combine indexes over one id space: intersecting
    ids that mean different things yields the empty set, and an empty set is a
    figure reading zero rather than an error anybody sees."""

    spec: IndexBy
    bucketed: bool
    """True when this fans out (a field or composite index), false when it is a
    single bucket that narrows (a predicate, presence or age index)."""

    label: str | None = None


@dataclass(frozen=True)
class CompiledMeasure:
    name: str
    kind: str
    shape: Literal["duration", "field", "moment"]
    unit: MeasureUnit | None = None
    later: str | None = None
    earlier: str | None = None
    clock: bool = False
    field_path: str | None = None
    moment: str | None = None


@dataclass(frozen=True)
class FigurePlan:
    name: str
    scope: str
    doc: str
    display: str
    unit: FigureUnit
    calculate: CalcExpr
    across: str | None = None
    sets: dict[str, SetExpr] = field(default_factory=dict)
    combines: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """binding -> (source figure, dimension being summed away or None)."""

    indexes: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    scope_index: str | None = None
    """The index that fans this figure out. Absent for a rollup, which has no
    index at all -- its subjects come from the roster and from whatever its parts
    are stored under. Every reader has to cope with that: looking a rollup's
    index up and getting nothing is not an error in any language, so the absence
    has to be in the type."""

    band: Ladder | None = None
    """The ladder that turns this figure's number into a word.

    Evaluated when the figure is served and stored nowhere: it is pure over
    this figure's value and the figures it compares against, so there is
    nothing to keep in step. That is why moving a goal re-bands the board on
    the next request rather than marking the figure pending and withholding
    the word through a rebuild.

    `band_reads` is separate from `reads` for exactly that reason. `reads` are
    the figures a *stored* value was computed from, and moving one rebuilds
    this figure; a figure only the band compares against moves no stored value
    here and must not force a rebuild -- but it does move the served answer,
    so the serving side has to know about it or every connected screen keeps
    the old word until a reload.
    """

    band_reads: tuple[str, ...] = ()
    band_fields: tuple[str, ...] = ()
    """`<kind>.<field>` names the band reads off the subject's own record.

    Separate from `band_reads` because the move that invalidates them is a
    different one: a goal figure moving shows up as a change on that figure,
    where an allowance moving is a write to a fact kind and moves no figure
    at all. Without this the record-shaped threshold -- the one that replaced
    the dial for the common case -- had no serving edge, so a limit could
    drop from five to one and every connected board kept saying "ok".
    """

    grain: str | None = None
    """The bucket rule of the scope index's tail part, when it has one -- a
    stored grain (`minute` through `quarter`) or a selective rule's canonical
    text (`first monday of month`). This is the ordered sequence a reading's
    integer windows walk: `over 1-6` over a month-grained figure is the last
    six months, bucket 1 the month the anchor falls in. A grained figure has
    one value per subject per bucket rather than one per subject, so it
    cannot be served by subject and a reading over a range is what a screen
    asks for. Not in the version hash directly: the index spec that carries
    it already is."""

    carried: bool = False
    """Whether this figure's buckets are a step function -- see
    `ast.FigureDecl.carried`. In the version hash, because it changes what a
    stored value means."""

    ordered_by: str | None = None
    """Which field decides "latest" for a declared-field read: the field the
    scope group truncates on. Derived rather than written, so the ordering
    and the bucketing cannot disagree about when a change happened."""

    dimension_part: str | None = None
    depth: int = 0
    """How many figures deep this is. Stored, and every path sorts by it: the
    declaration order of a file is not a dependency order, and on a cold build
    the wrong order stores a nought for everybody and never revisits it."""

    version: str = ""


@dataclass(frozen=True)
class ReadingPlan:
    name: str
    scope: str
    mode: Literal["window", "live"]
    doc: str
    display: str
    unit: Literal["count", "duration", "effort"]
    calculate: tuple[Statistic, ...]
    requires: tuple[Requirement, ...] = ()
    band: Ladder | None = None
    """The ladder that words this reading's verdict, or None where it declares
    no band."""

    band_on: StatisticFn | None = None
    """Which statistic the word judges -- the mean when the definition left it
    unwritten, resolved here so no reader has to know the default. It is also
    how a figure named in the ladder is reduced over the window: same buckets,
    same statistic."""

    band_reads: tuple[str, ...] = ()
    """The figures the ladder compares against, in name order."""

    source: str | None = None
    """The figure a windowed reading summarises."""

    live_measure: str | None = None
    live_set: SetExpr | None = None
    indexes: tuple[str, ...] = ()
    version: str = ""


@dataclass(frozen=True)
class ProjectPlan:
    name: str
    kind: str
    doc: str
    fields: tuple[tuple[str, str, FieldType, Join | None], ...] = ()
    reads: tuple[tuple[str, str, FigureUnit, bool], ...] = ()
    """(binding, figure, unit, band). The last says whether this binds the
    figure's *band* -- the word its own `band:` block answers -- rather than its
    number, in which case the unit is `level` whatever the figure's own is."""

    values: tuple[tuple[str, CalcExpr, FigureUnit], ...] = ()
    flags: tuple[FlagDecl, ...] = ()
    frm: SetExpr | None = None
    omit: Condition | None = None
    """The row-level gate: a row it holds for is off the page and out of the
    summary. Unknown keeps the row -- see the declaration's own note."""

    sort: SortDecl | None = None
    limit: int | None = None
    joins: tuple[Join, ...] = ()
    indexes: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    version: str = ""


@dataclass(frozen=True)
class SummarisePlan:
    name: str
    over: str
    doc: str
    counts: tuple[tuple[str, Condition | None], ...] = ()
    totals: tuple[tuple[str, str, FigureUnit, Condition | None], ...] = ()
    values: tuple[tuple[str, CalcExpr, FigureUnit], ...] = ()
    flags: tuple[FlagDecl, ...] = ()
    version: str = ""


@dataclass(frozen=True)
class BundleMemberPlan:
    """One member of a compiled bundle: its slot address, a kind, a name,
    and (for a windowed reading) the bucket spans to serve it over. `kind`
    speaks the plan vocabulary -- a `summarise` declaration compiles to a
    `summary` member, matching the `Result.kind` its answer travels under.

    `slot` is the address a client reads this member at -- structural, so it
    is hashed into the bundle's version, and an address only: the member's
    answer keeps its own definition's label and doc."""

    slot: str
    kind: Literal["figure", "reading", "projection", "summary"]
    name: str
    windows: tuple[WindowSpec, ...] | None = None


@dataclass(frozen=True)
class BundlePlan:
    """A named composition of definitions, served as one request.

    It defines no calculation and stores no values, so its version is unlike
    every other: a content hash over the member list alone (kinds, names and
    window arguments, in written order -- order is substantive because the
    response preserves it). The hash exists for exactly one surface, the
    committed library artifact, where a changed tile shows as a moved hash in
    the diff. It appears in **no storage key and no number's citation**: each
    member's `Result` carries its own version and provenance, and nothing on
    screen ever cites the bundle.

    Member *versions* are deliberately not hashed in -- the same asymmetry a
    windowed reading has with its source figure. A member redefined
    underneath shows as that member's own moved version, on the artifact and
    on the wire; the tile's composition did not change, so its hash does not.
    """

    name: str
    doc: str
    members: tuple[BundleMemberPlan, ...]
    version: str = ""


@dataclass(frozen=True)
class Library:
    indexes: dict[str, CompiledIndex]
    measures: dict[str, CompiledMeasure]
    figures: tuple[FigurePlan, ...]
    readings: tuple[ReadingPlan, ...]
    projections: tuple[ProjectPlan, ...]
    summaries: tuple[SummarisePlan, ...]
    source: str
    """The concatenated `.fig` text, kept so the Data screen can show any
    declaration exactly as written. A figure whose formula is only readable by
    checking the repository out is a figure nobody checks."""

    facts: dict[str, CompiledFact] = field(default_factory=dict)
    """The declared world, when the source declares one. Empty for a
    schema-taught world -- and that emptiness is load-bearing: it is what
    tells the write boundary there are no fields to verify against."""

    bundles: tuple[BundlePlan, ...] = ()
    """The composition stratum: named tiles over the declarations above.
    Defaulted so a library built without them is a library with none, which
    is also what keeps every pre-bundle artifact readable."""

    def figure(self, name: str) -> FigurePlan | None:
        for plan in self.figures:
            if plan.name == name:
                return plan
        return None

    def reading(self, name: str) -> ReadingPlan | None:
        for plan in self.readings:
            if plan.name == name:
                return plan
        return None

    def projection(self, name: str) -> ProjectPlan | None:
        for plan in self.projections:
            if plan.name == name:
                return plan
        return None

    def summary(self, name: str) -> SummarisePlan | None:
        for plan in self.summaries:
            if plan.name == name:
                return plan
        return None

    def bundle(self, name: str) -> BundlePlan | None:
        for plan in self.bundles:
            if plan.name == name:
                return plan
        return None
