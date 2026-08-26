"""Turning stored values into the one response shape.

Every route and the websocket both end here, so there is one place that decides
what an answer looks like and one place that decides whether there is an answer
at all.

**The availability judgement is the interesting part.** A figure is stored, so
it has a pointer and can be behind a deploy or stale against a moved dial. Both
of those are a full table of plausible numbers about a definition that no longer
applies, which is worse than a dash -- so they are withheld and the response says
which it was. A live reading has neither, because it stores nothing.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ..lang.ast import Count, Extreme, ListOf, SetExpr, SetIndex, SetOp, SetRef
from ..lang.ast import Sum as LangSum
from ..lang.plan import FigurePlan, Library, ProjectPlan, ReadingPlan, SummarisePlan, Value
from ..lang.settings import fingerprint as settings_fingerprint
from ..results import (
    Evidence,
    EvidenceMember,
    Level,
    Ok,
    Result,
    Row,
    Subject,
    Unavailable,
    Unit,
    Window,
)
from ..schema import Schema
from ..store import EngineStore, FactSource, StoredValue
from .buckets import SEPARATOR, day_range, subject_of
from .engine import (  # the same hashes the pass records, shared deliberately
    _index_version,
    _versions_if_legacy_current,
)
from .evaluate import band_of
from .project import ProjectedRow, RenderedFlag, Summary, format_value
from .read import (
    Sample,
    level_of,
    sample_from_buckets,
    sample_from_days,
    series_of,
    statistics_of,
    unmet_of,
)


def now_ms() -> float:
    return time.time() * 1000.0


def _iso(at_ms: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(at_ms / 1000.0, tz=UTC).isoformat()


def _label_of(name: str) -> str:
    """A definition's heading, from its own name: `open_mrs` -> "Open mrs".

    Mechanical rather than declared, for the reason an index's label fallback is:
    a missing one degrades to something true rather than to nothing, and the
    ugliness is the prompt to write a better display template.
    """
    leaf = name.split(".", 1)[1] if "." in name else name
    return leaf.replace("_", " ").capitalize()


async def availability(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    settings: Mapping[str, Any],
) -> Ok | Unavailable:
    pointer = await store.pointer(tenant, plan.name)
    if pointer is None:
        return Unavailable(
            because="never-computed",
            detail="this board has not run this definition yet; the next sync will",
        )
    if pointer.version != plan.version:
        return Unavailable(
            because="behind-deploy",
            detail=(
                f"values here were computed by {plan.name}@{pointer.version}, and this build "
                f"holds @{plan.version}"
            ),
        )
    wanted = settings_fingerprint(dict(settings), list(plan.settings))
    if pointer.settings_fingerprint != wanted:
        return Unavailable(
            because="setting-moved",
            detail="a dial this definition reads has changed and the rebuild has not finished",
        )
    if not await _any_index_holds(store, library, tenant, plan):
        # **A nought written by walking the roster is not a measured nought.**
        # Every subject gets one, so a board with no connection stores a
        # complete, confident table of zeroes. Without this the response is
        # indistinguishable from a team whose queue is empty.
        return Unavailable(
            because="nothing-collected",
            detail="nothing this definition reads has been collected for this board",
        )
    return Ok()


async def _any_index_holds(
    store: EngineStore, library: Library, tenant: str, plan: FigurePlan, seen: set[str] | None = None
) -> bool:
    """Has anything this figure depends on actually been collected?

    **Transitively, through `reads`.** A rollup and a band have no indexes of
    their own -- their whole construction is that they are built from another
    figure -- so asking only `plan.indexes` answers "nothing to check" and the
    gate passes. On a board that has collected nothing that produced a complete,
    confident table: every person `0` open merge requests, every band a green
    "ok", and the card's own safety rung bypassed, because the band was not
    *nothing*, it was a confident word.

    A figure with no indexes and no reads has nothing to be about, which the
    checker refuses -- so falling through to `False` cannot strand a real one.
    """
    seen = seen if seen is not None else set()
    if plan.name in seen:
        return False
    seen.add(plan.name)

    for name in plan.indexes:
        if await store.index_has_rows(tenant, name):
            return True
    for source in plan.reads:
        below = library.figure(source)
        if below is not None and await _any_index_holds(store, library, tenant, below, seen):
            return True
    return False


# ------------------------------------------------------------- figures --


async def serve_figure(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    settings: Mapping[str, Any],
    at_ms: float | None = None,
) -> Result:
    at = at_ms if at_ms is not None else now_ms()
    state = await availability(store, library, tenant, plan, settings)
    subjects: list[Subject] = []

    if isinstance(state, Ok):
        for stored in await store.values(tenant, plan.name, plan.version):
            tail = (
                stored.subject.split(SEPARATOR, 1)[1] if SEPARATOR in stored.subject else None
            )
            subjects.append(
                Subject(
                    id=stored.subject,
                    name=stored.label,
                    value=_wire(stored.value),
                    # Rendered from the *stored* value, not the wired one, and
                    # the asymmetry is the decision: a day of durations ships
                    # its measurements as text ("1.0h, 2.0h") because rendered
                    # evidence is what this pane exists to show, while `value`
                    # stays null because a numeric list is something a browser
                    # can reduce over, and this app's claim is that it has
                    # nothing to reduce.
                    display=format_value(stored.value, plan.unit, settings),
                    # The figure's own band, from its own definition. This used
                    # to be a second figure found by scanning the library for a
                    # `level` one that combined this plan -- so the word came
                    # from a definition this response never named, and the page
                    # showing the formula did not contain it. It is a `band:`
                    # block on the plan now, evaluated here against the value
                    # beside it and the tenant's live dials.
                    level=_level_word(band_of(plan.band, stored.value, settings)),
                    dimension=tail,
                )
            )
        # One name per subject, the newest row's. Labels are frozen per row at
        # write time, and a day-keyed subject has many rows -- so a renamed
        # person would otherwise appear under two headings, their days split
        # by whoever sorts between the old name and the new. The newest row
        # (`<person>@<ISO day>` sorts lexicographically, so max(id) is the
        # latest day) carries the freshest label held.
        freshest: dict[str, str] = {}
        for row in sorted(subjects, key=lambda s: s.id):
            freshest[subject_of(row.id)] = row.name
        subjects = [
            s.model_copy(update={"name": freshest[subject_of(s.id)]}) for s in subjects
        ]
        # The id tiebreak is what makes a person's day rows chronological: the
        # key is `<person>@<ISO day>` and ISO days sort lexicographically. The
        # pane renders in this order, so it is part of the answer.
        subjects.sort(key=lambda s: (s.name.lower(), subject_of(s.id), s.id))

    return Result(
        kind="figure",
        name=plan.name,
        version=plan.version,
        at=_iso(at),
        zone=_zone_of(library, plan, settings),
        unit=_unit(plan.unit),
        label=_label_of(plan.name),
        doc=plan.doc,
        state=state,
        banded=plan.band is not None,
        subjects=subjects,
    )


def _level_word(word: str | None) -> Level:
    """A band word from the definition, preserved unchanged.

    For `when` ladders: the author's own word (over, warn, at-risk, ...).
    For `band` clauses: the engine's word (good, watch, poor) from thresholds.

    An absence is `unknown`, which the browser must render as neutral -- never as
    good, which a missing `case` would do if green were the default.
    """
    return word if word is not None else "unknown"


# ------------------------------------------------------------ evidence --


async def serve_evidence(
    store: EngineStore,
    facts: FactSource,
    library: Library,
    schema: Schema,
    tenant: str,
    plan: FigurePlan,
    settings: Mapping[str, Any],
    subject: str,
) -> Evidence | None:
    """One stored value's citation, joined back to what it cites.

    Returns `None` when the figure is available and this subject has no row --
    an address that names nothing, which a route turns into a 404 with the
    reason. An unavailable figure answers with its state instead, because an
    empty members list under an Ok state would read as "this value cites
    nothing": a confident claim about a figure the tenant has never run.
    """
    state = await availability(store, library, tenant, plan, settings)
    if not isinstance(state, Ok):
        return Evidence(
            figure=plan.name, version=plan.version, subject=subject, state=state
        )

    stored = await store.value(tenant, plan.name, plan.version, subject)
    if stored is None:
        return None

    # `values[i] measures members[i]` is a contract the evaluator keeps by
    # construction. A stored row that breaks it -- written by some earlier era
    # of the engine -- must not be repaired by pairing what aligns: that prints
    # the right numbers under the wrong records, which is worse than printing
    # none. The records still list; the measurements are withheld, and `note`
    # says so -- without it the panel is byte-identical to a healthy count's,
    # and the reason would exist only in this comment.
    values: list[float | None] | None = None
    note: str | None = None
    if isinstance(stored.value, list):
        if len(stored.value) == len(stored.members):
            values = stored.value
        else:
            note = (
                "This row's stored measurements and its citation disagree in length "
                "-- a row written by an older engine. The records are listed and no "
                "measurement is shown against any of them, because pairing the ones "
                "that align would print the right numbers under the wrong records."
            )

    def measurement(position: int) -> str | None:
        if values is None or values[position] is None:
            return None
        return format_value(values[position], plan.unit, settings)

    display = format_value(stored.value, plan.unit, settings)

    if plan.combines:
        # A rollup: its evidence is the cells it read, not the records
        # underneath them -- re-listing those would re-derive the number a
        # second way, which is the thing writing it over other figures was
        # meant to delete.
        #
        # One row per (member, source), every source, in declaration order.
        # Taking the first source holding a row and stopping would print the
        # operand `max` rejected as the sole citation for the one it chose,
        # and show a quotient's numerator with the denominator invisible.
        # Each row names its figure, because two operands of one calculation
        # are two different claims and an unlabelled number under a total is
        # not evidence of anything.
        #
        # A (member, source) pair with no row at the source's live version is
        # a different claim from a part holding nought ("the cell has gone"
        # explains a total that looks too big; a nought never does), so it is
        # listed and marked rather than dropped.
        sources = list(dict.fromkeys(src for src, _ in plan.combines.values()))
        members = []
        for key in stored.members:
            for name in sources:
                below = library.figure(name)
                part: StoredValue | None = (
                    await store.value(tenant, name, below.version, key)
                    if below is not None
                    else None
                )
                members.append(
                    EvidenceMember(
                        key=key,
                        figure=name,
                        title=part.label if part is not None else None,
                        held=part is not None,
                        display=(
                            format_value(part.value, below.unit, settings)
                            if part is not None and below is not None
                            else None
                        ),
                    )
                )
        return Evidence(
            figure=plan.name,
            version=plan.version,
            subject=subject,
            state=state,
            display=display,
            note=note,
            members=members,
            parts=True,
            source=sources[0] if len(sources) == 1 else None,
        )

    kind = _cited_kind(plan, library)
    if kind is not None:
        # A leaf figure: the members are record keys in one fact kind. Which
        # kind is read off the indexes of the set the calculation actually
        # names -- not off `scope_index`, which fans the figure out and need
        # not be what `calculate` counts (a figure scoped by one kind can
        # count a tenant-wide set of another). And it is the index's
        # `id_space`, not its `kind`, because `keyed as` buckets one kind's
        # records under another kind's ids. Either mistake looks the same:
        # titles looked up in the wrong table, every member marked missing.
        held = {
            row.key: row.value
            for row in await facts.some(tenant, kind, list(stored.members))
        }
        name_field = schema.name_fields.get(kind)
        url_field = schema.url_fields.get(kind)
        members = []
        for position, key in enumerate(stored.members):
            value = held.get(key)
            title = _field_of(value, name_field)
            url = _field_of(value, url_field)
            members.append(
                EvidenceMember(
                    key=key,
                    title=title,
                    url=url,
                    held=value is not None,
                    display=measurement(position),
                )
            )
        return Evidence(
            figure=plan.name,
            version=plan.version,
            subject=subject,
            state=state,
            display=display,
            note=note,
            members=members,
            kind=kind,
        )

    # The members span more than one fact kind (a ladder over sets of two
    # kinds), so there is no one table to look titles up in. The keys are
    # served bare with nothing claimed about them -- `held` stays True because
    # "not held" is a claim, and no lookup was made that could earn it.
    return Evidence(
        figure=plan.name,
        version=plan.version,
        subject=subject,
        state=state,
        display=display,
        note=note,
        members=[EvidenceMember(key=k) for k in stored.members],
    )


def _field_of(value: Mapping[str, Any] | None, field: str | None) -> str | None:
    """A record's schema-declared field, or nothing -- never a guess."""
    from .buckets import read_path

    if value is None or field is None:
        return None
    found = read_path(value, field)
    text = found[0] if found else None
    return text if isinstance(text, str) and text else None


def _cited_kind(plan: FigurePlan, library: Library) -> str | None:
    """The one fact kind a figure's members are keys of, or None.

    Reads the set the calculation names (every record-set shape carries one)
    and resolves it to the id spaces of the indexes underneath, following
    references. The ladder and arithmetic shapes name no set, so their
    members are the union of everything in `depends` -- those resolve only
    when every set agrees on one id space, which is exactly when a lookup is
    honest.
    """
    calc = plan.calculate
    if isinstance(calc, (Count, ListOf, Extreme)) or (
        isinstance(calc, LangSum) and calc.measure is not None
    ):
        names = [calc.set]
    else:
        names = list(plan.sets)

    spaces: set[str] = set()
    seen: set[str] = set()
    for name in names:
        _spaces_of(plan.sets.get(name), plan.sets, library, spaces, seen)
    return spaces.pop() if len(spaces) == 1 else None


def _spaces_of(
    expr: SetExpr | None,
    sets: Mapping[str, SetExpr],
    library: Library,
    out: set[str],
    seen: set[str],
) -> None:
    if expr is None:
        return
    if isinstance(expr, SetIndex):
        index = library.indexes.get(expr.index)
        if index is not None:
            out.add(index.id_space)
    elif isinstance(expr, SetRef):
        if expr.name in seen:
            return
        seen.add(expr.name)
        _spaces_of(sets.get(expr.name), sets, library, out, seen)
    elif isinstance(expr, SetOp):
        _spaces_of(expr.left, sets, library, out, seen)
        _spaces_of(expr.right, sets, library, out, seen)


# ------------------------------------------------------------ readings --


async def serve_reading(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: ReadingPlan,
    settings: Mapping[str, Any],
    trailing: Sequence[int],
    at_ms: float | None = None,
) -> Result:
    """One request serves several windows from a single fetch.

    Anchored on the **widest** window's start. Anchoring on the narrowest
    silently turns the month into a fortnight and reports a plausible mean for
    it, which is the shape of every bug this codebase has spent a day on.
    """
    at = at_ms if at_ms is not None else now_ms()
    source = library.figure(plan.source or "")
    if source is None:
        raise ValueError(f"{plan.name} reads a figure that is not in the library")

    state = await availability(store, library, tenant, source, settings)
    zone = _zone_of(library, source, settings)
    grain = source.grain
    series_by = next((s.by for s in plan.calculate if s.fn == "series"), None)
    widest = max(trailing)
    frm, to = day_range(at, zone, widest)
    # A sub-day label on the window's final day -- "2026-08-25T14:30" -- sorts
    # *after* the bare day "2026-08-25", so day-string bounds would silently
    # drop the current day from every sub-day window: a board reporting the
    # team has shipped nothing all morning, every morning. "T23:59" is the
    # largest label a day can carry at any grain.
    fetch_frm, fetch_to = (frm, to) if grain == "day" else (frm + "T00:00", to + "T23:59")

    def _sample(inside: list[tuple[str, Value]], w_frm: str, w_to: str) -> Sample:
        if grain == "day":
            return sample_from_days(inside, w_frm, w_to)  # type: ignore[arg-type]
        return sample_from_buckets(inside, w_frm, w_to, series_by)  # type: ignore[arg-type]

    subjects: list[Subject] = []
    if isinstance(state, Ok):
        rows = await store.values_in_range(
            tenant, source.name, source.version, fetch_frm, fetch_to
        )
        by_subject: dict[str, list[tuple[str, Value]]] = {}
        names: dict[str, str] = {}
        for stored in rows:
            base = subject_of(stored.subject)
            day = stored.subject.split(SEPARATOR, 1)[1]
            by_subject.setdefault(base, []).append((day, stored.value))
            names.setdefault(base, stored.label)

        for base, days in sorted(by_subject.items()):
            windows: list[Window] = []
            for span in trailing:
                w_frm, w_to = day_range(at, zone, span)
                # The day prefix of a label is its local day whatever the
                # grain, so one comparison serves both key shapes.
                inside = [(d, v) for d, v in days if w_frm <= d[:10] <= w_to]
                sample = _sample(inside, w_frm, w_to)
                windows.append(
                    _window(plan, sample, span, w_frm, w_to, zone, settings, series_by)
                )
            subjects.append(
                Subject(
                    id=base,
                    name=names.get(base, base),
                    windows=windows,
                    level=windows[0].level if windows else "unknown",
                )
            )

    empty = Subject(
        id="",
        name="",
        windows=[
            _window(
                plan,
                _sample([], *day_range(at, zone, span)),
                span,
                *day_range(at, zone, span),
                zone,
                settings,
                series_by,
            )
            for span in trailing
        ],
    )

    return Result(
        kind="reading",
        name=plan.name,
        version=plan.version,
        at=_iso(at),
        zone=zone,
        unit=_unit(plan.unit),
        label=_label_of(plan.name),
        doc=plan.doc,
        state=state,
        banded=plan.band is not None,
        subjects=subjects,
        empty=empty,
    )


def _window(
    plan: ReadingPlan,
    sample: Sample,
    trailing: int,
    frm: str,
    to: str,
    zone: str | None,
    settings: Mapping[str, Any],
    series_by: str | None,
) -> Window:
    unmet = unmet_of(plan, sample)
    stats: dict[str, float | None] = statistics_of(plan, sample)
    if unmet:
        # **Every statistic is withheld together.** Reporting the worst case
        # while suppressing the mean is the "three statistics or none" bargain
        # broken, and a worst case printed alone is by construction the outlier.
        #
        # Annotated above rather than inferred: without it this reassignment
        # narrows the dict to `dict[str, None]`, and the `is not None` filter
        # below becomes unreachable -- which one mypy version reports and another
        # does not, so it passed here and failed in CI.
        stats = dict.fromkeys(stats)
    wants_series = any(s.fn == "series" for s in plan.calculate)
    # `count` is a tally whatever the source measures, so it renders as a plain
    # number rather than through the reading's own unit -- otherwise a queue of
    # three prints as three seconds.
    rendered = {
        key: format_value(value, "count" if key == "count" else plan.unit, settings)
        for key, value in stats.items()
        if value is not None
    }
    return Window(
        trailing=trailing,
        frm=frm,
        to=to,
        zone=zone,
        mean=stats.get("mean"),
        median=stats.get("median"),
        worst=stats.get("worst"),
        total=stats.get("total"),
        count=stats.get("count"),
        series=series_of(sample) if wants_series and not unmet else None,
        series_by=_series_grain(series_by) if wants_series and not unmet else None,
        display=rendered,
        sample=len(sample.values),
        days_covered=sample.days_covered,
        days_requested=sample.days_requested,
        level="unknown" if unmet else _level_word_from(level_of(plan, stats, settings)),
        unmet=unmet,
    )


def _level_word_from(word: str) -> Level:
    """Preserve the band word from level_of unchanged.

    The engine generates good/watch/poor from band clauses, or returns the word
    from a when ladder. Both flow through unchanged.
    """
    return word


def _zone_of(library: Library, plan: FigurePlan, settings: Mapping[str, Any]) -> str | None:
    from ..lang.check import _index_fields

    if plan.scope_index is None:
        return None
    for part in _index_fields(library.indexes[plan.scope_index].spec):
        if part.zone is not None:
            node: Any = settings
            for segment in part.zone.split("."):
                node = node.get(segment) if isinstance(node, Mapping) else None
            return str(node) if node is not None else None
    return None


# --------------------------------------------------------- projections --


def serve_projection(
    plan: ProjectPlan,
    rows: Sequence[ProjectedRow],
    summary_plan: SummarisePlan | None,
    summary: Summary | None,
    settings: Mapping[str, Any],
    at_ms: float,
    state: Ok | Unavailable,
) -> Result:
    subjects = [
        Subject(
            id=row.id,
            name=str(row.values.get("key") or row.values.get("name") or row.id),
            row=_row(row.values, row.units, row.flags, settings),
        )
        for row in rows
    ]
    return Result(
        kind="projection",
        name=plan.name,
        version=plan.version,
        at=_iso(at_ms),
        unit="count",
        label=_label_of(plan.name),
        doc=plan.doc,
        state=state,
        # Never banded, even when the projection binds `band of` a figure:
        # those words are row values, cited to the figure whose thresholds
        # produced them, and a projection subject carries no level of its own.
        banded=False,
        subjects=subjects,
        summary=(
            _row(summary.values, summary.units, summary.flags, settings)
            if summary is not None
            else None
        ),
    )


def _row(
    values: Mapping[str, Value],
    units: Mapping[str, str],
    flags: Sequence[RenderedFlag],
    settings: Mapping[str, Any],
) -> Row:
    """A projected row on the wire: the numbers, the text, and the sentences.

    One function for a row and for the summary above it, because they are the
    same shape and the difference between them is what they are *about*. Two
    copies is how one of them quietly stops carrying `display`.

    A list never reaches here -- the checker refuses a day-keyed figure to a
    projection, and nothing else produces one -- but `_wire` is applied anyway
    rather than trusted, because a collection on the wire is something a browser
    can reduce over and the property this app claims is that it has nothing to
    reduce.

    `display` deliberately does *not* get the same treatment: `format_value`
    joins a list into text, so a list would arrive as `values: null` beside
    `display: "1, 2, 3"`. For a projection row that pair stays unreachable --
    the checker refuses a day-keyed figure to a projection. Where it *is*
    reachable, `serve_figure`, the two halves were decided together: the text
    travels as evidence for the Data screen, the numbers do not (`_wire`
    explains the split). If a projection row ever produces a list, that is
    the decision to inherit, not a second `_wire`.
    """
    return Row(
        values={k: _wire(v) for k, v in values.items()},
        display={
            k: format_value(v, _unit(units.get(k, "count")), settings)
            for k, v in values.items()
        },
        units={k: _unit(u) for k, u in units.items()},
        flags=[_flag(f) for f in flags],
    )


def _flag(rendered: RenderedFlag) -> Any:
    from ..results import Flag

    return Flag(
        name=rendered.name,
        label=rendered.label,
        detail=rendered.detail,
        action=rendered.action,
        severity="attention" if rendered.severity == "attention" else "info",
    )


_SERIES_GRAINS: frozenset[str] = frozenset({"15 minutes", "hour", "day"})


def _series_grain(by: str | None) -> Literal["15 minutes", "hour", "day"] | None:
    """Checked against the closed set rather than cast, the `_unit` bargain:
    a grain off the list is a bug in the planner, and this says so instead of
    passing it through to a typed client that cannot handle the word."""
    if by is None:
        return None
    if by not in _SERIES_GRAINS:
        raise ValueError(f"{by} is not a series grain this contract can carry")
    return by  # type: ignore[return-value]


_UNITS: frozenset[str] = frozenset(
    {"count", "duration", "effort", "share", "days", "level", "moment"}
)


def _unit(unit: str) -> Unit:
    """Checked against the closed set rather than cast.

    A user-defined narrowing predicate *is* a cast, which is how v1 widened one
    of these at a single site and put a value outside its own declared type on
    the wire. A unit that is not on the list is a bug in the planner, and this
    says so rather than passing it through.
    """
    if unit not in _UNITS:
        raise ValueError(f"{unit} is not a unit this contract can carry")
    return unit  # type: ignore[return-value]


def _wire(value: Value) -> float | str | None:
    """A stored list never reaches the wire as *numbers*.

    A screen that received a day's individual measurements as a list could
    compute its own mean -- which is the door v1 left open and lost the whole
    property through. The statistics over them are a reading's answer, and
    there is a route for that.

    What does travel for such a value is `display`: the measurements rendered
    to text by the server (`serve_figure` formats the stored value, not this
    one). That was decided, not leaked -- the Data screen's job is showing the
    evidence behind a number, and text is for eyes, not for reducing.
    """
    if isinstance(value, list):
        return None
    return value


# ------------------------------------------------------ projections, live --


async def answer_projection(
    store: EngineStore,
    facts: Any,
    library: Library,
    tenant: str,
    plan: ProjectPlan,
    settings: Mapping[str, Any],
) -> Result:
    """A projection, whole: read every row, summarise them, then page.

    **In that order, always**, which is the whole reason this is one function
    rather than four lines each caller writes. A summary is about the
    population and the sort and the limit are about the page, so summarising
    after either produces a headline describing the first three hundred rows
    under a heading naming the roadmap -- a wrong number that reads as a right
    one, with nothing downstream able to detect it.

    There were three copies of this sequence before the roadmap needed a fourth:
    the results route, the socket's first paint, and the sync. Each was correct
    and each was one careless reordering away from not being, which is exactly
    the shape of thing that should be written once.
    """
    from .project import ordered, summarise

    at = now_ms()
    rows, state, _missing = await project_rows(
        store, facts, library, tenant, plan, settings, at
    )
    summary_plan = next((s for s in library.summaries if s.over == plan.name), None)
    # **No summary over a population the server has just said it does not
    # have.** `summarise` is total: handed no rows it answers every count as
    # nought, every total as 0.0, and fires whichever flags a board of nothing
    # earns -- *"None of the 0 epics on this board is both in play and dated."*
    # Served beside a state reading `nothing-collected`, that is a complete,
    # confident table of zeroes underneath a sentence saying nothing was
    # collected, on the page whose entire job is checking.
    #
    # Withheld here rather than hidden by the screen, because the screen is not
    # the only reader: a fabricated row on the wire is available to anything
    # that asks, and the rule is that an absence is never a nought.
    summary = (
        summarise(summary_plan, rows, settings, at)
        if summary_plan is not None and isinstance(state, Ok)
        else None
    )
    shown = ordered(plan, rows)
    if plan.limit is not None:
        shown = shown[: plan.limit]
    return serve_projection(plan, shown, summary_plan, summary, settings, at, state)


async def project_rows(
    store: EngineStore,
    facts: Any,
    library: Library,
    tenant: str,
    plan: ProjectPlan,
    settings: Mapping[str, Any],
    at_ms: float,
) -> tuple[list[ProjectedRow], Ok | Unavailable, list[str]]:
    """Every row of a projection, and which of its figures were unavailable.

    **Every record, never the page.** The sort and the limit are applied by the
    caller *after* this returns, because a summary over a projection aggregates
    what this produces -- and a summary of the first three hundred rows under a
    heading naming the whole population is a wrong number that reads as a right
    one, with nothing downstream able to detect it.

    A projection has no pointer of its own: it stores nothing, so there is no
    version a tenant can be behind on and nothing to be stale against a moved
    dial. Every row is this build's definition over this board's records,
    always. What *can* be missing is the figures a row reads, which is why the
    unavailable ones are named rather than the whole projection being withheld:
    a row whose judgements are absent still carries its key and its name, and
    dropping it would empty the screen over a gap in one column.
    """
    from ..engine.project import project

    records = await facts.of_kind(tenant, plan.kind)
    if not records:
        return [], Unavailable(
            because="nothing-collected",
            detail=f"no {plan.kind} records have been collected for this board",
        ), []

    if plan.frm is not None:
        # The discipline a figure's pointer enforces, applied to the
        # population -- per grouping. The buckets `from` filters through are
        # stored state, each built under a recorded spec version, and the
        # engine records that version only after the rebuild actually ran.
        # Only the groupings THIS population reads are compared: an
        # unrelated filter arriving elsewhere in the library must not
        # unseat a page whose own buckets are exactly what the pass built.
        # A mismatch on one of its own means the buckets describe a
        # different definition (or none), and filtering through them would
        # serve an Ok page with records silently missing: a confident zero
        # on exactly the screen this gate was written for.
        stamps = await store.index_stamps(tenant)
        built = {name: stamp.version for name, stamp in stamps.items()}
        legacy = None
        if not built:
            # The upgrade window: per-index stamps arrive at the first pass,
            # but a pre-0.7 whole-set stamp matching this library is the same
            # proof of currency the pass's seed accepts -- one shared rule,
            # so the reader and the writer cannot disagree about it.
            legacy = await store.legacy_index_set(tenant)
            built = _versions_if_legacy_current(legacy, library) or {}
        stale = [
            name
            for name in plan.indexes
            if name in library.indexes
            and built.get(name) != _index_version(library.indexes[name])
        ]
        if stale:
            # Two different absences: a tenant nothing ever bucketed, and a
            # tenant whose buckets exist but describe other definitions. A
            # mismatched legacy stamp is the SECOND kind -- it was bucketed,
            # under an older library -- and calling it never-computed would
            # tell an upgraded deployment its history vanished.
            never = not built and legacy is None
            return [], Unavailable(
                because="never-computed" if never else "behind-deploy",
                detail=(
                    "this board has not bucketed the population's groups and "
                    "filters yet; the next sync will"
                    if never
                    else "the population's buckets were built under a previous "
                    "definition; the next sync rebuilds them"
                ),
            ), []

        # After the emptiness check, deliberately: `nothing-collected` is a
        # claim about the sync, and a population that matches nothing is a
        # truthful empty page over records that were collected.
        wanted = await _population(store, tenant, plan)
        records = [record for record in records if record.key in wanted]

    values: dict[str, dict[tuple[str, bool], Value]] = {}
    missing: list[str] = []
    for _binding, figure_name, _unit, band in plan.reads:
        source = library.figure(figure_name)
        if source is None:
            continue
        state = await availability(store, library, tenant, source, settings)
        if not isinstance(state, Ok):
            missing.append(figure_name)
            continue
        for stored in await store.values(tenant, source.name, source.version):
            # The band is derived here rather than read: it is evaluated from
            # the value beside it and the tenant's live dials, so a projection
            # binding a band costs no extra query and cannot be stale against a
            # threshold the way a stored one was.
            values.setdefault(stored.subject, {})[(figure_name, band)] = (
                band_of(source.band, stored.value, settings) if band else stored.value
            )

    joins = await _joined(facts, tenant, plan)
    # A None is a row the plan's `omit` gate dropped. Filtered here, before
    # the summary, the sort and the limit ever see the list -- the gate is the
    # definition of *on the page*, and a summary counting a row no page shows
    # is a tile nobody can check.
    rows = [
        row
        for record in records
        if (
            row := project(
                plan,
                record.key,
                record.value,
                {f: v for f, v in values.get(record.key, {}).items()},
                joins,
                settings,
                at_ms,
            )
        )
        is not None
    ]
    return rows, Ok(), missing


async def _population(store: EngineStore, tenant: str, plan: ProjectPlan) -> frozenset[str]:
    """Which records are on the page at all: the projection's `from`, resolved.

    Resolved by the same walker a figure's `depends` runs through, deliberately
    -- a second implementation of set algebra would be two answers to "who is in
    this population". The checker has already refused scoped buckets, fan-out
    indexes and named sets here, so every index is a single bucket, the subject
    the resolver wants is never read, and the readers beyond buckets are
    unreachable.
    """
    from .evaluate import Readers, _resolve

    members = {name: await store.members(tenant, name, "") for name in plan.indexes}

    def read_bucket(index: str, bucket: str | None) -> frozenset[str]:
        return members.get(index, frozenset())

    def unreachable(*_: str) -> Any:
        raise AssertionError("a projection population reads nothing but buckets")

    readers = Readers(
        buckets=read_bucket,
        measures=unreachable,
        moments=unreachable,
        parts=unreachable,
        settings=unreachable,
    )
    assert plan.frm is not None
    return _resolve(plan.frm, "", {}, readers)


async def _joined(
    facts: Any, tenant: str, plan: ProjectPlan
) -> dict[tuple[str, str], dict[str, list[Mapping[str, Any]]]]:
    """The records a projection's joins reach, loaded once per relation.

    One query per distinct `(kind, path)` rather than one per row: a lookup per
    row over five thousand records would be five thousand queries to answer a
    question with a dozen distinct answers.
    """
    from .buckets import read_path

    out: dict[tuple[str, str], dict[str, list[Mapping[str, Any]]]] = {}
    for join in plan.joins:
        key = (join.kind, join.path)
        if key in out:
            continue
        table: dict[str, list[Mapping[str, Any]]] = {}
        # One entry per *record* per value, never per occurrence. `read_path`
        # flattens, so a person record listing one account id twice would
        # otherwise appear as two candidates, the exactly-one rule would answer
        # nothing, and that person's whole drawer would empty while their
        # figures stayed right -- the engine's resolver dedupes the same
        # relation (`{k: sorted(set(v))}` in `_resolver`), and two builders of
        # one relation disagreeing about duplicates is precisely the kind of
        # drift this codebase hunts. Not reachable through today's identity
        # writer, which is why this is a guard with a comment rather than a
        # bug fix with a test.
        holders: dict[str, set[str]] = {}
        for row in await facts.of_kind(tenant, join.kind):
            for value in read_path(row.value, join.path):
                if row.key in holders.setdefault(value, set()):
                    continue
                holders[value].add(row.key)
                table.setdefault(value, []).append(row.value)
        out[key] = table
    return out
