"""Checking and lowering: from what was written to what the engine runs.

Every rule that can be enforced here rather than at evaluation time is, because
a definition that does not compile is a build failure and a definition that
fails at evaluation time is a blank tile in front of a customer.

This is also where the language's one safety property is upheld: **`depends` may
narrow only by set membership**, never by record contents, so what a figure
declares it reads is what it actually reads.

Every refusal in this file names a rule and, wherever it can, says what the
mistake would have *done*. That is not politeness. Almost every rule here exists
because the alternative compiles, runs, and produces a plausible number -- and a
message that only says "invalid" sends somebody looking for a typo.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, NoReturn, assert_never

from ..schema import Schema
from ..windows import (
    WindowSpec,
    refuse_reach,
    usable_zone,
    window_token,
)
from .ast import (
    SECONDS_PER,
    Arith,
    BucketAll,
    BucketScope,
    BucketStat,
    BundleDecl,
    BundleMember,
    ByAge,
    ByComposite,
    ByField,
    ByPredicate,
    ByPresence,
    CalcExpr,
    Condition,
    Coord,
    Count,
    DaysBetween,
    Decl,
    DurationMeasure,
    Extreme,
    FactDecl,
    FactField,
    FieldDecl,
    FieldMeasure,
    FieldPick,
    FieldTotal,
    FigureDecl,
    FigureRef,
    FigureUnit,
    FlagDecl,
    IndexBy,
    IndexDecl,
    IndexField,
    Ladder,
    ListOf,
    MomentMeasure,
    Number,
    Part,
    Pick,
    ProjectDecl,
    ReadingDecl,
    Requirement,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    Setting,
    StatisticFn,
    SubjectField,
    Sum,
    SummariseDecl,
    Text,
    ValueDecl,
)
from .hash import version_of
from .lex import DefinitionError
from .parse import parse
from .plan import (
    BundleMemberPlan,
    BundlePlan,
    CompiledFact,
    CompiledFactField,
    CompiledIndex,
    CompiledMeasure,
    FigurePlan,
    Library,
    ProjectPlan,
    ReadingPlan,
    SummarisePlan,
)


class CheckError(DefinitionError):
    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.message = message
        self.line = line


class WorldConflict(CheckError):
    """The source declares facts and the schema also declares kinds.

    Its own type because one caller has to recognise it: `PUT /definitions`
    retries a conflicted compile against a kind-stripped schema so a live
    schema-taught deployment can adopt facts without blanking its
    definitions first -- and matching on message text would make that repair
    hang off a comma."""


def compile_source(source: str, schema: Schema) -> Library:
    """Check and lower one concatenated source against the host's world.

    The schema is a compile-time input exactly as the source is: which kinds
    exist and which dials each position may name are what half the refusals in
    this file check against. It is deliberately **not** hashed into any
    version -- a version is the hash of a definition's semantics, and the same
    definition under two hosts is the same definition.
    """
    return _Checker(source, schema).run()


# What a `calculate` produces, for the purposes of deciding what may be done
# with it. Not the same as a unit: two calculations can both be numbers and
# differ in what the number means.
_Kind = str  # 'number' | 'text' | 'list' | 'moment'


class _Checker:
    def __init__(self, source: str, schema: Schema) -> None:
        self._source = source
        self._schema = schema
        self._decls: list[Decl] = list(parse(source).decls)
        self.facts: dict[str, CompiledFact] = {}
        self.indexes: dict[str, CompiledIndex] = {}
        self.measures: dict[str, CompiledMeasure] = {}
        self.figures: list[FigurePlan] = []
        self.readings: list[ReadingPlan] = []
        self.projections: list[ProjectPlan] = []
        self.summaries: list[SummarisePlan] = []
        self.bundles: list[BundlePlan] = []
        self._names: dict[str, str] = {}

    # --------------------------------------------------------------- run --

    def run(self) -> Library:
        # Facts first, in a pass of their own and order-free: they are the
        # world every other declaration is checked against, and unlike a
        # figure they can rest on nothing, so there is no cycle to forbid.
        for d in self._decls:
            if isinstance(d, FactDecl):
                self._fact_decl(d)
        self._world()
        self._id_spaces()
        for d in self._decls:
            if isinstance(d, IndexDecl):
                self._index(d)
        for d in self._decls:
            if isinstance(d, (DurationMeasure, FieldMeasure, MomentMeasure)):
                self._measure(d)
        for d in self._decls:
            if isinstance(d, FigureDecl):
                self._figure(d)
        for d in self._decls:
            if isinstance(d, ReadingDecl):
                self._reading(d)
        for d in self._decls:
            if isinstance(d, ProjectDecl):
                self._projection(d)
        for d in self._decls:
            if isinstance(d, SummariseDecl):
                self._summary(d)
        # Bundles last, deliberately: a member may only be one of the seven
        # computing kinds, all checked above, so a bundle may sit anywhere in
        # the source and still resolve -- there is no cycle to order against,
        # because a bundle cannot name a bundle.
        for d in self._decls:
            if isinstance(d, BundleDecl):
                self._bundle(d)
        return Library(
            indexes=self.indexes,
            measures=self.measures,
            figures=tuple(self.figures),
            readings=tuple(self.readings),
            projections=tuple(self.projections),
            summaries=tuple(self.summaries),
            source=self._source,
            facts=self.facts,
            bundles=tuple(self.bundles),
        )

    def _claim(self, name: str, what: str, line: int) -> None:
        """One namespace for all nine declaration kinds -- facts, groups, filters
        and measures included, not just the rendered ones.

        A citation is `name@version`, so two declarations sharing a name would
        make a citation ambiguous -- and the Data screen addresses a definition
        by name alone, so a filter shadowing a figure would put the filter's
        one-liner on the source pane where the figure's formula belongs.
        """
        held = self._names.get(name)
        article = "an" if what[0] in "aeiou" else "a"
        if held is not None:
            raise CheckError(
                f"{name} is already {held}. {article.capitalize()} {what} needs its own "
                'name: a citation is "name@version", and two definitions under one name '
                "make it ambiguous.",
                line,
            )
        self._names[name] = f"{article} {what}"

    def _fact_kind(self, named: str, where: str, line: int) -> str:
        if named not in self._kinds:
            raise CheckError(
                f'{where} "{named}", which is not a fact kind. Those are: '
                f'{", ".join(sorted(self._kinds)) or "none"}.',
                line,
            )
        return named

    # --------------------------------------------------------------- fact --

    def _fact_decl(self, d: FactDecl) -> None:
        self._claim(d.name, "fact", d.line)
        _unique_fields(d.name, d.fields, d.line)
        top = {f.name: f for f in d.fields}
        for pointer, word in ((d.name_field, "name"), (d.url_field, "url")):
            if pointer is None:
                continue
            held = top.get(pointer)
            if held is None:
                # Refused rather than ignored, for the schema's own reason: a
                # pointer at a field that does not exist is a typo, and
                # ignoring it renders raw ids for ever while everything looks
                # configured.
                raise CheckError(
                    f'fact {d.name} says its {word} is "{pointer}", which it does not '
                    f'declare. Declared: {", ".join(sorted(top))}.',
                    d.line,
                )
            if held.type != "text":
                raise CheckError(
                    f'fact {d.name}\'s {word} field "{pointer}" is '
                    f'{"a nested record" if held.type is None else "a " + held.type}, '
                    "and a record is rendered and linked by text.",
                    d.line,
                )
        fields = _compiled_fields(d.fields)
        self.facts[d.name] = CompiledFact(
            name=d.name,
            fields=fields,
            name_field=d.name_field,
            url_field=d.url_field,
            doc=d.doc,
            version=version_of({"name": d.name, "fields": _fact_field_hash(fields)}),
        )

    def _world(self) -> None:
        """Kinds, name fields and url fields, from whichever door taught them.

        One door or the other, never both: a schema with kinds beside a source
        with facts is two declarations of one world, and whichever a reader
        trusted, the other would drift.
        """
        if self.facts and self._schema.kinds:
            first = next(d for d in self._decls if isinstance(d, FactDecl))
            raise WorldConflict(
                "the source declares facts, and the schema also declares kinds. The "
                "world is declared in one place: drop the schema's kinds (and its name "
                "and url fields) -- they derive from the facts.",
                first.line,
            )
        if self.facts:
            self._kinds: frozenset[str] = frozenset(self.facts)
            self._name_fields: dict[str, str] = {
                k: f.name_field for k, f in self.facts.items() if f.name_field is not None
            }
        else:
            self._kinds = self._schema.kinds
            self._name_fields = dict(self._schema.name_fields)

    def _record_field(
        self, kind: str, path: str, what: str, line: int, *, named: bool = False
    ) -> tuple[CompiledFactField, bool] | None:
        """The declared field a path lands on, and whether it crossed a list.

        None in a schema-taught world: no fields were declared, so nothing can
        be checked -- the origin project's specimen tests are the host-side
        stand-in there. In a fact-taught world a path that resolves to nothing
        is a build failure here, because at run time it is a silently empty
        bucket or a column of dashes, for everybody, for ever.

        `named` says the caller's `what` already ends with the dotted name.
        Without it the message doubles -- `reads shop_courier.stale_days reads
        "stale_days"` -- which reads as two different names, in the one
        sentence an author has to find their typo in.
        """
        reads = "" if named else f' reads "{path}"'
        fact = self.facts.get(kind)
        if fact is None:
            return None
        at = {f.name: f for f in fact.fields}
        crossed = False
        found: CompiledFactField | None = None
        segments = path.split(".")
        for i, segment in enumerate(segments):
            found = at.get(segment)
            if found is None:
                level = kind if i == 0 else f'{kind}.{".".join(segments[:i])}'
                raise CheckError(
                    f'{what}{reads}, and "{segment}" is not a field of {level}. '
                    f'Declared there: {", ".join(sorted(at)) or "nothing"}.',
                    line,
                )
            crossed = crossed or found.many
            at = {c.name: c for c in found.children}
        assert found is not None
        if found.type is None:
            raise CheckError(
                f"{what}{reads}, which is a nested record rather than a value. "
                f'Its fields are: {", ".join(sorted(at))}.',
                line,
            )
        return found, crossed

    # ---------------------------------------------------------- id space --

    def _id_spaces(self) -> None:
        """`keyed as` is a property of a fact kind, not of one declaration.

        Every group and filter over one kind must agree about that kind's id
        space, otherwise the guard could be defeated by writing a second
        declaration and leaving the clause off -- which is the quietest
        possible way to lose it.
        """
        claimed: dict[str, tuple[str, int]] = {}
        for d in self._decls:
            if not isinstance(d, IndexDecl) or d.keyed_as is None:
                continue
            word = _decl_word(d.spec)
            self._fact_kind(d.keyed_as, f"{word} {d.name} is keyed as", d.line)
            held = claimed.get(d.kind)
            if held is not None and held[0] != d.keyed_as:
                raise CheckError(
                    f'{word} {d.name} says {d.kind} is keyed as "{d.keyed_as}", but another '
                    f'declaration says "{held[0]}". A fact kind has one id space.',
                    d.line,
                )
            claimed[d.kind] = (d.keyed_as, d.line)
        self._keyed: dict[str, str] = {k: v[0] for k, v in claimed.items()}

    # -------------------------------------------------------------- index --

    def _index(self, d: IndexDecl) -> None:
        word = _decl_word(d.spec)
        self._claim(d.name, word, d.line)
        self._fact_kind(d.kind, f"{word} {d.name} is over", d.line)

        for part in _index_fields(d.spec):
            if part.through is not None:
                self._fact_kind(
                    part.through.kind, f"{word} {d.name} resolves through", d.line
                )
            if part.zone is not None:
                self._check_zone(d, part, word)
        if isinstance(d.spec, ByAge) and d.spec.through is not None:
            self._fact_kind(
                d.spec.through.kind,
                f"{word} {d.name} reads its age threshold from",
                d.line,
            )
            self._record_field(
                d.spec.through.kind,
                d.spec.read or "",
                f"{word} {d.name} reads its age threshold from "
                f"{d.spec.through.kind}.{d.spec.read}",
                d.line,
                named=True,
            )
            self._record_field(
                d.kind,
                d.spec.local or "",
                f"{word} {d.name} joins to its owner through {d.spec.local}",
                d.line,
            )
        self._index_fields_exist(d, word)

        self.indexes[d.name] = CompiledIndex(
            name=d.name,
            kind=d.kind,
            id_space=self._keyed.get(d.kind, d.kind),
            spec=d.spec,
            bucketed=isinstance(d.spec, (ByField, ByComposite)),
            label=d.label,
        )

    def _check_zone(self, d: IndexDecl, part: IndexField, word: str) -> None:
        """Whose calendar cuts this part's buckets, and whether it is a
        calendar this group can actually reach.

        The record is the bucket's **subject** -- the composite's first part,
        resolved through its hop where it has one -- so naming a kind that
        part does not resolve to would look every key up in the wrong table
        and find nothing. Every record would be in no bucket, which reads as a
        board that has collected nothing rather than as a wrong declaration.
        """
        zone = part.zone
        assert zone is not None
        if zone.named is not None:
            # Written in the definition, so it is checked here rather than
            # treated as absent at run time: an unusable word on a record is a
            # fact about one subject, and an unusable word in the source is a
            # figure that answers nothing for anybody.
            if usable_zone(zone.named) is None:
                raise CheckError(
                    f'{word} {d.name} cuts its buckets in "{zone.named}", which is not '
                    "a calendar anybody keeps. Write an IANA name -- `Asia/Tokyo`, "
                    "`America/New_York` -- or read one off the subject's record.",
                    d.line,
                )
            return
        assert zone.kind is not None and zone.field is not None
        self._fact_kind(zone.kind, f"{word} {d.name} reads its calendar from", d.line)
        self._record_field(
            zone.kind,
            zone.field,
            f"{word} {d.name} reads its calendar from {zone.kind}.{zone.field}",
            d.line,
            named=True,
        )
        parts = _index_fields(d.spec)
        subject = parts[0] if parts and parts[0] is not part else None
        if subject is None:
            # No subject part: the record being bucketed is the thing, so the
            # calendar has to be on it.
            if zone.kind != d.kind:
                raise CheckError(
                    f"{word} {d.name} buckets {d.kind} records with no subject part, so "
                    f"the calendar is read off the record itself -- but it names "
                    f"{zone.kind}.{zone.field}. Name a field on {d.kind}, or give the "
                    "group a subject part whose records carry the calendar.",
                    d.line,
                )
            return
        if subject.through is not None and subject.through.kind != zone.kind:
            raise CheckError(
                f"{word} {d.name} fans out by {subject.through.kind} and reads its "
                f"calendar from {zone.kind}.{zone.field}. The calendar is a fact about "
                "the subject, so it has to be a field on the subject's own record -- "
                f"named on {zone.kind}, every key would be looked up in the wrong table "
                "and every record would land in no bucket.",
                d.line,
            )

    def _index_fields_exist(self, d: IndexDecl, word: str) -> None:
        """Every field a group or filter reads, against the declared world.

        Bucketing deliberately crosses lists -- `accounts.account_id` means
        "any account_id of any account" -- so a `many` in the path is fine
        everywhere here except an age filter, which reads *one* instant and
        would otherwise take the first parseable element it met.
        """
        if not self.facts:
            return
        what = f"{word} {d.name}"
        for part in _index_fields(d.spec):
            found = self._record_field(d.kind, part.field, what, d.line)
            assert found is not None
            node, _ = found
            if part.through is not None:
                self._record_field(part.through.kind, part.through.path, what, d.line)
            if (part.truncate is not None or part.select is not None) and node.type != "moment":
                raise CheckError(
                    f"{what} buckets {part.field} by {part.truncate or part.select}, and "
                    f"{part.field} is a {node.type}. A time bucket reads an instant, so "
                    "it needs a moment.",
                    d.line,
                )
        spec = d.spec
        if isinstance(spec, ByAge):
            found = self._record_field(d.kind, spec.field, what, d.line)
            assert found is not None
            node, crossed = found
            if node.type != "moment":
                raise CheckError(
                    f"{what} narrows by the age of {spec.field}, which is a {node.type} "
                    "rather than a moment.",
                    d.line,
                )
            if crossed:
                raise CheckError(
                    f"{what} narrows by the age of {spec.field}, which crosses a list -- "
                    "several instants per record, and an age reads one.",
                    d.line,
                )
        if isinstance(spec, ByPredicate):
            found = self._record_field(d.kind, spec.field, what, d.line)
            assert found is not None
            node, _ = found
            # A bare `true`/`false` and a quoted `"true"` are different claims
            # -- the flag's value versus a word a text field holds -- and only
            # the parser knows which was written, which is why `quoted` rides
            # on the spec. Each refusal below is a predicate that would match
            # nothing (or, with `!=`, everything) for ever, with nothing
            # thrown.
            boolean = not spec.quoted and spec.value in ("true", "false")
            if node.type == "flag" and spec.value not in ("true", "false"):
                raise CheckError(
                    f'{what} compares {spec.field}, a flag, against "{spec.value}". A '
                    "flag holds true or false, so nothing would ever match -- and with "
                    f'"!=" everything would.',
                    d.line,
                )
            if boolean and node.type != "flag":
                raise CheckError(
                    f"{what} compares {spec.field}, a {node.type}, against {spec.value}, "
                    "which is how a flag is tested. Quote it if the field really holds "
                    "that word.",
                    d.line,
                )
            if node.type == "number" and spec.quoted:
                raise CheckError(
                    f'{what} compares {spec.field}, a number, against the quoted '
                    f'"{spec.value}". A number\'s bucket key is the number\'s own '
                    "spelling, so write it bare -- quoted, a stray decimal point is a "
                    "predicate that never matches.",
                    d.line,
                )

    # ------------------------------------------------------------ measure --

    def _measure(self, d: DurationMeasure | FieldMeasure | MomentMeasure) -> None:
        self._claim(d.name, "measure", d.line)
        self._fact_kind(d.kind, f"measure {d.name} is over", d.line)
        # The mirror image of the figure rule below: `sum(<kind>.<name> over
        # set)` and `latest(<kind>.<name> over set)` read a measure where one
        # exists and the record's own field otherwise. A measure winning was
        # documented as the safe order, on the argument that declaring one
        # could never silently change an existing figure -- which is exactly
        # backwards. Declaring `measure shop_order.weight` is what changes an
        # already-written `sum(shop_order.weight over mine)` from the field
        # to the measure, with no edit to the figure and no way to see it in
        # its text. Refused where the collision is made.
        leaf = d.name.split(".", 1)[1] if "." in d.name else d.name
        fact = self.facts.get(d.kind)
        if fact is not None and any(f.name == leaf for f in fact.fields):
            raise CheckError(
                f"measure {d.name} takes a name that {d.kind} already has as a field. "
                "Both are read as `<kind>.<name>` over a set, so declaring this would "
                "change what an existing figure computes without changing its text. "
                "Rename the measure.",
                d.line,
            )
        self._measure_fields_exist(d)

        if isinstance(d, DurationMeasure):
            self.measures[d.name] = CompiledMeasure(
                name=d.name,
                kind=d.kind,
                shape="duration",
                later=d.later,
                earlier=d.earlier,
                clock=d.clock,
            )
        elif isinstance(d, FieldMeasure):
            self.measures[d.name] = CompiledMeasure(
                name=d.name, kind=d.kind, shape="field", unit=d.unit, field_path=d.field
            )
        elif isinstance(d, MomentMeasure):
            self.measures[d.name] = CompiledMeasure(
                name=d.name, kind=d.kind, shape="moment", moment=d.moment
            )
        else:
            assert_never(d)

    def _measure_fields_exist(self, d: DurationMeasure | FieldMeasure | MomentMeasure) -> None:
        """A measure reads one value off one record, so its paths are held to
        both halves of that: the declared type, and no list anywhere on the
        way. Across a `many`, `read_number` skips (a silent nothing for every
        record with two elements) and `read_instant` first-wins (a
        fabrication); the checker refusing here is what keeps either from
        wearing a plausible number.
        """
        if not self.facts:
            return
        what = f"measure {d.name}"

        def one(path: str, wanted: str, verb: str) -> None:
            found = self._record_field(d.kind, path, what, d.line)
            assert found is not None
            node, crossed = found
            if node.type != wanted:
                raise CheckError(
                    f'{what} {verb} "{path}", which is a {node.type} rather than a '
                    f"{wanted}.",
                    d.line,
                )
            if crossed:
                raise CheckError(
                    f'{what} {verb} "{path}", which crosses a list -- several values '
                    "per record, and a measure reads one.",
                    d.line,
                )

        if isinstance(d, DurationMeasure):
            for side in ((d.earlier,) if d.clock else (d.later, d.earlier)):
                one(side, "moment", "subtracts")
        elif isinstance(d, FieldMeasure):
            one(d.field, "number", "reads")
        elif isinstance(d, MomentMeasure):
            one(d.moment, "moment", "names")
        else:
            assert_never(d)

    # ------------------------------------------------------------- figure --

    def _figure(self, d: FigureDecl) -> None:
        self._claim(d.name, "figure", d.line)
        scope = d.name.split(".", 1)[0]
        self._fact_kind(scope, f"figure {d.name} is scoped to", d.line)
        # A figure and a field of its scope are both `<kind>.<name>`, and an
        # expression reads either. Two things under one spelling is what this
        # language exists to refuse, so the collision is refused where it is
        # made rather than left to whichever resolution order won.
        leaf = d.name.split(".", 1)[1]
        fact = self.facts.get(scope)
        if fact is not None and any(f.name == leaf for f in fact.fields):
            raise CheckError(
                f'figure {d.name} takes a name that {scope} already has as a field. '
                "Both are read as `<kind>.<name>`, so one spelling would answer two "
                "things and a reader could not tell which. Rename the figure.",
                d.line,
            )

        if d.across is not None:
            self._fact_kind(d.across, f"figure {d.name} is split across", d.line)
            if d.across == scope:
                raise CheckError(
                    f"figure {d.name} is scoped to {scope} and split across {scope}. Both "
                    "parts of a pair would be the same subject, so every value would be "
                    "keyed by one id twice.",
                    d.line,
                )

        set_names = self._named_sets(d)
        combines = self._combines(d)
        # `sum(<figure>)` is the rollup, written where every other operation
        # is written. Desugared into the same binding the retired `combine`
        # block produced, so the plan, the evaluator and the pass see one
        # shape and only the surface changed.
        d, combines = self._resolve_rollups(d, combines, set_names)
        if d.sets and combines:
            raise CheckError(
                f"figure {d.name} names a population in `depends` and rolls up another "
                "figure's parts in the same calculation. Those are two populations "
                "arriving at one number with no rule for how they relate -- adding a "
                "count of records to a total of stored values produces a number no "
                "definition makes a claim about. Reading one figure's *value* beside a "
                "population is fine and expected: that is a number, not a second "
                "population.",
                d.line,
            )
        # Resolved before the scope check, because a figure that reads another
        # figure and names no group takes its subjects the way a rollup does --
        # and until the dotted names are resolved, nothing knows it reads one.
        d = replace(d, calculate=self._resolve_figure_reads(d, combines, scope))
        scope_index, grain, dimension_part = self._scope_index(
            d, set_names, scope, reads=bool(combines)
        )
        # Rewritten before anything reads the calculation, so the plan, the
        # hash, the kind check and the evaluator all see one tree. `latest`
        # over a measure and `latest` over a declared field are different
        # operations wearing one word; which one was meant is decidable here
        # and nowhere later, because only the checker holds the library.
        d = replace(d, calculate=self._resolve_field_reads(d, scope_index, grain))
        if grain is None and d.bucketed and combines:
            # A figure built on other figures has no group to read a grain
            # from, so it inherits the sequence it is declared over. Without
            # this it would compile as one-value-per-subject and store a
            # single row per site under a coordinate key -- readable by
            # nothing, and visibly empty only to whoever wrote the reading.
            grain = self._grain_of(d, combines)
        if d.carried and scope_index is None:
            raise CheckError(
                f"figure {d.name} is carried forward, and it is built from other figures "
                "rather than from records. A carry anchors on the buckets somebody changed "
                "something in, which is a fact about records -- a figure with no group has "
                "none to anchor on. Left to compile it did nothing at all, while the "
                "suffix sat in the version hash claiming a behaviour that never ran.",
                d.line,
            )
        if d.carried and grain in ("minute", "15 minutes", "hour"):
            raise CheckError(
                f"figure {d.name} is carried forward at {grain} grain, and a pass cannot "
                "honour that. Extension is the pass noticing time -- the clock itself is "
                "never an event -- so a figure owing a new bucket every minute gets one "
                "per sync, and its most recent bucket reads as an absence for as long as "
                "the gap. Fenced the way an age filter is fenced to whole days: the "
                "unenforceable version is refused rather than left to disappoint. Carry "
                "at day grain or coarser.",
                d.line,
            )
        if d.carried and grain is None:
            raise CheckError(
                f"figure {d.name} is carried forward, but it has no sequence of buckets to "
                "carry across. A carry fills the buckets between the ones somebody changed "
                "something in, and a figure with one value per subject has none.",
                d.line,
            )
        if d.bucketed and grain is None:
            # The mirror of the refusal in `_scope_index`, and checked here
            # rather than there because a rollup reaches no group at all --
            # `_scope_index` returns early for it, and a `bucketed` rollup is
            # exactly the case worth catching: it would have readings written
            # over a sequence that does not exist, answering an absence for
            # ever with nothing to say why.
            raise CheckError(
                f"figure {d.name} says `bucketed`, but nothing gives it a sequence of "
                "buckets: the group that fans it out would have to end in a truncated "
                "or selective part (`... by month in tenant.timezone`). A figure "
                "declaring a sequence it does not have is one a reading can be written "
                "over and never find a bucket in.",
                d.line,
            )

        kind = self._calc_kind(d.calculate, d, set_names, combines, scope)
        unit = self._figure_unit(d, kind, combines)

        indexes = sorted(_indexes_in_sets(d.sets))
        measures = sorted(_measures_in(d.calculate))
        reads = sorted({source for source, _ in combines.values()})

        depth = 0
        for source_name in reads:
            source = _find(self.figures, source_name)
            assert source is not None  # resolved above, or by `_combines`
            depth = max(depth, source.depth + 1)

        band, band_reads, band_fields = self._check_band(d, unit, kind, scope, grain)

        plan = FigurePlan(
            name=d.name,
            scope=scope,
            doc=d.doc,
            display=d.display,
            unit=unit,
            calculate=d.calculate,
            across=d.across,
            sets={s.name: s.expr for s in d.sets},
            combines=combines,
            indexes=tuple(indexes),
            measures=tuple(measures),
            reads=tuple(reads),
            scope_index=scope_index,
            band=band,
            band_reads=band_reads,
            band_fields=band_fields,
            grain=grain,
            carried=d.carried,
            ordered_by=_ordered_by_of(d.calculate),
            dimension_part=dimension_part,
            depth=depth,
        )
        self.figures.append(_versioned_figure(plan, self.indexes, self.measures, self.figures))

    def _check_band(
        self, d: FigureDecl, unit: FigureUnit, kind: str, scope: str, grain: str | None
    ) -> tuple[Ladder | None, tuple[str, ...], tuple[str, ...]]:
        """The rules that keep a band a band, and the facts it may name.

        A band used to be a figure of its own -- a `level`-unit figure combining
        the one below it -- and the board found the pair by scanning the library
        at serve time. So the word on screen came from a definition the page
        never named, and the page showing the formula did not contain it.

        Folding it in is only an improvement if it cannot become a second
        calculation hiding in the same block, which is what these refusals are
        for. Each of them compiles and produces something plausible.

        The threshold it compares against is a **figure** -- a number computed
        from records, carried forward across the buckets nobody moved it in,
        cited like any other. It used to be a dial, and that made the one part
        of a card that decides whether a reader should worry the one part no
        evidence could explain. Returns the ladder with each figure reference
        resolved, and the names it reads.
        """
        if d.band is None:
            return (None, (), ())
        band = d.band
        if not isinstance(band, Ladder):  # pragma: no cover - the parser refuses it first
            raise CheckError(f"figure {d.name}'s band is not a ladder.", d.line)

        # A word, never a number. `level` is what tells every reader downstream
        # that this is a band rather than a quantity -- a rung answering a number
        # would be a second figure written inside the first, and it would reach
        # a screen as an unexplained integer in the Band column.
        for expr in [*(r.then for r in band.rungs), band.otherwise]:
            if not isinstance(expr, Text):
                raise CheckError(
                    f"figure {d.name}'s band must answer a word on every rung. A band names "
                    "which of a few states this number is in; a rung answering a number "
                    "would be a second calculation, and it would reach a screen as an "
                    "integer in a column of words.",
                    getattr(expr, "line", d.line),
                )

        # There is exactly one binding, and it is this figure's own answer.
        # Anything else in scope would make this a place to compute a second
        # number from the sets and sources above -- which is what a figure of its
        # own is for, and what folding this in was supposed to stop needing.
        for name in _parts_in(band):
            if name != "value":
                raise CheckError(
                    f'figure {d.name}\'s band reads "{name}". The only thing in scope here '
                    'is "value", this figure\'s own answer -- a band says which state a '
                    "number is in, and reading anything else would make it a second "
                    "calculation sharing the first one's name.",
                    d.line,
                )

        # A word has no order and a list has no single value, so there is
        # nothing for a rung to compare against. Both would band every subject
        # by the `otherwise` rung, silently. The list refusal is on the
        # calculation *kind*, not the unit -- a list figure's unit is the unit
        # of its members ("duration"), so a unit check never fires for it.
        if unit == "level" or kind == "list":
            raise CheckError(
                f"figure {d.name} answers a "
                + ("list" if kind == "list" else unit)
                + " and cannot be banded. A band compares the figure's value against a "
                "threshold, and there is nothing to compare: every subject would fall "
                "through to the bottom rung.",
                d.line,
            )

        reads: set[str] = set()

        def threshold(name: str, sequenced: bool, line: int) -> None:
            """One figure a rung compares against, with every way it could be
            silently wrong refused by name."""
            source = _find(self.figures, name)
            if source is None:
                raise CheckError(
                    f'figure {d.name}\'s band compares against "{name}", which is not a '
                    "figure declared before it. A band's threshold is a fact: a figure "
                    "computed from records, so the number that decides whether a reader "
                    "should worry can be cited like every other number on the card. If "
                    "this was a tenant dial, declare the goal as a figure over the "
                    "records that set it -- carried forward if it is set once and left "
                    "alone -- and name that figure here. Declared so far: "
                    f"{', '.join(f.name for f in self.figures) or 'none'}.",
                    line,
                )
            if source.scope != scope:
                raise CheckError(
                    f"figure {d.name}'s band compares against {name}, which is one value "
                    f"per {source.scope}. Different scopes are different id spaces, so "
                    "every lookup would miss and every subject would lose its word.",
                    line,
                )
            if source.across != d.across:
                raise CheckError(
                    f"figure {d.name}'s band compares against {name}, which is split "
                    f"{'across ' + source.across if source.across else 'across nothing'} "
                    f"where this figure is split "
                    f"{'across ' + d.across if d.across else 'across nothing'}. One side "
                    "holds a value per pair and the other a value per subject, so the "
                    "keys never meet.",
                    line,
                )
            if not sequenced and source.grain is not None and grain == source.grain:
                # Both sides are sequenced and the author wrote the bare
                # spelling. That reads like a static declaration when it is a
                # point-in-time value, and it is a one-word fix -- answered
                # here rather than by the grain message, which would send the
                # author looking for a mismatch that is not there.
                raise CheckError(
                    f"figure {d.name}'s band compares against {name} bare, and it holds "
                    f"one value per {source.grain}. Name the coordinate this figure is "
                    f"already at: `{name}:{{bucket}}`.",
                    line,
                )
            # Days against days, months against months. A grain mismatch is
            # not a near miss: the two subject keys are `c1@2026-07` and
            # `c1@2026-07-14`, so they never meet and every row bands unknown
            # -- which reads as missing data rather than as a wrong definition.
            if source.grain != grain:
                raise CheckError(
                    f"figure {d.name}'s band compares against {name}, which holds one "
                    f"value per {source.grain or 'subject'} where this figure holds one "
                    f"per {grain or 'subject'}. A band and the number it judges share a "
                    "bucketing, so days compare against days and months against months.",
                    line,
                )
            if sequenced != (source.grain is not None):
                raise CheckError(  # pragma: no cover - the grain check answers first
                    f"figure {d.name}'s band compares against {name} at the wrong shape.",
                    line,
                )
            if source.unit != unit:
                raise CheckError(
                    f"figure {d.name} answers a {unit} and its band compares against "
                    f"{name}, which answers a {source.unit}. Both are numbers by the time "
                    "the ladder sees them, so the comparison would run and be wrong by "
                    "whatever the two units differ by.",
                    line,
                )
            reads.add(name)

        def resolve(e: CalcExpr) -> CalcExpr:
            """Rewrite each dotted name into what it actually names.

            A figure first, then a field on the subject's own record. The two
            are the same shape (`<kind>.<name>`), which is why a figure taking
            a name one of its scope's fields already has is refused where it
            is declared -- one spelling answering two things is the thing this
            language is arranged against.
            """
            if isinstance(e, Setting):
                if _find(self.figures, e.path) is None:
                    field = self._subject_field(
                        f"figure {d.name}'s band",
                        e.path,
                        scope,
                        e.line or d.line,
                        e.scale,
                    )
                    if field is not None:
                        return field
                threshold(e.path, sequenced=False, line=e.line or d.line)
                return FigureRef(name=e.path, line=e.line)
            if isinstance(e, Coord):
                threshold(e.name, sequenced=True, line=e.line or d.line)
                return e
            if isinstance(e, Ladder):
                return replace(
                    e,
                    rungs=tuple(
                        replace(
                            r,
                            left=resolve(r.left),
                            then=resolve(r.then),
                            right=resolve(r.right) if r.right is not None else None,
                        )
                        for r in e.rungs
                    ),
                    otherwise=resolve(e.otherwise),
                )
            if isinstance(e, Arith):
                return replace(e, left=resolve(e.left), right=resolve(e.right))
            if isinstance(e, Pick):
                return replace(e, left=resolve(e.left), right=resolve(e.right))
            return e

        resolved = _scaled(resolve(band), unit, f"figure {d.name}")
        assert isinstance(resolved, Ladder)
        fields = tuple(
            sorted(
                f"{n.kind}.{n.field}"
                for n in _walk(resolved)
                if isinstance(n, SubjectField)
            )
        )
        return (resolved, tuple(sorted(reads)), fields)

    def _named_sets(self, d: FigureDecl) -> dict[str, SetExpr]:
        out: dict[str, SetExpr] = {}
        for s in d.sets:
            if s.name in out:
                raise CheckError(f'set "{s.name}" is defined twice', s.line)
            self._check_set(s.expr, out, d, s.line)
            self._one_id_space(d, s.name, s.expr, out, s.line)
            out[s.name] = s.expr
        return out

    def _one_id_space(
        self,
        d: FigureDecl,
        set_name: str,
        expr: SetExpr,
        defined: dict[str, SetExpr],
        line: int,
    ) -> None:
        """A set may only combine indexes over one id space.

        The rule was written down -- on `CompiledIndex.id_space`, and it is the
        whole reason `keyed as` exists -- and never enforced. Intersecting ids
        that mean different things yields the **empty set**, which is a figure
        reading nought for everybody rather than an error anybody sees:

            m = work_issue.assigned_to:{team_person} & code_change.open

        compiles, runs, and reports that nobody is doing anything, for ever.
        """
        spaces = self._spaces_in(expr, defined)
        if len(spaces) > 1:
            raise CheckError(
                f'set "{set_name}" in figure {d.name} combines record sets over '
                f"{' and '.join(sorted(spaces))}. A set is a set of ids, and ids from two "
                "spaces have nothing in common -- intersecting them is empty and the figure "
                "reads nought for everybody, with nothing thrown. Use `keyed as` if the two "
                "really do share an id space.",
                line,
            )

    def _spaces_in(self, expr: SetExpr, defined: dict[str, SetExpr]) -> set[str]:
        if isinstance(expr, SetIndex):
            idx = self.indexes.get(expr.index)
            return {idx.id_space} if idx is not None else set()
        if isinstance(expr, SetRef):
            held = defined.get(expr.name)
            return self._spaces_in(held, defined) if held is not None else set()
        if isinstance(expr, SetOp):
            return self._spaces_in(expr.left, defined) | self._spaces_in(expr.right, defined)
        assert_never(expr)

    def _field_matches_set(
        self,
        owner: str,
        pick: FieldPick | FieldTotal,
        sets: dict[str, SetExpr],
        line: int,
    ) -> None:
        """A field read reads a record, so the set must hold that kind's ids.

        The same silence `_measure_matches_set` guards, one construct along:
        over a set of other ids every lookup misses, the extreme finds
        nothing, and the figure answers an absence for everybody, for ever,
        with nothing thrown.
        """
        expr = sets.get(pick.set)
        if expr is None:
            return
        spaces = self._spaces_in(expr, sets)
        if spaces and pick.kind not in spaces:
            raise CheckError(
                f"figure {owner} reads {pick.kind}.{pick.field} over \"{pick.set}\", which "
                f'holds {" and ".join(sorted(spaces))} ids. Every lookup would miss, so the '
                "figure would answer nothing for everybody with nothing thrown.",
                line,
            )

    def _measure_matches_set(
        self,
        owner: str,
        measure: CompiledMeasure,
        set_name: str,
        sets: dict[str, SetExpr],
        line: int,
    ) -> None:
        """A measure reads a record, so it must be over the set's own kind.

        The same silence one layer along from the rule above: applied to a set
        of other ids, every lookup misses, every record is skipped, and a total
        answers nought while an extreme answers nothing -- for everybody, for
        ever. `test_fields.py` cannot catch it, because it checks a measure's
        path against *its own* kind's specimen.
        """
        expr = sets.get(set_name)
        if expr is None:
            return
        spaces = self._spaces_in(expr, sets)
        if spaces and measure.kind not in spaces:
            raise CheckError(
                f"figure {owner} applies {measure.name}, which reads {measure.kind}, to "
                f'"{set_name}", which holds {" and ".join(sorted(spaces))} ids. Every lookup '
                "would miss, so the figure would answer nought for everybody with nothing "
                "thrown.",
                line,
            )

    def _check_set(
        self, expr: SetExpr, defined: dict[str, SetExpr], d: FigureDecl, line: int
    ) -> None:
        if isinstance(expr, SetIndex):
            idx = self.indexes.get(expr.index)
            if idx is None:
                raise CheckError(
                    f'there is no group or filter called "{expr.index}". Declared: '
                    f"{', '.join(sorted(self.indexes)) or 'none'}.",
                    expr.line,
                )
            if isinstance(expr.bucket, BucketScope):
                if not idx.bucketed:
                    raise CheckError(
                        f"{expr.index} is a filter, so it has a single bucket and "
                        f"cannot be addressed per subject. Write {expr.index} on its own.",
                        expr.line,
                    )
            elif isinstance(expr.bucket, BucketAll):
                if idx.bucketed:
                    raise CheckError(
                        f"{expr.index} buckets by "
                        f"{' and '.join(p.field for p in _index_fields(idx.spec))}, so it "
                        f"needs a bucket: write {expr.index}:{{{d.name.split('.')[0]}}}. Read "
                        "unbucketed it looks for a bucket keyed by the empty string, finds "
                        "nothing, and the figure reads zero for everybody.",
                        expr.line,
                    )
            else:
                assert_never(expr.bucket)
        elif isinstance(expr, SetRef):
            if expr.name not in defined:
                raise CheckError(
                    f'"{expr.name}" is not a group or filter and not a set defined above it.',
                    expr.line,
                )
        elif isinstance(expr, SetOp):
            self._check_set(expr.left, defined, d, line)
            self._check_set(expr.right, defined, d, line)
        else:
            assert_never(expr)

    def _resolve_rollups(
        self,
        d: FigureDecl,
        combines: dict[str, tuple[str, str | None]],
        set_names: dict[str, SetExpr],
    ) -> tuple[FigureDecl, dict[str, tuple[str, str | None]]]:
        """`sum(<figure>)` -- add up the parts of a figure split across a
        dimension, so that a total and its parts cannot disagree.

        This was a block: four lines and a name for one operation whose only
        legal consumer was the `sum` on the line below it. `over <kind>`
        restated what the source already declares, and the binding was an
        alias for a value used once, immediately. Neither said anything the
        calculation could not.

        The binding it desugars to is the figure's own name, so the shape the
        plan carries is unchanged and nothing downstream learns a new one.
        """
        out = dict(combines)

        def walk(e: CalcExpr) -> CalcExpr:
            if (
                isinstance(e, Sum)
                and e.measure is None
                and "." in e.set
                and e.set not in set_names
            ):
                source = _find(self.figures, e.set)
                if source is None:
                    raise CheckError(
                        f'figure {d.name} adds up "{e.set}", which is not a figure '
                        "declared before it. Declared so far: "
                        f"{', '.join(f.name for f in self.figures) or 'none'}.",
                        e.line,
                    )
                if source.across is None:
                    raise CheckError(
                        f"figure {d.name} adds up {e.set}, which is not split across "
                        "anything. A rollup of an undimensioned figure totals a single "
                        "value and looks right for ever, which is why this is refused "
                        f"rather than allowed to mean the same as reading {e.set} "
                        "outright.",
                        e.line,
                    )
                if any(
                    name != e.set and held[1] is not None for name, held in out.items()
                ):
                    raise CheckError(
                        f"figure {d.name} rolls up more than one dimensioned figure. A "
                        "rollup's members are addresses carrying no figure name, so two "
                        "would be indistinguishable once stored.",
                        e.line,
                    )
                out[e.set] = (e.set, source.across)
                return replace(e, set=e.set, measure=None)
            if isinstance(e, Ladder):
                return replace(
                    e,
                    rungs=tuple(
                        replace(
                            r,
                            left=walk(r.left),
                            then=walk(r.then),
                            right=walk(r.right) if r.right is not None else None,
                        )
                        for r in e.rungs
                    ),
                    otherwise=walk(e.otherwise),
                )
            if isinstance(e, (Arith, Pick)):
                return replace(e, left=walk(e.left), right=walk(e.right))
            return e

        return replace(d, calculate=walk(d.calculate)), out

    def _combines(self, d: FigureDecl) -> dict[str, tuple[str, str | None]]:
        """The retired `combine:` block, refused with the line it becomes.

        It was four lines and a name for one operation whose only legal
        consumer was the `sum` immediately below it -- and neither half of it
        said anything the calculation could not. `over <kind>` restated what
        the source declares, and the binding was an alias for a value used
        once. A rollup is written where every other operation is written.

        Refused rather than kept working, because two spellings of one thing
        is what this language is arranged against, and the version hash would
        fork between them for no semantic reason.
        """
        for c in d.combines:
            if c.over is None:
                raise CheckError(
                    f"figure {d.name} binds {c.figure} in a combine block. To read one "
                    f"figure's value, name it in the calculation: `{c.figure}` where "
                    f"`{c.name}` is now.",
                    c.line,
                )
            raise CheckError(
                f"figure {d.name} has a combine block, and there is no such block any "
                f"more. A rollup is an expression: write `sum({c.figure})` in "
                "`calculate:` where `sum(" + c.name + ")` is now, and delete the block. "
                f"`over {c.over}` went with it -- {c.figure} declares what it is split "
                "across, and saying it twice is a second place for the two to disagree.",
                c.line,
            )
        return {}

    def _scope_index(
        self,
        d: FigureDecl,
        sets: dict[str, SetExpr],
        scope: str,
        reads: bool = False,
    ) -> tuple[str | None, str | None, str | None]:
        """Exactly one group must fan the figure out, and this works out which.

        A figure built on another figure has none, and that is legitimate --
        its subjects come from the roster and from whatever the figures it
        reads are stored under. Everything else must have exactly one, because
        two would mean a value keyed by two different things and none would
        mean the figure has no subjects at all while still computing a number,
        which renders as a board-wide total attributed to nobody.

        `reads` covers both shapes of that: a `combine` rollup, and a figure
        named outright in the calculation.
        """
        if reads and not sets:
            return None, None, None

        found: list[tuple[str, CompiledIndex]] = []
        for expr in sets.values():
            for name, bucket in _scope_indexes(expr):
                idx = self.indexes[name]
                if isinstance(bucket, BucketScope):
                    found.append((name, idx))

        if not found:
            raise CheckError(
                f"figure {d.name} names no group addressed by {{{scope}}}, so it has no "
                "subjects. It would compute one number for nobody.",
                d.line,
            )

        names = {n for n, _ in found}
        if len(names) > 1:
            raise CheckError(
                f"figure {d.name} is fanned out by more than one group "
                f"({', '.join(sorted(names))}). A value would be keyed by two different "
                "things.",
                d.line,
            )

        name, idx = found[0]
        parts = _index_fields(idx.spec)
        first = parts[0]
        if first.through is not None and first.through.kind != scope:
            raise CheckError(
                f"figure {d.name} is scoped to {scope}, but {name} resolves its first part "
                f"through {first.through.kind}.",
                d.line,
            )
        # The calendar is read off the subject's own record, and the group
        # cannot always tell whose: `courier_id` says nothing about which kind
        # its values key into, so a group written with a plain key field
        # accepted any kind at all. The figure is where the two meet, because
        # naming the scope is what says which kind those keys belong to.
        # Unchecked, every key is looked up in the wrong table, every lookup
        # misses, and the figure serves nothing -- which reads as a board that
        # has collected nothing rather than as a wrong declaration.
        zoned = next(
            (p.zone for p in parts if p.zone is not None and p.zone.kind is not None),
            None,
        )
        if zoned is not None and zoned.kind != scope:
            raise CheckError(
                f"figure {d.name} is fanned out by {scope}, but {name} reads its calendar "
                f"from {zoned.kind}.{zoned.field}. The calendar is a fact about the subject, "
                f"so it has to be a field on {scope}'s own record -- read off {zoned.kind}, "
                "every subject key would be looked up in the wrong table and no record would "
                "land in any bucket.",
                d.line,
            )
        if first.truncate is not None or first.select is not None:
            raise CheckError(
                f"{name} buckets its first part by {first.truncate or first.select}, so it "
                f"fans {d.name} out by date rather than by {scope}. A bucket of time has "
                "no roster and no name.",
                d.line,
            )

        grain: str | None = None
        dimension_part: str | None = None
        if len(parts) > 1:
            if len(parts) > 2:
                raise CheckError(
                    f"{name} has {len(parts)} parts and fans out {d.name}. A subject and one "
                    "more is all a value key can carry.",
                    d.line,
                )
            tail = parts[1]
            if tail.truncate is not None or tail.select is not None:
                grain = tail.truncate or tail.select
                # `across` is answered before the missing `bucketed`, and the
                # order is load-bearing rather than arbitrary. A figure that
                # says `across` over a truncated part has made a *wrong*
                # claim; one that says nothing has merely omitted one. Told
                # to add `bucketed`, the author of the first would add it
                # beside the `across` and hit a second refusal -- so the
                # message that names the real mistake goes first.
                if d.across is not None:
                    raise CheckError(
                        f"figure {d.name} is split across {d.across}, but {name} buckets "
                        f"its second part by {grain}. A bucket of time is not a "
                        "dimension: it has no roster and no name, and whether a figure is "
                        "time-keyed is what decides if a reading may roll it up over a "
                        "range.",
                        d.line,
                    )
                if not d.bucketed:
                    raise CheckError(
                        f"figure {d.name} is fanned out by {name}, which buckets its "
                        f"second part by {grain} -- so it holds one value per {scope} "
                        f"per {grain}, not one per {scope}. Say so: "
                        f"`figure {d.name} bucketed:`. Unsaid, every reader downstream "
                        "is wrong in its own way -- a projection binds a column that "
                        "never resolves, a bundle subscribes to every stored bucket of "
                        "every subject, and a rollup totals a sequence as though it "
                        "were one number.",
                        d.line,
                    )
            else:
                if d.across is None:
                    raise CheckError(
                        f"{name} gives {d.name} one value per pair, but the figure does not "
                        f"say what the second part is. Write `across <fact kind>` -- without "
                        "it every reader downstream is silently wrong: the display template "
                        "renders the variable as literal text and the generated sentence "
                        "describes the whole population beside a number that is a slice of "
                        "it.",
                        d.line,
                    )
                dimension_part = tail.field
                through = tail.through.kind if tail.through is not None else None
                if through is not None and through != d.across:
                    raise CheckError(
                        f"figure {d.name} is split across {d.across}, but {name} resolves its "
                        f"second part through {through}.",
                        d.line,
                    )
                if d.across not in self._name_fields:
                    raise CheckError(
                        f"figure {d.name} is split across {d.across}, which has no name "
                        "field, so every row would be headed by a raw id.",
                        d.line,
                    )
        return name, grain, dimension_part

    def _grain_of(
        self, d: FigureDecl, combines: dict[str, tuple[str, str | None]]
    ) -> str | None:
        """This figure's own grain.

        From the group that fans it out, or -- for a figure built on other
        figures rather than on records -- from the sources it combines. A
        `combine` figure has no group at all, so its sequence is inherited:
        subtracting two monthly figures per coordinate is itself monthly, and
        there is nowhere else the grain could come from.

        Worked out here rather than threaded through, because the combine
        check runs before `_scope_index` has settled -- and the answer is a
        property of the declaration either way.
        """
        for named in d.sets:
            for index in _indexes_in_sets((named,)):
                spec = self.indexes[index].spec if index in self.indexes else None
                if spec is None:
                    continue
                found = _ordering_grain(spec)
                if found is not None:
                    return found
        for source_name, _ in combines.values():
            source = _find(self.figures, source_name)
            if source is not None and source.grain is not None:
                return str(source.grain)
        return None

    def _coord_source(
        self,
        d: FigureDecl,
        e: Coord,
        combines: dict[str, tuple[str, str | None]],
    ) -> FigurePlan:
        """Which figure a `:{bucket}` names.

        In a `calculate` it is a `combine` binding; in a `band:` rung it is a
        figure named outright, because a band has no bindings but `value`.
        """
        bound = combines.get(e.name)
        name = bound[0] if bound is not None else e.name
        source: FigurePlan | None = _find(self.figures, name)
        if source is None:
            known = ", ".join(sorted(combines)) or "nothing"
            raise CheckError(
                f'figure {d.name} reads "{e.name}:{{bucket}}", which names neither a '
                f"combine binding nor a figure declared before it. Bound here: {known}.",
                e.line,
            )
        return source

    def _subject_field(
        self, owner: str, name: str, scope: str, line: int, scale: str | None = None
    ) -> SubjectField | None:
        """`shop_courier.max_orders`, where the figure is scoped to couriers.

        None when the name does not look like a field of the subject's kind at
        all, so the caller can go on to say what it *did* expect. A name that
        clearly means this and is wrong -- another kind, a field nobody
        declared, a word where a number belongs -- is refused here, because
        each of those has a different fix and one message for all three would
        name none of them.
        """
        kind, _, field = name.partition(".")
        if not field or kind not in self._kinds:
            return None
        if self.facts.get(kind) is None:
            # A schema-taught world declares no fields, so there is nothing to
            # check a name against -- and an unchecked field read is a silent
            # absence for every subject. The route opens when the world is
            # declared in the language, where the checker can see the field.
            return None
        if kind != scope:
            raise CheckError(
                f'{owner} reads "{name}", and {kind} is not what this value is about '
                f"-- it is one value per {scope}. A field is read off the subject's own "
                "record, because that is the only record there is a key for; picking "
                f"one of {kind}'s would be a fabrication. Read it through a figure "
                f"scoped to {scope}, or through a group.",
                line,
            )
        found = self._record_field(
            kind, field, f"{owner} reads {name}", line, named=True
        )
        if found is not None and found[0].type not in ("number", None):
            raise CheckError(
                f'{owner} reads "{name}", which is declared as {found[0].type}. A '
                "threshold is a number: a word read off a record would be arbitrary "
                "text compared against a quantity.",
                line,
            )
        if found is not None and found[1]:
            raise CheckError(
                f'{owner} reads "{name}", whose path crosses a list, so one record '
                "holds several of them. Which one is the subject's has no answer, and "
                "first-wins would be a fabrication about the wrong element.",
                line,
            )
        return SubjectField(kind=kind, field=field, line=line, scale=scale)

    def _resolve_figure_reads(
        self,
        d: FigureDecl,
        combines: dict[str, tuple[str, str | None]],
        scope: str,
    ) -> CalcExpr:
        """Turn a dotted name in a calculation into the figure it names.

        A calculation needs three kinds of number: its own population's
        (a count, a sum), another figure's, and a fixed one. The middle one
        used to arrive through `combine` alone, and a figure has `depends`
        **or** `combine` -- so "how much room is left before this courier's
        limit" was unwritable, because it needs a count of records *and* a
        stored value. The gap did not show while a threshold could be a dial;
        taking dials away is what made it the thing standing in the way.

        So a figure may be named outright, and it means one value looked up
        under this subject's own key -- not a second population. That is why
        it desugars into a `combine` binding rather than becoming a construct
        of its own: the read, the depth ordering and the invalidation are the
        ones already there, and a second path to "this figure rests on that
        one" is a second chance for the two to disagree about what must
        rebuild.

        The exclusivity rule is untouched. `depends` beside an explicit
        `combine:` block is still refused, because *that* really is two
        populations arriving at one calculation with no rule for how they
        relate.
        """

        def bind(name: str, line: int) -> None:
            source = _find(self.figures, name)
            if source is None:
                raise CheckError(
                    f'figure {d.name} reads "{name}", which is not a figure declared '
                    "before it. A figure may only read one declared earlier -- a cycle "
                    "has no line number, and on a cold build the wrong order stores a "
                    "nought and never revisits it. If this was a tenant dial: a "
                    "definition's numbers come from facts, so name a figure computed "
                    "from the records that set this one, or write the number where a "
                    "reader can see it. Declared so far: "
                    f"{', '.join(f.name for f in self.figures) or 'none'}.",
                    line,
                )
            if source.scope != scope:
                raise CheckError(
                    f"figure {d.name} reads {name}, which is one value per "
                    f"{source.scope}. Different scopes are different id spaces, so every "
                    "lookup would miss and every subject would read nothing.",
                    line,
                )
            if source.across is not None and d.across != source.across:
                raise CheckError(
                    f"figure {d.name} reads {name}, which is split across "
                    f"{source.across} -- it holds one value per pair, not one per "
                    "subject, so a bare read would take whichever part sorted first "
                    "and look right for ever. Add them up: "
                    f"`combine: parts = {name} over {source.across}`.",
                    line,
                )
            combines.setdefault(name, (name, None))

        def walk(e: CalcExpr) -> CalcExpr:
            if isinstance(e, Setting):
                if _find(self.figures, e.path) is None:
                    field = self._subject_field(
                        f"figure {d.name}", e.path, scope, e.line or d.line
                    )
                    if field is not None:
                        return field
                bind(e.path, e.line or d.line)
                # A bare `Part` from here on: the checker's Part branch already
                # holds every rule about reading a combined figure, including
                # the refusal of a sequenced one named without its coordinate.
                return Part(name=e.path, line=e.line)
            if isinstance(e, Coord) and e.name not in combines:
                bind(e.name, e.line or d.line)
                return e
            if isinstance(e, Ladder):
                return replace(
                    e,
                    rungs=tuple(
                        replace(
                            r,
                            left=walk(r.left),
                            then=walk(r.then),
                            right=walk(r.right) if r.right is not None else None,
                        )
                        for r in e.rungs
                    ),
                    otherwise=walk(e.otherwise),
                )
            if isinstance(e, Arith):
                return replace(e, left=walk(e.left), right=walk(e.right))
            if isinstance(e, Pick):
                return replace(e, left=walk(e.left), right=walk(e.right))
            return e

        return walk(d.calculate)

    def _resolve_field_reads(
        self, d: FigureDecl, scope_index: str | None, grain: str | None
    ) -> CalcExpr:
        """Turn `latest(<kind>.<field> over <set>)` into a `FieldPick`, and
        `sum(<kind>.<field> over <set>)` into a `FieldTotal`.

        The dotted name is a measure or it is a fact field, and over `latest`
        the two mean genuinely different things: over a moment measure it
        answers *when*, over a field it answers *what the value was then*. A
        measure wins if one exists by that name -- otherwise declaring a
        measure could silently change what an existing figure computes.
        """

        def walk(e: CalcExpr) -> CalcExpr:
            if isinstance(e, Extreme) and e.measure not in self.measures:
                kind, _, field = e.measure.partition(".")
                if kind in self._kinds and field:
                    return self._field_pick(d, e, kind, field, scope_index, grain)
            if (
                isinstance(e, Sum)
                and e.measure is not None
                and e.measure not in self.measures
            ):
                kind, _, field = e.measure.partition(".")
                if kind in self._kinds and field:
                    return self._field_total(d, e, kind, field)
            if isinstance(e, Ladder):
                return replace(
                    e,
                    rungs=tuple(
                        replace(
                            r,
                            left=walk(r.left),
                            then=walk(r.then),
                            right=walk(r.right) if r.right is not None else None,
                        )
                        for r in e.rungs
                    ),
                    otherwise=walk(e.otherwise),
                )
            if isinstance(e, Arith):
                return replace(e, left=walk(e.left), right=walk(e.right))
            if isinstance(e, Pick):
                return replace(e, left=walk(e.left), right=walk(e.right))
            return e

        return walk(d.calculate)

    def _field_total(
        self, d: FigureDecl, e: Sum, kind: str, field: str
    ) -> FieldTotal:
        """One resolved `sum(<kind>.<field> over <set>)`.

        `latest` refuses a word and a path crossing a list; `sum` was given
        the same shortcut and neither guard. Both failures are quiet at run
        time because `read_number` answers None for each of them and a total
        skips what it cannot read: a word field totals 0.0 for every subject,
        and a crossing path totals only the records that happened to hold
        exactly one element -- a real-looking number over a population
        nobody chose, whose evidence cites only the contributors and so
        reads as consistent.
        """
        found = self._record_field(
            kind, field, f"figure {d.name} totals {kind}.{field}", e.line, named=True
        )
        if found is not None and found[0].type not in ("number", None):
            raise CheckError(
                f"figure {d.name} totals {kind}.{field}, which is declared as "
                f"{found[0].type}. A total is arithmetic over numbers; a word contributes "
                "nothing to it, so the answer would be a confident nought rather than a "
                "refusal.",
                e.line,
            )
        if found is not None and found[1]:
            raise CheckError(
                f"figure {d.name} totals {kind}.{field}, whose path crosses a list, so one "
                "record holds several of them. A record holding two would contribute "
                "nothing while a record holding one contributed normally, which is a total "
                "over the records that happened to hold exactly one. Declare a measure that "
                "says which element is meant, or total a field the record holds once.",
                e.line,
            )
        return FieldTotal(kind=kind, field=field, set=e.set, line=e.line)

    def _field_pick(
        self,
        d: FigureDecl,
        e: Extreme,
        kind: str,
        field: str,
        scope_index: str | None,
        grain: str | None,
    ) -> FieldPick:
        """One resolved field read, with every way it could be silently wrong
        refused by name."""
        found = self._record_field(
            kind,
            field,
            f"figure {d.name} takes the {e.which} of {kind}.{field}",
            e.line,
            named=True,
        )
        if found is not None and found[0].type not in ("number", None):
            raise CheckError(
                f"figure {d.name} takes the {e.which} of {kind}.{field}, which is declared "
                f"as {found[0].type}. A figure's value is a number, a word a ladder chose, "
                "or a list -- a word read straight off a record would be arbitrary text "
                "with a version hash, which is what the ladder's closed vocabulary exists "
                "to prevent.",
                e.line,
            )
        if found is not None and found[1]:
            raise CheckError(
                f"figure {d.name} takes the {e.which} of {kind}.{field}, whose path crosses "
                "a list, so one record holds several of them. Which one is 'the latest' "
                "has no answer, and first-wins would be a fabrication about the wrong "
                "element.",
                e.line,
            )
        if grain is None or scope_index is None:
            raise CheckError(
                f"figure {d.name} takes the {e.which} of a declared field, and it has no "
                "sequence of buckets. The ordering that decides which record is latest is "
                "the group's own time part, so this construct only means something inside "
                "a `bucketed` figure.",
                e.line,
            )
        ordered_by = _ordering_field(self.indexes[scope_index].spec)
        if ordered_by is None:
            raise CheckError(
                f"figure {d.name} takes the {e.which} of a declared field, but the group "
                f"that fans it out buckets by {grain} without a field to order within a "
                "bucket by.",
                e.line,
            )
        return FieldPick(
            which=e.which,
            kind=kind,
            field=field,
            set=e.set,
            ordered_by=ordered_by,
            line=e.line,
        )

    # ------------------------------------------------- calculate: kinds --

    def _calc_kind(
        self,
        e: CalcExpr,
        d: FigureDecl,
        sets: dict[str, SetExpr],
        combines: dict[str, tuple[str, str | None]],
        scope: str,
    ) -> _Kind:
        """What this calculation produces, checking every rule on the way down."""
        if isinstance(e, Count):
            self._require_set(d.name, e.set, sets, e.line)
            return "number"

        if isinstance(e, ListOf):
            m = self._require_measure(d.name, e.measure, e.line)
            self._require_set(d.name, e.set, sets, e.line)
            self._measure_matches_set(d.name, m, e.set, sets, e.line)
            if m.clock:
                raise CheckError(
                    f"figure {d.name} lists {e.measure}, which is measured to now. A figure "
                    "is stored, and a value computed from the clock is stale the instant it "
                    "is written with nothing to ever move it -- every number would be real "
                    "exactly once. Only a live reading may name a clock measure.",
                    e.line,
                )
            if m.shape == "moment":
                raise CheckError(
                    f"figure {d.name} lists {e.measure}, which names a single instant rather "
                    "than measuring anything. A column of epochs is not a distribution.",
                    e.line,
                )
            if m.shape == "field":
                raise CheckError(
                    f"figure {d.name} lists {e.measure}, which reads a field rather than "
                    "measuring between two moments. A list is the evidence behind a span, "
                    "and a field total is a sum.",
                    e.line,
                )
            return "list"

        if isinstance(e, Sum):
            if e.measure is not None:
                m = self._require_measure(d.name, e.measure, e.line)
                if e.set not in sets:
                    raise CheckError(
                        f"sum({e.measure} over {e.set}) applies a measure to records, but "
                        f'"{e.set}" is not a set defined in depends. Left to fall through it '
                        "would total nothing.",
                        e.line,
                    )
                if m.clock:
                    raise CheckError(
                        f"figure {d.name} totals {e.measure}, which is measured to now.",
                        e.line,
                    )
                if m.shape == "moment":
                    raise CheckError(
                        f"figure {d.name} totals {e.measure}, which names a single instant. "
                        "Adding two dates together is a date in the future and means nothing.",
                        e.line,
                    )
                if m.shape == "duration":
                    raise CheckError(
                        f"figure {d.name} totals {e.measure}, which is the seconds between "
                        "two moments. Adding waits together is not a quantity anybody asked "
                        "for; a list is what a span reads.",
                        e.line,
                    )
                self._measure_matches_set(d.name, m, e.set, sets, e.line)
                return "number"
            if e.set not in combines:
                raise CheckError(
                    f'sum({e.set}) adds up the parts of a dimensioned figure, but "{e.set}" '
                    "is not bound in combine. Left to fall through it would total a set's "
                    "ids.",
                    e.line,
                )
            if combines[e.set][1] is None:
                raise CheckError(
                    f'figure {d.name} calculates sum({e.set}), which adds up the parts of a '
                    f'dimensioned figure, but "{e.set}" is bound as a single value. Add '
                    "`over <fact kind>`, or read it by name.",
                    e.line,
                )
            return "number"

        if isinstance(e, Part) and e.name in combines and self._grain_of(d, combines) is not None:
            bound = _find(self.figures, combines[e.name][0])
            if bound is not None and bound.grain is None:
                raise CheckError(
                    f'figure {d.name} is keyed by {self._grain_of(d, combines)} and reads "{e.name}" '
                    f"as a single value, but {bound.name} holds one value per subject. The "
                    "coordinate read beside it looks the source up under `subject@bucket` "
                    "and this one under `subject`, so the scalar resolves to nothing at "
                    "every coordinate and the figure answers an absence for ever.",
                    e.line,
                )

        if isinstance(e, Part):
            if e.name not in combines:
                raise CheckError(
                    f'calculate reads "{e.name}", which is not defined in combine.', e.line
                )
            if combines[e.name][1] is not None:
                raise CheckError(
                    f'"{e.name}" is bound as the parts of a dimensioned figure, so it is a '
                    f"set of values rather than one. Write sum({e.name}).",
                    e.line,
                )
            source = _find(self.figures, combines[e.name][0])
            assert source is not None
            if source.grain is not None:
                raise CheckError(
                    f'figure {d.name} reads "{e.name}" bare, and it is one value per '
                    f"{source.grain} rather than one number. Written plain it reads like a "
                    "static declaration when it is a point-in-time value -- and with two "
                    "sequences in one expression nothing says the arithmetic is per "
                    "coordinate, so the obvious implementation is a positional zip: right "
                    "until one source starts a bucket later than the other, and then every "
                    f"number is paired with the wrong one. Write `{e.name}:{{bucket}}`.",
                    e.line,
                )
            if source.unit == "level":
                raise CheckError(
                    f"figure {d.name} reads {source.name}, which stores a word rather than a "
                    "number. Arithmetic and comparison need a number; band the figure "
                    "underneath instead.",
                    e.line,
                )
            return "moment" if source.unit == "moment" else "number"

        if isinstance(e, Number):
            return "number"
        if isinstance(e, Text):
            return "text"

        if isinstance(e, Setting):
            return "number"

        if isinstance(e, Ladder):
            return self._ladder_kind(e, d, sets, combines, scope)

        if isinstance(e, Arith):
            for side in (e.left, e.right):
                k = self._calc_kind(side, d, sets, combines, scope)
                if k != "number":
                    raise CheckError(
                        f"arithmetic needs numbers on both sides, and this side is a {k}.",
                        e.line,
                    )
            return "number"

        if isinstance(e, Pick):
            for side in (e.left, e.right):
                k = self._calc_kind(side, d, sets, combines, scope)
                if k != "number":
                    raise CheckError(
                        f"{e.which} needs numbers on both sides, and this side is a {k}.",
                        e.line,
                    )
            return "number"

        if isinstance(e, DaysBetween):
            raise CheckError(
                f"figure {d.name} measures days from \"{e.frm}\" to \"{e.to}\", which reads "
                "the clock. A stored value computed from the clock is stale the instant it is "
                "written and nothing would ever recompute it, because the clock is not an "
                "event. A projection may do this; it stores nothing.",
                e.line,
            )

        if isinstance(e, BucketStat):
            if not d.bucketed:
                raise CheckError(
                    f"figure {d.name} takes {e.fn}({e.set}), and it is not `bucketed`. A "
                    "distribution statistic needs a declared boundary to be a claim about: "
                    "without one the population is everything ever collected, so the number "
                    "drifts with the data's age -- it moves when nothing happened, and "
                    "nobody can say what it is a " + e.fn + " of.",
                    e.line,
                )
            m = self._require_measure(d.name, e.measure, e.line)
            self._require_set(d.name, e.set, sets, e.line)
            if m.shape == "moment":
                raise CheckError(
                    f"figure {d.name} takes {e.fn} of {e.measure}, which names an instant. "
                    "Averaging moments is a date, which is not a quantity anybody asked "
                    "for.",
                    e.line,
                )
            if m.clock:
                raise CheckError(
                    f"figure {d.name} takes {e.fn} of {e.measure}, which is measured to now. "
                    "A stored value may not read a clock.",
                    e.line,
                )
            self._measure_matches_set(d.name, m, e.set, sets, e.line)
            return "number"

        if isinstance(e, Coord):
            source = self._coord_source(d, e, combines)
            mine = self._grain_of(d, combines)
            if source.grain is not None and mine is not None and mine != source.grain:
                # Two sequences that are not the same sequence. A coordinate
                # means "the same bucket in both", and months against days
                # share no bucket key at all -- the join would match nothing
                # and every coordinate would answer an absence, which looks
                # exactly like a figure waiting to be computed.
                raise CheckError(
                    f"figure {d.name} is keyed by {mine} and reads {e.name}, which is "
                    f"keyed by {source.grain}. A coordinate is the same bucket in both "
                    "sequences, and these are two different sequences.",
                    e.line,
                )
            if source.grain is None:
                raise CheckError(
                    f"figure {d.name} reads {e.name}:{{bucket}}, but {source.name} holds one "
                    "value per subject rather than a sequence -- there is no coordinate to "
                    "read it at. Name it bare.",
                    e.line,
                )
            if source.scope != scope:
                raise CheckError(
                    f"figure {d.name} reads {e.name}:{{bucket}}, which is one value per "
                    f"{source.scope}. Different scopes are different id spaces, so every "
                    "lookup would miss.",
                    e.line,
                )
            return "number"

        if isinstance(e, FieldPick):
            self._require_set(d.name, e.set, sets, e.line)
            self._field_matches_set(d.name, e, sets, e.line)
            return "number"

        if isinstance(e, FieldTotal):
            self._require_set(d.name, e.set, sets, e.line)
            self._field_matches_set(d.name, e, sets, e.line)
            return "number"

        if isinstance(e, Extreme):
            m = self._require_measure(d.name, e.measure, e.line)
            self._require_set(d.name, e.set, sets, e.line)
            if m.shape != "moment":
                raise CheckError(
                    f"figure {d.name} takes the {e.which} of {e.measure}, which measures "
                    "a quantity rather than naming an instant. Without that rule this would "
                    "be a general maximum over a column, which is a construct no definition "
                    "has asked for.",
                    e.line,
                )
            self._measure_matches_set(d.name, m, e.set, sets, e.line)
            return "moment"

        if isinstance(e, SubjectField):
            return "number"

        if isinstance(e, FigureRef):  # pragma: no cover - band-only
            raise CheckError(
                f"figure {d.name} reads {e.name} in its calculation. A figure named "
                "outright is a band's threshold; a calculation reads another figure "
                "through `combine`.",
                e.line,
            )

        assert_never(e)

    def _ladder_kind(
        self,
        e: Ladder,
        d: FigureDecl,
        sets: dict[str, SetExpr],
        combines: dict[str, tuple[str, str | None]],
        scope: str,
    ) -> _Kind:
        results: list[_Kind] = []
        for rung in e.rungs:
            left = self._calc_kind(rung.left, d, sets, combines, scope)
            if left == "list":
                raise CheckError(
                    "a when clause compares one value, and this side is a list.", rung.line
                )
            if rung.right is not None:
                right = self._calc_kind(rung.right, d, sets, combines, scope)
                if (left == "text" or right == "text") and left != right:
                    raise CheckError(
                        "a when clause compares like with like, and this rung compares a "
                        "word with a number.",
                        rung.line,
                    )
            results.append(self._calc_kind(rung.then, d, sets, combines, scope))
        results.append(self._calc_kind(e.otherwise, d, sets, combines, scope))

        if any(k == "list" for k in results):
            raise CheckError(
                f"figure {d.name} returns a list from a when clause. A list is the evidence "
                "behind a span rather than a value a rung can answer.",
                e.line,
            )
        if len(set(results)) > 1:
            raise CheckError(
                f"figure {d.name} returns a number from one branch and a word from another. "
                "One stored value cannot be both, and every reader downstream branches on "
                "which it is.",
                e.line,
            )
        kind = results[0]
        if kind == "number":
            raise CheckError(
                f"figure {d.name} returns a number from its when ladder. A ladder answers a "
                "band -- a word -- and a ladder that answered numbers would carry an absence "
                "out under a numeric unit, where nothing can hold it.",
                e.line,
            )
        return kind

    def _figure_unit(
        self, d: FigureDecl, kind: _Kind, combines: dict[str, tuple[str, str | None]]
    ) -> FigureUnit:
        """A figure's unit is derived wherever it can be, and declared only where
        nothing else can tell.

        A count is a count, a list of a duration measure is a duration, a ladder
        returns a level. Arithmetic is the one shape where nothing can derive it:
        the same two operands divided give a share and subtracted give the
        quantity they were both in, and 0.6 renders as "60%" or as "0.6" with no
        way to tell which was meant.
        """
        # A declared-field read joins arithmetic on the "nothing can derive
        # it" side -- all three shapes of it, including the bare
        # `<kind>.<field>` this release added, which was left off and so
        # silently took `count`. A count is a count and a sum of an effort
        # measure is an effort because the construct says what the number is;
        # a field read says only that a record carries a number. The fact layer is
        # structural on purpose -- `value as number` claims a shape, never a
        # meaning -- so there is nothing riding on it to inherit, and the
        # same integer would print as "144000" or as "5d" with no way to
        # tell which was meant. That is the rule in its sharpened form:
        # declare only what cannot be derived, and a redundant declaration
        # is still refused above.
        arithmetic = isinstance(
            d.calculate, (Arith, Pick, FieldPick, FieldTotal, SubjectField)
        )
        if d.unit is not None and not arithmetic:
            raise CheckError(
                f'figure {d.name} declares "unit {d.unit}", and its calculation already says '
                "what the number is. A second place to write it is a first place for the two "
                "to disagree.",
                d.line,
            )
        if arithmetic and d.unit is None:
            raise CheckError(
                f"figure {d.name} produces a number nothing can name. The same two operands "
                "divided give a share and subtracted give the quantity they were both in, "
                'and 0.6 renders as "60%" or as "0.6" with no way to tell which was meant. '
                "Add `unit <share|days|effort|count|duration>`.",
                d.line,
            )
        if d.unit is not None:
            return d.unit

        if kind == "text":
            return "level"
        if kind == "moment":
            return "moment"
        if kind == "list":
            e = d.calculate
            assert isinstance(e, ListOf)
            return "duration"
        if isinstance(d.calculate, BucketStat):
            # A statistic over a measure is in the measure's own quantity: the
            # median of a column of durations is a duration. Derived, so it
            # must not be declared -- the rule above already refuses that.
            m = self.measures[d.calculate.measure]
            if m.shape == "duration":
                return "duration"
            return "effort" if m.unit == "effort" else "count"
        if isinstance(d.calculate, Sum) and d.calculate.measure is not None:
            m = self.measures[d.calculate.measure]
            return "effort" if m.unit == "effort" else "count"
        if isinstance(d.calculate, Coord):
            # A passthrough is the source's own number at a coordinate, so it
            # is in the source's own unit. Derived like `Part`'s, and for the
            # same reason: declaring it here is refused as redundant, so a
            # missed derivation leaves nothing able to say what the number is.
            binding = d.calculate.name
            if binding in combines:
                source: FigurePlan | None = _find(self.figures, combines[binding][0])
                if source is not None:
                    return source.unit
        if isinstance(d.calculate, (Sum, Part)):
            # A rollup or a bare read inherits from **the binding it reads**,
            # not from whichever binding happens to carry an inheritable unit.
            # Looping over every `combine` meant a figure that binds a count and
            # an effort and then reads the count came out as effort -- and 3
            # renders through the effort branch as "0.4h".
            binding = d.calculate.set if isinstance(d.calculate, Sum) else d.calculate.name
            held = combines.get(binding)
            if held is not None:
                source = _find(self.figures, held[0])
                if source is not None:
                    inherited: FigureUnit = source.unit
                    if inherited in ("effort", "share", "days", "duration"):
                        return inherited
        return "count"

    # ------------------------------------------------------------ reading --

    def _reading(self, d: ReadingDecl) -> None:
        self._claim(d.name, "reading", d.line)
        scope = d.name.split(".", 1)[0]
        self._fact_kind(scope, f"reading {d.name} is scoped to", d.line)

        for arg in d.args:
            if arg != "range":
                raise CheckError(
                    f'"{arg}" is not an argument a reading can take. Only "range" exists: an '
                    "argument may narrow the population and may not change the calculation, "
                    "because a statistic, a minimum or a band decide what the number means "
                    "and those are hashed into the version.",
                    d.line,
                )
        if len(d.sets) != 1:
            raise CheckError(
                f"reading {d.name} reads {len(d.sets)} sources. One is supported: a reading "
                "over two would have to say which its sample and its band were about.",
                d.line,
            )
        s = d.sets[0]
        bound = {s.name}

        if s.windowed is not None:
            return self._windowed_reading(d, s.windowed.figure, bound, scope)
        assert s.live is not None
        return self._live_reading(d, s.live.measure, s.live.set, bound, scope)

    def _windowed_reading(
        self, d: ReadingDecl, figure: str, bound: set[str], scope: str
    ) -> None:
        if "range" not in d.args:
            raise CheckError(
                f"reading {d.name} summarises stored values, so it must declare (range) -- "
                "without it there is nothing to say which days take part.",
                d.line,
            )
        source = _find(self.figures, figure)
        if source is None:
            if _find(self.readings, figure) is not None:
                raise CheckError(
                    f"{figure} is a reading, and a reading may only read a figure. Composing "
                    "them is how a team number becomes a mean of means, weighting each "
                    "person equally instead of each record.",
                    d.line,
                )
            raise CheckError(f'there is no figure called "{figure}".', d.line)
        if source.grain is None:
            raise CheckError(
                f"{source.name} is not time-keyed -- the group that fans it out must end in "
                "a time bucket part (`by day`, `by month`, `by first monday of month`, ...) "
                "for there to be a sequence to read over.",
                d.line,
            )
        if source.scope != scope:
            raise CheckError(
                f'reading {d.name} is scoped to "{scope}" but reads {source.name}, which is '
                f"scoped to {source.scope}.",
                d.line,
            )
        if source.unit == "level":
            raise CheckError(
                f"{source.name} stores a word rather than a number, so there is no statistic "
                "to take. What a reader wants from a band over time -- how long somebody "
                "spent at over -- is a different figure over an event that does not exist.",
                d.line,
            )
        if source.unit == "moment":
            raise CheckError(
                f"{source.name} stores an instant rather than a quantity.", d.line
            )

        self._statistics(d, bound, live=False, source=source)
        unit = self._reading_unit(source.unit, d)
        band, band_on, band_reads = self._band(d, scope, source)
        requires = d.requires
        if not requires and any(s.fn in ("mean", "median", "worst") for s in d.calculate):
            # The unwritten minimum sample is one value, injected here so it is
            # hashed like a written one -- a floor applied at read time would
            # let two engines render the same version differently. One rather
            # than none because a distribution over nothing is a claim nobody
            # can make, and going through a requirement is what makes the
            # response say what fell short instead of nulling silently. A sum
            # gets no default (a sum of nothing is nought and nought must
            # render), and neither does a live reading (an empty queue is a
            # real count of nought pending, not a shortfall).
            requires = (Requirement(count=1, set=d.sets[0].name, line=d.line),)
        self.readings.append(
            _versioned_reading(
                ReadingPlan(
                    name=d.name,
                    scope=scope,
                    mode="window",
                    doc=d.doc,
                    display=d.display,
                    unit=unit,
                    calculate=d.calculate,
                    requires=requires,
                    band=band,
                    band_on=band_on,
                    band_reads=band_reads,
                    source=source.name,
                ),
                self.indexes,
                self.measures,
            )
        )

    def _live_reading(
        self, d: ReadingDecl, measure: str, expr: SetExpr, bound: set[str], scope: str
    ) -> None:
        if d.args:
            raise CheckError(
                f"reading {d.name} measures records as they stand, so it takes no arguments "
                "-- there is no range because nothing is stored to pick from. Written "
                "(range) it would accept a window, ignore it, and return today's answer "
                "under a heading saying thirty days.",
                d.line,
            )
        m = self.measures.get(measure)
        if m is None:
            if _find(self.figures, measure) is not None:
                raise CheckError(
                    f"{measure} is a figure, and `over` measures records rather than "
                    "summarising stored values.",
                    d.line,
                )
            raise CheckError(f'there is no measure called "{measure}".', d.line)
        if m.shape == "field":
            raise CheckError(
                f"reading {d.name} measures {measure}, which reads a field rather than the "
                "clock or a span.",
                d.line,
            )

        # A live reading is fanned out by exactly one scope-bucketed group, and
        # all three ways of getting that wrong compile and produce a plausible
        # nothing: a filter addressed by {scope} misses because its bucket is
        # keyed by the empty string, a group read unbucketed misses the same
        # way, and a set with no scope anywhere produces no subjects at all
        # while the empty case holds the whole board's queue.
        scoped: list[str] = []
        for name, bucket in _scope_indexes(expr):
            idx = self.indexes.get(name)
            if idx is None:
                raise CheckError(f'there is no group or filter called "{name}".', d.line)
            if isinstance(bucket, BucketScope):
                if not idx.bucketed:
                    raise CheckError(
                        f"{name} is a filter, so it has a single bucket and cannot "
                        f"be addressed per subject.",
                        d.line,
                    )
                scoped.append(name)
            elif idx.bucketed:
                raise CheckError(
                    f"{name} buckets by "
                    f"{' and '.join(p.field for p in _index_fields(idx.spec))}, so it needs a "
                    f"bucket: write {name}:{{{scope}}}.",
                    d.line,
                )
        if len(scoped) != 1:
            raise CheckError(
                f"reading {d.name} is fanned out by {len(scoped)} groups addressed by "
                f"{{{scope}}}. Exactly one is needed: with none there are no subjects at all, "
                "and the empty case -- what somebody with nothing looks like -- would hold "
                "the whole board's answer attributed to nobody.",
                d.line,
            )

        self._statistics(d, bound, live=True, source=None)
        band, band_on, band_reads = self._band(d, scope, None)
        self.readings.append(
            _versioned_reading(
                ReadingPlan(
                    name=d.name,
                    scope=scope,
                    mode="live",
                    doc=d.doc,
                    display=d.display,
                    unit="duration",
                    calculate=d.calculate,
                    requires=d.requires,
                    band=band,
                    band_on=band_on,
                    band_reads=band_reads,
                    live_measure=measure,
                    live_set=expr,
                    indexes=tuple(sorted(_indexes_in(expr))),
                ),
                self.indexes,
                self.measures,
            )
        )

    def _statistics(
        self,
        d: ReadingDecl,
        bound: set[str],
        live: bool,
        source: FigurePlan | None,
    ) -> None:
        counts = source is not None and source.unit == "count"
        grain = source.grain if source is not None else None
        seen: set[str] = set()
        series_declared = 0
        delta_declared = 0
        for stat in d.calculate:
            if stat.set not in bound:
                raise CheckError(
                    f'calculate reads "{stat.set}", which is not a set defined in depends.',
                    stat.line,
                )
            if stat.fn == "count" and not live:
                raise CheckError(
                    f"reading {d.name} summarises stored values, and count({stat.set}) over "
                    "those is already reported as the sample -- one quantity under two names. "
                    "For a count figure they would not even agree, because the sample is the "
                    "buckets that contributed rather than records.",
                    stat.line,
                )
            if counts and stat.fn in ("mean", "median", "worst"):
                raise CheckError(
                    f"the figure under {d.name} stores a count, so {stat.fn}({stat.set}) is a "
                    f"{stat.fn} per *{grain}* wearing a label that says per record -- a "
                    "plausible number of roughly the right magnitude, which is the worst "
                    "kind of wrong. Only sum is allowed over a count.",
                    stat.line,
                )
            if stat.fn == "delta":
                delta_declared += 1
                if live:
                    raise CheckError(
                        f"reading {d.name} takes a delta, and it measures records as they "
                        "stand. A delta is the change between adjacent stored buckets, and "
                        "a live reading stores none -- it would answer an empty list under "
                        "a heading promising a trend.",
                        stat.line,
                    )
                if delta_declared > 1:
                    raise CheckError(
                        f"reading {d.name} declares two deltas. A response carries one, so "
                        "the second would be whichever the serve path kept, silently.",
                        stat.line,
                    )
                if grain in ("minute", "15 minutes"):
                    raise CheckError(
                        f"reading {d.name} takes a delta over a figure keyed by {grain}. A "
                        "delta's cells are its source's own buckets -- there is no grain to "
                        "group them to, the way a series has -- so ninety days of "
                        "quarter-hours would be 8,640 cells on the wire: the raw collection "
                        "the payload exists to withhold. Take the delta over a day-keyed "
                        "figure, or declare one at the grain the trend is about.",
                        stat.line,
                    )
            if stat.fn == "series":
                series_declared += 1
                if series_declared > 1:
                    raise CheckError(
                        f"reading {d.name} declares two series. A response carries one, so "
                        "the second would be whichever the serve path kept, silently.",
                        stat.line,
                    )
                if live:
                    raise CheckError(
                        f"reading {d.name} declares a series, but a live reading measures "
                        "records as they stand -- there are no stored buckets to be the "
                        "points.",
                        stat.line,
                    )
                if grain == "minute":
                    raise CheckError(
                        f"reading {d.name} takes a series over a figure keyed by the "
                        "minute, and a series' points are the stored buckets: over a "
                        "sparse figure a minute bucket holds one record, so the point "
                        "*is* the record -- the raw collection the payload exists to "
                        "withhold. Group the figure by a coarser rule under its own "
                        "name, and read that.",
                        stat.line,
                    )
            seen.add(stat.fn)
        if "sum" in seen and seen & {"mean", "median", "worst"}:
            raise CheckError(
                f"reading {d.name} calculates both a sum and a distribution. Two numbers a "
                "reader can divide produce a third that no definition claims.",
                d.line,
            )
        for r in d.requires:
            if r.set not in bound:
                raise CheckError(
                    f'requires names "{r.set}", which is not a set defined in depends.', r.line
                )

    def _reading_unit(
        self, source: FigureUnit, d: ReadingDecl
    ) -> Literal["count", "duration", "effort"]:
        if source == "effort":
            raise CheckError(
                f"{d.name} reads a figure measured in effort -- seconds of working time, "
                "rendered against the tenant's working day. Every renderer on the reading "
                "path branches on count or duration, so an effort would be banded as "
                "wall-clock and printed as raw seconds.",
                d.line,
            )
        return "count" if source == "count" else "duration"

    def _band(
        self, d: ReadingDecl, scope: str, source: FigurePlan | None
    ) -> tuple[Ladder | None, StatisticFn | None, tuple[str, ...]]:
        """A reading's band: the same ladder a figure writes, judged over one
        of the reading's statistics.

        `source` is the figure a windowed reading summarises, and None for a
        live one. It is what decides the grain a threshold figure has to share:
        a window is a span of *that* figure's buckets, so a goal cut monthly
        against a reading over daily buckets is a comparison whose two sides
        never meet.
        """
        if d.band is None:
            return (None, None, ())
        band = d.band
        # The default is `mean`, so a band with no `on` over a reading that
        # calculates no mean is checked too. Left unchecked it compiled and
        # `level_of` answered "unknown" for every subject at every value -- a row
        # that is permanently grey and reads as missing data rather than as a
        # broken definition.
        wanted = band.on or "mean"
        if wanted not in {s.fn for s in d.calculate}:
            written = f"{band.on}(...)" if band.on else "the mean, by default"
            raise CheckError(
                f"reading {d.name} bands on {written}, which it does not calculate. It "
                f"calculates {', '.join(sorted({s.fn for s in d.calculate}))}; name one of "
                "those with `on`, or the band colours nothing and every row reads unknown.",
                band.line,
            )
        if wanted in ("delta", "series"):
            raise CheckError(
                f"reading {d.name} bands on {wanted}(...), which is one cell per bucket "
                "rather than one number. A band compares a single value against a "
                "threshold, so there is nothing here for it to colour -- left to "
                "compile, every row would band unknown for ever, which reads as missing "
                "data rather than as a broken definition. Band a scalar statistic, or "
                "leave the band off.",
                band.line,
            )

        for name in _parts_in(band.ladder):
            if name != "value":
                raise CheckError(
                    f'reading {d.name}\'s band reads "{name}". The only binding in scope '
                    'is "value" -- the statistic this band judges -- because a band says '
                    "which state a number is in, and reading anything else would make it "
                    "a second calculation sharing the reading's name.",
                    band.line,
                )
        for expr in [*(r.then for r in band.ladder.rungs), band.ladder.otherwise]:
            if not isinstance(expr, Text):
                raise CheckError(
                    f"reading {d.name}'s band must answer a word on every rung. A rung "
                    "answering a number would be a second statistic, and it would reach "
                    "a screen as an integer in a column of words.",
                    getattr(expr, "line", band.line),
                )
        if any(isinstance(node, Coord) for node in _walk(band.ladder)):
            raise CheckError(
                f"reading {d.name}'s band reads a `:{{bucket}}` coordinate. A reading "
                "answers over a *window* of buckets rather than at one of them, so there "
                "is no coordinate to stand at -- name the figure bare and it is read "
                "over the same window, through the same statistic.",
                band.line,
            )

        reads: set[str] = set()

        def threshold(name: str, line: int) -> None:
            found = _find(self.figures, name)
            if found is None:
                raise CheckError(
                    f'reading {d.name}\'s band compares against "{name}", which is not a '
                    "figure. A band's threshold is a fact -- a figure computed from "
                    "records -- so the number deciding whether a reader should worry can "
                    "be cited like every other number on the card. If this was a tenant "
                    "dial, declare the goal as a figure over the records that set it and "
                    "name that figure here.",
                    line,
                )
            if found.scope != scope:
                raise CheckError(
                    f"reading {d.name}'s band compares against {name}, which is one value "
                    f"per {found.scope} where this reading is scoped to {scope}. Different "
                    "scopes are different id spaces, so every lookup would miss.",
                    line,
                )
            if found.unit == "level":
                raise CheckError(
                    f"reading {d.name}'s band compares against {name}, which stores a word. "
                    "There is no order between words, so the comparison has no answer.",
                    line,
                )
            # The figure path has always refused this and the reading path
            # never did. It matters more here, not less: the retired `band low
            # against <dial> in minutes` clause was removed on the argument
            # that "a figure carries its own unit and the checker compares the
            # two, so the mistake is unwritable" -- which was true of figures
            # and false of readings. A count of deliveries judged against a
            # duration in seconds clears every rung, for ever.
            if source is not None and found.unit != source.unit:
                raise CheckError(
                    f"reading {d.name} answers a {source.unit} and its band compares "
                    f"against {name}, which answers a {found.unit}. Both are numbers by "
                    "the time the ladder sees them, so the comparison would run and be "
                    "wrong by whatever the two units differ by.",
                    line,
                )
            if source is None:
                # A live reading has no window and no sequence: it counts or
                # measures records as they stand, so its threshold is one
                # value per subject and a sequenced figure has nothing to
                # reduce over.
                if found.grain is not None:
                    raise CheckError(
                        f"reading {d.name} measures records as they stand and its band "
                        f"compares against {name}, which holds one value per {found.grain}. "
                        "There is no window to reduce that sequence over, so the "
                        "comparison would have to pick a bucket -- and whichever it picked "
                        "would be a fabrication.",
                        line,
                    )
            elif found.grain != source.grain:
                raise CheckError(
                    f"reading {d.name} reads {source.name}, which holds one value per "
                    f"{source.grain}, and its band compares against {name}, which holds one "
                    f"value per {found.grain or 'subject'}. A window is a span of the source "
                    "figure's own buckets, so the threshold has to be cut the same way -- "
                    "days against days, months against months.",
                    line,
                )
            reads.add(name)

        def resolve(e: CalcExpr) -> CalcExpr:
            if isinstance(e, Setting):
                threshold(e.path, e.line or band.line)
                return FigureRef(name=e.path, line=e.line)
            if isinstance(e, Ladder):
                return replace(
                    e,
                    rungs=tuple(
                        replace(
                            r,
                            left=resolve(r.left),
                            then=resolve(r.then),
                            right=resolve(r.right) if r.right is not None else None,
                        )
                        for r in e.rungs
                    ),
                    otherwise=resolve(e.otherwise),
                )
            if isinstance(e, Arith):
                return replace(e, left=resolve(e.left), right=resolve(e.right))
            if isinstance(e, Pick):
                return replace(e, left=resolve(e.left), right=resolve(e.right))
            return e

        # The reading's own unit is the figure it windows -- a window over
        # durations answers a duration -- so a literal in its ladder needs the
        # same scale a figure's does. `count` is the one statistic that
        # changes the quantity: how many buckets held a value is a tally
        # whatever the buckets held.
        answered = "count" if wanted == "count" else (source.unit if source else "count")
        resolved = _scaled(resolve(band.ladder), answered, f"reading {d.name}")
        assert isinstance(resolved, Ladder)
        return (resolved, wanted, tuple(sorted(reads)))

    # --------------------------------------------------------- projection --

    def _population(self, d: ProjectDecl, kind: str, expr: SetExpr) -> None:
        """The `from` clause: which records get a row at all.

        The same set language a figure's `depends` speaks, with rules of its
        own because there is no subject here: `from` decides which records
        *become* rows, so nothing exists yet to scope a bucket by, and a
        projection has no depends block for a bare name to refer to. Each
        refusal below is a case that would otherwise resolve to the empty set
        -- and an empty population is not an error anybody sees, it is a page
        with no rows that looks like a complete one.
        """
        if isinstance(expr, SetOp):
            self._population(d, kind, expr.left)
            self._population(d, kind, expr.right)
            return
        if isinstance(expr, SetRef):
            raise CheckError(
                f'projection {d.name} draws its population from "{expr.name}", which is '
                "not a declared filter. A projection has no depends block to define a "
                "named set, so `from` may only combine predicate and presence filters.",
                expr.line,
            )
        assert isinstance(expr, SetIndex)
        idx = self.indexes.get(expr.index)
        if idx is None:
            raise CheckError(
                f'there is no group or filter called "{expr.index}". Declared: '
                f"{', '.join(sorted(self.indexes)) or 'none'}.",
                expr.line,
            )
        if isinstance(expr.bucket, BucketScope):
            raise CheckError(
                f"projection {d.name}'s population scopes {expr.index} to a subject, but "
                "`from` decides which records become rows, so there is no row to scope a "
                "bucket by. Name the filter on its own.",
                expr.line,
            )
        if idx.bucketed:
            raise CheckError(
                f"projection {d.name}'s population reads {expr.index}, a group bucketing "
                f"by {' and '.join(p.field for p in _index_fields(idx.spec))} rather than "
                "holding a single bucket. Read whole it looks for a bucket keyed by the "
                "empty string, finds nothing, and the page is empty while looking "
                "complete. Only a predicate or a presence filter may appear in `from`.",
                expr.line,
            )
        if isinstance(idx.spec, ByAge):
            raise CheckError(
                f"projection {d.name}'s population reads {expr.index}, which narrows by "
                "age against the clock. Membership there is as stale as the last "
                "reconcile, and no pointer covers a filter only a `from` reads -- moving "
                "the dial it names would change which records are on the page and "
                "nothing would rebuild it. Name a predicate or a presence filter instead.",
                expr.line,
            )
        # Compared in id space, not raw kind: a `keyed as` kind's record keys
        # ARE the other kind's ids, so an index over its own records -- or any
        # index in that shared space -- holds exactly the keys its rows carry.
        # Comparing raw kinds refused those legitimate pages with a message
        # stating the opposite of the truth.
        if idx.id_space != self._keyed.get(kind, kind):
            raise CheckError(
                f"projection {d.name} is over {kind} and its population reads "
                f"{expr.index}, whose members are {idx.id_space} ids. Ids from another "
                f"space match no {kind} record, so every row would be filtered away -- "
                "an empty page that looks like a complete one, with nothing thrown.",
                expr.line,
            )

    def _projection(self, d: ProjectDecl) -> None:
        self._claim(d.name, "projection", d.line)
        kind = d.name.split(".", 1)[0]
        self._fact_kind(kind, f"projection {d.name} is over", d.line)
        if d.frm is not None:
            self._population(d, kind, d.frm)

        bound: dict[str, str] = {}
        moments: set[str] = set()
        fields: list[tuple[str, str, object, object]] = []
        joins: list[object] = []

        for f in d.fields:
            self._bind(bound, f.name, "field", d.name, f.line)
            if f.join is not None:
                self._fact_kind(f.join.kind, f"projection {d.name} joins through", f.line)
                joins.append(f.join)
            self._row_field_exists(d, kind, f)
            if f.type == "date":
                moments.add(f.name)
                bound[f.name] = "date"
            elif f.type == "number" or f.type == "flag":
                bound[f.name] = "number"
            else:
                bound[f.name] = "text"
            fields.append((f.name, f.path, f.type, f.join))

        reads: list[tuple[str, str, FigureUnit, bool]] = []
        for r in d.reads:
            self._bind(bound, r.name, "read", d.name, r.line)
            source = _find(self.figures, r.figure)
            if source is None:
                raise CheckError(f'there is no figure called "{r.figure}".', r.line)
            if r.band and source.band is None:
                raise CheckError(
                    f"projection {d.name} reads the band of {r.figure}, which declares no "
                    "band. Every row would bind nothing under that name, so every rung "
                    "testing it would stop and every flag gated on it would never fire -- "
                    "a column of dashes and a silently shorter page.",
                    r.line,
                )
            if source.scope != kind:
                raise CheckError(
                    f"projection {d.name} is over {kind} and reads {r.figure}, which is one "
                    f"value per {source.scope}. Every row would be looked up under an id from "
                    "another space and find nothing -- a column of dashes, for ever.",
                    r.line,
                )
            if source.grain is not None:
                raise CheckError(
                    f"projection {d.name} reads {r.figure}, which is time-keyed: one value "
                    f"per {source.grain} rather than one per row.",
                    r.line,
                )
            if source.across is not None:
                raise CheckError(
                    f"projection {d.name} reads {r.figure}, which is split across "
                    f"{source.across}.",
                    r.line,
                )
            # A banded read binds the word, whatever the figure's own unit is:
            # `band of team_person.wip` is a level over a count.
            unit: FigureUnit = "level" if r.band else source.unit
            if unit == "moment":
                moments.add(r.name)
                bound[r.name] = "date"
            elif unit == "level":
                bound[r.name] = "text"
            else:
                bound[r.name] = "number"
            reads.append((r.name, r.figure, unit, r.band))

        values: list[tuple[str, CalcExpr, FigureUnit]] = []
        for v in d.values:
            self._bind(bound, v.name, "value", d.name, v.line)
            unit = self._row_value(v, bound, moments, d.name, "projection")
            bound[v.name] = "text" if unit == "level" else "number"
            values.append((v.name, v.expr, unit))

        for flag in d.flags:
            self._flag(flag, bound, d.name, "projection")

        if d.omit is not None:
            # The same row language a flag's `when` speaks, checked the same
            # way: a gate naming nothing would judge every row by a value that
            # is never there, keep them all, and read as a rule being enforced.
            self._condition(d.omit, bound, d.name, "projection")

        if d.sort is not None:
            if d.sort.name not in bound:
                raise CheckError(
                    f'projection {d.name} sorts by "{d.sort.name}", which nothing here binds.',
                    d.sort.line,
                )
            if d.sort.name in moments:
                raise CheckError(
                    f'projection {d.name} sorts by "{d.sort.name}", which is a moment.',
                    d.sort.line,
                )
        if d.limit is not None and d.sort is None:
            raise CheckError(
                f"projection {d.name} takes {d.limit} rows and does not say in what order. "
                "Which rows survive would be arbitrary, would look like a complete list, and "
                "would change between runs for reasons no reader can see.",
                d.line,
            )

        # Whatever survived as a bare dotted name here named no figure, no
        # column and no field, so there is nothing left for it to be.
        for path in sorted(
            {p for _, e, _ in values for p in _settings_in(e)}
            | _flag_settings(d.flags)
            | _condition_settings(d.omit)
        ):
            raise CheckError(
                f'projection {d.name} reads "{path}", which is a tenant dial. A '
                "definition's numbers come from facts: bind the figure that holds this "
                "threshold with `read:` and compare against that column, or write the "
                "number here where a reader can see it. A dial varies invisibly -- it "
                "moves which rows earn a flag with nothing in the row to say so.",
                d.line,
            )

        plan = ProjectPlan(
            name=d.name,
            kind=kind,
            doc=d.doc,
            fields=tuple(fields),  # type: ignore[arg-type]
            reads=tuple(reads),
            values=tuple(values),
            flags=d.flags,
            frm=d.frm,
            omit=d.omit,
            sort=d.sort,
            limit=d.limit,
            joins=tuple(joins),  # type: ignore[arg-type]
            indexes=tuple(sorted(_indexes_in(d.frm))) if d.frm is not None else (),
            figures=tuple(r.figure for r in d.reads),
        )
        self.projections.append(
            ProjectPlan(**{**plan.__dict__, "version": version_of(_project_hash(plan, self.indexes))})
        )

    def _row_field_exists(self, d: ProjectDecl, kind: str, f: FieldDecl) -> None:
        """A row field against the declared world: the path exists, the type
        agrees, and nothing on the way is a list.

        The type must agree *exactly* -- `as date` over a text field compiles
        today and parses nothing at render time, a column of dashes wearing a
        declared type. And a row is about one record, so a path crossing a
        `many` is refused whole: a field holds one value, and "the first in
        sorted order" is an answer about no element in particular.
        """
        if not self.facts:
            return
        what = f"projection {d.name}"
        source_kind = kind
        if f.join is not None:
            found = self._record_field(kind, f.join.field, what, f.line)
            assert found is not None
            _, crossed = found
            if crossed:
                raise CheckError(
                    f'{what} joins through "{f.join.field}", which crosses a list -- '
                    "several candidate ids per record, so the join would answer "
                    "nothing for every row that carries two.",
                    f.line,
                )
            # The matching path on the other kind must exist too: unmatched,
            # the join table is empty and every row's column is None -- a
            # permanently blank column wearing a declared join.
            self._record_field(f.join.kind, f.join.path, what, f.line)
            source_kind = f.join.kind
        found = self._record_field(source_kind, f.path, what, f.line)
        assert found is not None
        node, crossed = found
        if crossed:
            raise CheckError(
                f'{what} binds "{f.name}" from "{f.path}", which crosses a list -- '
                "several values per record, and a row's field holds one.",
                f.line,
            )
        wanted = {"text": "text", "date": "moment", "number": "number", "flag": "flag"}[f.type]
        if node.type != wanted:
            raise CheckError(
                f'{what} binds "{f.name}" as {f.type}, but {source_kind}.{f.path} is a '
                f"{node.type} rather than a {wanted}.",
                f.line,
            )

    # ------------------------------------------------------------ summary --

    def _summary(self, d: SummariseDecl) -> None:
        self._claim(d.name, "summary", d.line)
        over = _find(self.projections, d.over)
        if over is None:
            raise CheckError(
                f'there is no projection called "{d.over}". A summary aggregates the rows of '
                "exactly one projection declared before it.",
                d.line,
            )

        row_names = (
            {f[0] for f in over.fields} | {r[0] for r in over.reads} | {v[0] for v in over.values}
        )
        bound: dict[str, str] = {}

        for c in d.counts:
            self._bind(bound, c.name, "count", d.name, c.line)
            if c.name in row_names:
                raise CheckError(
                    f'summary {d.name} binds "{c.name}", which is already a value of {d.over}. '
                    "One word would mean one row in one line and the whole population in the "
                    "next, whichever way it resolved.",
                    c.line,
                )
            if c.when is not None:
                self._condition(c.when, _row_kinds(over), d.name, "count")
            bound[c.name] = "number"

        for t in d.totals:
            self._bind(bound, t.name, "total", d.name, t.line)
            if t.of not in row_names:
                raise CheckError(
                    f'total "{t.name}" adds up "{t.of}", which {d.over} does not bind. Bound '
                    f"there: {', '.join(sorted(row_names))}.",
                    t.line,
                )
            column = _column_kind(over, t.of)
            if column != "number":
                raise CheckError(
                    f'total "{t.name}" adds up "{t.of}", which holds {column}. Only a number '
                    "may be summed: a word would concatenate and a date is not in the numeric "
                    "namespace at all, so it would answer nothing for every row.",
                    t.line,
                )
            if t.when is not None:
                self._condition(t.when, _row_kinds(over), d.name, "total")
            bound[t.name] = "number"

        values: list[tuple[str, CalcExpr, FigureUnit]] = []
        for v in d.values:
            self._bind(bound, v.name, "value", d.name, v.line)
            unit = self._row_value(v, bound, set(), d.name, "summary")
            bound[v.name] = "text" if unit == "level" else "number"
            values.append((v.name, v.expr, unit))

        for flag in d.flags:
            self._flag(flag, bound, d.name, "summary")

        # The `where` of a count and of a total are walked too. They were not
        # before, so a dial named there reached no fingerprint -- moving it
        # changed the tally with nothing noticing. Refused now rather than
        # collected, which closes the hole from the other side.
        for path in sorted(
            {p for _, e, _ in values for p in _settings_in(e)}
            | _flag_settings(d.flags)
            | {p for c in d.counts for p in _condition_settings(c.when)}
            | {p for t in d.totals for p in _condition_settings(t.when)}
        ):
            raise CheckError(
                f'summary {d.name} reads "{path}", which is a tenant dial. A summary '
                "counts the rows a projection produced, so a threshold belongs on the "
                "projection: bind the figure that holds it with `read:` and compare "
                "against that column, or write the number where a reader can see it.",
                d.line,
            )

        plan = SummarisePlan(
            name=d.name,
            over=d.over,
            doc=d.doc,
            counts=tuple((c.name, c.when) for c in d.counts),
            totals=tuple((t.name, t.of, t.unit, t.when) for t in d.totals),
            values=tuple(values),
            flags=d.flags,
        )
        self.summaries.append(
            SummarisePlan(
                **{
                    **plan.__dict__,
                    "version": version_of(
                        {
                            "name": plan.name,
                            "over": plan.over,
                            "over_version": over.version,
                            "counts": [[n, _cond_hash(c)] for n, c in plan.counts],
                            "totals": [[n, o, u, _cond_hash(c)] for n, o, u, c in plan.totals],
                            "values": [[n, _calc_hash(e), u] for n, e, u in plan.values],
                            "flags": [_flag_hash(f) for f in plan.flags],
                        }
                    ),
                }
            )
        )

    # ------------------------------------------------------------- bundle --

    def _bundle(self, d: BundleDecl) -> None:
        """A bundle names things; the checker's job here is making sure each
        name is the thing its keyword claims, so a broken tile is a build
        error rather than a serve-time surprise.

        One deliberate exception: a bare live-reading member compiles, and a
        tile naming one answers the same not-yet-servable refusal the
        reading's own route gives. That mirrors the language's standing
        position on live readings -- declared, checked, versioned, not yet
        served -- and refusing the member here would mean re-teaching every
        such tile on the day live serving lands, for a rule the reading
        itself does not have."""
        self._claim(d.name, "bundle", d.line)
        self._fact_kind(d.name.split(".", 1)[0], f"bundle {d.name} is named under", d.line)

        slots: set[str] = set()
        served: set[tuple[str, str, tuple[WindowSpec, ...] | None]] = set()
        members: list[BundleMemberPlan] = []
        for m in d.members:
            if m.slot in slots:
                raise CheckError(
                    f'bundle {d.name} binds "{m.slot}" twice. A slot is the address a '
                    "client reads one member at, and two members under one address "
                    "would make which one it gets arbitrary.",
                    m.line,
                )
            slots.add(m.slot)
            # Two slots over one member are welcome when their window lists
            # differ -- two spans of one reading are two questions. The same
            # member under the same windows is refused: two names for one
            # answer, the duplication the pre-binding grammar refused whole.
            question = (m.kind, m.name, m.windows)
            if question in served:
                raise CheckError(
                    f"bundle {d.name} names {m.name} twice over the same windows. Two "
                    "slots would be two names for one answer; if the second slot wants "
                    "a different question, give it a different window list.",
                    m.line,
                )
            served.add(question)
            members.append(self._bundle_member(d, m))

        plan = BundlePlan(name=d.name, doc=d.doc, members=tuple(members))
        self.bundles.append(
            BundlePlan(**{**plan.__dict__, "version": version_of(_bundle_hash(plan))})
        )

    def _bundle_member(self, d: BundleDecl, m: BundleMember) -> BundleMemberPlan:
        if m.kind == "figure":
            figure = _find(self.figures, m.name)
            if figure is None:
                self._not_a_member(d, m, "figure")
            # The two shapes the bulk results surface deliberately does not
            # push, refused here for the same reason at compile time: a tile
            # is a subscription, and neither shape is what a tile wants.
            if figure.grain is not None:
                raise CheckError(
                    f"bundle {d.name} names {m.name}, which is time-keyed: one value per "
                    f"subject per {figure.grain}, so the tile would carry every stored "
                    "bucket of every subject on every request. What a tile wants from a "
                    "time-keyed figure is a statistic over a window -- declare a reading "
                    "over it and name that.",
                    m.line,
                )
            if figure.across is not None:
                raise CheckError(
                    f"bundle {d.name} names {m.name}, which is split across "
                    f"{figure.across}: one value per pair rather than one per subject. "
                    "Name the rollup that adds its parts up -- a tile serving raw pairs "
                    "would put a slice of a population under a heading naming the whole "
                    "of it.",
                    m.line,
                )
            return BundleMemberPlan(slot=m.slot, kind="figure", name=m.name)

        if m.kind == "reading":
            reading = _find(self.readings, m.name)
            if reading is None:
                self._not_a_member(d, m, "reading")
            if reading.mode == "live" and m.windows is not None:
                raise CheckError(
                    f"bundle {d.name} gives {m.name} windows, and {m.name} measures "
                    "records as they stand -- there is nothing stored to window. A live "
                    "member is named bare, the way a live reading declares `()`: the "
                    "member's argument list and the reading's source form encode "
                    "liveness twice, loudly.",
                    m.line,
                )
            if m.windows is not None and reading.source is not None:
                # The refusal the serving path makes at request time, made
                # here at compile time where the member's window list is
                # written down: a span whose reach exceeds the ceiling under
                # the source's own bucket rule. One shared implementation,
                # so the tile's build error and the route's 422 speak the
                # same words -- and rule-aware, because 121 positions is
                # under a week of hours and thirty years of quarters.
                source = _find(self.figures, reading.source)
                rule = (source.grain if source is not None else None) or "day"
                for spec in m.windows:
                    refusal = refuse_reach(spec, rule)
                    if refusal is not None:
                        raise CheckError(f"bundle {d.name}: {refusal}", m.line)
            return BundleMemberPlan(
                slot=m.slot, kind="reading", name=m.name, windows=m.windows
            )

        if m.kind == "projection":
            if _find(self.projections, m.name) is None:
                self._not_a_member(d, m, "projection")
            return BundleMemberPlan(slot=m.slot, kind="projection", name=m.name)

        if m.kind == "summarise":
            if _find(self.summaries, m.name) is None:
                self._not_a_member(d, m, "summary")
            return BundleMemberPlan(slot=m.slot, kind="summary", name=m.name)

        assert_never(m.kind)

    def _not_a_member(self, d: BundleDecl, m: BundleMember, wanted: str) -> NoReturn:
        """Why a member did not resolve, said in terms of what the name
        actually is -- a keyword mismatch is a different mistake from a typo,
        and a bundle smuggled in under `figure` is a third."""
        held = self._names.get(m.name)
        if held == "a bundle":
            raise CheckError(
                f"bundle {d.name} names {m.name}, which is a bundle. A bundle may not "
                "name another bundle: composition stays flat, so there is no nesting to "
                "walk and no cycle to refuse.",
                m.line,
            )
        if held is not None:
            raise CheckError(
                f"bundle {d.name} names {m.name} as a {wanted}, but it is {held}. A "
                "member is written under its own keyword, so what travels -- a value, "
                "windows, rows, or the population row alone -- is never a surprise.",
                m.line,
            )
        raise CheckError(f'there is no {wanted} called "{m.name}".', m.line)

    # ------------------------------------------------------------- shared --

    def _bind(self, bound: dict[str, str], name: str, what: str, owner: str, line: int) -> None:
        if name in bound:
            raise CheckError(f'{owner} binds "{name}" twice.', line)

    def _row_value(
        self,
        v: ValueDecl,
        bound: dict[str, str],
        moments: set[str],
        owner: str,
        noun: str,
    ) -> FigureUnit:
        kind = self._row_kind(v.expr, bound, moments, owner, noun)
        arithmetic = not isinstance(v.expr, Ladder)
        if isinstance(v.expr, Ladder):
            if kind == "text":
                if v.unit is not None:
                    raise CheckError(
                        f'value "{v.name}" is a ladder returning words and declares '
                        f'"in {v.unit}". Its unit is worked out for you.',
                        v.line,
                    )
                return "level"
            if v.unit is None:
                raise CheckError(
                    f'value "{v.name}" is a ladder returning numbers and does not say what '
                    "they are. A ladder returning words has a unit nothing needs to be told; "
                    "one returning numbers is a different thing wearing the same syntax.",
                    v.line,
                )
            return v.unit
        if arithmetic and v.unit is None:
            raise CheckError(
                f'value "{v.name}" is arithmetic and does not say what its number is. The '
                "same two operands divided give a share and subtracted give the quantity they "
                "were both in.",
                v.line,
            )
        assert v.unit is not None
        return v.unit

    def _row_kind(
        self, e: CalcExpr, bound: dict[str, str], moments: set[str], owner: str, noun: str
    ) -> _Kind:
        if isinstance(e, Number):
            return "number"
        if isinstance(e, Text):
            return "text"
        if isinstance(e, Setting):
            return "number"
        if isinstance(e, Part):
            if e.name not in bound:
                raise CheckError(
                    f'{noun} {owner} reads "{e.name}", which nothing binds. Bound: '
                    f"{', '.join(sorted(bound)) or 'nothing'}.",
                    e.line,
                )
            return bound[e.name]
        if isinstance(e, DaysBetween):
            for side in (e.frm, e.to):
                if side == "now":
                    continue
                if side not in moments:
                    raise CheckError(
                        f'{noun} {owner} measures days from "{e.frm}" to "{e.to}", and '
                        f'"{side}" is not a moment. A span needs two instants; `as date` is '
                        "what says a field holds one.",
                        e.line,
                    )
            return "number"
        if isinstance(e, Arith):
            for operand in (e.left, e.right):
                found = self._row_kind(operand, bound, moments, owner, noun)
                if found != "number":
                    raise CheckError(
                        f"arithmetic needs numbers on both sides, and this side is a {found}.",
                        e.line,
                    )
            return "number"
        if isinstance(e, Pick):
            for operand in (e.left, e.right):
                found = self._row_kind(operand, bound, moments, owner, noun)
                if found != "number":
                    raise CheckError(
                        f"{e.which} needs numbers on both sides, and this side is a {found}.",
                        e.line,
                    )
            return "number"
        if isinstance(e, Ladder):
            results = []
            for rung in e.rungs:
                self._row_kind(rung.left, bound, moments, owner, noun)
                if rung.right is not None:
                    self._row_kind(rung.right, bound, moments, owner, noun)
                results.append(self._row_kind(rung.then, bound, moments, owner, noun))
            results.append(self._row_kind(e.otherwise, bound, moments, owner, noun))
            if len(set(results)) > 1:
                raise CheckError(
                    f"a value in {noun} {owner} returns a word from one rung and a number "
                    "from another.",
                    e.line,
                )
            return results[0]
        if isinstance(e, Coord):
            raise CheckError(
                f"{noun} {owner} reads {e.name}:{{bucket}}. A row is one record, not a "
                "coordinate in a sequence -- a sequenced figure is read over a range by a "
                "reading.",
                e.line,
            )
        if isinstance(e, (Count, ListOf, Sum, Extreme, FieldPick, FieldTotal, BucketStat)):
            verb = {
                "Count": "counts",
                "ListOf": "lists",
                "Sum": "totals",
                "Extreme": "takes the extreme of",
                # A projection has one record per row already, so "the latest
                # of a set" is an aggregate here exactly as a count is.
                "FieldPick": "takes the extreme of",
                "FieldTotal": "totals",
                "BucketStat": "averages",
            }[type(e).__name__]
            raise CheckError(
                f"{noun} {owner} {verb} something. A projection aggregates nothing -- those "
                "are figures, and offering them here would be a second way to compute a "
                "number this product claims has exactly one.",
                e.line,
            )
        if isinstance(e, FigureRef):  # pragma: no cover - band-only
            raise CheckError(
                f"{noun} {owner} names the figure {e.name} outright. A row reads a figure "
                "through `read:`, which binds it by name.",
                e.line,
            )
        if isinstance(e, SubjectField):  # pragma: no cover - figure-only
            raise CheckError(
                f"{noun} {owner} reads {e.kind}.{e.field} off a subject's record. A row "
                "IS a record: name the field in `field:` and it is bound for the row.",
                e.line,
            )
        assert_never(e)

    def _condition(self, c: Condition, bound: dict[str, str], owner: str, noun: str) -> None:
        self._row_kind(c.left, bound, set(), owner, noun)
        if c.right is not None:
            self._row_kind(c.right, bound, set(), owner, noun)

    def _flag(self, f: FlagDecl, bound: dict[str, str], owner: str, noun: str) -> None:
        self._condition(f.when, bound, owner, noun)
        for template in (f.label, f.detail, f.action):
            if template is None:
                continue
            for ref in _placeholders(template):
                if ref not in bound:
                    raise CheckError(
                        f"flag {f.name} interpolates {{{ref}}}, which nothing here binds. A "
                        "placeholder naming nothing would print the word undefined in front "
                        "of a reader.",
                        f.line,
                    )

    def _require_set(
        self, owner: str, name: str, sets: dict[str, SetExpr], line: int
    ) -> None:
        if name not in sets:
            raise CheckError(
                f'calculate reads "{name}", which is not a set defined in depends. Defined: '
                f"{', '.join(sorted(sets)) or 'none'}.",
                line,
            )

    def _require_measure(self, owner: str, name: str, line: int) -> CompiledMeasure:
        m = self.measures.get(name)
        if m is None:
            raise CheckError(
                f'there is no measure called "{name}". Declared: '
                f"{', '.join(sorted(self.measures)) or 'none'}.",
                line,
            )
        return m


# ------------------------------------------------------------- helpers --


def _unique_fields(owner: str, fields: tuple[FactField, ...], line: int) -> None:
    seen: set[str] = set()
    for f in fields:
        if f.name in seen:
            raise CheckError(
                f'fact {owner} declares "{f.name}" twice. One record, one field, one '
                "type -- a second declaration is a first place for two to disagree.",
                f.line or line,
            )
        seen.add(f.name)
        _unique_fields(f"{owner}.{f.name}", f.children, f.line or line)


def _compiled_fields(fields: tuple[FactField, ...]) -> tuple[CompiledFactField, ...]:
    return tuple(
        CompiledFactField(
            name=f.name,
            type=f.type,
            many=f.many,
            doc=f.doc,
            children=_compiled_fields(f.children),
        )
        for f in fields
    )


def _fact_field_hash(fields: tuple[CompiledFactField, ...]) -> list[object]:
    """The fields and their shapes, and nothing else: prose, the name field
    and the url field are rendering, out of the hash for the reason a display
    template is.

    Sorted by name, because declaration order is rendering too -- every
    consumer keys the fields by name, so reordering a fact's body is the
    "plan built in a different order" case the hashing rules promise not to
    fork a version over. Contrast a projection's row fields, where the list
    order *is* the answer.
    """
    return [
        {
            "name": f.name,
            "type": f.type,
            "many": f.many or None,
            "children": _fact_field_hash(f.children) or None,
        }
        for f in sorted(fields, key=lambda f: f.name)
    ]


def _decl_word(spec: IndexBy) -> str:
    """The keyword this declaration was written under.

    For messages only: a refusal should speak the author's vocabulary --
    "group" for the shapes that fan out, "filter" for the ones that narrow --
    rather than the implementation's collective term for both.
    """
    if isinstance(spec, (ByField, ByComposite)):
        return "group"
    if isinstance(spec, (ByPredicate, ByPresence, ByAge)):
        return "filter"
    assert_never(spec)


def _index_fields(spec: IndexBy) -> list[IndexField]:
    if isinstance(spec, ByField):
        return [spec.part]
    if isinstance(spec, ByComposite):
        return list(spec.parts)
    if isinstance(spec, ByPredicate):
        return [IndexField(field=spec.field)]
    if isinstance(spec, ByPresence):
        return [IndexField(field=spec.field)]
    if isinstance(spec, ByAge):
        return [IndexField(field=spec.field)]
    assert_never(spec)


def _scope_indexes(expr: SetExpr) -> list[tuple[str, object]]:
    if isinstance(expr, SetIndex):
        return [(expr.index, expr.bucket)]
    if isinstance(expr, SetRef):
        return []
    if isinstance(expr, SetOp):
        return _scope_indexes(expr.left) + _scope_indexes(expr.right)
    assert_never(expr)


def _indexes_in(expr: SetExpr | None) -> set[str]:
    if expr is None:
        return set()
    return {name for name, _ in _scope_indexes(expr)}


def _indexes_in_sets(sets: tuple) -> set[str]:  # type: ignore[type-arg]
    out: set[str] = set()
    for s in sets:
        out |= _indexes_in(s.expr)
    return out


def _walk(e: CalcExpr) -> list[CalcExpr]:
    out = [e]
    if isinstance(e, (Arith, Pick)):
        out += _walk(e.left) + _walk(e.right)
    elif isinstance(e, Ladder):
        for rung in e.rungs:
            out += _walk(rung.left)
            if rung.right is not None:
                out += _walk(rung.right)
            out += _walk(rung.then)
        out += _walk(e.otherwise)
    return out


def _settings_in(e: CalcExpr) -> set[str]:
    return {n.path for n in _walk(e) if isinstance(n, Setting)}


def _parts_in(e: CalcExpr) -> set[str]:
    """Every bare name an expression reads.

    A bare name is whatever the definition bound above -- a set, a combined
    figure, or, inside a band, the one reserved word `value`. Used to check that
    a band reads nothing else, which is what keeps it a band rather than a second
    calculation sharing the first one's name.
    """
    return {n.name for n in _walk(e) if isinstance(n, Part)}


def _measures_in(e: CalcExpr) -> set[str]:
    out: set[str] = set()
    for n in _walk(e):
        # Written as two arms rather than one `or`, because collapsing them
        # loses the narrowing: a `Sum`'s measure is optional and the combined
        # condition proves it is present without the type checker being able to
        # see that it did.
        if isinstance(n, (ListOf, Extreme, BucketStat)):
            out.add(n.measure)
        elif isinstance(n, Sum) and n.measure is not None:
            out.add(n.measure)
    return out


def _flag_settings(flags: tuple[FlagDecl, ...]) -> set[str]:
    out: set[str] = set()
    for f in flags:
        out |= _condition_settings(f.when)
    return out


def _condition_settings(c: Condition | None) -> set[str]:
    if c is None:
        return set()
    out = _settings_in(c.left)
    if c.right is not None:
        out |= _settings_in(c.right)
    return out


def _placeholders(template: str) -> list[str]:
    """`{name}` and `{name|singular:plural}`.

    The plural form reads the same binding it prints, so a sentence cannot
    pluralise on one number and print another.
    """
    out: list[str] = []
    i = 0
    while i < len(template):
        if template[i] == "{":
            end = template.find("}", i)
            if end == -1:
                break
            inner = template[i + 1 : end]
            out.append(inner.split("|", 1)[0])
            i = end + 1
        else:
            i += 1
    return out


def _find(items, name: str):  # type: ignore[no-untyped-def]
    for item in items:
        if item.name == name:
            return item
    return None


def _row_kinds(plan: ProjectPlan) -> dict[str, str]:
    """What each of a projection's columns holds, for a summary's `where`.

    Typed as "number" for everything once, which let `count x where start >= 5`
    compile over a *date* and `where name >= 5` over a *word* -- both refused one
    layer down in the projection itself. At evaluation each answers nothing, so
    the count is a silent nought rather than a refusal.
    """
    out: dict[str, str] = {}
    for name, _path, ftype, _join in plan.fields:
        out[name] = {"text": "text", "date": "date", "number": "number", "flag": "number"}[ftype]
    for name, _figure, unit, _band in plan.reads:
        out[name] = "text" if unit == "level" else "date" if unit == "moment" else "number"
    for name, _expr, unit in plan.values:
        out[name] = "text" if unit == "level" else "number"
    return out


def _column_kind(plan: ProjectPlan, name: str) -> str:
    for n, _, ftype, _ in plan.fields:
        if n == name:
            return {"text": "a word", "date": "a date", "number": "number", "flag": "number"}[
                ftype
            ]
    for n, _, unit, _band in plan.reads:
        if n == name:
            return "a word" if unit == "level" else "a date" if unit == "moment" else "number"
    for n, _, unit in plan.values:
        if n == name:
            return "a word" if unit == "level" else "number"
    return "nothing"


# ------------------------------------------------------------- hashing --


def _calc_hash(e: CalcExpr) -> object:
    """The parts of an expression that decide the number. Line numbers are not
    among them: moving a definition down a file must not fork its version."""
    if isinstance(e, Count):
        return {"op": "count", "set": e.set}
    if isinstance(e, ListOf):
        return {"op": "list", "measure": e.measure, "set": e.set}
    if isinstance(e, Sum):
        return {"op": "sum", "set": e.set, "measure": e.measure}
    if isinstance(e, Part):
        return {"op": "part", "name": e.name}
    if isinstance(e, Number):
        return {"op": "number", "value": e.value}
    if isinstance(e, Text):
        return {"op": "text", "value": e.value}
    if isinstance(e, Setting):
        return {"op": "setting", "path": e.path}
    if isinstance(e, FieldTotal):
        return {"op": "total", "kind": e.kind, "field": e.field, "set": e.set}
    if isinstance(e, SubjectField):
        # Which record and which field: two figures reading two fields of one
        # record are two definitions, and a hash that could not tell them
        # apart would let one become the other under a version claiming
        # nothing moved.
        return {"op": "subject", "kind": e.kind, "field": e.field}
    if isinstance(e, FigureRef):
        # Distinct from a setting of the same spelling, deliberately: the two
        # would resolve against different things, and a hash that could not
        # tell them apart would let a definition swap one for the other under
        # a version claiming nothing moved.
        return {"op": "figure", "name": e.name}
    if isinstance(e, Ladder):
        return {
            "op": "when",
            "rungs": [
                {
                    "left": _calc_hash(r.left),
                    "cmp": r.op,
                    "right": _calc_hash(r.right) if r.right is not None else None,
                    "then": _calc_hash(r.then),
                }
                for r in e.rungs
            ],
            "otherwise": _calc_hash(e.otherwise),
        }
    if isinstance(e, Arith):
        return {"op": e.op, "left": _calc_hash(e.left), "right": _calc_hash(e.right)}
    if isinstance(e, Pick):
        return {"op": e.which, "left": _calc_hash(e.left), "right": _calc_hash(e.right)}
    if isinstance(e, DaysBetween):
        return {"op": "days", "from": e.frm, "to": e.to}
    if isinstance(e, Extreme):
        return {"op": e.which, "measure": e.measure, "set": e.set}
    if isinstance(e, Coord):
        return {"op": "coord", "name": e.name}
    if isinstance(e, BucketStat):
        return {"op": e.fn, "measure": e.measure, "set": e.set}
    if isinstance(e, FieldPick):
        # The ordering field is hashed with the rest: it decides *which*
        # record in the bucket answers, so two figures ordering differently
        # over one set are two different numbers.
        return {
            "op": e.which,
            "kind": e.kind,
            "field": e.field,
            "set": e.set,
            "orderedBy": e.ordered_by,
        }
    assert_never(e)


def _set_hash(expr: SetExpr) -> object:
    if isinstance(expr, SetIndex):
        bucket = "scope" if isinstance(expr.bucket, BucketScope) else "all"
        return {"index": expr.index, "bucket": bucket}
    if isinstance(expr, SetRef):
        return {"ref": expr.name}
    if isinstance(expr, SetOp):
        return {"op": expr.op, "left": _set_hash(expr.left), "right": _set_hash(expr.right)}
    assert_never(expr)


def _index_hash(idx: CompiledIndex) -> object:
    """An index's *spec*, and the fact kind it resolves through.

    Not its label and not its `keyed as`: changing what `code_change.open` means
    changes what every figure over it counts, even though the figure's own text
    is untouched -- but a label is prose and an id space decides what the checker
    permits rather than what the arithmetic produces.
    """
    spec = idx.spec
    if isinstance(spec, ByField):
        body: object = {"by": "field", "part": _field_hash(spec.part)}
    elif isinstance(spec, ByComposite):
        body = {"by": "composite", "parts": [_field_hash(p) for p in spec.parts]}
    elif isinstance(spec, ByPredicate):
        body = {"by": "predicate", "field": spec.field, "op": spec.op, "value": spec.value}
    elif isinstance(spec, ByPresence):
        body = {"by": "presence", "field": spec.field, "negated": spec.negated or None}
    elif isinstance(spec, ByAge):
        body = {
            "by": "age",
            "field": spec.field,
            "direction": spec.direction,
            # The threshold, however it is written: a number of days, or the
            # field and join it is read off. Both decide who is in the filter,
            # so both are the spec.
            "days": spec.days,
            "read": spec.read,
            "local": spec.local,
            "through": (
                {"kind": spec.through.kind, "path": spec.through.path}
                if spec.through is not None
                else None
            ),
        }
    else:
        assert_never(spec)
    return {"name": idx.name, "kind": idx.kind, "spec": body}


def _ordered_by_of(e: CalcExpr) -> str | None:
    """The ordering a resolved field read settled on, lifted onto the plan so
    the engine need not walk the tree to find it."""
    if isinstance(e, FieldPick):
        return e.ordered_by or None
    if isinstance(e, Ladder):
        for rung in e.rungs:
            for side in (rung.left, rung.then, rung.right):
                if side is not None and (found := _ordered_by_of(side)) is not None:
                    return found
        return _ordered_by_of(e.otherwise)
    if isinstance(e, (Arith, Pick)):
        return _ordered_by_of(e.left) or _ordered_by_of(e.right)
    return None


def _ordering_grain(spec: IndexBy) -> str | None:
    """The grain a composite group's truncated part declares."""
    if isinstance(spec, ByComposite):
        for part in spec.parts:
            if part.truncate is not None or part.select is not None:
                return part.truncate or part.select
    return None


def _ordering_field(spec: IndexBy) -> str | None:
    """Which field a composite group truncates on -- the field that says
    *when* each record happened.

    This is what orders records inside a bucket, and it is derived rather
    than written for the reason the zone is derived: the group already
    decided which instant files a record under which bucket, and a second
    declaration of that would be a second thing to keep in step. Order by a
    different field than you bucketed by and a bucket reports a superseded
    value, silently.
    """
    if isinstance(spec, ByComposite):
        for part in spec.parts:
            if part.truncate is not None or part.select is not None:
                return part.field
    return None


def _field_hash(part: IndexField) -> object:
    return {
        "field": part.field,
        "through": (
            {"kind": part.through.kind, "path": part.through.path}
            if part.through is not None
            else None
        ),
        "truncate": part.truncate,
        # Absent-unless-declared, like every optional key `canonical` drops:
        # a selective rule is new vocabulary, and every spec written before
        # it existed must keep its version.
        "select": part.select,
        # The calendar: the record and field carrying it, or the one written
        # in the definition. A group cut on one calendar and one cut on
        # another file the same instant under different labels, so the two
        # are different specs -- and `named` is absent-unless-declared, so
        # every spec written before a literal was possible keeps its version.
        "zone": (
            {
                "kind": part.zone.kind,
                "field": part.zone.field,
                "named": part.zone.named,
            }
            if part.zone is not None
            else None
        ),
    }


def _measure_hash(m: CompiledMeasure) -> object:
    return {
        "name": m.name,
        "kind": m.kind,
        "shape": m.shape,
        "unit": m.unit,
        "later": m.later,
        "earlier": m.earlier,
        "clock": m.clock or None,
        "field": m.field_path,
        "moment": m.moment,
    }


def _flag_hash(f: FlagDecl) -> object:
    """A flag's prose *is* in the hash, unlike a figure's display template.

    A figure's display describes a number that did not move; a flag's sentence
    is the whole content of what the projection produces for that row.
    """
    return {
        "name": f.name,
        "when": _cond_hash(f.when),
        "label": f.label,
        "detail": f.detail,
        "action": f.action,
        "severity": f.severity,
    }


def _cond_hash(c: Condition | None) -> object:
    if c is None:
        return None
    return {
        "left": _calc_hash(c.left),
        "op": c.op,
        "right": _calc_hash(c.right) if c.right is not None else None,
    }


def _bundle_hash(plan: BundlePlan) -> object:
    """The member list, in written order, and nothing else.

    Slots are in: clients couple to them as addresses, so a renamed slot is
    a changed tile on the review surface (and still in no storage key and no
    citation -- the hash's one purpose is the committed artifact's diff).
    Windows ride as a fourth element only when declared, the same
    absent-unless-declared shape a statistic's grain hashes with, so a bare
    member and one written before windows existed cannot differ -- and each
    window hashes as its canonical token, so `over 30`, `over 1-30` and
    `over 30 in days` are one question with one hash. Prose is out like
    everywhere else; member *versions* are out deliberately -- each member's
    answer carries its own version, so a member moving underneath is that
    member's moved hash on the artifact, not a second copy of it here.
    """
    return {
        "name": plan.name,
        "members": [
            [m.slot, m.kind, m.name]
            if m.windows is None
            else [m.slot, m.kind, m.name, [window_token(w) for w in m.windows]]
            for m in plan.members
        ],
    }


def _versioned_figure(
    plan: FigurePlan,
    indexes: dict[str, CompiledIndex],
    measures: dict[str, CompiledMeasure],
    declared: list[FigurePlan],
) -> FigurePlan:
    body = {
        "name": plan.name,
        "scope": plan.scope,
        "across": plan.across,
        "unit": plan.unit,
        "sets": {k: _set_hash(v) for k, v in plan.sets.items()} or None,
        "combines": (
            [
                # A rollup hashes its source's *version*: redefine the parts and
                # the total must rebuild too, or it reads a number derived from a
                # definition that no longer exists, for ever, with the corrected
                # parts printed underneath it.
                {"name": k, "figure": f, "over": o, "version": _version_of_source(declared, f)}
                for k, (f, o) in plan.combines.items()
            ]
            or None
        ),
        "calculate": _calc_hash(plan.calculate),
        # **Hashed, where `bucketed` beside it is not**, and the asymmetry is
        # the rule rather than an inconsistency. `bucketed` mirrors the
        # group's spec, which is already hashed wherever it is read; this
        # changes what the stored values *are*. The same records under the
        # same calculation give two buckets without it and twelve with it, so
        # a version reused across the change would serve carried numbers from
        # a definition that never claimed any. `or None` keeps it
        # absent-unless-declared, so every figure written before the suffix
        # existed keeps its version and no tenant rebuilds for a feature it
        # does not use.
        "carried": plan.carried or None,
        # The band is an answer this figure gives, so a definition that starts
        # banding differently is a different definition. Hashed
        # absent-unless-declared, which `canonical` gives for free by dropping
        # `None` -- so every figure written before bands existed keeps its
        # version, and no board rebuilds for a feature it does not use.
        #
        # It costs nothing to move: a band is evaluated on read and stored
        # nowhere, so a new version invalidates no value.
        "band": _calc_hash(plan.band) if plan.band is not None else None,
        "indexes": [_index_hash(indexes[n]) for n in plan.indexes] or None,
        "measures": [_measure_hash(measures[n]) for n in plan.measures] or None,
    }
    return FigurePlan(**{**plan.__dict__, "version": version_of(body)})


def _version_of_source(declared: list[FigurePlan], name: str) -> str:
    for plan in declared:
        if plan.name == name:
            return plan.version
    return ""


def _versioned_reading(
    plan: ReadingPlan,
    indexes: dict[str, CompiledIndex],
    measures: dict[str, CompiledMeasure],
) -> ReadingPlan:
    body: dict[str, object] = {
        "name": plan.name,
        "scope": plan.scope,
        "mode": plan.mode,
        "unit": plan.unit,
        # Two elements, exactly the shape a grainless statistic has always
        # hashed as -- `series(...) by <grain>` is retired, and the readings
        # that never wrote one keep their historic versions.
        "calculate": [[s.fn, s.set] for s in plan.calculate],
        "requires": [[r.count, r.set] for r in plan.requires] or None,
        # The ladder and the statistic it judges. `mean` hashes as absent, so
        # a band that names the default explicitly is the same definition as
        # one that leaves it out -- the same absent-unless-declared shape
        # every other optional key here has.
        "band": (
            {
                "ladder": _calc_hash(plan.band),
                "on": plan.band_on if plan.band_on not in (None, "mean") else None,
            }
            if plan.band is not None
            else None
        ),
    }
    if plan.mode == "window":
        # The source figure's *name*, not its version: the version travels to the
        # screen on the response, so a reading citing a moved figure is visible
        # rather than silently re-versioned.
        body["source"] = plan.source
    else:
        # A live reading has no source on the wire, so its own version is the
        # only provenance token there is -- and with names alone, editing a
        # predicate or a measure field would change every number while two
        # different definitions cited identically.
        body["measure"] = _measure_hash(measures[plan.live_measure or ""])
        body["set"] = _set_hash(plan.live_set) if plan.live_set is not None else None
        body["indexes"] = [_index_hash(indexes[n]) for n in plan.indexes] or None
    return ReadingPlan(**{**plan.__dict__, "version": version_of(body)})


def _project_hash(plan: ProjectPlan, indexes: dict[str, CompiledIndex]) -> object:
    return {
        "name": plan.name,
        "kind": plan.kind,
        "from": _set_hash(plan.frm) if plan.frm is not None else None,
        # The population's index *specs* beside the expression, for the reason
        # a live reading hashes its own (above): with names alone, redefining
        # a predicate changes which records get a row while two different
        # definitions cite identically -- on library.json, the one surface the
        # review reads, and on the wire. `or None` so a projection with no
        # `from` stays on its historic hash; `canonical` drops absent keys.
        "from_indexes": [_index_hash(indexes[n]) for n in plan.indexes] or None,
        "fields": [
            {
                "name": n,
                "path": p,
                "type": t,
                # The join decides *which record* a path is read off, so the same
                # path through two relations is two different columns.
                "join": ({"field": j.field, "kind": j.kind, "path": j.path} if j else None),
            }
            for n, p, t, j in plan.fields
        ],
        # The band flag is hashed beside the name: `band of X` and a bare `X`
        # are two different columns off one figure, and a version that could
        # not tell them apart would let a projection change what every row
        # says while claiming nothing had moved. Written as a third element
        # rather than conditionally, because `canonical` drops nothing from a
        # list -- so this does move every existing projection's version once,
        # which costs nothing: a projection stores no values.
        "reads": [[n, f, b] for n, f, _, b in plan.reads],
        "values": [[n, _calc_hash(e), u] for n, e, u in plan.values],
        "flags": [_flag_hash(f) for f in plan.flags],
        # `omit` decides who is on the page as surely as `from` does, so it
        # moves the version the same way. `canonical` drops the key when it is
        # None, which keeps every gateless projection on its historic hash.
        "omit": _cond_hash(plan.omit),
        "sort": ([plan.sort.name, plan.sort.direction] if plan.sort is not None else None),
        "limit": plan.limit,
    }


__all__ = ["CheckError", "WorldConflict", "compile_source"]


_TIMED_UNITS = frozenset({"duration", "effort"})


def _scaled(e: CalcExpr, unit: FigureUnit | None, owner: str) -> CalcExpr:
    """Every literal in a ladder, converted to what the figure stores.

    A figure whose value is a span of time stores seconds, and a threshold
    written against it has to say which scale it was written in -- `561600`
    is six and a half days and nobody reads it as one. The scale is converted
    here rather than carried into the plan so the evaluator compares two
    numbers in one unit and the version hash records the seconds, which is
    what the comparison actually is.

    Refused on any other unit, by the rule the whole language follows:
    declare what a reader downstream would otherwise get silently wrong,
    never what the declaration already says. A count of deliveries is a
    tally, and `3 days` beside it is a second claim about what the number
    measures, disagreeing with the calculation that produced it.
    """
    timed = unit in _TIMED_UNITS

    def walk(node: CalcExpr) -> CalcExpr:
        if isinstance(node, SubjectField):
            if node.scale is not None and not timed:
                raise CheckError(
                    f"{owner}'s band compares against {node.kind}.{node.field} in "
                    f"{node.scale}, and the figure answers a {unit or 'level'}. A "
                    "scale says what a span of time is measured in, and this number "
                    "is not one.",
                    node.line,
                )
            if node.scale is None and timed:
                raise CheckError(
                    f"{owner} answers a {unit}, which is stored in seconds, and its "
                    f"band compares against {node.kind}.{node.field} with no scale. A "
                    "field is structural -- the record says a number is there and "
                    f"nothing about what it measures -- so write "
                    f"`{node.kind}.{node.field} minutes` (or seconds, hours, days, "
                    "weeks). Read as seconds when it meant minutes, the comparison "
                    "runs and is wrong by sixty for ever.",
                    node.line,
                )
            return node
        if isinstance(node, Number):
            if node.scale is not None and not timed:
                raise CheckError(
                    f"{owner}'s band compares against {node.value:.10g} {node.scale}, and "
                    f"the figure answers a {unit or 'level'}. A scale says what a span "
                    "of time is measured in, and this number is not one -- the "
                    "calculation already says what it is.",
                    node.line,
                )
            if node.scale is None and timed and node.value != 0:
                written = f"{node.value:.10g}"
                days = node.value / SECONDS_PER["days"]
                raise CheckError(
                    f"{owner} answers a {unit}, which is stored in seconds, and its "
                    f"band compares against {written} with no scale. Read as seconds "
                    f"that is {days:.10g} days -- write it that way "
                    f"(`{days:.10g} days`, or seconds, minutes, hours, weeks). "
                    "561600 is six and a half days and nobody reads it as one.",
                    node.line,
                )
            if node.scale is not None:
                return replace(
                    node, value=node.value * SECONDS_PER[node.scale], scale=None
                )
            return node
        if isinstance(node, Ladder):
            return replace(
                node,
                rungs=tuple(
                    replace(
                        r,
                        left=walk(r.left),
                        then=walk(r.then),
                        right=walk(r.right) if r.right is not None else None,
                    )
                    for r in node.rungs
                ),
                otherwise=walk(node.otherwise),
            )
        if isinstance(node, (Arith, Pick)):
            return replace(node, left=walk(node.left), right=walk(node.right))
        return node

    return walk(e)
