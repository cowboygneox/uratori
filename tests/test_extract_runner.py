"""The `extract` runner: a pure function of one page (documents-plan-v3, D4).

Property-style tests over hand-built word layers, never a real PDF -- a
page is exactly the `Word` sequence its word layer already is, and the
runner's whole contract is that the same plan over the same words produces
the same records, byte for byte. `tests/test_documents.py` and the D5
end-to-end test (package 3c) exercise the real PDF-to-word-layer pipeline
feeding this; this file exercises the matchers themselves.
"""

from __future__ import annotations

from uratori.documents.extract import (
    ROW_SEPARATOR,
    in_scope,
    row_key,
    run_extract,
)
from uratori.lang.ast import (
    BucketAll,
    ByPredicate,
    ByPresence,
    DateAfter,
    ExtractField,
    FieldCopy,
    NumberAfter,
    SetIndex,
    TextAfter,
    WordLadder,
    WordRung,
)
from uratori.lang.plan import CompiledIndex, ExtractPlan
from uratori.server.provenance import ProvenanceRow
from uratori.server.words import Word

NOW_MS = 1_700_000_000_000.0


def words(*lines: list[str]) -> list[Word]:
    """A word layer: one list of tokens per text line, ids assigned in
    reading order, boxes laid out left to right so provenance boxes are at
    least plausible."""
    out: list[Word] = []
    wid = 0
    for line_idx, tokens in enumerate(lines):
        x = 0.0
        for token in tokens:
            out.append(
                Word(
                    id=wid,
                    text=token,
                    x0=x,
                    y0=line_idx * 0.05,
                    x1=x + 0.04,
                    y1=line_idx * 0.05 + 0.02,
                    line=line_idx,
                    block=0,
                    source="pdf",
                    confidence=None,
                )
            )
            wid += 1
            x += 0.05
    return out


def extract(*fields: ExtractField, many: bool = False, many_up_to: int | None = None) -> ExtractPlan:
    return ExtractPlan(
        name="measurement",
        source="medical_record_page",
        over=None,
        many=many,
        many_up_to=many_up_to,
        fields=fields,
        copies=(),
    )


def run(
    plan: ExtractPlan,
    page_words: list[Word],
    *,
    page_key: str = "d1/p0001",
    derived_on_page: dict[str, dict[str, object]] | None = None,
    derived_provenance_on_page: dict[str, list[ProvenanceRow]] | None = None,
    indexes: dict[str, CompiledIndex] | None = None,
):
    return run_extract(
        plan,
        page_key=page_key,
        page_record={},
        words=page_words,
        derived_on_page=derived_on_page or {},
        derived_provenance_on_page=derived_provenance_on_page or {},
        indexes=indexes or {},
        now_ms=NOW_MS,
    )


# ---------------------------------------------------------- number after --


def test_number_after_a_single_declared_unit_needs_no_printed_one() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=NumberAfter(("Weight:",), ("kg",))))
    result = run(plan, words(["Weight:", "82"]))
    assert not result.failures
    assert result.records[0].body["patient_id"] == 82.0


def test_number_after_converts_lb_to_kg() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=NumberAfter(("Wt:",), ("kg", "lb"))))
    result = run(plan, words(["Wt:", "180", "lb"]))
    assert not result.failures
    value = result.records[0].body["patient_id"]
    assert isinstance(value, float)
    assert abs(value - 180 * 0.45359237) < 1e-9


def test_number_after_with_more_than_one_unit_and_none_printed_is_a_failure() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=NumberAfter(("Weight:",), ("kg", "lb"))))
    result = run(plan, words(["Weight:", "82"]))
    assert not result.records
    assert result.failures[0].reason == (
        "a number with no printed unit, and more than one unit is declared"
    )


def test_number_after_reads_the_ft_in_composite() -> None:
    plan = extract(
        ExtractField(name="patient_id", matcher=NumberAfter(("Height:",), ("cm", "in", "ft_in")))
    )
    result = run(plan, words(["Height:", "5", "ft", "10", "in"]))
    assert not result.failures
    value = result.records[0].body["patient_id"]
    assert abs(value - (5 * 30.48 + 10 * 2.54)) < 1e-9
    assert result.records[0].provenance[0].word_ids == (1, 2, 3, 4)


def test_number_after_cites_only_the_number_and_its_unit() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=NumberAfter(("Weight:",), ("kg", "lb"))))
    result = run(plan, words(["Weight:", "82", "kg"]))
    row = result.records[0].provenance[0]
    assert row.word_ids == (1, 2)
    assert row.printed == "82 kg"
    assert row.anchored is True
    assert row.reproducible is True


def test_number_after_with_no_alternative_match_is_a_failure() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=NumberAfter(("Weight:",), ())))
    result = run(plan, words(["Height:", "180"]))
    assert result.failures[0].reason == "no alternative matched"


# ------------------------------------------------------------ date after --


def test_date_after_reads_iso() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=DateAfter(("Date:",))))
    result = run(plan, words(["Date:", "2024-03-02"]))
    assert result.records[0].body["patient_id"] == "2024-03-02"


def test_date_after_reads_us_when_unambiguous() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=DateAfter(("DOS",))))
    result = run(plan, words(["DOS", "13/04/2024"]))
    assert result.records[0].body["patient_id"] == "2024-04-13"


def test_date_after_reads_month_name_form() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=DateAfter(("Visit",))))
    result = run(plan, words(["Visit", "Mar", "4,", "2024"]))
    assert result.records[0].body["patient_id"] == "2024-03-04"


def test_date_after_an_unresolvable_ambiguity_is_a_failure() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=DateAfter(("DOS",))))
    result = run(plan, words(["DOS", "03/04/2024"]))
    assert not result.records
    assert result.failures[0].reason == "date ambiguous"


# ------------------------------------------------------------ text after --


def test_text_after_trims_and_reads_the_identifier() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=TextAfter(("MRN:",))))
    result = run(plan, words(["MRN:", "004412"]))
    assert result.records[0].body["patient_id"] == "004412"


def test_text_after_refuses_an_at_symbol() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=TextAfter(("MRN:",))))
    result = run(plan, words(["MRN:", "12@34"]))
    assert not result.records
    assert result.failures[0].reason == 'identifier contains "@"'


def test_text_after_refuses_a_control_character() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=TextAfter(("MRN:",))))
    result = run(plan, words(["MRN:", "12\x0034"]))
    assert not result.records
    assert result.failures[0].reason == "identifier contains a control character"


# ---------------------------------------------------------- word ladder --


def test_word_ladder_first_match_wins() -> None:
    plan = extract(
        ExtractField(
            name="patient_id",
            matcher=WordLadder(
                rungs=(
                    WordRung(word="vitals", alternatives=("VITAL SIGNS", "Wt:")),
                    WordRung(word="labs", alternatives=("CBC",)),
                ),
                otherwise="other",
            ),
        )
    )
    result = run(plan, words(["Routine", "Wt:", "82", "kg"]))
    assert result.records[0].body["patient_id"] == "vitals"


def test_word_ladder_falls_to_otherwise() -> None:
    plan = extract(
        ExtractField(
            name="patient_id",
            matcher=WordLadder(
                rungs=(WordRung(word="vitals", alternatives=("VITAL SIGNS",)),),
                otherwise="other",
            ),
        )
    )
    result = run(plan, words(["Nothing", "here"]))
    assert result.records[0].body["patient_id"] == "other"
    assert result.records[0].provenance == ()


def test_word_ladder_with_no_otherwise_and_no_match_is_a_failure() -> None:
    plan = extract(
        ExtractField(
            name="patient_id",
            matcher=WordLadder(rungs=(WordRung(word="vitals", alternatives=("X",)),)),
        )
    )
    result = run(plan, words(["Nothing", "here"]))
    assert not result.records
    assert result.failures[0].reason == "no alternative matched"


# -------------------------------------------------------------------- copy --


def test_a_copy_reads_the_upstream_extracts_value_and_provenance() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=FieldCopy(extract="page_identity", field="patient_id")))
    upstream_row = ProvenanceRow(
        field="patient_id",
        page_key="d1/p0001",
        word_ids=(0,),
        boxes=(),
        printed="MRN: 4412",
        value="4412",
        anchored=True,
    )
    result = run(
        plan,
        words(["irrelevant"]),
        derived_on_page={"page_identity": {"patient_id": "4412"}},
        derived_provenance_on_page={"page_identity": [upstream_row]},
    )
    assert result.records[0].body["patient_id"] == "4412"
    row = result.records[0].provenance[0]
    assert row.extractor == "copy:page_identity"
    assert row.word_ids == (0,)
    assert row.value == "4412"


def test_a_copy_of_the_built_in_page_field() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=FieldCopy(extract="x", field="page")))
    result = run(
        plan,
        words(["irrelevant"]),
        page_key="d1/p0007",
        derived_on_page={"x": {}},
    )
    assert result.records[0].body["patient_id"] == "d1/p0007"


def test_a_copy_with_no_upstream_record_on_this_page_is_a_failure() -> None:
    plan = extract(ExtractField(name="patient_id", matcher=FieldCopy(extract="page_identity", field="patient_id")))
    result = run(plan, words(["irrelevant"]))
    assert not result.records
    assert "no page_identity record on this page" in result.failures[0].reason


# ---------------------------------------------------------- whole record --


def test_a_record_with_one_failing_field_produces_no_record_at_all() -> None:
    """Short-circuit: a half-matched record is a guess about which half
    mattered, so the whole subject fails on the first bad field."""
    plan = extract(
        ExtractField(name="patient_id", matcher=TextAfter(("MRN:",))),
        ExtractField(name="page", matcher=NumberAfter(("Weight:",), ())),
    )
    result = run(plan, words(["MRN:", "4412"], ["no", "weight", "here"]))
    assert not result.records
    assert len(result.failures) == 1


# -------------------------------------------------------------- many by row --


def test_many_by_row_keys_are_zero_padded_to_the_ceiling() -> None:
    assert row_key("d1/p0003", 1, 20) == "d1/p0003#r01"
    assert row_key("d1/p0003", 10, 20) == "d1/p0003#r10"
    assert ROW_SEPARATOR == "#"


def test_many_by_row_produces_one_record_per_matching_line_scoped_to_that_line() -> None:
    plan = extract(
        ExtractField(name="measured_at", matcher=DateAfter(("Date:",))),
        ExtractField(name="weight_kg", matcher=NumberAfter(("Weight:",), ("kg",))),
        many=True,
        many_up_to=20,
    )
    layer = words(
        ["Date:", "2024-01-01", "Weight:", "80"],
        ["Date:", "2024-06-01", "Weight:", "82"],
    )
    result = run(plan, layer)
    assert not result.failures
    assert [r.key for r in result.records] == ["d1/p0001#r01", "d1/p0001#r02"]
    assert result.records[0].body["weight_kg"] == 80.0
    assert result.records[1].body["weight_kg"] == 82.0


def test_many_by_row_one_bad_row_does_not_sink_the_others() -> None:
    plan = extract(
        ExtractField(name="measured_at", matcher=DateAfter(("Date:",))),
        ExtractField(name="weight_kg", matcher=NumberAfter(("Weight:",), ("kg", "lb"))),
        many=True,
        many_up_to=20,
    )
    layer = words(
        ["Date:", "2024-01-01", "Weight:", "80", "kg"],
        # No printed unit, and two are declared -- this row fails.
        ["Date:", "2024-06-01", "Weight:", "82"],
    )
    result = run(plan, layer)
    assert [r.key for r in result.records] == ["d1/p0001#r01"]
    assert len(result.failures) == 1
    assert result.failures[0].subject == "d1/p0001#r02"


def test_many_by_row_with_no_matching_line_is_one_page_level_failure() -> None:
    plan = extract(
        ExtractField(name="measured_at", matcher=DateAfter(("Date:",))),
        many=True,
        many_up_to=20,
    )
    result = run(plan, words(["nothing", "to", "see"]))
    assert not result.records
    assert len(result.failures) == 1
    assert result.failures[0].subject == "d1/p0001"
    assert result.failures[0].field == "measured_at"


# ----------------------------------------------------------------- over --


def test_in_scope_with_no_over_is_always_true() -> None:
    assert in_scope(None, source_kind="medical_record_page", page_record={}, derived_on_page={}, indexes={}, now_ms=NOW_MS)


def test_in_scope_evaluates_a_presence_filter_on_a_derived_kind() -> None:
    index = CompiledIndex(
        name="page_class.vitals",
        kind="page_class",
        id_space="page_class",
        spec=ByPredicate(field="type", op="==", value="vitals"),
        bucketed=False,
    )
    expr = SetIndex(index="page_class.vitals", bucket=BucketAll())
    in_ = in_scope(
        expr,
        source_kind="medical_record_page",
        page_record={},
        derived_on_page={"page_class": {"type": "vitals"}},
        indexes={"page_class.vitals": index},
        now_ms=NOW_MS,
    )
    out = in_scope(
        expr,
        source_kind="medical_record_page",
        page_record={},
        derived_on_page={"page_class": {"type": "labs"}},
        indexes={"page_class.vitals": index},
        now_ms=NOW_MS,
    )
    missing = in_scope(
        expr,
        source_kind="medical_record_page",
        page_record={},
        derived_on_page={},
        indexes={"page_class.vitals": index},
        now_ms=NOW_MS,
    )
    assert in_ is True
    assert out is False
    assert missing is False


def test_in_scope_evaluates_the_source_kind_itself() -> None:
    index = CompiledIndex(
        name="medical_record_page.rotated",
        kind="medical_record_page",
        id_space="medical_record_page",
        spec=ByPresence(field="rotation", negated=False),
        bucketed=False,
    )
    expr = SetIndex(index="medical_record_page.rotated", bucket=BucketAll())
    assert in_scope(
        expr,
        source_kind="medical_record_page",
        page_record={"rotation": 90},
        derived_on_page={},
        indexes={"medical_record_page.rotated": index},
        now_ms=NOW_MS,
    )


# -------------------------------------------------------------- determinism --


def test_the_same_plan_over_the_same_words_produces_byte_for_byte_identical_output() -> None:
    plan = extract(
        ExtractField(name="measured_at", matcher=DateAfter(("Date:",))),
        ExtractField(name="weight_kg", matcher=NumberAfter(("Weight:",), ("kg", "lb"))),
        many=True,
        many_up_to=20,
    )
    layer = words(
        ["Date:", "2024-01-01", "Weight:", "80", "kg"],
        ["Date:", "2024-06-01", "Weight:", "180", "lb"],
    )
    first = run(plan, layer)
    second = run(plan, layer)
    assert first == second
