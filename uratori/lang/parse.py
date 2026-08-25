"""Recursive descent over the token stream, one method per production.

The parser says what was written. It rejects only what it cannot *represent* --
a missing colon, a name where a number belongs -- and never what it cannot
justify. "There is no index called that" and "a ladder may not return a list"
are the checker's, because those errors want to name a rule and list the
alternatives, and a parser has neither the library nor the vocabulary to do it.
"""

from __future__ import annotations

from dataclasses import replace

from .ast import (
    AbsenceTest,
    Arith,
    ArithOperator,
    Band,
    BucketAll,
    BucketScope,
    ByAge,
    ByComposite,
    ByField,
    ByPredicate,
    ByPresence,
    CalcExpr,
    Combine,
    Comparison,
    Condition,
    Count,
    CountDecl,
    DaysBetween,
    Decl,
    DeclaredUnit,
    Document,
    DurationMeasure,
    Extreme,
    FieldDecl,
    FieldMeasure,
    FigureDecl,
    FlagDecl,
    GroupGrain,
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
    ThresholdUnit,
    Through,
    TotalDecl,
    Truncation,
    ValueDecl,
    WindowedSource,
)
from .lex import SyntaxError_, Token, lex, prose_above

_DECLARED_UNITS: frozenset[str] = frozenset({"share", "days", "effort", "count", "duration"})
_DERIVED_UNITS: frozenset[str] = frozenset({"level", "moment"})
_MEASURE_UNITS: frozenset[str] = frozenset({"effort", "count"})
_FIELD_TYPES: frozenset[str] = frozenset({"text", "date", "number", "flag"})
_THRESHOLD_UNITS: frozenset[str] = frozenset({"minutes", "hours", "days"})
_STATISTICS: frozenset[str] = frozenset({"mean", "median", "worst", "sum", "count", "series"})
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
}


def _explained(decl: Decl, lines: list[str]) -> Decl:
    """Attach the `#` comment run above a declaration as its doc.

    The comments are the customer-facing explanation -- one spelling for all
    six declaration kinds, kept out of the block so the directives a reviewer
    came to check are not buried in prose. The lexer strips comments, so this
    reads the raw lines; the declaration's own line number says where to look.

    The four rendered kinds are refused without one: each is served to a
    reader, and an unexplained number on screen is the thing this language
    exists to prevent.
    """
    if not isinstance(decl, (FigureDecl, ReadingDecl, ProjectDecl, SummariseDecl)):
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
                    'expected "index", "measure", "figure", "reading", "projection" or '
                    f'"summarise", got {self._describe()}'
                )
            if tok.value == "index":
                doc.decls.append(self._index())
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
            else:
                raise self._error(
                    'expected "index", "measure", "figure", "reading", "projection" or '
                    f'"summarise", got {self._describe()}'
                )
            self._skip_newlines()
        return doc

    # ------------------------------------------------------------- index --

    def _index(self) -> IndexDecl:
        line = self._peek().line
        self._keyword("index")
        name = self._name("an index name, e.g. code_change.open")
        kind = self._prefix_of(name, "an index", line)

        # `keyed as` sits next to the name because it qualifies the *kind*
        # rather than the bucketing, and a reader meets it in the order it
        # matters: these records are keyed like that kind's, and here is how
        # they are bucketed.
        keyed_as: str | None = None
        if self._at_word("keyed"):
            self._next()
            self._keyword("as")
            keyed_as = self._name("the fact kind whose ids these records use")

        spec: IndexBy
        if self._at_word("from"):
            self._next()
            spec = self._index_from()
        elif self._at_word("where"):
            self._next()
            spec = self._index_where()
        else:
            raise self._error(
                f'expected "from" or "where" after the index name, got {self._describe()}'
            )

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
        path = self._name("a field to bucket by")
        through: Through | None = None
        truncate: Truncation | None = None
        zone: str | None = None

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
            truncate = self._truncation()
            if self._at_word("in"):
                self._next()
                zone = self._name("the setting naming the calendar, e.g. tenant.timezone")

        return IndexField(field=path, through=through, truncate=truncate, zone=zone)

    def _truncation(self) -> Truncation:
        """`by day`, `by minute` or `by 15 minutes` -- the stored grains.

        Everything else is refused by name. `week` and `month` because a range
        over days produces either; `hour` because a range over quarter-hours
        does -- coarser grains are groupings at read time, and storing one
        beside the buckets it is made of would be two answers to one question.
        Other minute counts wait for a definition to ask.
        """
        if self._is("number"):
            count = self._next().value
            self._keyword("minutes")
            if count != "15":
                raise self._error(
                    f'"{count} minutes" is not a truncation. The stored grains are "day", '
                    '"minute" and "15 minutes": a truncation decides how many values a '
                    "figure has, so each one is a decision about grain rather than a "
                    "convenience, and no definition has asked for this one."
                )
            return "15 minutes"
        word = self._name('a truncation: "day", "minute" or "15 minutes"')
        if word in ("day", "minute"):
            return word  # type: ignore[return-value]
        raise self._error(
            f'"{word}" is not a truncation. The stored grains are "day", "minute" and '
            '"15 minutes". Coarser spans -- an hour, a week, a month -- are produced by '
            "reading a range over the stored buckets; storing them as well would be two "
            "answers to one question."
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
            setting = self._name("a settings path holding a number of days")
            return ByAge(
                field=field,
                direction="older" if word == "older" else "younger",
                setting=setting,
            )

        if self._at_op("==") or self._at_op("!="):
            symbol = self._next().value
            value = self._predicate_value()
            return ByPredicate(field=field, op="==" if symbol == "==" else "!=", value=value)

        raise self._error(
            f'expected "==", "!=", "is set", "older than" or "younger than", got '
            f"{self._describe()}"
        )

    def _predicate_value(self) -> str:
        tok = self._peek()
        if tok.kind == "string":
            return self._next().value
        if tok.kind == "name" and tok.value in ("true", "false"):
            return self._next().value
        if tok.kind == "number":
            return self._next().value
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
        if self._at_word("across"):
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
                calculate = self._calculate_block()
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
        expr = self._calc_body()
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

    def _calculate_block(self) -> CalcExpr:
        self._keyword("calculate")
        self._punct(":")
        self._end_of_line()
        self._expect("indent", "an indented block after calculate")
        expr = self._calc_body()
        self._expect("dedent", "the end of the calculate block")
        return expr

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
        name = self._name("an index name or a set defined above")
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

    def _calc_body(self) -> CalcExpr:
        """A ladder, or a single expression.

        A ladder is recognised by its first word rather than by lookahead,
        because `when` cannot begin any other expression.
        """
        if self._at_word("when"):
            return self._ladder()
        expr = self._calc_expr()
        self._end_of_line()
        self._skip_newlines()
        return expr

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

        if name in ("count", "list", "sum", "max", "min", "latest", "earliest") and self._at_op("("):
            return self._call(name, line)

        if name == "days" and self._at_word("from"):
            self._next()
            frm = self._name("the earlier moment")
            self._keyword("to")
            to = self._name("the later moment")
            return DaysBetween(frm=frm, to=to, line=line)

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
        band: Band | None = None
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

    def _band(self) -> Band:
        line = self._peek().line
        self._keyword("band")
        word = self._name('"low" or "high"')
        if word not in ("low", "high"):
            raise self._error(
                f'"{word}" is not a direction. "low" means lower is better -- a wait, a '
                'latency. "high" is for a share.'
            )

        on: StatisticFn | None = None
        if self._at_word("on"):
            self._next()
            stat = self._name("the statistic to colour")
            if stat not in _STATISTICS:
                raise self._error(f'"{stat}" is not a statistic.')
            on = stat  # type: ignore[assignment]

        self._keyword("against")
        setting = self._name("a settings path")

        unit: ThresholdUnit | None = None
        if self._at_word("in"):
            self._next()
            u = self._name("the unit the threshold is written in")
            if u not in _THRESHOLD_UNITS:
                raise self._error(
                    f'"{u}" is not a threshold unit. Those are: '
                    f'{", ".join(sorted(_THRESHOLD_UNITS))}.'
                )
            unit = u  # type: ignore[assignment]

        self._end_of_line()
        return Band(
            direction="low" if word == "low" else "high",
            setting=setting,
            on=on,
            unit=unit,
            line=line,
        )

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
            by: GroupGrain | None = None
            if self._at_word("by"):
                if fn != "series":
                    raise self._error(
                        f"only a series takes a grain. {fn}(...) runs over the window's raw "
                        "values whatever the series grain is, so a grain written on it "
                        "would be a declaration that does nothing."
                    )
                self._next()
                by = self._group_grain()
            out.append(Statistic(fn=fn, set=target, by=by, line=line))  # type: ignore[arg-type]
            self._end_of_line()
            self._skip_newlines()
        self._expect("dedent", "the end of the calculate block")
        return out

    def _group_grain(self) -> GroupGrain:
        if self._is("number"):
            count = self._next().value
            self._keyword("minutes")
            if count != "15":
                raise self._error(
                    f'"{count} minutes" is not a series grain. Those are "15 minutes", '
                    '"hour" and "day".'
                )
            return "15 minutes"
        word = self._name('a series grain: "15 minutes", "hour" or "day"')
        if word in ("hour", "day"):
            return word  # type: ignore[return-value]
        if word == "minute":
            raise self._error(
                "a series may not answer at minute resolution, though the minute is a "
                "storable grain. Over a sparse figure a minute group holds one record, so "
                "the point is the record -- the raw collection the payload exists to "
                'withhold. The finest series grain is "15 minutes".'
            )
        raise self._error(
            f'"{word}" is not a series grain. Those are "15 minutes", "hour" and "day".'
        )

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
                    'expected "from", "field", "read", "value", "flag", "sort" or "limit", '
                    f"got {self._describe()}"
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
            expr = self._calc_body()
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
