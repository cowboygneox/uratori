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

from typing import Literal, NoReturn, assert_never

from ..schema import Schema
from ..windows import (
    WindowSpec,
    refuse_reach,
    window_token,
)
from .ast import (
    Arith,
    BucketAll,
    BucketScope,
    BundleDecl,
    BundleMember,
    ByAge,
    ByComposite,
    ByField,
    ByPredicate,
    ByPresence,
    CalcExpr,
    Condition,
    Count,
    DaysBetween,
    Decl,
    DurationMeasure,
    Extreme,
    FactDecl,
    FactField,
    FieldDecl,
    FieldMeasure,
    FigureDecl,
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
        self, kind: str, path: str, what: str, line: int
    ) -> tuple[CompiledFactField, bool] | None:
        """The declared field a path lands on, and whether it crossed a list.

        None in a schema-taught world: no fields were declared, so nothing can
        be checked -- the origin project's specimen tests are the host-side
        stand-in there. In a fact-taught world a path that resolves to nothing
        is a build failure here, because at run time it is a silently empty
        bucket or a column of dashes, for everybody, for ever.
        """
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
                    f'{what} reads "{path}", and "{segment}" is not a field of {level}. '
                    f'Declared there: {", ".join(sorted(at)) or "nothing"}.',
                    line,
                )
            crossed = crossed or found.many
            at = {c.name: c for c in found.children}
        assert found is not None
        if found.type is None:
            raise CheckError(
                f'{what} reads "{path}", which is a nested record rather than a value. '
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
            if part.zone is not None and part.zone not in self._schema.bucket_settings:
                raise CheckError(
                    f'{word} {d.name} buckets by {part.truncate or part.select} in "{part.zone}", which is not a setting '
                    f"a {word} may name. Those are: {', '.join(self._schema.bucket_settings)}. "
                    "Moving one re-buckets a tenant's whole history, which is why the list is "
                    "short.",
                    d.line,
                )
        if isinstance(d.spec, ByAge) and d.spec.setting not in self._schema.bucket_settings:
            raise CheckError(
                f'{word} {d.name} narrows by age against "{d.spec.setting}", which is not a '
                f"setting a {word} may name. Those are: {', '.join(self._schema.bucket_settings)}.",
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

        if d.sets and d.combines:
            raise CheckError(
                f"figure {d.name} has both a depends and a combine block. Reading record "
                "sets and another figure would be two populations arriving at one calculation "
                "with no rule for how they relate -- adding a count of records to a total of "
                "stored values produces a number no definition makes a claim about.",
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
        scope_index, grain, dimension_part = self._scope_index(d, set_names, scope)

        kind = self._calc_kind(d.calculate, d, set_names, combines, scope)
        unit = self._figure_unit(d, kind, combines)

        indexes = sorted(_indexes_in_sets(d.sets))
        measures = sorted(_measures_in(d.calculate))
        reads = [c.figure for c in d.combines]
        settings = sorted(set(_settings_in(d.calculate)) | _zone_settings(indexes, self.indexes))
        for path in _settings_in(d.calculate):
            if path not in self._schema.figure_settings:
                raise CheckError(
                    f'figure {d.name} reads "{path}", which is not a setting a calculation '
                    f"may name. Those are: {', '.join(self._schema.figure_settings)}.",
                    d.line,
                )

        depth = 0
        for c in d.combines:
            source = _find(self.figures, c.figure)
            if source is None:
                raise CheckError(
                    f'there is no figure called "{c.figure}". A figure may only read one '
                    "declared before it -- a cycle has no line number, and on a cold build "
                    "the wrong order stores a nought and never revisits it. Declared so far: "
                    f"{', '.join(f.name for f in self.figures) or 'none'}.",
                    c.line,
                )
            depth = max(depth, source.depth + 1)

        band_settings = self._check_band(d, unit, kind)

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
            settings=tuple(settings),
            scope_index=scope_index,
            band=d.band if isinstance(d.band, Ladder) else None,
            band_settings=band_settings,
            grain=grain,
            dimension_part=dimension_part,
            depth=depth,
        )
        self.figures.append(_versioned_figure(plan, self.indexes, self.measures, self.figures))

    def _check_band(self, d: FigureDecl, unit: FigureUnit, kind: str) -> tuple[str, ...]:
        """The rules that keep a band a band, and the dials it may name.

        A band used to be a figure of its own -- a `level`-unit figure combining
        the one below it -- and the board found the pair by scanning the library
        at serve time. So the word on screen came from a definition the page
        never named, and the page showing the formula did not contain it.

        Folding it in is only an improvement if it cannot become a second
        calculation hiding in the same block, which is what these four refusals
        are for. Each of them compiles and produces something plausible.
        """
        if d.band is None:
            return ()
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

        paths = sorted(set(_settings_in(band)))
        for path in paths:
            if path not in self._schema.figure_settings:
                raise CheckError(
                    f'figure {d.name}\'s band reads "{path}", which is not a setting a '
                    f"calculation may name. Those are: {', '.join(self._schema.figure_settings)}.",
                    d.line,
                )
        return tuple(paths)

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

    def _combines(self, d: FigureDecl) -> dict[str, tuple[str, str | None]]:
        out: dict[str, tuple[str, str | None]] = {}
        seen_over = 0
        for c in d.combines:
            if c.name in out:
                raise CheckError(
                    f'figure {d.name} binds "{c.name}" twice in its combine block.', c.line
                )
            source = _find(self.figures, c.figure)
            if source is None:
                raise CheckError(
                    f'there is no figure called "{c.figure}". Declared so far: '
                    f"{', '.join(f.name for f in self.figures) or 'none'}.",
                    c.line,
                )
            # No self-reference check here, deliberately: `_find` only searches
            # figures declared *before* this one, so a figure can never resolve
            # to itself and the branch would be unreachable. The name simply
            # does not resolve, and "there is no figure called X" is the honest
            # message -- an unreachable refusal is worse than none, because it
            # reads as a guard somebody is relying on.
            if c.over is not None:
                seen_over += 1
                if source.across is None:
                    raise CheckError(
                        f"figure {d.name} adds up {c.figure} over {c.over}, but {c.figure} is "
                        "not split across anything. A rollup of an undimensioned figure totals "
                        "a single value and looks right for ever, which is why this is refused "
                        "rather than allowed to mean the same as a bare read.",
                        c.line,
                    )
                if source.across != c.over:
                    raise CheckError(
                        f"figure {d.name} adds up {c.figure} over {c.over}, but {c.figure} is "
                        f"split across {source.across}.",
                        c.line,
                    )
                if seen_over > 1:
                    raise CheckError(
                        f"figure {d.name} rolls up more than one dimensioned figure. A "
                        "rollup's members are addresses carrying no figure name, so two would "
                        "be indistinguishable once stored.",
                        c.line,
                    )
            else:
                if source.across is not None:
                    raise CheckError(
                        f"figure {d.name} reads {c.figure} as a single value, but {c.figure} "
                        f"is split across {source.across}. A bare read would take whichever "
                        "part sorted first -- a number about one source under a heading that "
                        f"says nothing about a source. Write `over {source.across}`.",
                        c.line,
                    )
                if source.grain is not None:
                    raise CheckError(
                        f"figure {d.name} reads {c.figure} as a single value, but {c.figure} "
                        f"is time-keyed, so it has one value per {source.grain} rather than "
                        "one per subject.",
                        c.line,
                    )
            if source.scope != d.name.split(".", 1)[0]:
                raise CheckError(
                    f"figure {d.name} is scoped to {d.name.split('.', 1)[0]} but reads "
                    f"{c.figure}, which is scoped to {source.scope}. The two are different id "
                    "spaces, so every lookup would miss and the figure would be empty.",
                    c.line,
                )
            out[c.name] = (c.figure, c.over)
        return out

    def _scope_index(
        self, d: FigureDecl, sets: dict[str, SetExpr], scope: str
    ) -> tuple[str | None, str | None, str | None]:
        """Exactly one group must fan the figure out, and this works out which.

        A rollup has none, and that is legitimate -- its subjects come from the
        roster. Everything else must have exactly one, because two would mean a
        value keyed by two different things and none would mean the figure has
        no subjects at all while still computing a number, which renders as a
        board-wide total attributed to nobody.
        """
        if d.combines:
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
                if d.across is not None:
                    raise CheckError(
                        f"figure {d.name} is split across {d.across}, but {name} buckets "
                        f"its second part by {grain}. A bucket of time is not a "
                        "dimension: it has no roster and no name, and whether a figure is "
                        "time-keyed is what decides if a reading may roll it up over a "
                        "range.",
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
        arithmetic = isinstance(d.calculate, (Arith, Pick))
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
        if isinstance(d.calculate, Sum) and d.calculate.measure is not None:
            m = self.measures[d.calculate.measure]
            return "effort" if m.unit == "effort" else "count"
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
        self._band(d, scope)
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
                    band=d.band,
                    source=source.name,
                    settings=tuple(sorted({d.band.setting} if d.band else set())),
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
        self._band(d, scope)
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
                    band=d.band,
                    live_measure=measure,
                    live_set=expr,
                    indexes=tuple(sorted(_indexes_in(expr))),
                    settings=tuple(sorted({d.band.setting} if d.band else set())),
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

    def _band(self, d: ReadingDecl, scope: str) -> None:
        if d.band is None:
            return
        if d.band.setting not in self._schema.reading_settings:
            raise CheckError(
                f'reading {d.name} bands against "{d.band.setting}", which is not a setting a '
                f"band may name. Those are: {', '.join(self._schema.reading_settings)}.",
                d.band.line,
            )
        # The default is `mean`, so a band with no `on` over a reading that
        # calculates no mean is checked too. Left unchecked it compiled and
        # `level_of` answered "unknown" for every subject at every value -- a row
        # that is permanently grey and reads as missing data rather than as a
        # broken definition.
        wanted = d.band.on or "mean"
        if wanted not in {s.fn for s in d.calculate}:
            written = f"{d.band.on}(...)" if d.band.on else "the mean, by default"
            raise CheckError(
                f"reading {d.name} bands on {written}, which it does not calculate. It "
                f"calculates {', '.join(sorted({s.fn for s in d.calculate}))}; name one of "
                "those with `on`, or the band colours nothing and every row reads unknown.",
                d.band.line,
            )
        if d.band.on == "count" and d.band.unit is not None:
            raise CheckError(
                f"reading {d.name} bands on count(...) and writes the threshold in "
                f"{d.band.unit}. A count of things has no time in it -- left to the duration "
                "path a count of 3 becomes 3/86400 against a threshold in days and every "
                "queue on every board bands good for ever.",
                d.band.line,
            )

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

        settings = sorted(
            {p for _, e, _ in values for p in _settings_in(e)}
            | _flag_settings(d.flags)
            | _condition_settings(d.omit)
        )
        for path in settings:
            if path not in self._schema.project_settings:
                raise CheckError(
                    f'projection {d.name} reads "{path}", which is not a setting a projection '
                    f"may name. Those are: {', '.join(self._schema.project_settings)}.",
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
            settings=tuple(settings),
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

        settings = sorted({p for _, e, _ in values for p in _settings_in(e)} | _flag_settings(d.flags))
        for path in settings:
            if path not in self._schema.project_settings:
                raise CheckError(
                    f'summary {d.name} reads "{path}", which is not a setting it may name.',
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
            settings=tuple(settings),
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
        if isinstance(e, (Count, ListOf, Sum, Extreme)):
            verb = {"Count": "counts", "ListOf": "lists", "Sum": "totals", "Extreme": "takes the extreme of"}[
                type(e).__name__
            ]
            raise CheckError(
                f"{noun} {owner} {verb} something. A projection aggregates nothing -- those "
                "are figures, and offering them here would be a second way to compute a "
                "number this product claims has exactly one.",
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
        if isinstance(n, (ListOf, Extreme)):
            out.add(n.measure)
        elif isinstance(n, Sum) and n.measure is not None:
            out.add(n.measure)
    return out


def _zone_settings(indexes: list[str], compiled: dict[str, CompiledIndex]) -> set[str]:
    out: set[str] = set()
    for name in indexes:
        idx = compiled.get(name)
        if idx is None:
            continue
        for part in _index_fields(idx.spec):
            if part.zone is not None:
                out.add(part.zone)
        if isinstance(idx.spec, ByAge):
            out.add(idx.spec.setting)
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
            "setting": spec.setting,
        }
    else:
        assert_never(spec)
    return {"name": idx.name, "kind": idx.kind, "spec": body}


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
        "zone": part.zone,
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
        "band": (
            {
                "direction": plan.band.direction,
                "setting": plan.band.setting,
                "on": plan.band.on if plan.band.on not in (None, "mean") else None,
                # `days` hashes as absent, so a band written before the keyword
                # existed keeps its version and `in days` written out is the same
                # definition.
                "unit": plan.band.unit if plan.band.unit not in (None, "days") else None,
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
