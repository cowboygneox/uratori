"""`audit` -- a second, model-backed reader over a page, in the language.

5b's whole job: the grammar compiles to an `AuditPlan`, a `read:` block binds
exactly three shapes a template may interpolate (another extract's field on
the same page, the page's document's field, or another auditor's verdict on
the same page), `context`/`prompt` refuse an unbound placeholder the same way
a `flag` does, and the version hashes the template text, the bound names, the
model id and the verified fields' names/types/units -- never the versions of
what a binding reads (`documents-plan-v3` D6, D6.5).

Every refusal is paired with a near-identical control that compiles, so a
checker that refused everything would not pass this file. No model code, no
provider, no worker: this is the grammar and the checker alone.
"""

from __future__ import annotations

import pytest

from uratori import CheckError, Schema, SyntaxError_, compile_source

from .test_extract import DOCUMENT_SOURCE, MEASUREMENT_EXTRACT, MEASUREMENT_FACT, SOURCE

TAUGHT = Schema(kinds=frozenset())


def compile_taught(source: str) -> object:
    return compile_source(source, TAUGHT)


def refuses(source: str, *needles: str) -> None:
    with pytest.raises((CheckError, SyntaxError_)) as caught:
        compile_taught(source)
    message = str(caught.value)
    for needle in needles:
        assert needle in message, message


AUDIT = """
# A second reader over the vitals on each page.
audit medical_record_page.vitals_audit:
    verifies page_identity, measurement
    model "claude-opus-5-5"
    read:
        document = medical_record.title
    context "Weights on this clinic's flowsheets are in pounds unless marked. This is {document}."
    display "{medical_record_page} {value}"
"""

WITH_AUDIT = SOURCE + AUDIT


# ------------------------------------------------------------- compiling --


def test_the_canonical_audit_compiles() -> None:
    library = compile_taught(WITH_AUDIT)
    assert set(library.audits) == {"medical_record_page.vitals_audit"}


def test_an_audit_is_scoped_to_its_name_prefix() -> None:
    library = compile_taught(WITH_AUDIT)
    plan = library.audits["medical_record_page.vitals_audit"]
    assert plan.scope == "medical_record_page"
    assert plan.model == "claude-opus-5-5"
    assert plan.verifies == ("measurement", "page_identity")


def test_a_read_binding_to_a_document_field_resolves() -> None:
    library = compile_taught(WITH_AUDIT)
    plan = library.audits["medical_record_page.vitals_audit"]
    assert len(plan.reads) == 1
    binding = plan.reads[0]
    assert binding.name == "document"
    assert binding.kind == "document_field"
    assert binding.source == "medical_record"
    assert binding.field == "title"


def test_context_and_prompt_are_both_optional() -> None:
    library = compile_taught(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# A plain second reader, no prompt override at all.
audit medical_record_page.plain_audit:
    verifies measurement
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
"""
    )
    plan = library.audits["medical_record_page.plain_audit"]
    assert plan.context is None
    assert plan.prompt is None
    assert plan.reads == ()


def test_a_read_binding_to_an_unverified_extracts_field_resolves() -> None:
    library = compile_taught(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Checks the identity extract against a second reading.
audit medical_record_page.identity_audit:
    verifies measurement
    model "claude-opus-5-5"
    read:
        mrn = page_identity.patient_id
    context "The identifier on this page reads {mrn}."
    display "{medical_record_page} {value}"
"""
    )
    binding = library.audits["medical_record_page.identity_audit"].reads[0]
    assert binding.kind == "extract_field"
    assert binding.source == "page_identity"
    assert binding.field == "patient_id"


def test_a_read_binding_to_another_auditors_verdict_resolves() -> None:
    library = compile_taught(
        WITH_AUDIT
        + """
# A second auditor, checking the first one's own work.
audit medical_record_page.vitals_audit_sonnet:
    verifies measurement
    model "claude-sonnet-5"
    read:
        other = medical_record_page.vitals_audit
    context "A different reader already said {other}."
    display "{medical_record_page} {value}"
"""
    )
    binding = library.audits["medical_record_page.vitals_audit_sonnet"].reads[0]
    assert binding.kind == "verdict"
    assert binding.source == "medical_record_page.vitals_audit"


def test_declaration_order_does_not_matter_for_a_verdict_read() -> None:
    """The second auditor is written FIRST in the source; the dependency walk
    in `_audits` must still check the one it reads before itself."""
    library = compile_taught(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Declared first, reads the second one's verdict.
audit medical_record_page.vitals_audit_sonnet:
    verifies measurement
    model "claude-sonnet-5"
    read:
        other = medical_record_page.vitals_audit
    context "A different reader already said {other}."
    display "{medical_record_page} {value}"

# Declared second, read by the first.
audit medical_record_page.vitals_audit:
    verifies measurement
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
"""
    )
    assert set(library.audits) == {
        "medical_record_page.vitals_audit",
        "medical_record_page.vitals_audit_sonnet",
    }


def test_a_figure_may_compare_an_audits_verdict_in_a_when_clause() -> None:
    """The language half of 5a's engine work: a figure reads an audit's
    verdict bare, exactly as it reads another figure, and the dotted name
    resolves to the audit rather than failing as an unknown field of the
    page."""
    library = compile_taught(
        WITH_AUDIT
        + """
# A page the second reader agrees with.
figure medical_record_page.vitals_checked:
    display "{medical_record_page} {value}"
    calculate:
        when medical_record_page.vitals_audit != "agrees" then "disputed"
        otherwise "clean"
"""
    )
    plan = library.figure("medical_record_page.vitals_checked")
    assert plan is not None
    assert "medical_record_page.vitals_audit" in plan.reads
    assert plan.depth == 0


def test_an_audits_verdict_may_not_enter_arithmetic() -> None:
    refuses(
        WITH_AUDIT
        + """
# Tries to add a word to a number.
figure medical_record_page.bad_math:
    display "{medical_record_page} {value}"
    calculate:
        medical_record_page.vitals_audit + 1
""",
        "word rather than",
    )


# -------------------------------------------------------------- refusals --


def test_an_audit_needs_at_least_one_verifies() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
audit medical_record_page.empty_audit:
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
""",
        "verifies nothing",
    )


def test_an_audit_needs_a_model() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
audit medical_record_page.no_model:
    verifies measurement
    display "{medical_record_page} {value}"
""",
        "has no model",
    )


def test_an_audit_needs_a_display_template() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
audit medical_record_page.no_display:
    verifies measurement
    model "claude-opus-5-5"
""",
        "has no display template",
    )


def test_context_and_prompt_are_mutually_exclusive() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
audit medical_record_page.both:
    verifies measurement
    model "claude-opus-5-5"
    context "extra"
    prompt "replacement"
    display "{medical_record_page} {value}"
""",
        "both",
        "context",
        "prompt",
    )


def test_an_audit_requires_explanation() -> None:
    """The `#` prose rule, same mechanism as every other rendered kind."""
    with pytest.raises(SyntaxError_, match="no explanation"):
        compile_taught(
            DOCUMENT_SOURCE
            + MEASUREMENT_FACT
            + MEASUREMENT_EXTRACT
            + """
audit medical_record_page.undocumented:
    verifies measurement
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
"""
        )


def test_verifies_names_a_declared_extract() -> None:
    refuses(
        DOCUMENT_SOURCE
        + """
# No explanation needed -- this is refused before prose is even checked.
audit medical_record_page.ghost:
    verifies nonexistent_extract
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
""",
        "not a declared",
        "extract",
    )


def test_verifies_the_same_extract_twice_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Names the same extract twice.
audit medical_record_page.double:
    verifies measurement, measurement
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"
""",
        "twice",
    )


def test_an_audit_must_be_scoped_to_a_page_kind() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# measurement is a derived fact, not a page kind.
audit measurement.bad_scope:
    verifies measurement
    model "claude-opus-5-5"
    display "{measurement} {value}"
""",
        "not declared",
        "page of",
    )


def test_a_read_block_may_not_bind_a_verified_extracts_field() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Tries to read the answer it is supposed to be checking.
audit medical_record_page.leaky:
    verifies measurement
    model "claude-opus-5-5"
    read:
        weight = measurement.weight_kg
    context "The extract already says {weight}."
    display "{medical_record_page} {value}"
""",
        "verifies",
        "blind reader",
    )


def test_a_read_block_target_must_resolve_to_something() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Reads a target that names nothing at all.
audit medical_record_page.dangling:
    verifies measurement
    model "claude-opus-5-5"
    read:
        nothing = not_a_thing.at_all
    context "{nothing}"
    display "{medical_record_page} {value}"
""",
        "names neither",
    )


def test_an_unbound_placeholder_in_context_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Its context mentions a name the read block never bound.
audit medical_record_page.unbound:
    verifies measurement
    model "claude-opus-5-5"
    context "This mentions {nowhere}, which nothing binds."
    display "{medical_record_page} {value}"
""",
        "nowhere",
        "does not bind",
    )


def test_an_unbound_placeholder_in_prompt_is_refused() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Its prompt mentions a name the read block never bound.
audit medical_record_page.unbound_prompt:
    verifies measurement
    model "claude-opus-5-5"
    prompt "This mentions {nowhere}, which nothing binds."
    display "{medical_record_page} {value}"
""",
        "nowhere",
        "does not bind",
    )


def test_a_read_block_name_may_not_repeat() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
audit medical_record_page.dup_bind:
    verifies measurement
    model "claude-opus-5-5"
    read:
        document = medical_record.title
        document = medical_record.title
    display "{medical_record_page} {value}"
""",
        "bound twice",
    )


def test_two_auditors_may_not_read_each_others_verdict() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# Reads b's verdict.
audit medical_record_page.a:
    verifies measurement
    model "claude-opus-5-5"
    read:
        other = medical_record_page.b
    context "says {other}"
    display "{medical_record_page} {value}"

# Reads a's verdict -- a cycle.
audit medical_record_page.b:
    verifies measurement
    model "claude-opus-5-5"
    read:
        other = medical_record_page.a
    context "says {other}"
    display "{medical_record_page} {value}"
""",
        "own verdict",
    )


def test_an_audit_name_may_not_collide_with_a_figure() -> None:
    refuses(
        DOCUMENT_SOURCE
        + MEASUREMENT_FACT
        + MEASUREMENT_EXTRACT
        + """
# The audit.
audit medical_record_page.vitals_audit:
    verifies measurement
    model "claude-opus-5-5"
    display "{medical_record_page} {value}"

# A figure under the same name.
figure medical_record_page.vitals_audit:
    display "{medical_record_page} {value}"
    calculate:
        count(page_class.vitals)
""",
        "already",
    )


# ------------------------------------------------------------------ hash --


def test_prose_alone_does_not_move_the_version() -> None:
    before = compile_taught(WITH_AUDIT).audits["medical_record_page.vitals_audit"].version
    reworded = WITH_AUDIT.replace(
        "# A second reader over the vitals on each page.",
        "# A second reader over the vitals on each page -- reworded.",
    )
    after = compile_taught(reworded).audits["medical_record_page.vitals_audit"].version
    assert before == after


def test_the_model_id_is_hashed_into_the_version() -> None:
    before = compile_taught(WITH_AUDIT).audits["medical_record_page.vitals_audit"].version
    moved = WITH_AUDIT.replace('model "claude-opus-5-5"', 'model "claude-sonnet-5"')
    after = compile_taught(moved).audits["medical_record_page.vitals_audit"].version
    assert before != after


def test_the_template_text_is_hashed_into_the_version() -> None:
    before = compile_taught(WITH_AUDIT).audits["medical_record_page.vitals_audit"].version
    moved = WITH_AUDIT.replace(
        "Weights on this clinic's flowsheets are in pounds unless marked. This is {document}.",
        "Weights are usually in pounds. This is {document}.",
    )
    after = compile_taught(moved).audits["medical_record_page.vitals_audit"].version
    assert before != after
