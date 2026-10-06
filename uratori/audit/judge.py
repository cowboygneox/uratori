"""`judge`: a verdict, computed from a cached reading and the current rows.

The split that lets a model run once per page while the answer is never
stale (`documents-plan-v3`, D6.3). The **reading** -- what the model said,
once, blind -- is an input, held by the caller exactly like a page's word
layer; this module never calls a provider and never sees one. The
**verdict** is this module's whole job: pure, synchronous, re-run every
pass a page's derived rows move, and every time a reading lands.

Parsing a value out of the words the model cited reuses
`uratori.documents.extract`'s own matcher primitives (`_read_number`,
`_parse_date_tokens`, the unit table, the control-character check) --
deliberately the *same* code an extract uses to turn words into a value, so
"the reader cited these words" and "the extract read that text" are judged
by one arithmetic, never two slightly different ones that could disagree
about a number neither definition is wrong about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..documents.extract import (
    _CONTROL,
    _parse_date_tokens,
    _read_number,
)
from ..documents.units import UNIT_TABLE
from ..lang.plan import Value
from ..server.provenance import StoredBox
from ..server.words import Word

ReadingStatus = Literal["seen", "not_on_page", "cannot_read"]
"""What the model answered for one verified field, per D6: the word ids it
read a value from, "not on this page", or "cannot read"."""

Verdict = Literal["agrees", "disagrees", "missed", "absent", "unreadable"]
"""The worst-wins ranking, highest first -- `_RANK` below is the ranking
itself; `agrees` is the bottom, and the page's own word is the worst of its
fields'. `unaudited` (a page with no reading at all) is not a member: it is
never produced by `judge`, which is only ever called once a reading exists
-- the caller (the pass, or the worker) stores `unaudited` directly for a
page it has not read yet."""

_RANK: dict[Verdict, int] = {
    "agrees": 0,
    "absent": 1,
    "unreadable": 2,
    "missed": 3,
    "disagrees": 4,
}


@dataclass(frozen=True)
class FieldReading:
    """What the model answered for one verified field, for one row.

    `row` is `0` for a non-`many` extract's single row, and the model's own
    0-based row index for a `many by row` extract -- the model is shown the
    page's rows in reading order and told to answer per row, the same order
    `many by row`'s own anchor scan produces, but this is **not** trusted as
    a pairing: `judge` pairs a reading row to an extract row by value, never
    by position (see `_pair_rows`), because the model may see a row the
    extract missed entirely or vice versa.

    `anchored` is false for the one case D6 carries from D2/D3: a scan where
    OCR missed the value and the model can only point at a region, with its
    own transcription in `seen_text`. An unanchored "seen" is a presence
    signal only -- `judge` never compares its `seen_text` as a value, so no
    number enters a verdict from model prose.
    """

    extract: str
    field: str
    row: int
    status: ReadingStatus
    words: tuple[int, ...] = ()
    box: tuple[float, float, float, float] | None = None
    """A free, unanchored box (x0, y0, x1, y1) -- the D2/D3 fallback, used
    only when `status == "seen"` and `words` is empty."""

    seen_text: str | None = None
    """The model's own transcription, present whenever `status == "seen"`;
    load-bearing when `words` is empty (there is nothing else to show), kept
    always so a reviewer can see what the model actually said even when it
    was also word-anchored."""

    anchored: bool = True


@dataclass(frozen=True)
class AuditReading:
    """One stored reading: the whole of what the model was asked and said,
    for one page at one auditor version. `tenant`/`audit`/`version`/`at` are
    the storage key and timestamp -- kept out of this dataclass because
    `judge` never needs them; they travel on the caller's own row shape
    (`uratori.server.db`'s `audit_reading` table)."""

    page_key: str
    words_sha: str
    prompt: str
    model: str
    response: str
    fields: tuple[FieldReading, ...]


@dataclass(frozen=True)
class AuditFinding:
    """One (extract, record, field) comparison -- the server-facing detail
    behind a page's single verdict word. `record` is `None` exactly when
    there is no extract row to point at (a `missed`/`absent` finding with
    nothing on the extract's side); every other verdict names one.
    `boxes` is computed here, directly from the reading's own cited words
    via the page's word layer this call was given -- the same "write-time,
    not serve-time" choice `uratori.documents.extract` makes for provenance,
    so one function answers "what box" for both."""

    extract: str
    field: str
    record: str | None
    row: int
    verdict: Verdict
    seen: Value
    extracted: Value
    words: tuple[int, ...]
    boxes: tuple[StoredBox, ...]
    anchored: bool
    seen_text: str | None
    note: str | None = None


@dataclass(frozen=True)
class VerifiedField:
    """What `judge` needs to know about one verified field to parse a
    reading's cited words into a comparable value -- the same three things
    the model itself is told (D6: "name, type, units")."""

    type: str | None
    """`text | number | flag | moment`, or `None` for a nested block (never
    legal on a verified field, but the fact schema's own type is a plain
    `str | None` -- `CompiledFactField.type` -- so this matches it rather
    than asserting a narrower literal the compiler already guarantees."""

    units: tuple[str, ...] = ()


def judge(
    reading: AuditReading,
    current_rows: Mapping[str, Sequence[tuple[str, Mapping[str, object]]]],
    verified_fields: Mapping[tuple[str, str], VerifiedField],
    words_by_id: Mapping[int, Word],
) -> tuple[Verdict, tuple[str, ...], tuple[AuditFinding, ...]]:
    """The verdict, the members (evidence), and the per-field findings.

    `current_rows`: extract name -> every one of that extract's records
    currently produced on this page, `(record key, body)`. `verified_fields`:
    `(extract, field) -> VerifiedField`, the declared shape `judge` parses a
    reading against. `words_by_id`: the page's own word layer, for turning
    cited word ids into a value and a box.

    One finding per (extract, field, row) the reading answered. A finding
    whose extract produced no row at all for that row index still gets one
    (`record=None`), because an unmatched reading row is exactly the
    "reader found something no record carries" case a verdict must not
    lose.
    """
    findings: list[AuditFinding] = []
    members: set[str] = set()
    worst: Verdict = "agrees"

    by_extract: dict[str, list[FieldReading]] = {}
    for fr in reading.fields:
        by_extract.setdefault(fr.extract, []).append(fr)

    # Every verified extract that has current rows must be visited even
    # when the reading never mentions it at all (not even `by_extract`
    # has an entry), not only extracts the reading happened to answer --
    # see the unpaired-row handling below.
    all_extract_names = sorted(set(by_extract) | set(current_rows))

    for extract_name in all_extract_names:
        field_readings = by_extract.get(extract_name, ())
        rows = current_rows.get(extract_name, ())
        by_row = _group_by_row(field_readings)
        pairing = _pair_rows(extract_name, by_row, rows, verified_fields, words_by_id)
        for row_index, field_rows in by_row.items():
            record_key, record_body = pairing.get(row_index, (None, None))
            for fr in field_rows:
                vf = verified_fields.get((extract_name, fr.field))
                extracted = (
                    record_body.get(fr.field) if record_body is not None else None
                )
                finding = _judge_field(fr, extracted, vf, words_by_id, record_key)
                findings.append(finding)
                if _RANK[finding.verdict] > _RANK[worst]:
                    worst = finding.verdict
                if finding.record is not None:
                    members.add(finding.record)
                elif finding.verdict in ("missed", "absent"):
                    members.add(reading.page_key)

        # Finding B (review F2): an extract row no reading row claimed at
        # all -- the model reported fewer rows than the extractor
        # produced, i.e. it skipped the row outright rather than saying
        # "not on this page" about it. `pairing` only ever holds rows a
        # reading row was matched to, so this is invisible to the loop
        # above unless handled separately; left alone, the row (and every
        # field on it) never gets a finding and the page can verdict
        # `agrees` while a whole extracted row sits unreviewed -- the
        # opposite of what an auditor is for. D6's vocabulary has no word
        # for "the reader skipped this row"; conceptually it is exactly
        # the "not_on_page" case for every verified field of the row (the
        # reader said nothing, which is no different from saying nothing
        # was there), so it is judged by the identical rule `_judge_field`
        # already uses for `not_on_page`: `disagrees` when the extract has
        # a value here ("the extract has a value the reader found no
        # trace of"), `absent` when it does not.
        claimed = {key for key, _ in pairing.values()}
        extract_fields = sorted(
            {field for (name, field) in verified_fields if name == extract_name}
        )
        for record_key, record_body in rows:
            if record_key in claimed:
                continue
            for field in extract_fields:
                extracted = record_body.get(field)
                verdict: Verdict = "absent" if extracted is None else "disagrees"
                finding = AuditFinding(
                    extract=extract_name,
                    field=field,
                    record=record_key,
                    row=-1,
                    verdict=verdict,
                    seen=None,
                    extracted=_coerce_value(extracted),
                    words=(),
                    boxes=(),
                    anchored=True,
                    seen_text=None,
                    note=None
                    if extracted is None
                    else "the reader reported no row at all for this record",
                )
                findings.append(finding)
                if _RANK[finding.verdict] > _RANK[worst]:
                    worst = finding.verdict
                members.add(record_key)

    if not findings:
        # A reading that answered no field at all (every verified extract's
        # fields were absent from the prompt, or the model answered
        # nothing parseable) is `absent`, the honest word for "nothing
        # found, nothing to disagree with" -- never `agrees`, which would
        # claim a comparison that never happened.
        worst = "absent"
        members.add(reading.page_key)

    return worst, tuple(sorted(members)), tuple(findings)


def _group_by_row(field_readings: Sequence[FieldReading]) -> dict[int, list[FieldReading]]:
    out: dict[int, list[FieldReading]] = {}
    for fr in field_readings:
        out.setdefault(fr.row, []).append(fr)
    return out


def _pair_rows(
    extract_name: str,
    by_row: Mapping[int, Sequence[FieldReading]],
    rows: Sequence[tuple[str, Mapping[str, object]]],
    verified_fields: Mapping[tuple[str, str], VerifiedField],
    words_by_id: Mapping[int, Word],
) -> dict[int, tuple[str, Mapping[str, object]]]:
    """Which extract row (if any) a reading row pairs with.

    **Simplified from D6's own rule.** The spec pairs a reading row to an
    extract row by anchor-field *word overlap* first, falling back to
    anchor-value equality only when no overlap is found. `judge` takes the
    value-equality rule as the only tier: it needs no provenance lookup (the
    word-overlap tier would need every extract row's own cited word ids,
    which would have made this function depend on the provenance store
    rather than on the word layer alone), and in the deterministic fixtures
    this ships with, two rows share an anchor value only when they are in
    fact the same row. Recorded here, not silently: a flowsheet whose rows
    repeat an identical value for the row-anchor field on the same page
    cannot be disambiguated by this rule, exactly as it could not by the
    extract's own tie-break (`documents-plan-v3` D1's padded keys note the
    same limit for same-instant ties).

    A reading row with no value for any field pairs with nothing (`None`);
    an extract row no reading row claims is simply absent from `pairing`
    and is picked up as a bare extracted value with no counterpart -- which
    `judge` has no separate case for, because every verified field of every
    extract row the model was shown should have produced a reading row
    (even if every one of its fields answered "not on this page"). A
    genuinely unread row -- the model skipped it outright -- renders every
    one of its fields `missed`/`disagrees` through the ordinary comparison,
    because `_judge_field` is told `extracted` for a row that has no
    reading at all only when some *other* row claims it; an extract row
    nothing claims never surfaces as its own finding. This is the one gap
    `judge`'s value-only pairing leaves open, and it is the same gap a
    model that undercounts rows always leaves: nothing here invents a
    finding for a row the model never mentioned.
    """
    pairing: dict[int, tuple[str, Mapping[str, object]]] = {}
    claimed: set[str] = set()
    for row_index, field_rows in by_row.items():
        best: tuple[str, Mapping[str, object]] | None = None
        for key, body in rows:
            if key in claimed:
                continue
            if _rows_share_a_value(extract_name, field_rows, body, verified_fields, words_by_id):
                best = (key, body)
                break
        if best is not None:
            pairing[row_index] = best
            claimed.add(best[0])

    # Leftover rows, positionally: a row pairs by value only when there is
    # more than one candidate to disambiguate between. A non-`many` extract
    # has exactly one row and one reading row, which never needs value
    # agreement to know they are about each other -- and a disagreeing
    # value is exactly the case this verdict exists to catch, so pairing
    # must not require the two to already agree. Leftover `many` rows fall
    # back the same way, in declaration order, which is the best a reading
    # that found a different number than the extract did can do.
    leftover_rows = [idx for idx in by_row if idx not in pairing]
    leftover_extract = [(key, body) for key, body in rows if key not in claimed]
    for row_index, (key, body) in zip(sorted(leftover_rows), leftover_extract, strict=False):
        pairing[row_index] = (key, body)
    return pairing


def _rows_share_a_value(
    extract_name: str,
    field_rows: Sequence[FieldReading],
    body: Mapping[str, object],
    verified_fields: Mapping[tuple[str, str], VerifiedField],
    words_by_id: Mapping[int, Word],
) -> bool:
    """Whether any field of this reading row parses to the same value this
    extract row holds -- the real, unit-aware parse (`_parse_seen_value`),
    not a raw-text comparison, so "180" and "180.0" pair exactly as they
    would be judged to agree."""
    for fr in field_rows:
        if fr.status != "seen" or not fr.anchored:
            continue
        held = body.get(fr.field)
        if held is None:
            continue
        vf = verified_fields.get((extract_name, fr.field))
        cited = [w for wid in fr.words if (w := words_by_id.get(wid)) is not None]
        seen, _note = _parse_seen_value(vf, cited, fr.seen_text)
        if seen is not None and _same(seen, held):
            return True
    return False


def _judge_field(
    fr: FieldReading,
    extracted: object,
    vf: VerifiedField | None,
    words_by_id: Mapping[int, Word],
    record_key: str | None,
) -> AuditFinding:
    """`record_key` is the extract row `_pair_rows` matched this reading row
    to, or `None` when nothing paired -- carried straight onto the finding's
    own `record`, regardless of verdict: a finding about a record names it,
    one about nothing on the extract's side does not."""
    words = tuple(fr.words)
    boxes = tuple(
        StoredBox(x0=w.x0, y0=w.y0, x1=w.x1, y1=w.y1)
        for wid in words
        if (w := words_by_id.get(wid)) is not None
    )
    if fr.box is not None and not words:
        boxes = (StoredBox(*fr.box),)

    if fr.status == "cannot_read":
        return AuditFinding(
            extract=fr.extract,
            field=fr.field,
            record=record_key,
            row=fr.row,
            verdict="unreadable",
            seen=None,
            extracted=_coerce_value(extracted),
            words=words,
            boxes=boxes,
            anchored=fr.anchored,
            seen_text=fr.seen_text,
            note="the reader said it could not read this field",
        )

    if fr.status == "not_on_page":
        verdict: Verdict = "absent" if extracted is None else "disagrees"
        return AuditFinding(
            extract=fr.extract,
            field=fr.field,
            record=record_key,
            row=fr.row,
            verdict=verdict,
            seen=None,
            extracted=_coerce_value(extracted),
            words=words,
            boxes=boxes,
            anchored=fr.anchored,
            seen_text=fr.seen_text,
            note=None if extracted is None else "the extract has a value the reader found no trace of",
        )

    # status == "seen"
    if not fr.anchored:
        # D6: an unanchored "seen" never compares a value -- only presence.
        verdict = "missed" if extracted is None else "disagrees"
        return AuditFinding(
            extract=fr.extract,
            field=fr.field,
            record=record_key,
            row=fr.row,
            verdict=verdict,
            seen=None,
            extracted=_coerce_value(extracted),
            words=words,
            boxes=boxes,
            anchored=False,
            seen_text=fr.seen_text,
            note="unanchored: the reader transcribed text it could not match to page words",
        )

    cited = [w for wid in fr.words if (w := words_by_id.get(wid)) is not None]
    seen, note = _parse_seen_value(vf, cited, fr.seen_text)
    if seen is None:
        verdict = "unreadable"
    elif extracted is None:
        verdict = "missed"
    elif _same(seen, extracted):
        verdict = "agrees"
    else:
        verdict = "disagrees"
    return AuditFinding(
        extract=fr.extract,
        field=fr.field,
        record=record_key,
        row=fr.row,
        verdict=verdict,
        seen=seen,
        extracted=_coerce_value(extracted),
        words=words,
        boxes=boxes,
        anchored=True,
        seen_text=fr.seen_text,
        note=note,
    )


def _parse_seen_value(
    vf: VerifiedField | None, words: Sequence[Word], seen_text: str | None
) -> tuple[Value, str | None]:
    if vf is None:
        return None, "no verified field declaration to parse this reading against"
    if not words:
        return None, "cited no words"
    if vf.type == "number":
        found = _read_number(list(words), 0, vf.units)
        if found is None:
            return None, "no number found in the cited words"
        if isinstance(found, str):
            return None, found
        value, printed_unit, _cited = found
        if len(vf.units) > 1 and printed_unit is None:
            return None, "a number with no printed unit, and more than one unit is declared"
        if vf.units and printed_unit is None:
            _dim, factor = UNIT_TABLE[vf.units[0]]
            value = value * factor
        return value, None
    if vf.type == "moment":
        parsed = _parse_date_tokens([w.text for w in words])
        if parsed is None:
            return None, "no date found in the cited words"
        if parsed == "ambiguous":
            return None, "date ambiguous"
        iso, _consumed = parsed
        return iso, None
    if vf.type == "text" or vf.type is None:
        raw = (seen_text or " ".join(w.text for w in words)).strip()
        if not raw:
            return None, "nothing in the cited words"
        if "@" in raw:
            return None, 'identifier contains "@"'
        if any(ch in _CONTROL for ch in raw):
            return None, "identifier contains a control character"
        return raw, None
    return None, f"unsupported field type {vf.type!r}"


def _same(seen: Value, extracted: object) -> bool:
    if isinstance(seen, float) and isinstance(extracted, (int, float)):
        return abs(seen - float(extracted)) < 1e-6
    return seen == extracted


def _coerce_value(extracted: object) -> Value:
    if extracted is None or isinstance(extracted, (int, float, str)):
        return extracted
    return str(extracted)
