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
from typing import Any

from ..lang.ast import (
    Arith,
    Count,
    Extreme,
    Ladder,
    ListOf,
    Pick,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    SubjectField,
)
from ..lang.ast import Sum as LangSum
from ..lang.plan import (
    BundlePlan,
    FigurePlan,
    Library,
    ProjectPlan,
    ReadingPlan,
    SummarisePlan,
    Value,
)
from ..results import (
    BundleMemberResult,
    BundleResult,
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
from ..windows import (
    WindowError,
    WindowSpec,
    expand_window_args,
    refuse_reach,
    span_text,
    window_token,
)
from .buckets import (
    SEPARATOR,
    end_of_day_ms,
    measure_of,
    ordinal_rule_of,
    read_number,
    resolve_span,
    subject_of,
    tail_of,
)
from .carry import CarryReachExceeded
from .carry import materialise as _fill
from .engine import (  # the same hashes the pass records, shared deliberately
    _index_version,
    _versions_if_legacy_current,
    subject_zones,
    zone_ref,
)
from .evaluate import band_of
from .project import ProjectedRow, RenderedFlag, Summary, format_value
from .read import (
    Sample,
    delta_of,
    level_of,
    sample_over,
    series_of,
    statistics_of,
    threshold_of,
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


async def band_thresholds(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    facts: FactSource | None = None,
) -> dict[str, dict[str, Value]]:
    """The figures a band compares against, resolved per subject key.

    The join is subject-key equality and nothing cleverer, and that is the
    whole reason the checker insists a band's threshold shares this figure's
    grain and dimension. A monthly number is stored under `c1@2026-07` and so
    is the monthly goal, so the lookup lands on the same coordinate by
    construction -- misalignment is not merely unlikely, it is
    unrepresentable. A goal cut by day against a number cut by month would
    have keys that never meet, which is why that pairing is refused at compile
    time rather than left to answer nothing here.

    A threshold figure that is not yet servable contributes nothing, so every
    row it would have judged keeps its word withheld. That is the honest
    answer: the goal is not known, so whether the number clears it is not
    known either.
    """
    out: dict[str, dict[str, Value]] = {}
    for name in plan.band_reads:
        source = library.figure(name)
        if source is None:  # pragma: no cover - the checker resolved it
            continue
        if not isinstance(await availability(store, library, tenant, source), Ok):
            continue
        for stored in await store.values(tenant, source.name, source.version):
            out.setdefault(stored.subject, {})[name] = stored.value

    # A threshold read straight off the subject's record: one fetch of the
    # scope kind, keyed by the subject the row is about. A bucketed row is
    # `c1@2026-07`, and the record is the courier's, so the entry is written
    # under the record's own key and the lookup drops the coordinate -- a
    # courier has one allowance, not one per month. Keying it by the
    # coordinate instead is what the code used to do, and every lookup missed:
    # a sequenced figure banded this way rendered no word at all, on every
    # row, for ever.
    fields = _subject_fields_in(plan.band)
    if fields and facts is not None:
        held = {row.key: row.value for row in await facts.of_kind(tenant, plan.scope)}
        for subject, record in held.items():
            for kind, field in fields:
                value = read_number(record, field)
                if value is not None:
                    out.setdefault(subject, {})[f"{kind}.{field}"] = value
    return out


def thresholds_for(
    thresholds: Mapping[str, Mapping[str, Value]], subject: str
) -> dict[str, Value]:
    """The thresholds that judge one stored row.

    Two keyings meet here and both are right: a goal *figure* is joined at the
    row's own coordinate, because a monthly goal has one value per month, and
    a *record's* field is joined at the bare subject, because a courier has
    one allowance however many months they have worked. Merged rather than
    chosen between, so a ladder may name both.
    """
    at_subject = thresholds.get(subject_of(subject), {})
    at_row = thresholds.get(subject, {})
    if not at_subject:
        return dict(at_row)
    return {**at_subject, **at_row}


def _subject_fields_in(e: Any) -> set[tuple[str, str]]:
    """Every `<kind>.<field>` a ladder reads off the subject's record."""
    found: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, SubjectField):
            found.add((node.kind, node.field))
        elif isinstance(node, Ladder):
            for rung in node.rungs:
                walk(rung.left)
                walk(rung.then)
                if rung.right is not None:
                    walk(rung.right)
            walk(node.otherwise)
        elif isinstance(node, (Arith, Pick)):
            walk(node.left)
            walk(node.right)

    if e is not None:
        walk(e)
    return found


# ------------------------------------------------------------- figures --


async def serve_figure(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    at_ms: float | None = None,
    subject: str | None = None,
    facts: FactSource | None = None,
) -> Result:
    """One figure's current answer; with `subject`, only that subject's rows.

    The narrowing is a fetch scope, not a second serving path: availability,
    rendering, banding and ordering are the identical code either way, so a
    record page showing one courier's rows cannot disagree with the figure's
    own page showing everyone's. A narrowed answer covers the exact subject
    row and the rows filed under `subject@...` -- the day and dimension cells
    a grained or split figure keeps for one subject.
    """
    at = at_ms if at_ms is not None else now_ms()
    state = await availability(store, library, tenant, plan)
    subjects: list[Subject] = []
    thresholds: dict[str, dict[str, Value]] = {}

    if isinstance(state, Ok):
        thresholds = await band_thresholds(store, library, tenant, plan, facts)
        if subject is None:
            rows = await store.values(tenant, plan.name, plan.version)
        else:
            exact = await store.value(tenant, plan.name, plan.version, subject)
            rows = ([exact] if exact is not None else []) + await store.values_under(
                tenant, plan.name, plan.version, f"{subject}{SEPARATOR}"
            )
        for stored in rows:
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
                    display=format_value(stored.value, plan.unit),
                    # The figure's own band, from its own definition. This used
                    # to be a second figure found by scanning the library for a
                    # `level` one that combined this plan -- so the word came
                    # from a definition this response never named, and the page
                    # showing the formula did not contain it. It is a `band:`
                    # block on the plan now, evaluated here against the value
                    # beside it and the goal figures it names, resolved at this
                    # row's own coordinate.
                    level=_level_word(
                        band_of(plan.band, stored.value, thresholds_for(thresholds, stored.subject))
                    ),
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
        zone=None,
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
    subject: str,
) -> Evidence | None:
    """One stored value's citation, joined back to what it cites.

    Returns `None` when the figure is available and this subject has no row --
    an address that names nothing, which a route turns into a 404 with the
    reason. An unavailable figure answers with its state instead, because an
    empty members list under an Ok state would read as "this value cites
    nothing": a confident claim about a figure the tenant has never run.
    """
    state = await availability(store, library, tenant, plan)
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
        return format_value(values[position], plan.unit)

    display = format_value(stored.value, plan.unit)

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
                            format_value(part.value, below.unit)
                            if part is not None and below is not None
                            else None
                        ),
                        # The cell, split off the storage key the same way the
                        # serving path splits it: labels are frozen per subject,
                        # so without this every season cell of one team reads
                        # identically and the panel cannot be walked.
                        dimension=tail_of(key),
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
        # A sum's or an extreme's amount is its members *as a measure read
        # them*, and a bare roster of records does not lead to it -- "1.5h,
        # citing these two issues" says nothing about what either contributed.
        # Each held record is measured here through the same `measure_of` the
        # pass uses (the same call shape too: a stored figure never carries a
        # clock measure, exactly because the pass reads with no instant), so
        # this is the one implementation, not a second one. Live rather than
        # copied, which is the `Parts` decision at the record grain: a record
        # corrected since the pass visibly disagrees with the stored total,
        # and that disagreement is true. A `list` figure keeps its stored
        # positional values -- they *are* the measurements the value is made
        # of -- and a count measures nothing, so nothing is invented.
        read_through = _measure_read(plan.calculate) if note is None else None
        # Never live for a row that stores its measurements: a `list` row's
        # values are the addends, and when they misalign with the members the
        # note above withholds them -- a live re-read would quietly repair
        # exactly the row the note says cannot be paired. Keyed to the
        # *stored* shape, the same predicate the note reads, so the two can
        # never disagree -- gating on the plan's shape instead would let a
        # legacy list row under a sum-shaped plan serve the note and the
        # repaired pairings in one payload.
        live = (
            library.measures.get(read_through)
            if read_through is not None and not isinstance(stored.value, list)
            else None
        )
        members = []
        for position, key in enumerate(stored.members):
            value = held.get(key)
            title = _field_of(value, name_field)
            url = _field_of(value, url_field)
            shown = measurement(position)
            if shown is None and live is not None and value is not None:
                got = measure_of(live, value, None)
                shown = format_value(got, plan.unit) if got is not None else None
            members.append(
                EvidenceMember(
                    key=key,
                    title=title,
                    url=url,
                    held=value is not None,
                    display=shown,
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
            measure=read_through,
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


def _citing_spaces(plan: FigurePlan, library: Library) -> set[str]:
    """Every fact kind this figure's stored members may be keys of.

    Reads the set the calculation names (every record-set shape carries one)
    and resolves it to the id spaces of the indexes underneath, following
    references. The ladder and arithmetic shapes name no set, so their
    members are the union of everything in `depends` -- which may span two
    id spaces, and a record of EITHER kind can then appear in the citation.
    A rollup has no sets at all and answers empty: its members are stored
    cells, not records."""
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
    return spaces


def _measure_read(calc: Any) -> str | None:
    """The measure a figure's amount reads its members through, or None.

    Named on the evidence payload so a panel can say which definition turns
    the records into the value. A count reads none -- its records *are* the
    amount -- and the ladder/arithmetic shapes read their operands' stored
    rows, which the parts branch already names per row.
    """
    if isinstance(calc, ListOf):
        return calc.measure
    if isinstance(calc, LangSum) and calc.measure is not None:
        return calc.measure
    if isinstance(calc, Extreme):
        return calc.measure
    return None


def _cited_kind(plan: FigurePlan, library: Library) -> str | None:
    """The one fact kind a figure's members are keys of, or None.

    The evidence panel's question, not the record page's: joining titles is
    only honest when every member lives in one table, which is exactly when
    the spaces reduce to one.
    """
    spaces = _citing_spaces(plan, library)
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
    windows: Sequence[int | str | WindowSpec],
    at_ms: float | None = None,
    at_day: str | None = None,
    facts: FactSource | None = None,
) -> Result:
    """One request serves several windows from a single fetch.

    A window is a **span of positions in the source figure's own bucket
    sequence**, counted back from the anchor -- the trailing `30`, the
    offset `31-60`, or the per-bucket run `each:1-12` -- and it is an
    *argument*: it narrows which stored buckets take part and may never
    change the calculation. What one bucket is -- a day, a month, the first
    Monday of each month -- is the figure's group clause, declared and
    hashed there. Every declared rule applies per span unchanged: the floor
    withholds a thin span's statistics with the reasons named, the band
    bands each span, the series returns each span's own points.

    `at_day` is a caller's anchor date: bucket 1 becomes the bucket that
    local day's end falls in instead of now. It is an *argument*, never
    part of the version -- and the buckets it resolved to travel back on
    the result's `at` and each window's bounds as provenance. Resolved here
    rather than by the caller, because "the end of that day" only means
    something in the reading's own zone, and only this function knows it.

    A spec past the reach ceiling raises `WindowError`, which the HTTP door
    wears as a 422: a malformed argument is the caller's to fix.
    """
    # `expand_window_args`, not a comprehension over `expand_window_arg`: the
    # window-count ceiling lives in the plural form, and this is a door too --
    # the facade's own `answer(trailing=[...])`, which an embedding host calls
    # directly. It was the one door of four where `make_window_spec` and
    # `refuse_reach` were enforced and the count was not, which is the kind of
    # asymmetry that reads as deliberate and is not.
    specs = list(expand_window_args(windows))
    seen_tokens: set[str] = set()
    for spec in specs:
        token = window_token(spec)
        if token in seen_tokens:
            # The bundle grammar's rule, applied at the request door: the
            # same span twice is the same answer twice, and -- since every
            # span is a walk over stored points -- a repeatable parameter
            # with no duplicate check is a cost multiplier the reach
            # ceiling cannot see. Canonical tokens, so `30` and `1-30`
            # collide the way they hash -- and an `each` expansion collides
            # with the enumerated windows it is sugar for.
            raise WindowError(
                f'the window list names "{token}" twice. One request serves each '
                "window once."
            )
        seen_tokens.add(token)
    source = library.figure(plan.source or "")
    if source is None:
        raise ValueError(f"{plan.name} reads a figure that is not in the library")

    state = await availability(store, library, tenant, source)
    # **Every subject's own calendar, and they need not agree.** A window is
    # a span of positions in a subject's bucket sequence, and that sequence
    # was cut on the subject's calendar at write time -- so counting back
    # thirty days covers a different thirty dates for a courier in Tokyo than
    # for one in London. Resolving one span for the board would file both
    # against whichever calendar happened to be asked for, which is the
    # plausible wrong number the calendar moved off a dial to avoid.
    zone = zone_ref(library, source)
    zones = await subject_zones(facts, tenant, zone) if facts is not None else {}
    # The calendars actually in play. One, and the response can speak in it;
    # several, and the only honest top-level answer is UTC with each window
    # carrying its subject's own.
    #
    # A calendar written in the definition is one for everybody by
    # construction, and `subject_zones` builds no map for it -- there is no
    # record to read. Taken from the spec instead, so a board that shares a
    # calendar still says which, and every window is cut in it rather than
    # in UTC.
    written = zone.named if zone is not None else None
    if written is not None:
        in_play: list[str | None] = [written]
    else:
        in_play = list(sorted(set(zones.values()))) or [None]
    shared = in_play[0] if len(in_play) == 1 else None
    if at_day is not None:
        at = end_of_day_ms(at_day, shared)
    else:
        at = at_ms if at_ms is not None else now_ms()
    # **A caller's `?at=` may narrow what is reported and must never move
    # what is stored.** Handed an anchor in 2031 the resolver counts back
    # from there quite correctly, making every month between now and then a
    # bucket "in the past" -- and a lazy fill would write them all, and they
    # would stay, because no later pass removes a bucket the anchors still
    # justify. Months that have not happened would hold confident values for
    # ever, on the strength of one query string. `at_ms` is not clamped: it
    # is the embedding host's own clock rather than a request parameter, and
    # it is what makes "two months pass with no sync" testable at all -- and
    # for the same reason it is what the clamp is *against* where both are
    # given. Clamping a request parameter to the wall clock while the host
    # said its now was somewhere else fills forward to a date the host does
    # not believe in, which is the same fabrication one line up.
    host_now = at_ms if at_ms is not None else now_ms()
    fill_at = min(at, host_now) if at_day is not None else at
    rule = source.grain or "day"
    for spec in specs:
        refusal = refuse_reach(spec, rule)
        if refusal is not None:
            raise WindowError(refusal)
    def spans_in(where: str | None, anchor: float = at) -> list[tuple[WindowSpec, list[str]]]:
        if at_day is not None:
            anchor = end_of_day_ms(at_day, where)
        return [(spec, resolve_span(anchor, where, spec, rule)) for spec in specs]

    # One fetch covers every subject's labels: the bounds are the union
    # across the calendars in play, so a range no subject asked for is never
    # walked.
    covering = [
        label for where in in_play for _, labels in spans_in(where) for label in labels
    ]

    # One fetch serves every window, bounded by the oldest and newest
    # covered labels across all of them: an offset span (`391-400`) ends far
    # behind the anchor, and fetching up to the anchor anyway would walk the
    # whole offset -- the very cost the reach ceiling exists to bound -- to
    # serve a ten-day window. Stored labels *are* sequence labels (nothing
    # is regrouped on a read), so the bounds are labels and the store's
    # range scan needs no translation.
    all_labels = covering

    subjects: list[Subject] = []
    if isinstance(state, Ok) and all_labels:
        if source.carried:
            # **Lazy fill on first read.** A pass extends a carried figure to
            # the bucket it ran in, so between passes the present bucket has
            # no row -- and a screen asking for it would be told "never
            # computed" about a value that has demonstrably been in force for
            # months. A read that finds an unmaterialised bucket materialises
            # it, through the same function the pass uses, and then serves it.
            #
            # Legal for exactly one reason: a carried row is deterministic and
            # **time-invariant**. September's answer is the same answer for
            # ever once September exists, so writing it during a read cannot
            # make the store disagree with a pass. Nothing clock-*derived*
            # could be stored from here -- only the question of whether a
            # bucket exists yet is clock-dependent, and that is what the read
            # is answering.
            #
            # Concurrent first-readers race benignly: both compute identical
            # rows, and the insert-or-nothing write lets exactly one of them
            # count as having created each.
            try:
                await _fill(
                    store,
                    source,
                    tenant,
                {
                    subject_of(key)
                    for key in await store.bucket_keys(tenant, source.scope_index or "")
                },
                    at_ms=fill_at,
                    zones=zones if written is None else {},
                    written=written,
                    trigger="read",
                )
            except CarryReachExceeded as refused:
                # The same refusal the window's own reach ceiling gives, and
                # it wears the same clothes: a `WindowError` the HTTP door
                # answers 422 with, because the fix is the caller's or the
                # definition's. Escaping as a bare ValueError made it a 500,
                # which says the server misbehaved.
                raise WindowError(str(refused)) from None
        rows = await store.values_in_range(
            tenant, source.name, source.version, min(all_labels), max(all_labels)
        )
        by_subject: dict[str, list[tuple[str, Value]]] = {}
        names: dict[str, str] = {}
        for stored in rows:
            base = subject_of(stored.subject)
            label = stored.subject.split(SEPARATOR, 1)[1]
            by_subject.setdefault(base, []).append((label, stored.value))
            names.setdefault(base, stored.label)

        # The goals this reading's band compares against, over the identical
        # buckets. Fetched by the same range and reduced by the same statistic
        # below, so the window's total is judged against the total of the goal
        # across those buckets rather than against one bucket's worth of it.
        goals = await _band_goals(
            store, library, tenant, plan, min(all_labels), max(all_labels)
        )

        for base, held in sorted(by_subject.items()):
            served: list[Window] = []
            # A calendar written in the definition applies to every subject,
            # so it is the window's too -- taken here as well as at the top,
            # or the heading would say Auckland while the bounds were cut in
            # UTC.
            where = written or (zones.get(base) if zones else None)
            for spec, labels in spans_in(where):
                covered = set(labels)
                inside = [(d, v) for d, v in held if d in covered]
                sample = sample_over(inside, labels)  # type: ignore[arg-type]
                against = {
                    name: threshold_of(
                        plan.band_on or "mean",
                        sample,
                        sample_over(
                            [
                                (d, v)  # type: ignore[misc]  # a goal stores numbers
                                for d, v in per_subject.get(base, [])
                                if d in covered
                            ],
                            labels,
                        ),
                    )
                    for name, per_subject in goals.items()
                }
                served.append(
                    _window(plan, sample, spec, labels, rule, where, against)
                )
            subjects.append(
                Subject(
                    id=base,
                    name=names.get(base, base),
                    windows=served,
                    level=served[0].level if served else "unknown",
                )
            )

    # The empty subject is what somebody with nothing looks like. It has no
    # record of its own, so there is no calendar to read off one -- but where
    # the board *has* one answer, that is its answer too: cut in UTC while
    # every real row was cut in Kiritimati, its bounds would disagree with
    # every row beside it under a heading naming the calendar it did not use.
    # Where subjects disagree there is no such answer, and UTC labelled as
    # UTC is honest rather than borrowing whichever subject sorted first.
    empty = Subject(
        id="",
        name="",
        windows=[
            _window(plan, sample_over([], labels), spec, labels, rule, shared)
            for spec, labels in spans_in(shared)
        ],
    )

    return Result(
        kind="reading",
        name=plan.name,
        version=plan.version,
        at=_iso(at),
        # One calendar on the response only where every subject shares it.
        # Mixed, the honest answer is none at the top and each window's own
        # underneath -- a single zone printed over rows cut three different
        # ways would be a heading that lies about two thirds of them.
        zone=shared,
        unit=_unit(plan.unit),
        label=_label_of(plan.name),
        doc=plan.doc,
        state=state,
        banded=plan.band is not None,
        statistics=_statistic_keys(plan),
        banded_on=_banded_on(plan),
        subjects=subjects,
        empty=empty,
    )


async def _band_goals(
    store: EngineStore,
    library: Library,
    tenant: str,
    plan: ReadingPlan,
    frm: str,
    to: str,
) -> dict[str, dict[str, list[tuple[str, Value]]]]:
    """Every goal figure a reading's band names, by figure and by subject.

    One range fetch per goal, over the same label bounds the reading itself
    fetched -- the checker has already refused a goal cut at a different grain,
    so its labels are drawn from the same sequence and the window's `covered`
    set selects the same buckets on both sides.

    A goal that is not servable yet contributes nothing, and every window it
    would have judged reads `unknown` rather than falling to the comfortable
    rung.
    """
    out: dict[str, dict[str, list[tuple[str, Value]]]] = {}
    for name in plan.band_reads:
        goal = library.figure(name)
        if goal is None:  # pragma: no cover - the checker resolved it
            continue
        if not isinstance(await availability(store, library, tenant, goal), Ok):
            continue
        held: dict[str, list[tuple[str, Value]]] = {}
        for stored in await store.values_in_range(tenant, goal.name, goal.version, frm, to):
            if SEPARATOR not in stored.subject:  # pragma: no cover - grain is checked
                continue
            base, label = stored.subject.split(SEPARATOR, 1)
            held.setdefault(base, []).append((label, stored.value))
        out[name] = held
    return out


def _statistic_keys(plan: ReadingPlan) -> list[str]:
    """The declared statistics, spelled the way the wire spells them.

    `sum` fills the `total` field (see `statistics_of`), so the declared list
    must say `total` -- a column headed by a key the display map never uses
    would dash every row it draws."""
    return [{"sum": "total"}.get(s.fn, s.fn) for s in plan.calculate]


def _banded_on(plan: ReadingPlan) -> str | None:
    """Which statistic's column the band word belongs beside -- the `on`
    clause's statistic, the mean when unwritten, exactly as `level_of`
    reads it. One translation, kept beside the other, so the wire cannot
    name a column the banding never judged."""
    if plan.band is None:
        return None
    which = plan.band_on or "mean"
    return {"sum": "total"}.get(which, which)


def _window(
    plan: ReadingPlan,
    sample: Sample,
    spec: WindowSpec,
    labels: Sequence[str],
    rule: str,
    zone: str | None,
    thresholds: Mapping[str, float | None] = {},
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
    points = series_of(sample) if wants_series and not unmet else None
    # Withheld with everything else when a requirement falls short: a trend
    # over a sample too thin to state a mean of is the same claim wearing a
    # different shape, and "every statistic is withheld together" is the
    # bargain the floor exists to keep.
    changes = (
        delta_of(sample)
        if any(s.fn == "delta" for s in plan.calculate) and not unmet
        else None
    )
    # `count` is a tally whatever the source measures, so it renders as a plain
    # number rather than through the reading's own unit -- otherwise a queue of
    # three prints as three seconds.
    rendered = {
        key: format_value(value, "count" if key == "count" else plan.unit)
        for key, value in stats.items()
        if value is not None
    }
    return Window(
        span=span_text(spec),
        bucket=rule,
        # `trailing` keeps meaning exactly what it always has -- the last N
        # days -- and is absent for any span that is not that: an offset
        # bucket, or a month span, wearing a trailing-looking number is the
        # lie this field must not tell.
        trailing=spec.last if rule == "day" and spec.first == 1 else None,
        # None, never "", when the span resolved to no bucket at all -- which
        # happens only where the calendar runs out (an anchor in year 1, a
        # span reaching past it). An empty string in a date field is a value
        # that renders and sorts, and "no bucket" is an absence: rule 3's
        # distinction, at the one edge that produces it.
        frm=labels[0] if labels else None,
        to=labels[-1] if labels else None,
        # A selective rule's covered buckets are not contiguous, so the
        # edges alone would claim days no bucket covers: the full list is
        # the honest shape, and only there -- for contiguous rules it would
        # repeat what the edges already say, per window, per subject.
        buckets=list(labels) if ordinal_rule_of(rule) is not None else None,
        zone=zone,
        mean=stats.get("mean"),
        median=stats.get("median"),
        worst=stats.get("worst"),
        total=stats.get("total"),
        count=stats.get("count"),
        series=points,
        delta=changes,
        delta_display=(
            [
                None if v is None else format_value(v, plan.unit)
                for v in changes
            ]
            if changes is not None
            else None
        ),
        series_scale=_series_scale(points) if points is not None else None,
        display=rendered,
        sample=len(sample.values),
        buckets_covered=sample.buckets_covered,
        buckets_requested=sample.buckets_requested,
        level="unknown" if unmet else _level_word_from(level_of(plan, stats, thresholds)),
        unmet=unmet,
    )


def _series_scale(points: list[float | None]) -> list[float | None]:
    """Each point as a fraction of the window's own peak -- the bar heights,
    served. Computed here because deriving them in a browser is a maximum
    and a share, and this engine's one promise is that no client ever has
    to make either. A window whose peak is not positive scales to noughts:
    a zero drawn at zero height, never a bar invented to look alive."""
    peak = max((p for p in points if p is not None), default=0.0)
    if peak <= 0:
        return [None if p is None else 0.0 for p in points]
    return [None if p is None else p / peak for p in points]


def _level_word_from(word: str) -> Level:
    """Preserve the band word from level_of unchanged.

    The engine generates good/watch/poor from band clauses, or returns the word
    from a when ladder. Both flow through unchanged.
    """
    return word


# --------------------------------------------------------- projections --


def serve_projection(
    plan: ProjectPlan,
    rows: Sequence[ProjectedRow],
    summary_plan: SummarisePlan | None,
    summary: Summary | None,
    at_ms: float,
    state: Ok | Unavailable,
) -> Result:
    subjects = [
        Subject(
            id=row.id,
            name=str(row.values.get("key") or row.values.get("name") or row.id),
            row=_row(row.values, row.units, row.flags),
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
            _row(summary.values, summary.units, summary.flags)
            if summary is not None
            else None
        ),
    )


def _row(
    values: Mapping[str, Value],
    units: Mapping[str, str],
    flags: Sequence[RenderedFlag],
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
            k: format_value(v, _unit(units.get(k, "count")))
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
    at_ms: float | None = None,
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
    at = at_ms if at_ms is not None else now_ms()
    rows, state, _missing = await project_rows(
        store, facts, library, tenant, plan, at
    )
    return _compose_projection(library, plan, rows, state, at)


def _compose_projection(
    library: Library,
    plan: ProjectPlan,
    rows: list[ProjectedRow],
    state: Ok | Unavailable,
    at: float,
) -> Result:
    """Summarise, then page -- the tail of `answer_projection`, split out so a
    bundle can serve a projection member from rows it already evaluated
    without re-reading (or, worse, re-ordering) anything."""
    from .project import ordered, summarise

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
        summarise(summary_plan, rows, at)
        if summary_plan is not None and isinstance(state, Ok)
        else None
    )
    shown = ordered(plan, rows)
    if plan.limit is not None:
        shown = shown[: plan.limit]
    return serve_projection(plan, shown, summary_plan, summary, at, state)


async def project_rows(
    store: EngineStore,
    facts: Any,
    library: Library,
    tenant: str,
    plan: ProjectPlan,
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
        state = await availability(store, library, tenant, source)
        if not isinstance(state, Ok):
            missing.append(figure_name)
            continue
        # A `band of X` column words X by X's own definition, so it needs the
        # goals X compares against -- resolved once for the whole projection
        # rather than per row, since every row reads the same figure.
        against = (
            await band_thresholds(store, library, tenant, source, facts) if band else {}
        )
        for stored in await store.values(tenant, source.name, source.version):
            # The band is derived here rather than read: it is evaluated from
            # the value beside it and the goals its definition names, so a
            # projection binding a band costs no extra query and cannot be
            # stale against a threshold the way a stored one was.
            values.setdefault(stored.subject, {})[(figure_name, band)] = (
                band_of(source.band, stored.value, thresholds_for(against, stored.subject))
                if band
                else stored.value
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


# -------------------------------------------------------------- bundles --


def serve_summary(
    plan: SummarisePlan,
    summary: Summary | None,
    at_ms: float,
    state: Ok | Unavailable,
) -> Result:
    """A summary alone: the one row about the population, without the rows.

    The new serving capability a bundle adds -- everywhere else a summary
    travels on its projection's result. What must not change with the rows
    staying home is what the row is *about*: it is computed over ALL the
    projection's rows (the caller hands in a `Summary` built from them),
    because a summary of a page is a wrong number that reads right.

    `subjects` stays empty and `summary` carries the row -- the same two
    fields a projection result uses, so a reader of one shape has read the
    other. A withheld summary (`state` not ok) travels as `None` beside the
    reason: an absence, never a zero.
    """
    return Result(
        kind="summary",
        name=plan.name,
        version=plan.version,
        at=_iso(at_ms),
        unit="count",
        label=_label_of(plan.name),
        doc=plan.doc,
        state=state,
        banded=False,
        summary=(
            _row(summary.values, summary.units, summary.flags)
            if summary is not None
            else None
        ),
    )


async def answer_bundle(
    store: EngineStore,
    facts: Any,
    library: Library,
    tenant: str,
    plan: BundlePlan,
    default_trailing: Sequence[int],
) -> BundleResult:
    """A bundle, whole: every member's ordinary answer, in declaration order,
    at one instant.

    A bundle defines no calculation, so this function composes and computes
    nothing: each member is served by the same code that serves it alone,
    and the wrapper adds only the name, the hash and the order. Two things
    are deliberate here:

    - **One clock.** `at` is read once and handed to every member that takes
      an instant, extending the projection rule -- one instant reaches every
      row -- to the tile: a page beside a headline evaluated at two different
      moments can disagree with itself. There is deliberately no anchor
      parameter: an anchor moves only a reading's windows, and a tile whose
      reading sat in June beside a page served as it stands would disagree
      with itself under a wrapper claiming one clock -- the facade refuses
      the request instead.
    - **A shared projection is read and projected once.** When the bundle
      names a summary and the projection it is over, the records are read
      and the rows built once, and both members are served from them -- so
      the two cannot disagree about the population.
    """
    from .project import summarise

    at = now_ms()
    evaluated: dict[str, tuple[list[ProjectedRow], Ok | Unavailable]] = {}

    async def rows_of(name: str) -> tuple[list[ProjectedRow], Ok | Unavailable]:
        held = evaluated.get(name)
        if held is None:
            projection = library.projection(name)
            if projection is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{plan.name} names a projection that is not compiled")
            rows, state, _missing = await project_rows(
                store, facts, library, tenant, projection, at
            )
            held = (rows, state)
            evaluated[name] = held
        return held

    results: list[BundleMemberResult] = []

    def slotted(slot: str, result: Result) -> BundleMemberResult:
        # The slot travels beside the member's ordinary Result, never inside
        # it: the answer keeps its own definition's label and doc, because
        # nothing may let a bundle rename what a number is called.
        return BundleMemberResult(slot=slot, result=result)

    for member in plan.members:
        if member.kind == "figure":
            figure = library.figure(member.name)
            if figure is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{plan.name} names a figure that is not compiled")
            results.append(
                slotted(
                    member.slot,
                    await serve_figure(
                        store, library, tenant, figure, at_ms=at, facts=facts
                    ),
                )
            )
        elif member.kind == "reading":
            reading = library.reading(member.name)
            if reading is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{plan.name} names a reading that is not compiled")
            if reading.mode == "live":
                # The same refusal the reading's own route gives. Serving the
                # rest of the tile around a silently dropped member would be
                # a response quietly shorter than its definition.
                raise NotImplementedError("live readings are not servable yet")
            results.append(
                slotted(
                    member.slot,
                    await serve_reading(
                        store,
                        library,
                        tenant,
                        reading,
                        list(member.windows)
                        if member.windows is not None
                        else list(default_trailing),
                        at_ms=at,
                        facts=facts,
                    ),
                )
            )
        elif member.kind == "projection":
            projection = library.projection(member.name)
            if projection is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{plan.name} names a projection that is not compiled")
            rows, state = await rows_of(member.name)
            results.append(
                slotted(
                    member.slot,
                    _compose_projection(library, projection, rows, state, at),
                )
            )
        else:  # summary
            summary_plan = library.summary(member.name)
            if summary_plan is None:  # pragma: no cover - the checker refuses this
                raise ValueError(f"{plan.name} names a summary that is not compiled")
            rows, state = await rows_of(summary_plan.over)
            summary = (
                summarise(summary_plan, rows, at)
                if isinstance(state, Ok)
                else None
            )
            results.append(
                slotted(
                    member.slot,
                    serve_summary(summary_plan, summary, at, state),
                )
            )

    return BundleResult(
        name=plan.name,
        version=plan.version,
        at=_iso(at),
        label=_label_of(plan.name),
        doc=plan.doc,
        results=results,
    )


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
