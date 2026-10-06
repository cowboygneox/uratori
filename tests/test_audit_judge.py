"""`judge` -- the verdict, computed from a cached reading and current rows.

Pure, synchronous, no provider: every case here hand-builds a reading and
compares it against hand-built extract rows, pinning the comparison table
(`documents-plan-v3` D6) directly rather than through a worker or a fake
provider (5d's job).
"""

from __future__ import annotations

from uratori.audit.judge import AuditReading, FieldReading, VerifiedField, judge
from uratori.server.words import Word

PAGE = "doc1/p0001"


def word(id: int, text: str, line: int = 0) -> Word:
    return Word(
        id=id,
        text=text,
        x0=0.1 * id,
        y0=0.1,
        x1=0.1 * id + 0.05,
        y1=0.2,
        line=line,
        block=0,
        source="pdf",
        confidence=None,
    )


WORDS = {w.id: w for w in [word(0, "Wt:"), word(1, "82"), word(2, "kg")]}

WEIGHT_FIELD = {("measurement", "weight_kg"): VerifiedField(type="number", units=("kg", "lb"))}


def reading(*fields: FieldReading, model: str = "fake-v1") -> AuditReading:
    return AuditReading(
        page_key=PAGE,
        words_sha="sha1",
        prompt="read the weight",
        model=model,
        response="...",
        fields=fields,
    )


def test_agrees_when_the_reader_finds_the_same_value() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1, 2))
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 82.0})]}
    verdict, members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "agrees"
    assert members == ("doc1/p0001",)
    assert findings[0].verdict == "agrees"
    assert findings[0].seen == 82.0


def test_disagrees_when_the_reader_finds_a_different_value() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1, 2))
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 90.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "disagrees"
    assert findings[0].seen == 82.0
    assert findings[0].extracted == 90.0


def test_disagrees_when_the_extract_has_a_value_the_reader_never_found() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="not_on_page")
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 90.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "disagrees"
    assert findings[0].seen is None
    assert findings[0].extracted == 90.0


def test_missed_when_the_reader_finds_a_value_no_record_carries() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1, 2))
    )
    rows: dict[str, list[tuple[str, dict[str, object]]]] = {"measurement": []}
    verdict, members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "missed"
    assert findings[0].record is None
    assert members == (PAGE,)


def test_absent_when_neither_found_one() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="not_on_page")
    )
    rows: dict[str, list[tuple[str, dict[str, object]]]] = {"measurement": []}
    verdict, members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "absent"
    assert findings[0].verdict == "absent"
    assert members == (PAGE,)


def test_unreadable_when_the_reader_says_it_cannot_read() -> None:
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="cannot_read")
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 90.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, WORDS)
    assert verdict == "unreadable"
    assert findings[0].seen is None


def test_unreadable_when_the_cited_words_do_not_parse_as_the_declared_type() -> None:
    bad_words = {w.id: w for w in [word(0, "Date:"), word(1, "sometime")]}
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(0, 1))
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 90.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, bad_words)
    assert verdict == "unreadable"
    assert findings[0].note is not None


def test_a_single_declared_unit_needs_no_printed_unit() -> None:
    kg_only_words = {w.id: w for w in [word(1, "82")]}
    field = {("measurement", "weight_kg"): VerifiedField(type="number", units=("kg",))}
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1,))
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 82.0})]}
    verdict, _members, _findings = judge(r, rows, field, kg_only_words)
    assert verdict == "agrees"


def test_more_than_one_declared_unit_needs_a_printed_one() -> None:
    bare_number = {w.id: w for w in [word(1, "82")]}
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1,))
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 82.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, bare_number)
    assert verdict == "unreadable"
    assert "printed unit" in (findings[0].note or "")


def test_an_unanchored_reading_never_compares_a_value() -> None:
    """D6: an unanchored `seen` is a presence signal only."""
    r = reading(
        FieldReading(
            extract="measurement",
            field="weight_kg",
            row=0,
            status="seen",
            anchored=False,
            box=(0.1, 0.1, 0.2, 0.2),
            seen_text="82 kg",
        )
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 1234.0})]}
    verdict, _members, findings = judge(r, rows, WEIGHT_FIELD, {})
    # Never "agrees": the model's transcription was never compared as a value.
    assert verdict == "disagrees"
    assert findings[0].seen is None
    assert findings[0].boxes


def test_an_unanchored_reading_with_no_extracted_value_is_missed() -> None:
    r = reading(
        FieldReading(
            extract="measurement",
            field="weight_kg",
            row=0,
            status="seen",
            anchored=False,
            seen_text="82 kg",
        )
    )
    rows: dict[str, list[tuple[str, dict[str, object]]]] = {"measurement": []}
    verdict, _members, _findings = judge(r, rows, WEIGHT_FIELD, {})
    assert verdict == "missed"


def test_a_reading_with_no_fields_at_all_is_absent() -> None:
    r = reading()
    verdict, members, findings = judge(r, {}, WEIGHT_FIELD, {})
    assert verdict == "absent"
    assert members == (PAGE,)
    assert findings == ()


def test_the_worst_finding_wins_the_page() -> None:
    """One field agrees, one disagrees -- the page is `disagrees`."""
    height_words = {w.id: w for w in [word(10, "180")]}
    both_fields = {
        ("measurement", "weight_kg"): VerifiedField(type="number", units=("kg",)),
        ("measurement", "height_cm"): VerifiedField(type="number", units=("cm",)),
    }
    words = {**WORDS, **height_words}
    r = reading(
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(1, 2)),
        FieldReading(extract="measurement", field="height_cm", row=0, status="seen", words=(10,)),
    )
    rows = {"measurement": [("doc1/p0001", {"weight_kg": 82.0, "height_cm": 999.0})]}
    verdict, _members, findings = judge(r, rows, both_fields, words)
    assert verdict == "disagrees"
    kinds = {f.field: f.verdict for f in findings}
    assert kinds == {"weight_kg": "agrees", "height_cm": "disagrees"}


def test_many_by_row_pairs_reading_rows_to_extract_rows_by_value() -> None:
    """Two rows on the page, read out of order by the model; `judge` pairs
    each reading row to the extract row that shares its anchor value,
    never by position."""
    row_words = {
        w.id: w
        for w in [
            word(0, "2024-01-01"),
            word(1, "2024-02-02"),
            word(2, "70"),
            word(3, "75"),
        ]
    }
    fields = {("measurement", "weight_kg"): VerifiedField(type="number", units=("kg",))}
    r = reading(
        # Model answers row 0 with the SECOND day's weight.
        FieldReading(extract="measurement", field="weight_kg", row=0, status="seen", words=(3,), seen_text="75"),
        FieldReading(extract="measurement", field="weight_kg", row=1, status="seen", words=(2,), seen_text="70"),
    )
    rows = {
        "measurement": [
            ("doc1/p0001#r01", {"weight_kg": 70.0}),
            ("doc1/p0001#r02", {"weight_kg": 75.0}),
        ]
    }
    verdict, members, findings = judge(r, rows, fields, row_words)
    assert verdict == "agrees"
    assert set(members) == {"doc1/p0001#r01", "doc1/p0001#r02"}
    by_row = {f.row: f.record for f in findings}
    assert by_row == {0: "doc1/p0001#r02", 1: "doc1/p0001#r01"}
