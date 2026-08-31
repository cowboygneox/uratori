"""Recursive descent over the token stream, one method per production.

The parser says what was written. It rejects only what it cannot *represent* --
a missing colon, a name where a number belongs -- and never what it cannot
justify. "There is no group or filter called that" and "a ladder may not return a list"
are the checker's, because those errors want to name a rule and list the
alternatives, and a parser has neither the library nor the vocabulary to do it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ..windows import (
    WindowError,
    WindowSpec,
    make_window_spec,
    refuse_window_count,
    window_token,
)
from .ast import (
    AbsenceTest,
    Arith,
    ArithOperator,
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
    Combine,
    Comparison,
    Condition,
    Coord,
    Count,
    CountDecl,
    DaysBetween,
    Decl,
    DeclaredUnit,
    Document,
    DurationMeasure,
    Extreme,
    FactDecl,
    FactField,
    FieldDecl,
    FieldMeasure,
    FigureDecl,
    FlagDecl,
    IndexBy,
    IndexDecl,
    IndexField,
    Join,
    Ladder,
    ListOf,
    LiveSource,
    MeasureDecl,
    MeasureUnit,
    MomentMeasure,
    NamedSet,
    Number,
    Part,
    Pick,
    ProjectDecl,
    ReadDecl,
    ReadingBand,
    ReadingDecl,
    ReadingSet,
    Requirement,
    Rung,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    Setting,
    SortDecl,
    Statistic,
    StatisticFn,
    Sum,
    SummariseDecl,
    Text,
    Through,
    TotalDecl,
    Truncation,
    ValueDecl,
    WindowedSource,
    Zone,
)
from .lex import SyntaxError_, Token, lex, prose_above

_FACT_TYPES: frozenset[str] = frozenset({"text", "number", "flag", "moment"})
_DECLARED_UNITS: frozenset[str] = frozenset({"share", "days", "effort", "count", "duration"})
_DERIVED_UNITS: frozenset[str] = frozenset({"level", "moment"})
_MEASURE_UNITS: frozenset[str] = frozenset({"effort", "count"})
_FIELD_TYPES: frozenset[str] = frozenset({"text", "date", "number", "flag"})
_STATISTICS: frozenset[str] = frozenset(
    {"mean", "median", "worst", "sum", "count", "series", "delta"}
)
_COMPARISONS: frozenset[str] = frozenset({">=", ">", "<=", "<", "==", "!="})


def parse(source: str) -> Document:
    document = _Parser(lex(source)).document()
    lines = source.split("\n")
    document.decls = [_explained(d, lines) for d in document.decls]
    return document


_RENDERED: dict[type, str] = {
    FigureDecl: "figure",
    ReadingDecl: "reading",
    ProjectDecl: "projection",
    SummariseDecl: "summary",
    BundleDecl: "bundle",
}


def _explained(decl: Decl, lines: list[str]) -> Decl:
    """Attach the `#` comment run above a declaration as its doc.

    The comments are the customer-facing explanation -- one spelling for all
    nine declaration kinds, kept out of the block so the directives a reviewer
    came to check are not buried in prose. The lexer strips comments, so this
    reads the raw lines; the declaration's own line number says where to look.

    The five rendered kinds are refused without one: each is served to a
    reader, and an unexplained number on screen is the thing this language
    exists to prevent. A fact is refused too -- it is the schema a reader
    tracing a number lands on, and a schema nobody can read dead-ends the
    trace exactly where it was meant to bottom out. Its fields may carry a
    run of their own, at the field's indent, attached the same way.
    """
    if isinstance(decl, FactDecl):
        prose = prose_above(lines, decl.line)
        if not prose:
            raise SyntaxError_(
                f"fact {decl.name} has no explanation. Write `#` comment lines directly "
                "above the declaration -- they are what a reader tracing a number back "
                "to the schema sees, and a record kind nobody can read dead-ends the "
                "trace at its last step.",
                decl.line,
                0,
            )
        return replace(decl, doc=prose, fields=_field_docs(decl.fields, lines))
    if not isinstance(decl, (FigureDecl, ReadingDecl, ProjectDecl, SummariseDecl, BundleDecl)):
        return decl
    what = _RENDERED[type(decl)]
    prose = prose_above(lines, decl.line)
    if not prose:
        raise SyntaxError_(
            f"{what} {decl.name} has no explanation. Write `#` comment lines directly "
            "above the declaration -- they are the customer-facing definition, rendered "
            f"wherever the number is cited, and a {what} nobody can read is the thing "
            "this language exists to prevent. (A `# ----` rule line is a file banner, "
            "not prose.)",
            decl.line,
            0,
        )
    return replace(decl, doc=prose)


def _field_docs(fields: tuple[FactField, ...], lines: list[str]) -> tuple[FactField, ...]:
    """A fact field's `#` run, at the field's own indent.

    The same contiguity rules a declaration's explanation follows, via the
    same `prose_above` -- one implementation, so a comment that counts as
    prose here cannot fail to count on the served source pane.
    """
    return tuple(
        replace(
            f,
            doc=prose_above(lines, f.line, indented=True),
            children=_field_docs(f.children, lines),
        )
        for f in fields
    )


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._at = 0

    # ------------------------------------------------------------ cursor --

    def _peek(self) -> Token:
        return self._tokens[self._at]

    def _next(self) -> Token:
        tok = self._tokens[self._at]
        self._at += 1
        return tok

    def _is(self, kind: str) -> bool:
        return self._peek().kind == kind

    def _at_word(self, word: str) -> bool:
        tok = self._peek()
        return tok.kind == "name" and tok.value == word

    def _peek_at(self, ahead: int, kind: str, value: str) -> bool:
        """Whether the token `ahead` positions on is exactly this.

        One place needs two tokens of lookahead -- telling `name:{bucket}`
        from a `name:` that ends a clause -- and guessing from the colon
        alone is what broke a projection's flag condition.
        """
        at = self._at + ahead
        if at >= len(self._tokens):
            return False
        tok = self._tokens[at]
        return tok.kind == kind and tok.value == value

    def _at_op(self, op: str) -> bool:
        tok = self._peek()
        return tok.kind == "op" and tok.value == op

    def _describe(self) -> str:
        tok = self._peek()
        if tok.kind == "eof":
            return "the end of the file"
        if tok.kind == "newline":
            return "the end of the line"
        if tok.kind == "indent":
            return "an indented block"
        if tok.kind == "dedent":
            return "the end of a block"
        return f'"{tok.value}"'

    def _error(self, message: str, line: int | None = None) -> SyntaxError_:
        tok = self._peek()
        return SyntaxError_(message, line if line is not None else tok.line, tok.column)

    def _expect(self, kind: str, what: str) -> Token:
        if not self._is(kind):
            raise self._error(f"expected {what}, got {self._describe()}")
        return self._next()

    def _keyword(self, word: str) -> None:
        if not self._at_word(word):
            raise self._error(f'expected "{word}", got {self._describe()}')
        self._next()

    def _punct(self, op: str) -> None:
        if not self._at_op(op):
            raise self._error(f'expected "{op}", got {self._describe()}')
        self._next()

    def _name(self, what: str) -> str:
        if not self._is("name"):
            raise self._error(f"expected {what}, got {self._describe()}")
        return self._next().value

    def _string(self, what: str) -> str:
        if not self._is("string"):
            raise self._error(f"expected {what}, got {self._describe()}")
        return self._next().value

    def _end_of_line(self) -> None:
        if self._is("newline"):
            self._next()
            return
        if self._is("eof") or self._is("dedent"):
            return
        raise self._error(f"expected the end of the line, got {self._describe()}")

    def _skip_newlines(self) -> None:
        while self._is("newline"):
            self._next()

    # ---------------------------------------------------------- document --

    def document(self) -> Document:
        doc = Document()
        self._skip_newlines()
        while not self._is("eof"):
            tok = self._peek()
            if tok.kind != "name":
                raise self._error(
                    'expected "fact", "group", "filter", "measure", "figure", "reading", '
                    f'"projection", "summarise" or "bundle", got {self._describe()}'
                )
            if tok.value == "fact":
                doc.decls.append(self._fact())
            elif tok.value in ("group", "filter"):
                doc.decls.append(self._index(tok.value))
            elif tok.value == "measure":
                doc.decls.append(self._measure())
            elif tok.value == "figure":
                doc.decls.append(self._figure())
            elif tok.value == "reading":
                doc.decls.append(self._reading())
            elif tok.value == "projection":
                doc.decls.append(self._project())
            elif tok.value == "summarise":
                doc.decls.append(self._summarise())
            elif tok.value == "bundle":
                doc.decls.append(self._bundle())
            # The keyword was `project` for one release, which read as an
            # imperative -- "project this record" -- where every other keyword
            # here names the thing being declared. Named rather than silently
            # accepted: a `.fig` written against the old spelling should say what
            # to change, not fail as "expected a declaration".
            elif tok.value == "project":
                raise self._error(
                    'the keyword is "projection", not "project". A declaration is named '
                    "after the thing it produces, and this one produces a projection.",
                    tok.line,
                )
            # `index` was one keyword for two different questions, and the name
            # said neither. Named rather than silently accepted, for the same
            # reason `project` is: a `.fig` written against the old spelling
            # should say what to change, not fail as "expected a declaration".
            elif tok.value == "index":
                raise self._error(
                    '"index" split into "group" and "filter". A group fans records '
                    "out by a field (`group code_change.by_author from ...`); a "
                    "filter narrows to the records matching a test "
                    "(`filter code_change.open where ...`).",
                    tok.line,
                )
            else:
                raise self._error(
                    'expected "fact", "group", "filter", "measure", "figure", "reading", '
                    f'"projection", "summarise" or "bundle", got {self._describe()}'
                )
            self._skip_newlines()
        return doc

    # -------------------------------------------------------------- fact --

    def _fact(self) -> FactDecl:
        line = self._peek().line
        self._keyword("fact")
        name = self._name("a fact kind, e.g. shop_order")
        if "." in name:
            raise self._error(
                f'"{name}" cannot be a fact kind: a kind is named bare, and a dot would '
                "make it indistinguishable from a figure name.",
                line,
            )
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the fact kind")

        name_field: str | None = None
        url_field: str | None = None
        fields: list[FactField] = []
        seen: set[str] = set()

        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word in ("name", "url") and self._directive_ahead():
                self._once(seen, word, f"fact {name}")
                self._next()
                pointed = self._name("the field this points at")
                self._end_of_line()
                if word == "name":
                    name_field = pointed
                else:
                    url_field = pointed
            else:
                fields.append(self._fact_member(top=True))
            self._skip_newlines()
        self._expect("dedent", "the end of the fact block")

        if not fields:
            raise self._error(
                f"fact {name} has no fields, so it verifies nothing and no definition "
                "can read it.",
                line,
            )
        return FactDecl(
            name=name,
            doc="",
            fields=tuple(fields),
            name_field=name_field,
            url_field=url_field,
            line=line,
        )

    def _directive_ahead(self) -> bool:
        """`name ref` -- exactly a pointer and the end of the line.

        `name as text` is a *field* called name: the directive shape never
        contains `as`, so nothing here is a reserved word and the lookahead
        is one token.
        """
        after = self._tokens[self._at + 1 : self._at + 3]
        return (
            len(after) == 2
            and after[0].kind == "name"
            and after[1].kind in ("newline", "dedent", "eof")
        )

    def _fact_member(self, *, top: bool) -> FactField:
        line = self._peek().line
        word = self._peek().value
        following = self._tokens[self._at + 1]

        if word in ("one", "many") and following.kind == "name" and following.value != "as":
            return self._fact_nested(many=word == "many", top=top)

        if word in ("name", "url") and not top and self._directive_ahead():
            raise self._error(
                f'"{word}" lives at the top of the fact, not inside a nested block: it '
                "points at the field a whole record is rendered by, and a nested one "
                "would name an element nothing renders.",
                line,
            )

        if word == "field" and following.kind == "name" and following.value != "as":
            # The plausible spelling from every other schema language. A
            # fact's body is its fields, so the keyword would be noise on
            # every line -- and left to the generic path this fails as
            # 'expected "as"', pointing at nothing. The `as` guard keeps a
            # field genuinely called `field` writable, like `name` and `one`.
            raise self._error(
                'there is no "field" keyword here: a fact\'s body is its fields. '
                "Write the field bare -- `ref as text`.",
                line,
            )

        fname = self._name("a field, e.g. placed_at as moment")
        if "." in fname:
            raise self._error(
                f'"{fname}" cannot be a field name: a definition reads fields by dotted '
                "path, so the dot is the separator and a field carrying one could be "
                "declared and written but never read. Map the provider's name to an "
                "undotted one.",
                line,
            )
        if not self._at_word("as"):
            raise self._error(
                f'expected "as" after "{fname}" -- a field is `<name> as <type>`, '
                f"got {self._describe()}"
            )
        self._next()
        ftype = self._name("a field type")
        if ftype == "date":
            raise self._error(
                '"date" is the projection binding; a fact field holding an instant is '
                f'a "moment". Write `{fname} as moment`.',
                line,
            )
        if ftype not in _FACT_TYPES:
            raise self._error(
                f'"{ftype}" is not a field type. Those are: '
                f'{", ".join(sorted(_FACT_TYPES))}. A fact declares what the record '
                "structurally carries -- how a number is read is the measure's claim, "
                "not the field's.",
                line,
            )
        self._end_of_line()
        return FactField(name=fname, type=ftype, line=line)  # type: ignore[arg-type]

    def _fact_nested(self, *, many: bool, top: bool) -> FactField:
        line = self._peek().line
        word = self._next().value
        name = self._name("the nested field")
        if "." in name:
            raise self._error(
                f'"{name}" cannot be a field name: a definition reads fields by dotted '
                "path, so the dot is the separator and a field carrying one could be "
                "declared and written but never read. Map the provider's name to an "
                "undotted one.",
                line,
            )
        if self._at_word("as"):
            if many:
                raise self._error(
                    f"a list of scalars is not declarable: no construct can read one -- "
                    "a predicate compares one field against one literal and cannot test "
                    f"membership. `many {name}:` with an indented block declares a list "
                    "of records; a field the provider sends but nothing reads is left "
                    "out of the mapping.",
                    line,
                )
            raise self._error(
                f'"one" declares a nested record, and this is a single value. Write '
                f"`{name} as <type>` bare.",
                line,
            )
        self._punct(":")
        self._end_of_line()
        self._expect("indent", f"the fields of {name}")
        children: list[FactField] = []
        while not self._is("dedent") and not self._is("eof"):
            children.append(self._fact_member(top=False))
            self._skip_newlines()
        self._expect("dedent", f"the end of {name}")
        if not children:  # pragma: no cover - the lexer yields no empty indent
            raise self._error(f"{word} {name}: declares nothing.", line)
        return FactField(name=name, many=many, children=tuple(children), line=line)

    # ------------------------------------------------------------- index --

    def _index(self, keyword: str) -> IndexDecl:
        # Two keywords, one production, because the shapes share everything but
        # the spec: `group` fans records out by a field, `filter` narrows to
        # the records matching a test. The old `index` covered both, and the
        # word said neither -- so each keyword now *requires* its own tail,
        # and wearing the other one is refused by name below.
        line = self._peek().line
        self._keyword(keyword)
        example = "code_change.by_author" if keyword == "group" else "code_change.open"
        name = self._name(f"a {keyword} name, e.g. {example}")
        kind = self._prefix_of(name, f"a {keyword}", line)

        # `keyed as` sits next to the name because it qualifies the *kind*
        # rather than the bucketing, and a reader meets it in the order it
        # matters: these records are keyed like that kind's, and here is how
        # they are bucketed.
        keyed_as: str | None = None
        if self._at_word("keyed"):
            self._next()
            self._keyword("as")
            keyed_as = self._name("the fact kind whose ids these records use")

        # The pointer repeats a `keyed as` so following the advice verbatim
        # does not silently drop the id-space claim.
        keyed = f" keyed as {keyed_as}" if keyed_as is not None else ""
        spec: IndexBy
        if keyword == "group":
            if self._at_word("where"):
                raise self._error(
                    f"a group fans records out by a field, so it takes `from`. "
                    f"One bucket holding the records that match a test is a "
                    f"filter -- write `filter {name}{keyed} where ...`."
                )
            if not self._at_word("from"):
                raise self._error(
                    f'expected "from" after the group name, got {self._describe()}'
                )
            self._next()
            spec = self._index_from()
        else:
            if self._at_word("from"):
                raise self._error(
                    f"a filter narrows to the records matching a test, so it "
                    f"takes `where`. Bucketing every record by a field is a "
                    f"group -- write `group {name}{keyed} from ...`."
                )
            if not self._at_word("where"):
                raise self._error(
                    f'expected "where" after the filter name, got {self._describe()}'
                )
            self._next()
            spec = self._index_where()

        label: str | None = None
        if self._at_word("label"):
            self._next()
            label = self._string('a label, e.g. "authored by"')
        self._end_of_line()
        return IndexDecl(
            name=name, kind=kind, spec=spec, keyed_as=keyed_as, label=label, line=line
        )

    def _index_from(self) -> IndexBy:
        if self._at_op("("):
            self._next()
            parts: list[IndexField] = [self._index_field()]
            while self._at_op(","):
                self._next()
                parts.append(self._index_field())
            self._punct(")")
            return ByComposite(parts=tuple(parts))
        return ByField(part=self._index_field())

    def _index_field(self) -> IndexField:
        line = self._peek().line
        path = self._name("a field to bucket by")
        through: Through | None = None
        truncate: Truncation | None = None
        select: str | None = None
        zone: Zone | None = None

        if self._at_word("through"):
            self._next()
            target = self._name("a fact kind and path, e.g. team_person.accounts.accountId")
            kind, _, rest = target.partition(".")
            if not rest:
                raise self._error(
                    f'"{target}" needs a path: `through <fact kind>.<path>`, e.g. '
                    "`through team_person.accounts.accountId`"
                )
            through = Through(kind=kind, path=rest)

        if self._at_word("by"):
            self._next()
            truncate, select = self._bucket_rule()
            if self._at_word("in"):
                self._next()
                named = self._name(
                    "the record and field carrying the calendar, e.g. team_person.timezone"
                )
                kind, _, field = named.partition(".")
                if not field:
                    raise self._error(
                        f'"{named}" needs a kind and a field -- `team_person.timezone`. '
                        "A calendar is a fact about the subject now, not a tenant dial: "
                        "one dial cut every board's days on somebody else's midnight.",
                        line,
                    )
                zone = Zone(kind=kind, field=field)

        return IndexField(field=path, through=through, truncate=truncate, select=select, zone=zone)

    _GRAINS: tuple[str, ...] = ("minute", "hour", "day", "week", "month", "quarter")
    _ORDINALS: tuple[str, ...] = ("first", "second", "third", "fourth", "fifth")
    _WEEKDAYS: tuple[str, ...] = (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    )

    def _bucket_rule(self) -> tuple[Truncation | None, str | None]:
        """The bucket rule after `by`: a grain, or a selective calendar rule.

        Grains -- `minute`, `15 minutes`, `hour`, `day`, `week`, `month`,
        `quarter` -- truncate an instant to a calendar bucket. This is the
        one place calendar vocabulary belongs: the rule decides how many
        values a figure has and which bucket an event lands in, so it is a
        declaration, hashed -- and a reading's `over 1-6` is then just six
        positions in whatever sequence was declared here. A coarser figure
        beside a finer one is two declarations with two names and two
        hashes, each computed directly from the records.

        Selective rules -- `first monday of month` through `fifth sunday of
        month` -- pick a sparse day per month and are deliberately partial:
        an instant not on that day is in no bucket, the way a record with no
        value is in no `is set` bucket. Other minute counts than 15 wait for
        a definition to ask, exactly as `percentile` does.
        """
        if self._is("number"):
            count = self._next().value
            self._keyword("minutes")
            if count != "15":
                raise self._error(
                    f'"{count} minutes" is not a bucket rule. The sub-day grains are '
                    '"minute", "15 minutes" and "hour": a rule decides how many values a '
                    "figure has, so each one is a decision about grain rather than a "
                    "convenience, and no definition has asked for this one."
                )
            return "15 minutes", None
        word = self._name(
            'a bucket rule: a grain ("day", "month", ...) or an ordinal weekday '
            '("first monday of month")'
        )
        if word in self._GRAINS:
            return word, None  # type: ignore[return-value]
        if word in self._ORDINALS:
            weekday = self._name('a weekday: "monday" through "sunday"')
            if weekday not in self._WEEKDAYS:
                raise self._error(
                    f'"{weekday}" is not a weekday. An ordinal rule is written '
                    '"first monday of month" -- first..fifth, monday..sunday.'
                )
            self._keyword("of")
            self._keyword("month")
            return None, f"{word} {weekday} of month"
        raise self._error(
            f'"{word}" is not a bucket rule. The grains are "minute", "15 minutes", '
            '"hour", "day", "week", "month" and "quarter"; a selective rule is an '
            'ordinal weekday, "first monday of month" through "fifth sunday of month". '
            "The rule lives here, in the declaration, because it decides what a "
            "stored value means -- a reading's window is then bare positions in "
            "this sequence."
        )

    def _index_where(self) -> IndexBy:
        field = self._name("a field to test")

        if self._at_word("is"):
            self._next()
            negated = False
            if self._at_word("not"):
                self._next()
                negated = True
            self._keyword("set")
            return ByPresence(field=field, negated=negated)

        if self._at_word("older") or self._at_word("younger"):
            word = self._next().value
            self._keyword("than")
            direction: Literal["older", "younger"] = (
                "older" if word == "older" else "younger"
            )
            line = self._peek().line

            if self._peek().kind == "number":
                days = float(self._next().value)
                # `3 days` reads better than `3` and costs one optional word.
                # The unit is fixed either way -- see `ByAge` for why making it
                # configurable would make the unsafe version writable.
                if self._at_word("days"):
                    self._next()
                return ByAge(field=field, direction=direction, days=days)

            read = self._name("a number of days, or a field to read one from")
            if "." in read:
                raise self._error(
                    f'"{read}" is a dotted name, so it reads a tenant dial. A filter '
                    "runs over records before anything buckets them by subject, so "
                    "there is no subject to look a goal up by -- read the threshold "
                    "off the record's owner instead (`older than stale_days from "
                    "repo_id through code_repo.id`), or write the number of days "
                    "here where a reader can see it.",
                    line,
                )
            self._keyword("from")
            local = self._name("the field on this record that names its owner")
            self._keyword("through")
            joined = self._name("the owner's kind and the field its key is matched on")
            kind, _, path = joined.partition(".")
            if not path:
                raise self._error(
                    f'"{joined}" needs a kind and a path -- `code_repo.id`.', line
                )
            return ByAge(
                field=field,
                direction=direction,
                read=read,
                through=Through(kind=kind, path=path),
                local=local,
            )

        if self._at_op("==") or self._at_op("!="):
            symbol = self._next().value
            value, quoted = self._predicate_value()
            return ByPredicate(
                field=field, op="==" if symbol == "==" else "!=", value=value, quoted=quoted
            )

        raise self._error(
            f'expected "==", "!=", "is set", "older than" or "younger than", got '
            f"{self._describe()}"
        )

    def _predicate_value(self) -> tuple[str, bool]:
        tok = self._peek()
        if tok.kind == "string":
            return self._next().value, True
        if tok.kind == "name" and tok.value in ("true", "false"):
            return self._next().value, False
        if tok.kind == "number":
            # Normalised to the exact string a bucket key uses (`3.0` -> `3`),
            # because the comparison at evaluation is between strings: written
            # raw, a trailing `.0` produced a predicate that matched nothing,
            # for ever, with nothing thrown.
            raw = float(self._next().value)
            return (str(int(raw)) if raw.is_integer() else str(raw)), False
        raise self._error(
            f"expected a quoted value, a number, true or false, got {self._describe()}"
        )

    # ----------------------------------------------------------- measure --

    def _measure(self) -> MeasureDecl:
        line = self._peek().line
        self._keyword("measure")
        name = self._name("a measure name, e.g. code_change.open_seconds")
        kind = self._prefix_of(name, "a measure", line)
        self._punct("=")

        if self._at_word("moment"):
            self._next()
            moment = self._name("a field holding an instant")
            self._end_of_line()
            return MomentMeasure(name=name, kind=kind, moment=moment, line=line)

        first = self._name("a field, or the word now")
        if self._at_op("-"):
            self._next()
            earlier = self._name("the earlier moment")
            self._end_of_line()
            return DurationMeasure(
                name=name, kind=kind, later=first, earlier=earlier, clock=first == "now", line=line
            )

        if first == "now":
            raise self._error(
                '"now" is the clock, so it only means something subtracted from: '
                "`measure code_review_request.waiting_seconds = now - requestedAt`.",
                line,
            )

        self._keyword("in")
        unit = self._name('a unit: "effort" or "count"')
        if unit not in _MEASURE_UNITS:
            raise self._error(
                f'"{unit}" is not a measure unit. A field measure reads whatever the record '
                'carries, so it has to say what the number is: "effort" for seconds of '
                'working time, "count" for a tally.',
                line,
            )
        self._end_of_line()
        measure_unit: MeasureUnit = "effort" if unit == "effort" else "count"
        return FieldMeasure(name=name, kind=kind, field=first, unit=measure_unit, line=line)

    # ------------------------------------------------------------ figure --

    def _figure(self) -> FigureDecl:
        line = self._peek().line
        self._keyword("figure")
        name = self._name("a figure name, e.g. team_person.open_mrs")
        self._prefix_of(name, "a figure", line)

        across: str | None = None
        bucketed = False
        # `bucketed` and `across` are the two things a figure can say about
        # its second key part, and they are mutually exclusive by grammar as
        # well as by rule: a bucket of time has no roster and no name, so it
        # is not a dimension. Accepted in either order because directive
        # order is free everywhere else in this language and a reader should
        # not have to remember an exception.
        while self._at_word("across") or self._at_word("bucketed"):
            if self._at_word("bucketed"):
                self._next()
                if bucketed:
                    raise self._error(
                        f"figure {name} says `bucketed` twice.", line
                    )
                bucketed = True
                continue
            self._next()
            across = self._name("the fact kind this is split across")

        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the figure name")

        display = ""
        unit: DeclaredUnit | None = None
        sets: list[NamedSet] = []
        combines: list[Combine] = []
        calculate: CalcExpr | None = None
        carried = False
        band: CalcExpr | None = None
        seen: set[str] = set()

        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word == "band":
                self._once(seen, "band", name)
                band = self._band_block()
            elif word == "display":
                self._once(seen, "display", name)
                self._next()
                display = self._string("the display template")
                self._end_of_line()
            elif word == "unit":
                self._once(seen, "unit", name)
                self._next()
                unit = self._declared_unit()
                self._end_of_line()
            elif word == "depends":
                self._once(seen, "depends", name)
                sets = self._depends_block()
            elif word == "combine":
                self._once(seen, "combine", name)
                combines = self._combine_block()
            elif word == "calculate":
                self._once(seen, "calculate", name)
                calculate, carried = self._calculate_block()
            else:
                raise self._error(
                    'expected "display", "unit", "depends", "combine", "calculate" or "band", '
                    f"got {self._describe()}"
                )
            self._skip_newlines()

        self._expect("dedent", "the end of the figure block")

        if calculate is None:
            raise self._error(f"figure {name} has no calculate block, so it produces nothing.", line)
        if not display:
            raise self._error(
                f"figure {name} has no display template. Every recorded movement is rendered "
                "at write time and frozen, so a figure with nothing to be called cannot be "
                "reported.",
                line,
            )
        if band is not None and not isinstance(band, Ladder):
            raise self._error(
                f"figure {name}'s band is not a ladder. A band names which of a few words "
                "this number falls under, so it is a `when ... then ... otherwise` and "
                "nothing else -- a bare expression here would be a second calculation.",
                line,
            )

        return FigureDecl(
            name=name,
            doc="",
            display=display,
            calculate=calculate,
            across=across,
            bucketed=bucketed,
            carried=carried,
            unit=unit,
            sets=tuple(sets),
            combines=tuple(combines),
            band=band,
            line=line,
        )

    def _band_block(self) -> CalcExpr:
        """`band:` -- the second thing a figure answers.

        Parsed with the ordinary calculation parser rather than a grammar of its
        own, so a rung reads identically here and in `calculate` and there is one
        set of precedence rules. What narrows it to a band is the checker: the
        only binding in scope is `value`, this figure's own answer, and every
        rung must produce a word.
        """
        self._keyword("band")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after band")
        expr, _ = self._calc_body()
        self._expect("dedent", "the end of the band block")
        return expr

    def _declared_unit(self) -> DeclaredUnit:
        word = self._name("a unit")
        if word in _DERIVED_UNITS:
            raise self._error(
                f'"{word}" is worked out for you rather than declared: a ladder returns a '
                "level and an extreme returns a moment, so writing it would be a second "
                "place for one fact to live."
            )
        if word == "share":
            return "share"
        if word == "days":
            return "days"
        if word == "effort":
            return "effort"
        if word == "count":
            return "count"
        if word == "duration":
            return "duration"
        raise self._error(
            f'"{word}" is not a unit. Those are: {", ".join(sorted(_DECLARED_UNITS))}.'
        )

    def _depends_block(self) -> list[NamedSet]:
        self._keyword("depends")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after depends")
        out: list[NamedSet] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            name = self._name("a set name")
            self._punct("=")
            out.append(NamedSet(name=name, expr=self._set_expr(), line=line))
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the depends block")
        return out

    def _combine_block(self) -> list[Combine]:
        self._keyword("combine")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after combine")
        out: list[Combine] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            name = self._name("a name for what this binds")
            self._punct("=")
            figure = self._name("a figure name")
            over: str | None = None
            if self._at_word("over"):
                self._next()
                over = self._name("the fact kind being summed away")
            out.append(Combine(name=name, figure=figure, over=over, line=line))
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the combine block")
        return out

    def _calculate_block(self) -> tuple[CalcExpr, bool]:
        """The calculation, and whether it is carried forward.

        `carried forward` is a suffix on the expression rather than a
        directive of its own, because it is not a second thing the figure
        does -- it says what the one calculation *means* between the buckets
        that have records. Written as a sibling directive it would read like
        a switch on storage, and a reader could plausibly expect a figure
        with a `carried forward:` block and no calculate.
        """
        self._keyword("calculate")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after calculate")
        expr, carried = self._calc_body(allow_carried=True)
        self._expect("dedent", "the end of the calculate block")
        return expr, carried

    # --------------------------------------------------------------- set --

    def _set_expr(self) -> SetExpr:
        left = self._set_atom()
        while self._at_op("&") or self._at_op("|") or self._at_op("-"):
            line = self._peek().line
            symbol = self._next().value
            left = SetOp(
                op=(
                    "intersect"
                    if symbol == "&"
                    else "union"
                    if symbol == "|"
                    else "difference"
                ),
                left=left,
                right=self._set_atom(),
                line=line,
            )
        return left

    def _set_atom(self) -> SetExpr:
        line = self._peek().line
        if self._at_op("("):
            self._next()
            inner = self._set_expr()
            self._punct(")")
            return inner
        name = self._name("a group or filter name, or a set defined above")
        if self._at_op(":"):
            self._next()
            self._punct("{")
            variable = self._name("the scope variable")
            self._punct("}")
            return SetIndex(index=name, bucket=BucketScope(variable=variable), line=line)
        if "." in name:
            return SetIndex(index=name, bucket=BucketAll(), line=line)
        return SetRef(name=name, line=line)

    # --------------------------------------------------------- expression --

    def _calc_body(self, allow_carried: bool = False) -> tuple[CalcExpr, bool]:
        """A ladder, or a single expression, and whether it is carried forward.

        A ladder is recognised by its first word rather than by lookahead,
        because `when` cannot begin any other expression.

        `carried forward` is read here rather than after the block, because
        it sits on the expression's own line -- it says what this
        calculation means between the buckets that have records, so putting
        it on a line of its own would read like a directive about storage.
        """
        if self._at_word("when"):
            return self._ladder(), False
        expr = self._calc_expr()
        carried = False
        if allow_carried and self._at_word("carried"):
            self._next()
            self._keyword("forward")
            carried = True
        self._end_of_line()
        self._skip_newlines()
        return expr, carried

    def _ladder(self) -> CalcExpr:
        line = self._peek().line
        rungs: list[Rung] = []
        while self._at_word("when"):
            rung_line = self._peek().line
            self._next()
            left = self._calc_expr()
            op, right = self._condition_tail()
            self._keyword("then")
            then = self._calc_expr()
            rungs.append(Rung(left=left, op=op, then=then, right=right, line=rung_line))
            self._end_of_line()
            self._skip_newlines()

        if not self._at_word("otherwise"):
            raise self._error(
                'a "when" ladder must end in "otherwise". Without it a value can be missing '
                "because a case was never written, which renders exactly like a value that "
                "is missing because the definition says so -- and only one of those is a "
                "claim.",
                line,
            )
        self._next()
        otherwise = self._calc_expr()
        self._end_of_line()
        self._skip_newlines()
        return Ladder(rungs=tuple(rungs), otherwise=otherwise, line=line)

    def _condition_tail(self) -> tuple[Comparison | AbsenceTest, CalcExpr | None]:
        if self._at_word("is"):
            self._next()
            word = self._name('"nothing" or "something"')
            if word not in ("nothing", "something"):
                raise self._error(
                    f'"is {word}" is not a test. The two that exist are "is nothing" and '
                    '"is something", which ask about presence rather than about size.'
                )
            test: AbsenceTest = "nothing" if word == "nothing" else "something"
            return test, None
        if self._peek().kind == "op" and self._peek().value in _COMPARISONS:
            symbol = self._next().value
            comparison: Comparison = (
                ">="
                if symbol == ">="
                else ">"
                if symbol == ">"
                else "<="
                if symbol == "<="
                else "<"
                if symbol == "<"
                else "=="
                if symbol == "=="
                else "!="
            )
            return comparison, self._calc_expr()
        raise self._error(
            f'expected a comparison, "is nothing" or "is something", got {self._describe()}'
        )

    def _calc_expr(self) -> CalcExpr:
        return self._additive()

    def _additive(self) -> CalcExpr:
        left = self._multiplicative()
        while self._at_op("+") or self._at_op("-"):
            line = self._peek().line
            symbol = self._next().value
            plus_minus: ArithOperator = "+" if symbol == "+" else "-"
            left = Arith(op=plus_minus, left=left, right=self._multiplicative(), line=line)
        return left

    def _multiplicative(self) -> CalcExpr:
        left = self._atom()
        while self._at_op("*") or self._at_op("/"):
            line = self._peek().line
            symbol = self._next().value
            times_over: ArithOperator = "*" if symbol == "*" else "/"
            left = Arith(op=times_over, left=left, right=self._atom(), line=line)
        return left

    def _atom(self) -> CalcExpr:
        tok = self._peek()
        line = tok.line

        if self._at_op("("):
            self._next()
            inner = self._calc_expr()
            self._punct(")")
            return inner

        if tok.kind == "number":
            self._next()
            return Number(value=float(tok.value), line=line)

        if tok.kind == "string":
            self._next()
            return Text(value=tok.value, line=line)

        if tok.kind != "name":
            raise self._error(f"expected a value, got {self._describe()}")

        name = self._next().value

        if name in (
            "count", "list", "sum", "max", "min", "latest", "earliest",
            "mean", "median", "worst",
        ) and self._at_op("("):
            return self._call(name, line)

        if name == "days" and self._at_word("from"):
            self._next()
            frm = self._name("the earlier moment")
            self._keyword("to")
            to = self._name("the later moment")
            return DaysBetween(frm=frm, to=to, line=line)

        if self._at_op(":") and self._peek_at(1, "op", "{"):
            # `<name>:{bucket}` -- the coordinate selector. The `{` is part
            # of the test, not just of the parse: a projection's `flag x when
            # swing >= thresholds.blowout:` ends a condition with a colon
            # that opens a block, and treating that as a selector turned the
            # NFL example into a syntax error.
            #
            # Spelled exactly like a group's `:{scope}` because it is the
            # same idea one stratum up: address the one cell of a keyed
            # thing that this calculation is already standing at.
            self._next()
            self._punct("{")
            variable = self._name('the coordinate variable, which is "bucket"')
            if variable != "bucket":
                raise self._error(
                    f'"{variable}" is not a coordinate. A sequenced figure is read at '
                    "`:{bucket}` -- the bucket this calculation is already at.",
                    line,
                )
            if not self._at_op("}"):
                # `:{bucket - 1}` and friends land here. Refused in the
                # grammar rather than the checker because there is nothing
                # to explain about a construct that does not exist.
                raise self._error(
                    "a coordinate selector reads the bucket this calculation is at, and "
                    "nothing else. There is no `:{bucket - 1}`: a stored value whose "
                    "answer needs a bucket outside the population in view cannot be "
                    "checked against the response that carries it.",
                    line,
                )
            self._punct("}")
            return Coord(name=name, line=line)

        # A dotted name is a settings path; a bare one is something the
        # definition bound above. Nothing else can produce either shape, so no
        # lookahead is needed and a typo is named by the checker with the list
        # of what *was* bound.
        if "." in name:
            return Setting(path=name, line=line)
        return Part(name=name, line=line)

    def _call(self, name: str, line: int) -> CalcExpr:
        self._punct("(")
        if name == "count":
            target = self._name("a set defined in depends")
            self._punct(")")
            return Count(set=target, line=line)

        if name in ("max", "min"):
            left = self._calc_expr()
            self._punct(",")
            right = self._calc_expr()
            self._punct(")")
            return Pick(
                which="max" if name == "max" else "min", left=left, right=right, line=line
            )

        first = self._name("a measure or a set")
        if name in ("mean", "median", "worst"):
            self._keyword("over")
            target = self._name("a set defined in depends")
            self._punct(")")
            return BucketStat(
                fn="mean" if name == "mean" else "median" if name == "median" else "worst",
                measure=first,
                set=target,
                line=line,
            )

        if name in ("latest", "earliest"):
            self._keyword("over")
            target = self._name("a set defined in depends")
            self._punct(")")
            return Extreme(
                which="latest" if name == "latest" else "earliest",
                measure=first,
                set=target,
                line=line,
            )

        if name == "list":
            self._keyword("over")
            target = self._name("a set defined in depends")
            self._punct(")")
            return ListOf(measure=first, set=target, line=line)

        # sum
        if self._at_word("over"):
            self._next()
            target = self._name("a set defined in depends")
            self._punct(")")
            return Sum(set=target, measure=first, line=line)
        self._punct(")")
        return Sum(set=first, measure=None, line=line)

    # ----------------------------------------------------------- reading --

    def _reading(self) -> ReadingDecl:
        line = self._peek().line
        self._keyword("reading")
        name = self._name("a reading name, e.g. team_person.to_merge")
        self._prefix_of(name, "a reading", line)
        self._punct("(")
        args: list[str] = []
        while not self._at_op(")"):
            args.append(self._name("an argument name"))
            if self._at_op(","):
                self._next()
        self._punct(")")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the reading name")

        display = ""
        band: ReadingBand | None = None
        sets: list[ReadingSet] = []
        requires: list[Requirement] = []
        calculate: list[Statistic] = []
        seen: set[str] = set()

        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word == "display":
                self._once(seen, "display", name)
                self._next()
                display = self._string("the display template")
                self._end_of_line()
            elif word == "band":
                self._once(seen, "band", name)
                band = self._band()
            elif word == "depends":
                self._once(seen, "depends", name)
                sets = self._reading_depends()
            elif word == "requires":
                self._once(seen, "requires", name)
                requires = self._requires_block()
            elif word == "calculate":
                self._once(seen, "calculate", name)
                calculate = self._statistics_block()
            else:
                raise self._error(
                    'expected "display", "band", "depends", "requires" or "calculate", got '
                    f"{self._describe()}"
                )
            self._skip_newlines()

        self._expect("dedent", "the end of the reading block")
        if not display:
            raise self._error(f"reading {name} has no display template.", line)
        if not calculate:
            raise self._error(f"reading {name} calculates nothing.", line)

        return ReadingDecl(
            name=name,
            doc="",
            display=display,
            args=tuple(args),
            sets=tuple(sets),
            calculate=tuple(calculate),
            requires=tuple(requires),
            band=band,
            line=line,
        )

    def _band(self) -> ReadingBand:
        """`band [on <statistic>]:` and an indented ladder.

        The same block a figure writes, with one extra word: which statistic
        the verdict is about. A reading answers several numbers and a band is
        a verdict on one of them.
        """
        line = self._peek().line
        self._keyword("band")

        if not self._at_op(":") and not self._at_word("on"):
            # `band low against flow.leadTimeDays in minutes` -- the clause
            # this replaced. Named here rather than left to fail as "expected
            # a colon", because the author of an existing definition needs to
            # be told what the rewrite is, not that a colon is missing.
            raise self._error(
                'a band is a ladder now, not a direction and a dial: write `band:` (or '
                '`band on <statistic>:`) and an indented `when ... then "word"` ladder. '
                "The comparison operator carries the direction, and the threshold is a "
                "figure -- a fact with a unit of its own -- rather than a tenant dial "
                "that nothing on the screen could cite.",
                line,
            )

        on: StatisticFn | None = None
        if self._at_word("on"):
            self._next()
            stat = self._name("the statistic the band judges")
            if stat not in _STATISTICS:
                raise self._error(f'"{stat}" is not a statistic.')
            on = stat  # type: ignore[assignment]

        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after band")
        ladder = self._calc_body()[0]
        self._expect("dedent", "the end of the band block")
        if not isinstance(ladder, Ladder):
            raise self._error(
                "a band is a `when ... then \"word\"` ladder ending in `otherwise`. "
                "A band names which of a few states a number is in, so there is "
                "nothing for a bare expression to answer.",
                line,
            )
        return ReadingBand(ladder=ladder, on=on, line=line)

    def _reading_depends(self) -> list[ReadingSet]:
        self._keyword("depends")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after depends")
        out: list[ReadingSet] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            name = self._name("a set name")
            self._punct("=")
            source = self._name("a figure or a measure")
            if self._at_word("in"):
                self._next()
                self._keyword("range")
                out.append(
                    ReadingSet(name=name, windowed=WindowedSource(figure=source, line=line), line=line)
                )
            elif self._at_word("over"):
                self._next()
                out.append(
                    ReadingSet(
                        name=name,
                        live=LiveSource(measure=source, set=self._set_expr(), line=line),
                        line=line,
                    )
                )
            else:
                raise self._error(
                    f'expected "in range" or "over <set>", got {self._describe()}. A reading '
                    "either summarises a figure's stored values over a range, or measures "
                    "records as they stand right now."
                )
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the depends block")
        return out

    def _requires_block(self) -> list[Requirement]:
        self._keyword("requires")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after requires")
        out: list[Requirement] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            self._keyword("at")
            self._keyword("least")
            tok = self._expect("number", "a number of values")
            self._keyword("values")
            self._keyword("in")
            out.append(Requirement(count=int(float(tok.value)), set=self._name("a set"), line=line))
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the requires block")
        return out

    def _statistics_block(self) -> list[Statistic]:
        self._keyword("calculate")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after calculate")
        out: list[Statistic] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            fn = self._name("a statistic")
            if fn not in _STATISTICS:
                raise self._error(
                    f'"{fn}" is not a statistic. Those are: {", ".join(sorted(_STATISTICS))}. '
                    "The vocabulary is closed on purpose: each one is a claim about a "
                    "distribution that a reader has to be able to check."
                )
            self._punct("(")
            target = self._name("a set defined in depends")
            self._punct(")")
            if self._at_word("by"):
                raise self._error(
                    "a series' points are one per bucket of the figure's own sequence -- "
                    "the grain its group declared -- so `series(...) by <grain>` was "
                    "retired. A coarser view is its own declaration: group the figure "
                    "`by hour` (or `by day`, `by month`, ...) under its own name, and "
                    "read that."
                )
            out.append(Statistic(fn=fn, set=target, line=line))  # type: ignore[arg-type]
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the calculate block")
        return out

    # -------------------------------------------------------- projection --

    def _project(self) -> ProjectDecl:
        line = self._peek().line
        self._keyword("projection")
        name = self._name("a projection name, e.g. work_issue.item")
        self._prefix_of(name, "a projection", line)
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the projection name")

        frm: SetExpr | None = None
        fields: list[FieldDecl] = []
        reads: list[ReadDecl] = []
        values: list[ValueDecl] = []
        flags: list[FlagDecl] = []
        omit: Condition | None = None
        sort: SortDecl | None = None
        limit: int | None = None
        seen: set[str] = set()

        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word == "from":
                self._once(seen, "from", name)
                self._next()
                frm = self._set_expr()
                self._end_of_line()
            elif word == "field":
                self._once(seen, "field", name)
                fields = self._field_block()
            elif word == "read":
                self._once(seen, "read", name)
                reads = self._read_block()
            elif word == "value":
                self._once(seen, "value", name)
                values = self._value_block()
            elif word == "flag":
                # Not `once`: a row earns as many sentences as its state
                # deserves, and each carries an indented body of its own.
                flags.append(self._flag())
            elif word == "omit":
                # One gate, like one sort: a second `omit` would be a
                # conjunction wearing a clause's syntax, and the answer to
                # needing one is the answer the flag grammar gives -- a value
                # that is a ladder, tested here by its word.
                self._once(seen, "omit", name)
                self._next()
                self._keyword("when")
                left = self._calc_expr()
                op, right = self._condition_tail()
                omit = Condition(left=left, op=op, right=right)
                self._end_of_line()
            elif word == "sort":
                self._once(seen, "sort", name)
                sort = self._sort()
            elif word == "limit":
                self._once(seen, "limit", name)
                self._next()
                tok = self._expect("number", "a row count")
                limit = int(float(tok.value))
                self._end_of_line()
            else:
                raise self._error(
                    'expected "from", "field", "read", "value", "flag", "omit", "sort" or '
                    f'"limit", got {self._describe()}'
                )
            self._skip_newlines()

        self._expect("dedent", "the end of the projection block")

        if not fields and not reads and not values:
            raise self._error(
                f"projection {name} produces nothing: with no field, read or value block a "
                "row is a bare record id. Name at least the key and the link a screen needs.",
                line,
            )
        return ProjectDecl(
            name=name,
            doc="",
            frm=frm,
            fields=tuple(fields),
            reads=tuple(reads),
            values=tuple(values),
            flags=tuple(flags),
            omit=omit,
            sort=sort,
            limit=limit,
            line=line,
        )

    def _field_block(self) -> list[FieldDecl]:
        self._keyword("field")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after field")
        out: list[FieldDecl] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            name = self._name("a field name")
            self._punct("=")
            path = self._name("the path on the record")
            join: Join | None = None
            if self._at_word("from"):
                self._next()
                local = self._name("the field on this record identifying the other one")
                self._keyword("through")
                target = self._name("the other record's kind and path")
                kind, _, rest = target.partition(".")
                if not rest:
                    raise self._error(f'"{target}" needs a path: `through <fact kind>.<path>`.')
                join = Join(field=local, kind=kind, path=rest)
            self._keyword("as")
            ftype = self._name("a field type")
            if ftype not in _FIELD_TYPES:
                raise self._error(
                    f'"{ftype}" is not a field type. Those are: '
                    f'{", ".join(sorted(_FIELD_TYPES))}. A string is mute in the way an '
                    'integer is, so "date" is what lets a span know this is a moment.'
                )
            out.append(
                FieldDecl(name=name, path=path, type=ftype, join=join, line=line)  # type: ignore[arg-type]
            )
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the field block")
        return out

    def _read_block(self) -> list[ReadDecl]:
        self._keyword("read")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after read")
        out: list[ReadDecl] = []
        while not self._is("dedent") and not self._is("eof"):
            line = self._peek().line
            name = self._name("a name for what this binds")
            self._punct("=")
            # `band of <figure>` binds the word the figure's own `band:` block
            # answers; a bare name binds its number. Two spellings rather than a
            # second binding appearing automatically beside every read: a name
            # in scope that appears nowhere in the text is the thing this
            # language is arranged against.
            band = False
            if self._at_word("band"):
                self._next()
                self._keyword("of")
                band = True
            out.append(
                ReadDecl(
                    name=name, figure=self._name("a figure name"), band=band, line=line
                )
            )
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the read block")
        return out

    def _value_block(self) -> list[ValueDecl]:
        self._keyword("value")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after value")
        out: list[ValueDecl] = []
        while not self._is("dedent") and not self._is("eof"):
            out.append(self._value_decl())
            self._skip_newlines()
        self._expect("dedent", "the end of the value block")
        return out

    def _value_decl(self) -> ValueDecl:
        line = self._peek().line
        name = self._name("a value name")
        unit: DeclaredUnit | None = None
        if self._at_word("in"):
            self._next()
            unit = self._declared_unit()
        self._punct("=")
        if self._is("newline"):
            # A ladder's rungs live on the following lines, indented.
            self._next()
            self._expect("indent", "the rungs of the ladder")
            expr = self._ladder()
            self._expect("dedent", "the end of the ladder")
        else:
            expr, _ = self._calc_body()
        return ValueDecl(name=name, expr=expr, unit=unit, line=line)

    def _flag(self) -> FlagDecl:
        line = self._peek().line
        self._keyword("flag")
        name = self._flag_name()
        self._keyword("when")
        left = self._calc_expr()
        op, right = self._condition_tail()
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the flag")

        label = ""
        detail = ""
        action: str | None = None
        severity: str | None = None
        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word == "label":
                self._next()
                label = self._string("the short label")
            elif word == "detail":
                self._next()
                detail = self._string("the sentence under it")
            elif word == "action":
                self._next()
                action = self._string("what to do about it, in the imperative")
            elif word == "severity":
                self._next()
                severity = self._name('"info" or "attention"')
                if severity not in ("info", "attention"):
                    raise self._error(f'"{severity}" is not a severity.')
            else:
                raise self._error(
                    f'expected "label", "detail", "action" or "severity", got '
                    f"{self._describe()}"
                )
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the flag block")

        if not label or not detail or severity is None:
            raise self._error(
                f'flag {name} needs a label, a detail and a severity.', line
            )
        return FlagDecl(
            name=name,
            when=Condition(left=left, op=op, right=right),
            label=label,
            detail=detail,
            severity="attention" if severity == "attention" else "info",
            action=action,
            line=line,
        )

    def _flag_name(self) -> str:
        """A flag's name is a kind a screen groups by, so it is written with
        hyphens: `issue-long-wip`. The lexer cannot produce that as one token --
        `-` is set difference -- so it is reassembled here, where the grammar
        knows a bare word is expected and no expression can follow."""
        parts = [self._name("a flag name, e.g. issue-long-wip")]
        while self._at_op("-"):
            self._next()
            parts.append(self._name("the rest of the flag name"))
        return "-".join(parts)

    def _sort(self) -> SortDecl:
        line = self._peek().line
        self._keyword("sort")
        self._keyword("by")
        name = self._name("the value to sort by")
        word = self._name('"ascending" or "descending"')
        if word not in ("ascending", "descending"):
            raise self._error(f'"{word}" is not a sort direction.')
        self._end_of_line()
        return SortDecl(
            name=name,
            direction="ascending" if word == "ascending" else "descending",
            line=line,
        )

    # --------------------------------------------------------- summarise --

    def _summarise(self) -> SummariseDecl:
        line = self._peek().line
        self._keyword("summarise")
        name = self._name("a summary name, e.g. work_container.roadmap")
        self._prefix_of(name, "a summary", line)
        self._keyword("over")
        over = self._name("the projection whose rows this summarises")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the summary name")

        counts: list[CountDecl] = []
        totals: list[TotalDecl] = []
        values: list[ValueDecl] = []
        flags: list[FlagDecl] = []
        seen: set[str] = set()

        while not self._is("dedent") and not self._is("eof"):
            word = self._peek().value
            if word == "count":
                counts.append(self._count_decl())
            elif word == "total":
                totals.append(self._total_decl())
            elif word == "value":
                self._once(seen, "value", name)
                values = self._value_block()
            elif word == "flag":
                flags.append(self._flag())
            else:
                raise self._error(
                    f'expected "count", "total", "value" or "flag", got {self._describe()}'
                )
            self._skip_newlines()

        self._expect("dedent", "the end of the summary block")

        return SummariseDecl(
            name=name,
            over=over,
            doc="",
            counts=tuple(counts),
            totals=tuple(totals),
            values=tuple(values),
            flags=tuple(flags),
            line=line,
        )

    def _count_decl(self) -> CountDecl:
        line = self._peek().line
        self._keyword("count")
        name = self._name("a name for the count")
        when: Condition | None = None
        if self._at_word("where"):
            self._next()
            left = self._calc_expr()
            op, right = self._condition_tail()
            when = Condition(left=left, op=op, right=right)
        self._end_of_line()
        return CountDecl(name=name, when=when, line=line)

    def _total_decl(self) -> TotalDecl:
        line = self._peek().line
        self._keyword("total")
        name = self._name("a name for the total")
        self._keyword("in")
        unit = self._declared_unit()
        self._punct("=")
        of = self._name("the projection value being added up")
        when: Condition | None = None
        if self._at_word("where"):
            self._next()
            left = self._calc_expr()
            op, right = self._condition_tail()
            when = Condition(left=left, op=op, right=right)
        self._end_of_line()
        return TotalDecl(name=name, of=of, unit=unit, when=when, line=line)

    # ------------------------------------------------------------ bundle --

    def _bundle(self) -> BundleDecl:
        line = self._peek().line
        self._keyword("bundle")
        name = self._name("a bundle name, e.g. team_person.card")
        self._prefix_of(name, "a bundle", line)
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after the bundle name")

        members: list[BundleMember] = []
        while not self._is("dedent") and not self._is("eof"):
            members.append(self._bundle_member())
            self._skip_newlines()
        self._expect("dedent", "the end of the bundle block")
        if not members:  # pragma: no cover - the lexer yields no empty indent
            raise self._error(f"bundle {name} names nothing.", line)
        return BundleDecl(name=name, doc="", members=tuple(members), line=line)

    def _bundle_member(self) -> BundleMember:
        """One member: a slot binding, a declaration keyword, a name, and
        (for a reading) an optional window list. Names plus arguments and
        nothing else -- a member line that could carry a calculation would be
        a second place for a number to be defined, which is what a bundle
        exists not to be.
        """
        line = self._peek().line
        slot = self._name('a slot name, e.g. "latency = reading team_person.to_merge"')
        if not self._at_op("="):
            # The pre-binding grammar started the line with the keyword, so a
            # bare `figure team_person.wip` lands here -- taught forward with
            # the new form spelled out rather than refused as a bare
            # "expected =".
            if slot == "bundle":
                raise self._error(
                    "a bundle may not name another bundle. Composition stays flat -- "
                    "one level of bundles over the declarations that compute -- so "
                    "there is no nesting to walk and no cycle to refuse.",
                    line,
                )
            if slot in ("figure", "reading", "projection", "summarise"):
                raise self._error(
                    f'every member binds a slot: write "<slot> = {slot} <name>". The '
                    "slot is the address a client reads this member at, so a screen "
                    "couples to the tile's layout rather than to definition names.",
                    line,
                )
            raise self._error(
                f'expected "=" after the slot name "{slot}": a member is '
                '"<slot> = <keyword> <name>".',
                line,
            )
        if "." in slot:
            raise self._error(
                f'"{slot}" is not a slot name. A slot is a bare word the client '
                "addresses -- the definition name comes after the keyword.",
                line,
            )
        self._punct("=")

        word = self._peek().value
        if word == "bundle":
            raise self._error(
                "a bundle may not name another bundle. Composition stays flat -- one "
                "level of bundles over the declarations that compute -- so there is no "
                "nesting to walk and no cycle to refuse.",
                line,
            )
        if word not in ("figure", "reading", "projection", "summarise"):
            raise self._error(
                'expected "figure", "reading", "projection" or "summarise", got '
                f"{self._describe()}"
            )
        self._next()
        member_kind: Literal["figure", "reading", "projection", "summarise"] = (
            "figure"
            if word == "figure"
            else "reading"
            if word == "reading"
            else "projection"
            if word == "projection"
            else "summarise"
        )
        name = self._name(f"a {word} name")

        windows: tuple[WindowSpec, ...] | None = None
        if self._at_word("over"):
            if word != "reading":
                raise self._error(
                    f"only a reading member takes a window list; a {word} has no stored "
                    "days to window. A projection member means rows, a summarise member "
                    "means the population row alone, and a figure member is its current "
                    "value.",
                    line,
                )
            self._next()
            windows = self._window_specs(line)

        self._end_of_line()
        return BundleMember(slot=slot, kind=member_kind, name=name, windows=windows, line=line)

    def _window_specs(self, line: int) -> tuple[WindowSpec, ...]:
        """`over 7, 14, 30` / `over 1-30, 31-60` / `over each 1-12`.

        Each entry is a span of positions in the reading's own bucket
        sequence, counted back from the anchor -- bare integers and nothing
        else, because what a bucket *is* lives in the source figure's group
        clause, hashed, and an argument may never change what a number
        means. (`in hours` rode here briefly and was retired with the rest
        of the unit-suffixed argument tokens.) `each a-b` expands to the
        one-bucket windows `a-a ... b-b`, one window per bucket in order,
        so a per-bucket comparison is not twelve enumerated spans.
        """
        bounds: list[tuple[int, int, bool]] = []
        while True:
            each = False
            if self._at_word("each"):
                self._next()
                each = True
            first = self._window_bound(line)
            last = first
            if self._at_op("-"):
                self._next()
                last = self._window_bound(line)
                bounds.append((first, last, each))
            else:
                # A bare bound is `1-N` whether or not `each` precedes it, so
                # `over each 12` is twelve one-bucket windows and not the
                # single bucket 12 -- which is `each 12-12`, and is what the
                # bare form used to mean. An author writing the short spelling
                # wants a column per bucket; handing back one column with no
                # error is a wrong answer wearing the right shape.
                bounds.append((1, last, each))
            if self._at_op(","):
                self._next()
                continue
            break

        if self._at_word("in"):
            raise self._error(
                "a window list is bare positions -- `in hours` (and every unit on an "
                "argument) was retired. What one bucket is lives in the source "
                "figure's group clause (`by hour`, `by month`, ...), where it is "
                "hashed; the spans here walk that sequence.",
                line,
            )

        out: list[WindowSpec] = []
        # A set beside the list purely for the duplicate check: `each 1-3660`
        # is 3,660 windows, and a linear scan per window makes parsing one
        # `over` clause quadratic -- minutes of CPU inside `PUT /definitions`,
        # before the checker's own ceilings ever get a look.
        seen: set[WindowSpec] = set()
        for first, last, each in bounds:
            try:
                # Built once and validated first: `make_window_spec` carries the
                # bucket ceiling, so an oversized span is refused before it is
                # expanded into a window apiece.
                bounded = make_window_spec(first, last)
                specs = (
                    [WindowSpec(first=k, last=k) for k in range(bounded.first, bounded.last + 1)]
                    if each
                    else [bounded]
                )
            except WindowError as refusal:
                raise self._error(str(refusal), line) from refusal
            for spec in specs:
                if spec in seen:
                    # The same shape of mistake as a duplicate member: the window
                    # would serve twice, and a screen binding to window positions
                    # would show a duplicated column from a typo. Compared as
                    # canonical specs, so `over 30, 1-30` -- and an `each` span
                    # overlapping an enumerated one -- is caught as the same
                    # question twice.
                    raise self._error(
                        f'the window list names "{window_token(spec)}" twice. One request '
                        "serves each window once.",
                        line,
                    )
                seen.add(spec)
                out.append(spec)
        # The same ceiling the request doors apply, applied where the list is
        # written down, so a tile cannot commit at compile time to a request
        # the server would refuse at serve time.
        too_many = refuse_window_count(len(out))
        if too_many is not None:
            raise self._error(too_many, line)
        return tuple(out)

    def _window_bound(self, line: int) -> int:
        tok = self._expect("number", 'a bucket span, e.g. "30" or "31-60"')
        raw = float(tok.value)
        if not raw.is_integer():
            raise self._error(
                f'"{tok.value}" is not a span bound. A span is whole buckets, counted '
                "back from the anchor.",
                line,
            )
        return int(raw)

    # ------------------------------------------------------------ shared --

    def _once(self, seen: set[str], word: str, owner: str) -> None:
        if word in seen:
            raise self._error(f'{owner} has more than one "{word}" section')
        seen.add(word)

    def _prefix_of(self, name: str, what: str, line: int) -> str:
        kind, _, rest = name.partition(".")
        if not rest:
            raise self._error(
                f'"{name}" needs a prefix: {what} is named <fact kind>.<what>, and a '
                'citation is "name@version".',
                line,
            )
        return kind


__all__ = ["SyntaxError_", "parse"]
