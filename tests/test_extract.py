"""`extract` -- deterministic patterns over one page, in the language.

A document is a fact; an extract *calculates* records from one, the way a
figure calculates a value, never a model at run time (documents-plan-v3,
D4). These tests pin the checker's claims about it:

- **The target is a declared fact, and exactly one extract may produce it.**
- **`over` takes declared predicate/presence filters only**, over the
  source kind or a non-`many` extract of the same source -- never a group,
  never a bucket-scoped set, because the runner evaluates it fresh on one
  page before anything has landed.
- **A derived kind's `keyed as` claim is verified, not trusted** -- the
  opposite of an ordinary fact's, where the checker has no way to check.
- **Copies between extracts form a DAG.** A cycle is refused; a copy from
  a `many` extract is refused; a copy's type must match the field it
  writes.
- **Matchers are typed against the target fact's declared field types.**
- **The version is fully deterministic**: it moves on a matcher change and
  stays put across a prose edit, like every other definition's.

Every refusal is paired with a near-identical control that compiles, so a
checker that refused everything would not pass this file.
"""

from __future__ import annotations

import pytest

from uratori import CheckError, Schema, SyntaxError_, compile_source

TAUGHT = Schema(kinds=frozenset())

# A patient's uploaded records, and the two extracts every other one in the
# documents example copies from or is gated by.
DOCUMENT_SOURCE = """
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title
    source as text

# One page of one.
fact medical_record_page as page of medical_record

# Who a page says it is about: the identifier printed in its header.
fact page_identity:
    patient_id as text

# Read once, from the printed identifier.
extract page_identity from medical_record_page:
    patient_id = text after any of ["MRN:", "Patient ID:"]

# What kind of page this is.
fact page_class:
    type as text

# A page is "vitals" if it says so anywhere on it.
extract page_class from medical_record_page:
    type = "vitals" if page contains any of ["VITAL SIGNS", "Vitals", "Wt:"]

filter page_class.vitals keyed as medical_record_page where type == "vitals"
"""

MEASUREMENT_FACT = """
# One set of vitals read off one page.
fact measurement:
    patient_id as text
    page as text
    measured_at as moment
    weight_kg as number
    height_cm as number
"""

MEASUREMENT_EXTRACT = """
# Vitals read off one page: as many rows as the page carries.
extract measurement from medical_record_page:
    over page_class.vitals
    many by row up to 20
    patient_id  = page_identity.patient_id
    measured_at = date after any of ["Date:", "Visit date", "DOS"]
    weight_kg   = number after any of ["Weight:", "Wt:", "Wt", "WEIGHT"] in kg or lb
    height_cm   = number after any of ["Height:", "Ht:", "Ht", "HEIGHT"] in cm or in or ft_in
"""

SOURCE = DOCUMENT_SOURCE + MEASUREMENT_FACT + MEASUREMENT_EXTRACT


def compile_taught(source: str) -> object:
    return compile_source(source, TAUGHT)


def refuses(source: str, *needles: str) -> None:
    with pytest.raises((CheckError, SyntaxError_)) as caught:
        compile_taught(source)
    message = str(caught.value)
    for needle in needles:
        assert needle in message, message


# ------------------------------------------------------------- compiling --


def test_the_documents_example_compiles() -> None:
    library = compile_taught(SOURCE)
    assert set(library.extracts) == {"page_identity", "page_class", "measurement"}


def test_an_extract_is_named_like_the_fact_it_targets() -> None:
    library = compile_taught(SOURCE)
    plan = library.extracts["measurement"]
    assert plan.name == "measurement"
    assert plan.source == "medical_record_page"


def test_many_by_row_carries_its_declared_ceiling() -> None:
    library = compile_taught(SOURCE)
    plan = library.extracts["measurement"]
    assert plan.many is True
    assert plan.many_up_to == 20


def test_many_by_row_defaults_its_ceiling_when_none_is_written() -> None:
    library = compile_taught(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + """
# Vitals, with no stated ceiling.
extract measurement from medical_record_page:
    over page_class.vitals
    many by row
    patient_id  = page_identity.patient_id
    measured_at = date after any of ["Date:"]
    weight_kg   = number after any of ["Weight:"] in kg
    height_cm   = number after any of ["Height:"] in cm
"""
    )
    plan = library.extracts["measurement"]
    assert plan.many_up_to is not None and plan.many_up_to > 0


def test_a_copy_is_recorded_in_copies_and_hashed_into_the_version() -> None:
    library = compile_taught(SOURCE)
    plan = library.extracts["measurement"]
    assert plan.copies == ("page_identity",)


def test_a_non_many_extract_is_keyed_by_its_source() -> None:
    """`page_identity` and `page_class` are both non-`many`: a group or
    filter over them may claim `keyed as medical_record_page`, verified
    against the extract rather than trusted (D4.3)."""
    library = compile_taught(
        DOCUMENT_SOURCE
        + """
group page_identity.by_patient keyed as medical_record_page from patient_id
"""
    )
    assert library.indexes["page_identity.by_patient"].id_space == "medical_record_page"


# -------------------------------------------------------------- refusals --


def test_an_extract_with_no_matching_fact_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# No matching fact declared anywhere.
extract vitals_only from medical_record_page:
    type = text after any of ["Type:"]
""",
        "names no fact",
    )


def test_an_extract_may_not_target_a_document_shaped_kind() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A page kind is not a pattern's target.
extract medical_record_page from medical_record_page:
    patient_id = text after any of ["MRN:"]
""",
        "server-authored structure",
    )


def test_two_extracts_may_not_target_the_same_fact() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A second extract for a fact one already produces.
extract page_identity from medical_record_page:
    patient_id = text after any of ["Patient ID:"]
""",
        "already produced by another extract",
    )


def test_an_extracts_source_must_be_a_fact_kind() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# No such thing.
fact vitals:
    type as text
# Reads a source that was never declared.
extract vitals from nope:
    type = text after any of ["Type:"]
""",
        "not a fact kind",
    )


def test_an_extracts_source_must_be_a_page_kind() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# Not a page.
fact plain:
    ref as text
# A different target.
fact elsewhere:
    ref as text
# Reads an ordinary fact, not a page.
extract elsewhere from plain:
    ref = text after any of ["Ref:"]
""",
        'not declared "as page of"',
    )


def test_many_by_row_needs_an_anchor_field() -> None:
    """A `many` extract with no field that reads the page's own words has
    nothing to anchor a row to a line."""
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + """
# No field reads the page's own words -- only a copy.
extract measurement from medical_record_page:
    many by row
    patient_id = page_identity.patient_id
""",
        "needs at least one",
    )


def test_a_number_matcher_against_a_text_field_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# Wrong type on purpose.
fact mistyped:
    weight_kg as text

# A number matcher against a text field.
extract mistyped from medical_record_page:
    weight_kg = number after any of ["Weight:"] in kg
""",
        "reads a number",
    )


def test_a_date_matcher_against_a_number_field_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# Wrong type on purpose.
fact mistyped:
    measured_at as number

# A date matcher against a number field.
extract mistyped from medical_record_page:
    measured_at = date after any of ["Date:"]
""",
        "reads a date",
    )


def test_a_text_matcher_against_a_moment_field_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# Wrong type on purpose.
fact mistyped:
    patient_id as moment

# A text matcher against a moment field.
extract mistyped from medical_record_page:
    patient_id = text after any of ["MRN:"]
""",
        "reads text",
    )


def test_a_word_ladder_against_a_number_field_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# Wrong type on purpose.
fact mistyped:
    type as number

# A classification ladder against a number field.
extract mistyped from medical_record_page:
    type = "vitals" if page contains any of ["Wt:"]
""",
        "answers a word",
    )


def test_an_undeclared_unit_lists_the_vocabulary() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A weight.
fact mistyped:
    weight_kg as number

# A unit this engine does not know how to convert.
extract mistyped from medical_record_page:
    weight_kg = number after any of ["Weight:"] in stone
""",
        "not a unit this engine converts",
    )


def test_a_field_not_declared_on_the_target_fact_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# One field only.
fact narrow:
    patient_id as text

# Writes a field the fact never declared.
extract narrow from medical_record_page:
    patient_id = text after any of ["MRN:"]
    extra = text after any of ["X:"]
""",
        "does not declare",
    )


def test_writing_the_built_in_page_field_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + """
# Tries to write the built-in field itself.
extract measurement from medical_record_page:
    page = text after any of ["Page:"]
    patient_id = page_identity.patient_id
    measured_at = date after any of ["Date:"]
    weight_kg = number after any of ["Weight:"] in kg
    height_cm = number after any of ["Height:"] in cm
""",
        "built-in",
    )


def test_a_page_field_declared_as_the_wrong_type_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A "page" field of the wrong type.
fact mistyped:
    patient_id as text
    page as number

# Never reaches the mismatched field -- the fact itself is refused.
extract mistyped from medical_record_page:
    patient_id = text after any of ["MRN:"]
""",
        "the built-in `page` field",
    )


def test_an_extract_with_no_fields_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A target with nothing to write.
fact empty_target:
    patient_id as text

# No fields at all.
extract empty_target from medical_record_page:
""",
        "indented block",
    )


def test_an_extract_needs_an_explanation() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A target fact.
fact unexplained:
    patient_id as text

extract unexplained from medical_record_page:
    patient_id = text after any of ["MRN:"]
""",
        "no explanation",
    )


def test_a_copy_from_an_undeclared_extract_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A copier.
fact copier:
    patient_id as text

# Copies from an extract that was never declared.
extract copier from medical_record_page:
    patient_id = nonexistent.patient_id
""",
        "not a declared extract",
    )


def test_a_copy_from_a_many_extract_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# A copier.
fact copier:
    weight_kg as number

# Copies from a `many by row` extract -- which row?
extract copier from medical_record_page:
    weight_kg = measurement.weight_kg
""",
        "many` extract is refused",
    )


def test_a_copy_across_different_source_kinds_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A page from a different document kind entirely.
fact other_doc as document:
    name title

# Its page kind.
fact other_page as page of other_doc

# A copier reading the wrong source.
fact copier:
    patient_id as text

# Copies from an extract of a different source page kind.
extract copier from other_page:
    patient_id = page_identity.patient_id
""",
        "same source page kind",
    )


def test_a_copy_with_the_wrong_declared_type_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A number, copying a text field.
fact copier:
    patient_id as number

# The copy's type does not match what it copies.
extract copier from medical_record_page:
    patient_id = page_identity.patient_id
""",
        "and fact copier declares",
    )


def test_a_copy_cycle_is_refused() -> None:
    refuses(
        """
# A
fact page_identity:
    patient_id as text
# B
fact page_class:
    type as text
# extract A, copying from B
extract page_identity from medical_record_page:
    patient_id = page_class.patient_id
# extract B, copying from A
extract page_class from medical_record_page:
    type = page_identity.type
# doc
fact medical_record as document:
    name title
# page
fact medical_record_page as page of medical_record
""",
        "cycle is refused",
    )


def test_over_may_not_name_a_group() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
group page_class.by_type from type

# Gated by a group rather than a filter.
fact gated:
    patient_id as text

# `over` taking a group instead of a filter.
extract gated from medical_record_page:
    over page_class.by_type
    patient_id = page_identity.patient_id
""",
        "group, not a filter",
    )


def test_over_may_not_be_scoped_to_a_subject() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# An extract has no subject to scope `over` to.
fact gated:
    patient_id as text

# `over` scoped to a subject, which an extract has none of.
extract gated from medical_record_page:
    over page_class.vitals:{medical_record_page}
    patient_id = page_identity.patient_id
""",
        "scoped to a subject",
    )


def test_over_must_name_a_filter_of_the_same_source_or_a_non_many_extract_of_it() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# A different document kind entirely.
fact other_doc as document:
    name title
# Its page kind.
fact other_page as page of other_doc
# An ordinary fact, not even a page.
fact other_thing:
    flag as flag
filter other_thing.on where flag == true

# Gated by a filter over an unrelated kind.
fact gated:
    patient_id as text

# `over` naming a filter of a kind that is neither the source nor an
# extract of it.
extract gated from medical_record_page:
    over other_thing.on
    patient_id = page_identity.patient_id
""",
        "is neither",
    )


def test_keyed_as_against_a_derived_kind_is_verified_not_trusted() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
filter page_identity.named keyed as page_class where patient_id is set
""",
        "is produced by an extract whose records are keyed",
    )


def test_keyed_as_is_refused_over_a_many_extracts_kind() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
filter measurement.any keyed as medical_record_page where weight_kg is set
""",
        "many by row` extract",
    )


# ----------------------------------------------------------- versioning --


def test_prose_does_not_move_an_extracts_version() -> None:
    library = compile_taught(SOURCE)
    before = library.extracts["measurement"].version
    changed = SOURCE.replace(
        "# Vitals read off one page: as many rows as the page carries.",
        "# Vitals, read off one page -- rewritten prose, same patterns.",
    )
    after = compile_taught(changed).extracts["measurement"].version
    assert before == after


def test_a_new_alternative_moves_the_version() -> None:
    library = compile_taught(SOURCE)
    before = library.extracts["measurement"].version
    changed = SOURCE.replace(
        '"Weight:", "Wt:", "Wt", "WEIGHT"', '"Weight:", "Wt:", "Wt", "WEIGHT", "Mass:"'
    )
    after = compile_taught(changed).extracts["measurement"].version
    assert before != after


def test_changing_the_copied_from_extract_moves_this_extracts_version() -> None:
    """D4.4: an extract hashes the versions of the extracts it copies from,
    the way a rollup hashes its source's -- so a changed identity matcher
    re-extracts everything that trusts it."""
    library = compile_taught(SOURCE)
    before = library.extracts["measurement"].version
    changed = SOURCE.replace(
        'patient_id = text after any of ["MRN:", "Patient ID:"]',
        'patient_id = text after any of ["MRN:", "Patient ID:", "Med Rec #"]',
    )
    after = compile_taught(changed).extracts["measurement"].version
    assert before != after
