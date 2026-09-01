"""Evaluating a figure: the only implementation there is.

v1 compiled each definition into a generated function *and* kept an interpreter
beside it as a test oracle, with a test forcing the two to agree. Its own header
called that "a deliberate second implementation, which is normally the thing to
avoid". This is the interpreter, and there is no other -- so the thing the two
could disagree about no longer exists.

Pure and synchronous. Everything arrives through reader callbacks, which is not
an accident of structure: it is what makes the calculation testable without a
database, and it is why the same code can answer "what is this number" and
"which records is it made of" without a second query.

**Membership is the only thing evaluated here. No record is ever loaded**,
because `depends` cannot narrow by record contents -- so a figure's value is
decided entirely by which ids are in which sets.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import assert_never

from ..lang.ast import (
    SECONDS_PER,
    Arith,
    BucketAll,
    BucketScope,
    BucketStat,
    CalcExpr,
    Comparison,
    Coord,
    Count,
    DaysBetween,
    Extreme,
    FieldPick,
    FieldTotal,
    FigureRef,
    Ladder,
    ListOf,
    Number,
    Part,
    Pick,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    Setting,
    SubjectField,
    Sum,
    Text,
)
from ..lang.plan import FigurePlan, Value
from .buckets import SEPARATOR

BucketReader = Callable[[str, str | None], frozenset[str]]
"""(index name, bucket key or None for the single bucket) -> the ids in it."""

MeasureReader = Callable[[str, str], float | None]
"""(measure name, record id) -> its number, or None when the record cannot be
read. A record that cannot be measured stays in the evidence and takes no part
in the arithmetic."""

SettingReader = Callable[[str], float]

MomentReader = Callable[[str, str], float | None]
"""(measure name, record id) -> an epoch in milliseconds."""

SubjectFieldReader = Callable[[str, str, str], float | None]
"""(fact kind, field, subject key) -> the number that subject's record holds.

The subject's *own* record, which is why the key is the subject rather than a
member: a threshold on a courier is read for the courier the value is about.
"""

FieldReader = Callable[[str, str, str], float | None]
"""(fact kind, field path, record id) -> the number the record carries there.

A *declared field*, read straight off the record rather than through a
measure. The measure layer exists to say what a number means; where the field
already is the value -- an on-change record's `value` -- a measure would be a
second name for one field, so the calculation reads it directly and the
figure declares the unit, because nothing can derive it."""

InstantReader = Callable[[str, str, str], float | None]
"""(fact kind, field path, record id) -> an epoch in milliseconds.

Used for the *ordering* of a field read, never for its value: which record in
a bucket is the latest is decided by the field the group truncates on."""


@dataclass(frozen=True)
class Parts:
    """What a rollup reads: its source's values, and the addresses they are
    stored under.

    Addresses rather than copies, deliberately. A total and the parts printed
    under it can then visibly disagree when a part is corrected after the total
    was computed -- which is true, and looks like a bug. The alternative is
    copying the values onto the total when it is written, which would agree with
    a number that had stopped being right.
    """

    values: tuple[float, ...]
    subjects: tuple[str, ...]


PartReader = Callable[[str, str], Parts]
"""(figure name, subject) -> the parts stored under it."""


@dataclass(frozen=True)
class Result:
    value: Value
    members: tuple[str, ...]
    """The evidence: the record ids the value was computed from, in a stable
    order. For a `list` figure the members line up positionally with the
    values, which is what lets a screen show which record each number came
    from."""


class Readers:
    def __init__(
        self,
        buckets: BucketReader,
        measures: MeasureReader,
        moments: MomentReader,
        parts: PartReader,
        settings: SettingReader,
        fields: FieldReader | None = None,
        instants: InstantReader | None = None,
        subject_fields: SubjectFieldReader | None = None,
    ) -> None:
        self.buckets = buckets
        self.measures = measures
        self.moments = moments
        self.parts = parts
        self.settings = settings
        self.fields = fields or (lambda kind, path, member: None)
        self.instants = instants or (lambda kind, path, member: None)
        self.subject_fields = subject_fields or (lambda kind, field, subject: None)


def evaluate(plan: FigurePlan, subject: str, readers: Readers) -> Result:
    """One subject's value, and the evidence behind it."""
    resolved: dict[str, frozenset[str]] = {}
    for name, expr in plan.sets.items():
        resolved[name] = _resolve(expr, subject, resolved, readers)

    members = _members_of(plan, resolved, subject, readers)
    value = _eval(plan.calculate, plan, subject, resolved, readers)
    return Result(value=value, members=members)


def _members_of(
    plan: FigurePlan,
    resolved: dict[str, frozenset[str]],
    subject: str,
    readers: Readers,
) -> tuple[str, ...]:
    """The evidence, in the order the values are in.

    For a `list` figure the order is load-bearing: the stored values and the
    stored members are read back positionally, so a screen can say which record
    produced which number. Sorted, because a set has no order and an unstable
    one would rewrite the evidence on every sync while changing no value.
    """
    e = plan.calculate
    if isinstance(e, ListOf):
        ids = sorted(resolved.get(e.set, frozenset()))
        return tuple(i for i in ids if readers.measures(e.measure, i) is not None)
    if isinstance(e, Count):
        return tuple(sorted(resolved.get(e.set, frozenset())))
    if isinstance(e, Sum) and e.measure is not None:
        return tuple(sorted(resolved.get(e.set, frozenset())))
    if isinstance(e, Extreme):
        return tuple(sorted(resolved.get(e.set, frozenset())))
    if isinstance(e, BucketStat):
        # Only the records that took part: one the measure could not read is
        # not evidence for a number it contributed nothing to.
        return tuple(
            member
            for member in sorted(resolved.get(e.set, frozenset()))
            if readers.measures(e.measure, member) is not None
        )
    if isinstance(e, FieldTotal):
        # Only the records that contributed: one the field is blank on added
        # nothing, and citing it would send a reader to a record with no part
        # in the number.
        return tuple(
            member
            for member in sorted(resolved.get(e.set, frozenset()))
            if readers.fields(e.kind, e.field, member) is not None
        )
    if isinstance(e, FieldPick):
        # **The one record, not the whole bucket.** A field read's answer came
        # from exactly one change, and that change -- its value, when it was
        # made and by whom -- is what a reader asking "why 25 in September?"
        # has to be shown. Citing every record in the bucket would bury the
        # answer among the ones that lost.
        winner = _picked(e, resolved, readers)
        return () if winner is None else (winner,)
    if isinstance(e, Sum) and e.measure is None:
        source, _ = plan.combines[e.set]
        return readers.parts(source, subject).subjects
    if isinstance(e, (Part, Coord)):
        source, _ = plan.combines[e.name]
        return readers.parts(source, subject).subjects
    # A ladder or arithmetic over combined figures: the evidence is whatever the
    # sources were stored under, so a reader can take the next hop.
    out: list[str] = []
    for source, _ in plan.combines.values():
        out.extend(readers.parts(source, subject).subjects)
    if out:
        return tuple(sorted(set(out)))
    return tuple(sorted({m for s in resolved.values() for m in s}))


# ----------------------------------------------------------------- sets --


def _resolve(
    expr: SetExpr,
    subject: str,
    defined: dict[str, frozenset[str]],
    readers: Readers,
) -> frozenset[str]:
    if isinstance(expr, SetIndex):
        if isinstance(expr.bucket, BucketScope):
            return readers.buckets(expr.index, subject)
        if isinstance(expr.bucket, BucketAll):
            return readers.buckets(expr.index, None)
        assert_never(expr.bucket)
    if isinstance(expr, SetRef):
        return defined.get(expr.name, frozenset())
    if isinstance(expr, SetOp):
        left = _resolve(expr.left, subject, defined, readers)
        right = _resolve(expr.right, subject, defined, readers)
        if expr.op == "intersect":
            return left & right
        if expr.op == "union":
            return left | right
        if expr.op == "difference":
            return left - right
        assert_never(expr.op)
    assert_never(expr)


# ------------------------------------------------------------ evaluation --


def _eval(
    e: CalcExpr,
    plan: FigurePlan,
    subject: str,
    sets: dict[str, frozenset[str]],
    readers: Readers,
) -> Value:
    if isinstance(e, Count):
        return float(len(sets.get(e.set, frozenset())))

    if isinstance(e, ListOf):
        out: list[float | None] = []
        for member in sorted(sets.get(e.set, frozenset())):
            got = readers.measures(e.measure, member)
            if got is not None:
                out.append(got)
        return out

    if isinstance(e, Sum):
        if e.measure is not None:
            # A record the measure cannot read counts as nothing, which is the
            # same reading a roadmap takes of an unsized story: the total is the
            # size of the *known* work.
            total = 0.0
            for member in sets.get(e.set, frozenset()):
                got = readers.measures(e.measure, member)
                if got is not None:
                    total += got
            return total
        parts = readers.parts(*_source_of(plan, e.set), subject)
        return float(sum(parts.values))

    if isinstance(e, Part):
        parts = readers.parts(*_source_of(plan, e.name), subject)
        return _scalar(parts)

    if isinstance(e, Number):
        return e.value

    if isinstance(e, Text):
        return e.value

    if isinstance(e, Setting):
        return readers.settings(e.path)

    if isinstance(e, Ladder):
        for rung in e.rungs:
            left = _eval(rung.left, plan, subject, sets, readers)
            right = (
                _eval(rung.right, plan, subject, sets, readers) if rung.right is not None else None
            )
            verdict = _compare(left, rung.op, right)
            if verdict is None:
                # **A ladder stops on an unknown rather than falling through.**
                # `otherwise` is the bottom of the band, and banding somebody the
                # engine has never computed as comfortable is the confident wrong
                # answer everything here is arranged around avoiding.
                return None
            if verdict:
                return _eval(rung.then, plan, subject, sets, readers)
        return _eval(e.otherwise, plan, subject, sets, readers)

    if isinstance(e, Arith):
        left = _eval(e.left, plan, subject, sets, readers)
        right = _eval(e.right, plan, subject, sets, readers)
        return _arith(e.op, left, right)

    if isinstance(e, Pick):
        left = _eval(e.left, plan, subject, sets, readers)
        right = _eval(e.right, plan, subject, sets, readers)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            # **An absence propagates rather than losing.** The tempting reading
            # -- the larger of a known and an unknown is at least the known one
            # -- is sound about data and wrong about this engine: a missing value
            # means *not computed*, never "the subject has none of it". With an
            # epic's own estimate merely uncomputed, max(breakdown, own) would
            # answer the breakdown, so an epic sized well above its stories would
            # report a commitment too small and every share divided by it reads
            # high.
            return None
        return max(left, right) if e.which == "max" else min(left, right)

    if isinstance(e, Extreme):
        instants: list[float] = []
        for member in sets.get(e.set, frozenset()):
            got = readers.moments(e.measure, member)
            # A record whose timestamp cannot be read is **skipped, not counted
            # as nought**. Nought is a real instant: an epic all of whose
            # children carry an unreadable timestamp would otherwise read as
            # untouched since 1970.
            if got is not None:
                instants.append(got)
        if not instants:
            # The empty population answers nothing for the same reason.
            return None
        return max(instants) if e.which == "latest" else min(instants)

    if isinstance(e, BucketStat):
        # The statistic over the bucket's own records. A record the measure
        # cannot read takes no part -- the same skip `list` makes, and for the
        # same reason: it is a record nobody measured, not a record measuring
        # nought.
        #
        # The empty bucket answers **nothing**, never a nought. A month in
        # which nothing finished has no median job length, and a nought there
        # is a confident claim that everything was instantaneous.
        sample = [
            value
            for member in sorted(sets.get(e.set, frozenset()))
            if (value := readers.measures(e.measure, member)) is not None
        ]
        if not sample:
            return None
        if e.fn == "mean":
            return statistics.fmean(sample)
        if e.fn == "median":
            return statistics.median(sample)
        # "Worst" is the largest for every quantity this language stores --
        # each is a duration or a tally of something, and more is worse.
        return float(max(sample))

    if isinstance(e, Coord):
        # **Join by bucket key, never by position.** The subject a bucketed
        # figure is evaluated for already *is* a coordinate -- `s1@2026-06` --
        # so reading the source at the same coordinate is a lookup under the
        # same key, and misalignment is not merely unlikely but
        # unrepresentable.
        #
        # A coordinate the source holds no row for answers **nothing**. Not a
        # nought, which a band would happily colour comfortable, and not a
        # shift -- which is what a positional zip does the moment one source
        # starts a month later than the other: every value paired with the
        # wrong month, plausibly, for ever.
        parts = readers.parts(*_source_of(plan, e.name), subject)
        return _scalar(parts)

    if isinstance(e, FieldPick):
        # The value the most recent record in the bucket set it to. "Most
        # recent" is decided by the field the group truncates on, so the
        # ordering and the bucketing cannot disagree about when a change
        # happened.
        #
        # A record whose ordering instant cannot be read takes no part --
        # skipped rather than sorted to the bottom, for the reason `Extreme`
        # skips one: nought is a real instant, and a record with an
        # unreadable stamp would otherwise win every `earliest` for ever.
        # The empty set answers **nothing**, never a nought: a bucket nobody
        # changed anything in has no value of its own, which is precisely
        # what `carried forward` then fills from the bucket before it.
        winner = _picked(e, sets, readers)
        if winner is None:
            return None
        return readers.fields(e.kind, e.field, winner)

    if isinstance(e, DaysBetween):  # pragma: no cover - refused for a figure
        raise AssertionError(
            "a span reads the clock and the checker refuses it to a figure; this is a "
            "projection construct and should never reach here"
        )
    if isinstance(e, FieldTotal):
        # An unreadable member contributes nothing and is not an absence: the
        # same rule a measure sum keeps, because a record the field is blank
        # on carries no weight, literally.
        members = sets.get(e.set, frozenset())
        held = [readers.fields(e.kind, e.field, member) for member in sorted(members)]
        return float(sum(v for v in held if v is not None))
    if isinstance(e, SubjectField):
        return readers.subject_fields(e.kind, e.field, subject.split(SEPARATOR, 1)[0])
    if isinstance(e, FigureRef):  # pragma: no cover - band-only
        raise AssertionError(
            "a figure named outright is a band's threshold, evaluated by `band_of` "
            "against the goals resolved for the row; it never reaches the calculation"
        )

    assert_never(e)


def _picked(
    e: FieldPick, sets: dict[str, frozenset[str]], readers: Readers
) -> str | None:
    """Which record in the set answers -- the whole of what `latest` means
    here, in one place.

    Shared by the value and by the evidence deliberately. The evidence for a
    field read is the *one record the value came from*, and a second walk to
    find it could disagree with the first -- which would print "25, set on
    3 June by X" beside a number taken from a different row, the most
    convincing kind of wrong.

    A record whose ordering instant cannot be read takes no part: skipped
    rather than sorted to one end, for the reason `Extreme` skips one --
    nought is a real instant, and an unreadable stamp would otherwise win
    every `earliest` for ever.
    """
    best: tuple[float, str] | None = None
    for member in sorted(sets.get(e.set, frozenset())):
        at = readers.instants(e.kind, e.ordered_by, member)
        if at is None:
            continue
        # Ties break on the record key -- arbitrary, but *stable*. Two changes
        # stamped at the same instant are a data problem, and answering them
        # differently on each pass would be a figure that moves with nothing
        # behind it.
        here = (at, member)
        if best is None or (here > best if e.which == "latest" else here < best):
            best = here
    return None if best is None else best[1]


def _source_of(plan: FigurePlan, binding: str) -> tuple[str]:
    figure, _ = plan.combines[binding]
    return (figure,)


def _scalar(parts: Parts) -> Value:
    if not parts.values:
        return None
    if len(parts.values) > 1:
        raise ValueError(
            "a bare read resolved to more than one stored value. The checker refuses a bare "
            "read of a dimensioned figure, so reaching here means the stored values disagree "
            "with the plan -- which must abort rather than silently take the first."
        )
    return parts.values[0]


def _arith(op: str, left: Value, right: Value) -> Value:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    if op == "+":
        return float(left) + float(right)
    if op == "-":
        return float(left) - float(right)
    if op == "*":
        return float(left) * float(right)
    if op == "/":
        # **Division by nought is nothing, never infinity and never nought.**
        # Infinity through a percentage formatter is a catastrophic figure and
        # nought is a confident 0% over an epic with six of seven stories closed.
        if float(right) == 0.0:
            return None
        return float(left) / float(right)
    raise ValueError(f"unknown operator {op}")


def _compare(left: Value, op: Comparison | str, right: Value) -> bool | None:
    """None means "we cannot tell", and a ladder stops on it.

    The two presence tests answer *before* the null guard, because "is there a
    value at all" is never itself unknown -- that is the whole point of them.
    """
    if op == "nothing":
        return left is None
    if op == "something":
        return left is not None
    if left is None or right is None:
        return None
    if isinstance(left, list) or isinstance(right, list):
        return None
    if isinstance(left, str) or isinstance(right, str):
        # Text compares only by equality. Ordering words is exactly the trap the
        # field-type split exists to prevent, so the checker refuses it and this
        # is the belt.
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        return None
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"unknown comparison {op}")


def band_of(
    ladder: Ladder | None, value: Value, thresholds: Mapping[str, Value] = {}
) -> str | None:
    """Which word a figure's value falls under.

    Evaluated here rather than stored, and that is the whole design. A band used
    to be a figure of its own -- a `level`-unit figure combining the one below
    it, found by scanning the library at serve time -- so the word on screen came
    from a definition the page never named, and turning a threshold marked that
    figure pending, rebuilt every subject's value and withheld the band in
    between. All of it to store a word derivable from a number sitting next to
    it.

    `thresholds` carries the figures the ladder compares against, already
    resolved for this row -- by subject, and for a sequenced figure at this
    row's own coordinate, so days meet days and months meet months. Passing
    resolved values rather than a store handle is what keeps this a small
    walker: the only things a band may contain are `value`, literals and other
    figures' answers, and a band that could reach `_eval`'s sets and sources
    would be a second calculation sharing the first one's name.

    **`None` when any operand is `None`, never the bottom rung.** A ladder stops
    on an unknown, and the bottom rung of a band is reliably the comfortable one
    -- banding somebody the engine has never measured as comfortable is the
    confident wrong answer this whole engine is arranged around avoiding. That
    covers the threshold as well as the value: a month before anybody set a goal
    has no goal, and a nought would sit comfortably under every comparison.
    """
    if ladder is None:
        return None
    for rung in ladder.rungs:
        left = _band_operand(rung.left, value, thresholds)
        right = (
            _band_operand(rung.right, value, thresholds) if rung.right is not None else None
        )
        verdict = _compare(left, rung.op, right)
        if verdict is None:
            return None
        if verdict:
            return _band_word(rung.then)
    return _band_word(ladder.otherwise)


def _band_operand(e: CalcExpr, value: Value, thresholds: Mapping[str, Value]) -> Value:
    if isinstance(e, Part):
        # `value`, and the checker has already refused every other name.
        return value
    if isinstance(e, Number):
        return e.value
    if isinstance(e, Text):
        return e.value
    if isinstance(e, (FigureRef, Coord)):
        # A threshold this row has no answer for reads as an absence, which
        # withholds the word above. Never a nought: a goal nobody has set is
        # not a goal of zero, and the difference is the whole band.
        return thresholds.get(e.name)
    if isinstance(e, SubjectField):
        # Same rule one route along: a record with nothing in that field is a
        # subject nobody has set a limit for, and a nought would sit
        # comfortably under every comparison.
        held = thresholds.get(f"{e.kind}.{e.field}")
        if e.scale is None or not isinstance(held, (int, float)):
            return held
        # The record says a number and nothing about what it measures, so the
        # definition said -- and a figure about a span of time stores seconds.
        # A literal's scale is folded at compile time; a field's cannot be,
        # because the number is not there yet.
        return held * SECONDS_PER[e.scale]
    return None


def _band_word(e: CalcExpr) -> str | None:
    # Every rung answers a `Text`; the checker refuses anything else. Returning
    # None rather than asserting keeps a malformed stored plan from taking a
    # whole response down -- an unbanded row is visible, a 500 is not.
    return e.value if isinstance(e, Text) else None


def same_value(a: Value, b: Value) -> bool:
    """Whether two stored values are the same.

    Not `a == b`: a level is a word and `None == None` is true, so a naive
    equality test would suppress **every band change on the board** -- the
    movement from one unknown to another is not a movement, but the movement
    from "warn" to "over" is, and both compare equal under the wrong test if the
    comparison is written as `from == to` over numbers alone.
    """
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and list(a) == list(b)
    return a == b and type(a) is type(b)
