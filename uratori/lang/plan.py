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

from .ast import (
    Band,
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
    settings: tuple[str, ...] = ()
    scope_index: str | None = None
    """The index that fans this figure out. Absent for a rollup, which has no
    index at all -- its subjects come from the roster and from whatever its parts
    are stored under. Every reader has to cope with that: looking a rollup's
    index up and getting nothing is not an error in any language, so the absence
    has to be in the type."""

    band: Ladder | None = None
    """The ladder that turns this figure's number into a word.

    Evaluated when the figure is served and stored nowhere: it is pure over the
    value and the tenant's dials, so there is nothing to keep in step. That is
    why moving a threshold re-bands the board on the next request rather than
    marking the figure pending and withholding the band through a rebuild.

    `band_settings` is separate from `settings` for exactly that reason -- the
    dials a *stored* value was computed under are what the pointer's fingerprint
    covers, and a dial only the band reads must not force a rebuild of values it
    did not affect.
    """

    band_settings: tuple[str, ...] = ()
    day_keyed: bool = False
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
    band: Band | None = None
    source: str | None = None
    """The figure a windowed reading summarises."""

    live_measure: str | None = None
    live_set: SetExpr | None = None
    indexes: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
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
    sort: SortDecl | None = None
    limit: int | None = None
    joins: tuple[Join, ...] = ()
    indexes: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
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
    settings: tuple[str, ...] = ()
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
