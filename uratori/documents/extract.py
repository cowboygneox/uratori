"""`extract`: deterministic patterns over one page's word layer.

A pure function, by design (documents-plan-v3, D4): one page's own record,
its word layer, the records already produced for it this pass, and the
`over` filters it is gated by in -> the records it produces, their
provenance, and the rows it could not read. No I/O, no clock beyond the one
the caller already resolved to a number, and no model -- a model's job is at
*authoring* time (`examples/records/`, package 4), never in here.

**Determinism is the whole point.** The same version over the same word
layer must produce the same records, byte for byte -- which is why every
matcher below is plain, versioned code rather than anything that guesses:
`MATCHER_VERSION` is hashed into every extract's version precisely so a
change to tokenisation, line grouping or reading order forks a version
instead of silently changing what an unmoved definition produces.

**Failures are first-class, not exceptions.** A page the patterns cannot
read (no alternative matched, a number with no printed unit the field
leaves ambiguous, a date the grammar cannot resolve, an identifier carrying
`@`) answers an `ExtractFailure`, which the server stores so the authoring
loop can read it back (`GET /tenants/{t}/extracts/{name}/failures`) -- never
a guess, and never an exception that would take the whole pass down for one
bad page.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, assert_never

from ..engine.buckets import buckets_of
from ..lang.ast import (
    DateAfter,
    ExtractField,
    FieldCopy,
    NumberAfter,
    SetExpr,
    SetIndex,
    SetOp,
    SetRef,
    TextAfter,
    WordLadder,
)
from ..lang.plan import CompiledIndex, ExtractPlan, Value
from ..server.provenance import ProvenanceRow, StoredBox
from ..server.words import Word
from .units import (
    CM_PER_FOOT as _CM_PER_FOOT,
)
from .units import (
    CM_PER_INCH as _CM_PER_INCH,
)
from .units import (
    FOOT_MARKERS as _FOOT_MARKERS,
)
from .units import (
    INCH_MARKERS as _INCH_MARKERS,
)
from .units import (
    MATCHER_VERSION,
    UNIT_TABLE,
)
from .units import (
    MONTHS as _MONTHS,
)

_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


@dataclass(frozen=True)
class ExtractFailure:
    """One subject the patterns could not read: a page (whole-record and
    anchor failures) or a `many by row` row key. `field` is `None` only for
    a whole-page anchor failure that never found a row to fail on."""

    subject: str
    field: str | None
    reason: str


@dataclass(frozen=True)
class ExtractedRecord:
    key: str
    body: dict[str, Value]
    provenance: tuple[ProvenanceRow, ...]


@dataclass(frozen=True)
class PageExtractResult:
    records: tuple[ExtractedRecord, ...]
    failures: tuple[ExtractFailure, ...]


ThroughResolver = Callable[[str, str, str], list[str]]


def in_scope(
    expr: SetExpr | None,
    *,
    source_kind: str,
    page_record: Mapping[str, Any],
    derived_on_page: Mapping[str, Mapping[str, Any]],
    indexes: Mapping[str, CompiledIndex],
    now_ms: float,
) -> bool:
    """Whether a page is in an extract's `over` set -- evaluated per page
    with the engine's own `buckets_of`, on the page record and on the
    derived records already produced for the *same* page, never by reading
    stored buckets (D4.2): a pre-pass runs before anything lands, so there
    is no bucket diff to read yet.
    """
    if expr is None:
        return True
    return _in_scope(
        expr,
        source_kind=source_kind,
        page_record=page_record,
        derived_on_page=derived_on_page,
        indexes=indexes,
        now_ms=now_ms,
    )


def _in_scope(
    expr: SetExpr,
    *,
    source_kind: str,
    page_record: Mapping[str, Any],
    derived_on_page: Mapping[str, Mapping[str, Any]],
    indexes: Mapping[str, CompiledIndex],
    now_ms: float,
) -> bool:
    if isinstance(expr, SetOp):
        left = _in_scope(
            expr.left,
            source_kind=source_kind,
            page_record=page_record,
            derived_on_page=derived_on_page,
            indexes=indexes,
            now_ms=now_ms,
        )
        right = _in_scope(
            expr.right,
            source_kind=source_kind,
            page_record=page_record,
            derived_on_page=derived_on_page,
            indexes=indexes,
            now_ms=now_ms,
        )
        if expr.op == "intersect":
            return left and right
        if expr.op == "union":
            return left or right
        return left and not right
    if isinstance(expr, SetIndex):
        index = indexes[expr.index]
        record = page_record if index.kind == source_kind else derived_on_page.get(index.kind)
        if record is None:
            return False
        buckets = buckets_of(index, record, resolve=_no_through, now_ms=now_ms)
        return bool(buckets)
    if isinstance(expr, SetRef):  # pragma: no cover - refused at check time
        raise AssertionError(f"a checked extract's `over` never holds a bare set name ({expr.name})")
    assert_never(expr)


def _no_through(kind: str, field: str, value: str) -> list[str]:  # pragma: no cover
    # `over` is checker-restricted to `ByPredicate`/`ByPresence` (D4.2), and
    # neither ever calls its resolver -- this exists only to satisfy
    # `buckets_of`'s signature.
    return []


ROW_SEPARATOR = "#"
"""Never the engine's own `@` composite-subject separator (`part_of`
refuses that one in a key part) -- `#` is unrestricted and visually distinct
from it, so a row key and a bucketed subject key are never confused."""


def row_key(page_key: str, row: int, up_to: int) -> str:
    """`<source key>#r001`, zero-padded to `up_to`'s own width -- so `latest`
    (which breaks same-instant ties on the key as a string) sees row 2 sort
    after row 1 and before row 10, same-day flowsheet rows included."""
    width = len(str(up_to))
    return f"{page_key}{ROW_SEPARATOR}r{row:0{width}d}"


def run_extract(
    plan: ExtractPlan,
    *,
    page_key: str,
    page_record: Mapping[str, Any],
    words: Sequence[Word],
    derived_on_page: Mapping[str, Mapping[str, Any]],
    derived_provenance_on_page: Mapping[str, Sequence[ProvenanceRow]],
    indexes: Mapping[str, CompiledIndex],
    now_ms: float,
) -> PageExtractResult:
    """One extract, over one page. Pure: every input is a value the caller
    already holds, and the only output is what to write and what failed.
    """
    if not in_scope(
        plan.over,
        source_kind=plan.source,
        page_record=page_record,
        derived_on_page=derived_on_page,
        indexes=indexes,
        now_ms=now_ms,
    ):
        return PageExtractResult(records=(), failures=())

    lines = _lines_of(words)
    all_words = sorted(words, key=lambda w: w.id)

    if not plan.many:
        body, provenance, failure = _match_record(
            plan.fields,
            lines=lines,
            all_words=all_words,
            derived_on_page=derived_on_page,
            derived_provenance_on_page=derived_provenance_on_page,
            page_key=page_key,
        )
        if failure is not None:
            field_name, reason = failure
            return PageExtractResult(
                records=(),
                failures=(ExtractFailure(subject=page_key, field=field_name, reason=reason),),
            )
        if not body:
            # Every field was absent -- nothing on this page matched any of
            # them, which is "no record" (D4: a page with no identity match
            # produces no `page_identity` record), never a record with
            # nothing in it.
            return PageExtractResult(
                records=(),
                failures=(
                    ExtractFailure(
                        subject=page_key,
                        field=None,
                        reason="no alternative matched for any field",
                    ),
                ),
            )
        return PageExtractResult(
            records=(ExtractedRecord(key=page_key, body=body, provenance=tuple(provenance)),),
            failures=(),
        )

    # `many by row`: the anchor field is the first declared field whose
    # matcher reads the page's own words (never a copy or a ladder) -- one
    # candidate row per line it matches, in reading order, up to the
    # declared ceiling.
    anchor = next(
        (f for f in plan.fields if isinstance(f.matcher, (NumberAfter, DateAfter, TextAfter))),
        None,
    )
    assert anchor is not None  # the checker refuses a `many` extract with no such field
    assert plan.many_up_to is not None
    anchor_lines = _rows_for(anchor, lines)
    if not anchor_lines:
        return PageExtractResult(
            records=(),
            failures=(
                ExtractFailure(
                    subject=page_key,
                    field=anchor.name,
                    reason="no alternative matched anywhere on the page",
                ),
            ),
        )

    records: list[ExtractedRecord] = []
    failures: list[ExtractFailure] = []
    for row, line_idx in enumerate(anchor_lines[: plan.many_up_to], start=1):
        key = row_key(page_key, row, plan.many_up_to)
        body, provenance, failure = _match_record(
            plan.fields,
            lines={line_idx: lines[line_idx]},
            all_words=all_words,
            derived_on_page=derived_on_page,
            derived_provenance_on_page=derived_provenance_on_page,
            page_key=page_key,
        )
        if failure is not None:
            field_name, reason = failure
            failures.append(ExtractFailure(subject=key, field=field_name, reason=reason))
            continue
        records.append(ExtractedRecord(key=key, body=body, provenance=tuple(provenance)))
    return PageExtractResult(records=tuple(records), failures=tuple(failures))


class _Absent:
    """This field's alternative was never found anywhere on the page --
    "no visit note mentioned it", not "something is wrong with what is
    here". An absence, like any fact field's: the record is still written,
    just without this one, the same as a host write that said nothing
    about a field it does not know. A weight-only visit producing a
    `measurement` row with no `height_cm` is this, and it is exactly what
    lets D5's `carried forward` height do its job.

    Distinct from a hard failure (the alternative *was* found, but what
    followed could not be read: no number, no printed unit when more than
    one is declared, an unresolvable date, an identifier carrying `@`) --
    those abort the whole record, because a half-read record is a guess
    about which half mattered. A copy with no upstream record is a hard
    failure too (D4: "filing a measurement under nobody is worse than
    filing nothing"), never an absence.
    """


_ABSENT = _Absent()


def _match_record(
    fields: Sequence[ExtractField],
    *,
    lines: Mapping[int, list[Word]],
    all_words: list[Word],
    derived_on_page: Mapping[str, Mapping[str, Any]],
    derived_provenance_on_page: Mapping[str, Sequence[ProvenanceRow]],
    page_key: str,
) -> tuple[dict[str, Value], list[ProvenanceRow], tuple[str, str] | None]:
    """Every field of one record. A field whose alternative was never
    found is simply left out (`_Absent`); any other failure -- found but
    unreadable, or a copy with nothing to copy -- aborts the whole record,
    because that is a guess about which half of it mattered, not a claim
    the page never made."""
    body: dict[str, Value] = {}
    provenance: list[ProvenanceRow] = []
    for field in fields:
        outcome = _match_field(
            field,
            lines=lines,
            all_words=all_words,
            derived_on_page=derived_on_page,
            derived_provenance_on_page=derived_provenance_on_page,
            page_key=page_key,
        )
        if isinstance(outcome, _Absent):
            continue
        if isinstance(outcome, str):
            return {}, [], (field.name, outcome)
        value, row = outcome
        body[field.name] = value
        if row is not None:
            provenance.append(row)
    return body, provenance, None


def _match_field(
    field: ExtractField,
    *,
    lines: Mapping[int, list[Word]],
    all_words: list[Word],
    derived_on_page: Mapping[str, Mapping[str, Any]],
    derived_provenance_on_page: Mapping[str, Sequence[ProvenanceRow]],
    page_key: str,
) -> tuple[Value, ProvenanceRow | None] | _Absent | str:
    matcher = field.matcher

    if isinstance(matcher, NumberAfter):
        found = _scan_lines(lines, lambda lw: _number_after_on_line(matcher, lw))
        if found == "no alternative matched":
            return _ABSENT
        if isinstance(found, str):
            return found
        value, cited, printed = found
        return value, _provenance(field.name, page_key, cited, printed, value, matcher)

    if isinstance(matcher, DateAfter):
        found = _scan_lines(lines, lambda lw: _date_after_on_line(matcher, lw))
        if found == "no alternative matched":
            return _ABSENT
        if isinstance(found, str):
            return found
        value, cited, printed = found
        return value, _provenance(field.name, page_key, cited, printed, value, matcher)

    if isinstance(matcher, TextAfter):
        found = _scan_lines(lines, lambda lw: _text_after_on_line(matcher, lw))
        if found == "no alternative matched":
            return _ABSENT
        if isinstance(found, str):
            return found
        value, cited, printed = found
        return value, _provenance(field.name, page_key, cited, printed, value, matcher)

    if isinstance(matcher, WordLadder):
        found = _word_ladder(matcher, all_words)
        if isinstance(found, str):
            return found
        value, cited, printed = found
        if not cited:
            return value, None
        return value, _provenance(field.name, page_key, cited, printed, value, matcher)

    if isinstance(matcher, FieldCopy):
        return _copy(matcher, field.name, derived_on_page, derived_provenance_on_page, page_key)

    assert_never(matcher)


def _provenance(
    field_name: str,
    page_key: str,
    cited: Sequence[Word],
    printed: str,
    value: Value,
    matcher: object,
) -> ProvenanceRow:
    return ProvenanceRow(
        field=field_name,
        page_key=page_key,
        word_ids=tuple(w.id for w in cited),
        boxes=tuple(StoredBox(x0=w.x0, y0=w.y0, x1=w.x1, y1=w.y1) for w in cited),
        printed=printed or None,
        value=value,
        anchored=True,
        extractor="extract",
        parser=MATCHER_VERSION,
        matcher=_matcher_json(matcher),
        reproducible=True,
    )


def _matcher_json(matcher: object) -> dict[str, Any]:
    if isinstance(matcher, NumberAfter):
        return {"kind": "number_after", "alternatives": list(matcher.alternatives), "units": list(matcher.units)}
    if isinstance(matcher, DateAfter):
        return {"kind": "date_after", "alternatives": list(matcher.alternatives)}
    if isinstance(matcher, TextAfter):
        return {"kind": "text_after", "alternatives": list(matcher.alternatives)}
    if isinstance(matcher, WordLadder):
        return {"kind": "word_ladder"}
    return {"kind": "unknown"}


def _copy(
    matcher: FieldCopy,
    field_name: str,
    derived_on_page: Mapping[str, Mapping[str, Any]],
    derived_provenance_on_page: Mapping[str, Sequence[ProvenanceRow]],
    page_key: str,
) -> tuple[Value, ProvenanceRow | None] | str:
    source_body = derived_on_page.get(matcher.extract)
    if source_body is None:
        return f'no {matcher.extract} record on this page'
    if matcher.field == "page":
        value: Value = page_key
    else:
        value = source_body.get(matcher.field)
        if value is None:
            return f'{matcher.extract} did not write "{matcher.field}" on this page'
    upstream_rows = derived_provenance_on_page.get(matcher.extract, ())
    cited_row = next((r for r in upstream_rows if r.field == matcher.field), None)
    row = ProvenanceRow(
        field=field_name,
        page_key=cited_row.page_key if cited_row is not None else page_key,
        word_ids=cited_row.word_ids if cited_row is not None else (),
        boxes=cited_row.boxes if cited_row is not None else (),
        printed=cited_row.printed if cited_row is not None else None,
        value=value,
        anchored=cited_row.anchored if cited_row is not None else False,
        extractor=f"copy:{matcher.extract}",
        parser=MATCHER_VERSION,
        matcher={"kind": "copy", "extract": matcher.extract, "field": matcher.field},
        reproducible=True,
    )
    return value, row


# ------------------------------------------------------------- the words --


def _lines_of(words: Sequence[Word]) -> dict[int, list[Word]]:
    lines: dict[int, list[Word]] = {}
    for w in sorted(words, key=lambda w: w.id):
        lines.setdefault(w.line, []).append(w)
    return lines


def _rows_for(field: ExtractField, lines: Mapping[int, list[Word]]) -> list[int]:
    """Every line, in reading order, where this field's own matcher finds
    its alternative -- the candidate rows of a `many by row` extract."""
    matcher = field.matcher
    assert isinstance(matcher, (NumberAfter, DateAfter, TextAfter))
    out: list[int] = []
    for line_idx in sorted(lines):
        if _find_on_line(lines[line_idx], matcher.alternatives) is not None:
            out.append(line_idx)
    return out


def _scan_lines(
    lines: Mapping[int, list[Word]],
    attempt: Callable[[list[Word]], tuple[Value, list[Word], str] | str],
) -> tuple[Value, list[Word], str] | str:
    """Try every line in reading order; the first line whose attempt
    succeeds wins. More forgiving than refusing at the first line an
    alternative merely appears on, which matters on a page where a label
    repeats (a header and a flowsheet row) and only one occurrence is
    well-formed.

    A line where the alternative was found but what followed could not be
    read outranks a later line where it was not found at all: "no
    alternative matched" must mean *never found anywhere*, because the
    caller reads exactly that string to tell an absent field (fine; see
    `_Absent`) apart from a broken one (a hard failure) -- and a page
    where the label happens to repeat, broken once and absent elsewhere,
    must report the break.
    """
    broken: str | None = None
    for line_idx in sorted(lines):
        result = attempt(lines[line_idx])
        if not isinstance(result, str):
            return result
        if result != "no alternative matched" and broken is None:
            broken = result
    return broken if broken is not None else "no alternative matched"


def _normalize(token: str) -> str:
    return unicodedata.normalize("NFKC", token).strip().rstrip(":").strip().lower()


def _find_alternative(line_words: list[Word], alternative: str) -> int | None:
    """The index just past `alternative`'s last word on this line, or None."""
    needle = [_normalize(t) for t in alternative.split()]
    if not needle:
        return None
    n = len(needle)
    for start in range(len(line_words) - n + 1):
        window = [_normalize(w.text) for w in line_words[start : start + n]]
        if window == needle:
            return start + n
    return None


def _find_on_line(line_words: list[Word], alternatives: Sequence[str]) -> int | None:
    for alt in alternatives:
        pos = _find_alternative(line_words, alt)
        if pos is not None:
            return pos
    return None


def _read_number(
    line_words: list[Word], start: int, units: Sequence[str]
) -> tuple[float, str | None, list[Word]] | None:
    for i in range(start, len(line_words)):
        text = line_words[i].text.rstrip(",;")
        if not _NUMBER_RE.match(text):
            continue
        number = float(text)
        # `ft_in`: "<n> ft <n> in" (or `'`/`"`), two numbers assembled into
        # one centimetre value -- the one matcher whose box list has to
        # carry more than one word pair.
        if "ft_in" in units and i + 3 < len(line_words):
            foot_marker = line_words[i + 1].text.rstrip(".").lower()
            inch_text = line_words[i + 2].text.rstrip(",;")
            inch_marker = line_words[i + 3].text.rstrip(".").lower()
            if (
                foot_marker in _FOOT_MARKERS
                and _NUMBER_RE.match(inch_text)
                and inch_marker in _INCH_MARKERS
            ):
                total_cm = number * _CM_PER_FOOT + float(inch_text) * _CM_PER_INCH
                return total_cm, "ft_in", line_words[i : i + 4]
        printed_unit: str | None = None
        cited = [line_words[i]]
        if i + 1 < len(line_words):
            maybe_unit = line_words[i + 1].text.rstrip(".,").lower()
            if maybe_unit in units and maybe_unit in UNIT_TABLE:
                printed_unit = maybe_unit
                cited = [line_words[i], line_words[i + 1]]
        if printed_unit is not None:
            _dim, factor = UNIT_TABLE[printed_unit]
            return number * factor, printed_unit, cited
        return number, None, cited
    return None


def _number_after_on_line(matcher: NumberAfter, line_words: list[Word]) -> tuple[Value, list[Word], str] | str:
    pos = _find_on_line(line_words, matcher.alternatives)
    if pos is None:
        return "no alternative matched"
    found = _read_number(line_words, pos, matcher.units)
    if found is None:
        return "no number followed the matched text"
    value, printed_unit, cited = found
    if len(matcher.units) > 1 and printed_unit is None:
        return "a number with no printed unit, and more than one unit is declared"
    if not matcher.units:
        return value, cited, " ".join(w.text for w in cited)
    if printed_unit is None:
        # A single declared unit needs none printed: the field's own unit is
        # the answer.
        _dim, factor = UNIT_TABLE[matcher.units[0]]
        value = value * factor
    return value, cited, " ".join(w.text for w in cited)


def _parse_date_tokens(tokens: Sequence[str]) -> tuple[str, int] | Literal["ambiguous"] | None:
    if not tokens:
        return None
    first = tokens[0]
    m = _ISO_DATE_RE.match(first)
    if m:
        y, mo, da = int(m[1]), int(m[2]), int(m[3])
        return _instant(y, mo, da), 1
    m = _US_DATE_RE.match(first)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b > 12:
            return None
        if a <= 12 and b <= 12 and a != b and a != 0 and b != 0:
            return "ambiguous"
        month, day = (a, b) if a <= 12 else (b, a)
        if not (1 <= month <= 12) or not (1 <= day <= 31):
            return None
        return _instant(y, month, day), 1
    month_num = _MONTHS.get(first.rstrip(".").lower())
    if month_num is not None and len(tokens) >= 3:
        day_tok = tokens[1].rstrip(",")
        year_tok = tokens[2]
        if day_tok.isdigit() and year_tok.isdigit():
            return _instant(int(year_tok), month_num, int(day_tok)), 3
    return None


def _instant(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def _date_after_on_line(matcher: DateAfter, line_words: list[Word]) -> tuple[Value, list[Word], str] | str:
    pos = _find_on_line(line_words, matcher.alternatives)
    if pos is None:
        return "no alternative matched"
    tokens = [w.text for w in line_words[pos : pos + 3]]
    parsed = _parse_date_tokens(tokens)
    if parsed is None:
        return "no date followed the matched text"
    if parsed == "ambiguous":
        return "date ambiguous"
    iso, consumed = parsed
    cited = line_words[pos : pos + consumed]
    return iso, cited, " ".join(w.text for w in cited)


_CONTROL = {chr(i) for i in range(0, 0x20)} | {chr(0x7F)}


def _text_after_on_line(matcher: TextAfter, line_words: list[Word]) -> tuple[Value, list[Word], str] | str:
    pos = _find_on_line(line_words, matcher.alternatives)
    if pos is None:
        return "no alternative matched"
    if pos >= len(line_words):
        return "nothing followed the matched text"
    raw = line_words[pos].text
    value = raw.strip().rstrip(",;")
    if not value:
        return "nothing followed the matched text"
    if "@" in value:
        return 'identifier contains "@"'
    if any(ch in _CONTROL for ch in value):
        return "identifier contains a control character"
    return value, [line_words[pos]], raw


def _word_ladder(
    matcher: WordLadder, all_words: list[Word]
) -> tuple[Value, list[Word], str] | str:
    tokens = [_normalize(w.text) for w in all_words]
    for rung in matcher.rungs:
        for alt in rung.alternatives:
            needle = [_normalize(t) for t in alt.split()]
            n = len(needle)
            if n == 0:
                continue
            for start in range(len(tokens) - n + 1):
                if tokens[start : start + n] == needle:
                    cited = all_words[start : start + n]
                    return rung.word, cited, " ".join(w.text for w in cited)
    if matcher.otherwise is not None:
        return matcher.otherwise, [], ""
    return "no alternative matched"
