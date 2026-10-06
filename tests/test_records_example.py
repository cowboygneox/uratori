"""The records example is the documents plan's own showcase, and this pins
it as one (documents-plan-v3, D4/D5), the way `tests/test_nfl_example.py`
pins the NFL example.

Generate -> teach -> upload -> run -> assert, over a real Postgres and a
real filesystem blob store, with the synthetic PDFs
`examples/records/generate.py` builds -- no stand-in bytes, the same
posture `tests/test_extract_server.py` takes for D5's own BMI story. What
this file pins beyond that test:

- the BMI series over *years* of visits, not two, with height measured
  once (in pounds and inches) and carried forward through a plain-text
  relabelling, a `/Rotate 90` scan and a flowsheet;
- the evidence walk, reaching a real page and real, non-empty boxes;
- both of the bundle's deliberate failures, by name, on the failures
  route;
- the OCR'd page actually ingesting through Tesseract (not asserted
  numerically -- OCR fidelity is a documents-package concern, not this
  example's).
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from uratori import Schema
from uratori.server import create_app

EXAMPLE = Path(__file__).parent.parent / "examples" / "records"

WORLD = Schema(kinds=frozenset())


def _load_generate():
    spec = importlib.util.spec_from_file_location("records_generate", EXAMPLE / "generate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["records_generate"] = module
    spec.loader.exec_module(module)
    return module


generate = _load_generate()


def test_the_example_compiles_under_its_own_schema() -> None:
    """The README's first step is `PUT /schema` then `PUT /definitions`;
    if this fails, so does every fresh install following it."""
    from uratori import compile_source
    from uratori.server.contract import SchemaIn

    schema = SchemaIn(**__import__("json").loads((EXAMPLE / "schema.json").read_text())).build()
    library = compile_source((EXAMPLE / "definitions.fig").read_text(), schema)
    assert set(library.extracts) == {"page_identity", "page_class", "measurement"}
    figure_names = {plan.name for plan in library.figures}
    assert {"patient.height", "patient.weight", "patient.bmi"} <= figure_names


@dataclass
class RecordsServer:
    http: httpx.AsyncClient
    state: Any


async def _make_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[RecordsServer]:
    name = f"uratori_records_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()

    app = create_app(
        dsn=pg_dsn,
        pg_schema=name,
        token=None,
        version="test",
        blob_dir=str(tmp_path / "blobs"),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://uratori") as http:
            put = await http.put("/schema", json=WORLD.to_document())
            assert put.status_code == 200, put.text
            put = await http.put(
                "/definitions", json={"source": (EXAMPLE / "definitions.fig").read_text()}
            )
            assert put.status_code == 200, put.text
            yield RecordsServer(http=http, state=app.state.uratori)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def records_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[RecordsServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


async def _load_bundle(http: httpx.AsyncClient) -> None:
    """The example's own `load.py`, inlined: push the roster, then upload
    every document in the synthetic bundle."""
    bundle = generate.build_bundle()
    roster = await http.post(
        "/tenants/t1/facts", json={"writes": {"patient": bundle.patients}}
    )
    assert roster.status_code == 200, roster.text
    for filename, data in bundle.documents.items():
        up = await http.post(
            "/tenants/t1/documents/medical_record",
            files={"file": (filename, io.BytesIO(data), "application/pdf")},
        )
        assert up.status_code == 200, f"{filename}: {up.text}"


async def test_bmi_computes_for_every_day_weighed_with_height_carried_forward(
    records_server: RecordsServer,
) -> None:
    http = records_server.http
    await _load_bundle(http)

    bmi = await http.get("/tenants/t1/results/patient.bmi")
    assert bmi.status_code == 200, bmi.text
    by_subject = {s["id"]: s["value"] for s in bmi.json()["subjects"]}

    height_m = generate.A_HEIGHT_CM / 100

    def expected(weight_kg: float) -> float:
        return weight_kg / (height_m * height_m)

    # Three plain-text relabellings, a /Rotate 90 scan and a flowsheet
    # row, each on a different day, years apart -- all carrying the one
    # height measured on day one (`A_VISIT1_DATE`, in pounds and inches).
    for date, weight_kg in (
        (generate.A_VISIT2_DATE, generate.A_VISIT2_WEIGHT_KG),
        (generate.A_VISIT3_DATE, generate.A_VISIT3_WEIGHT_KG),
        (generate.A_VISIT4_DATE, generate.A_VISIT4_WEIGHT_KG),
        (generate.A_VISIT5_DATE, generate.A_VISIT5_WEIGHT_KG),  # the rotated page
        (generate.A_FLOWSHEET_DATES[0], generate.A_FLOWSHEET_WEIGHTS_KG[0]),
        (generate.A_FLOWSHEET_DATES[-1], generate.A_FLOWSHEET_WEIGHTS_KG[-1]),
    ):
        subject = f"{generate.PATIENT_A}@{date}"
        assert subject in by_subject, f"missing BMI row for {subject}"
        value = by_subject[subject]
        assert value is not None, f"BMI absent for {subject}"
        assert abs(value - expected(weight_kg)) < 0.01, (date, value)

    # The day height was actually measured carries its own BMI too.
    day1 = f"{generate.PATIENT_A}@{generate.A_VISIT1_DATE}"
    assert by_subject[day1] is not None
    assert abs(by_subject[day1] - expected(generate.A_VISIT1_WEIGHT_KG)) < 0.01


async def test_evidence_walks_bmi_to_weight_and_the_carried_height_to_its_original_page(
    records_server: RecordsServer,
) -> None:
    http = records_server.http
    await _load_bundle(http)

    subject = f"{generate.PATIENT_A}@{generate.A_VISIT2_DATE}"

    bmi_evidence = await http.get(
        "/tenants/t1/evidence/patient.bmi", params={"subject": subject}
    )
    assert bmi_evidence.status_code == 200, bmi_evidence.text
    figures_cited = {m["figure"] for m in bmi_evidence.json()["members"]}
    assert figures_cited == {"patient.weight", "patient.height"}

    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": subject}
    )
    [w_member] = weight_evidence.json()["members"]
    assert w_member["held"] is True
    [w_source] = w_member["sources"]
    assert w_source["field"] == "weight_kg"
    assert w_source["anchored"] is True
    assert len(w_source["boxes"]) >= 1
    assert "74" in (w_source["printed"] or "")

    # Height was measured once, on day one, in pounds and inches -- its
    # evidence on a *later* day must still cite that original page, never
    # a fabricated same-day record.
    height_evidence = await http.get(
        "/tenants/t1/evidence/patient.height", params={"subject": subject}
    )
    [h_member] = height_evidence.json()["members"]
    [h_source] = h_member["sources"]
    assert h_source["field"] == "height_cm"
    assert h_source["anchored"] is True
    assert len(h_source["boxes"]) >= 1
    assert h_source["printed"] == f"{generate.A_HEIGHT_FT} ft {generate.A_HEIGHT_IN} in"


async def test_the_image_only_page_ingests_through_ocr(records_server: RecordsServer) -> None:
    """Not pinned numerically -- Tesseract's own fidelity is a documents-
    package concern -- but the page must genuinely have no text layer and
    must genuinely have been OCR'd, not silently skipped."""
    http = records_server.http
    await _load_bundle(http)

    docs = await http.get("/tenants/t1/documents/medical_record")
    assert docs.status_code == 200, docs.text
    chart_a = next(d for d in docs.json()["documents"] if d["title"] == "chart_a.pdf")
    words = await http.get(
        f"/tenants/t1/documents/medical_record/{chart_a['id']}/pages/6/words"
    )
    assert words.status_code == 200, words.text
    sources = {w["source"] for w in words.json()["words"]}
    assert sources == {"ocr"}


async def test_the_two_deliberate_failures_are_named_on_the_failures_route(
    records_server: RecordsServer,
) -> None:
    http = records_server.http
    await _load_bundle(http)

    failures = await http.get("/tenants/t1/extracts/measurement/failures")
    assert failures.status_code == 200, failures.text
    rows = failures.json()["failures"]

    no_unit = [r for r in rows if r["field"] == "weight_kg"]
    assert no_unit, "expected the weight-with-no-unit page to fail"
    assert any("no printed unit" in r["reason"] for r in no_unit)

    no_identity = [r for r in rows if r["field"] == "patient_id"]
    assert no_identity, "expected the page with no identifier to fail"
    assert any("no page_identity record on this page" in r["reason"] for r in no_identity)


async def test_patient_b_demonstrates_the_remaining_spellings(
    records_server: RecordsServer,
) -> None:
    """`Ht:` and `Height` -- the two spellings patient A's chart never
    needs, since its own height is only ever printed once, in pounds and
    inches."""
    http = records_server.http
    await _load_bundle(http)

    bmi = await http.get("/tenants/t1/results/patient.bmi")
    by_subject = {s["id"]: s["value"] for s in bmi.json()["subjects"]}

    for date, height_cm, weight_kg in (
        (generate.B_VISIT1_DATE, generate.B_VISIT1_HEIGHT_CM, generate.B_VISIT1_WEIGHT_KG),
        (generate.B_VISIT2_DATE, generate.B_VISIT2_HEIGHT_CM, generate.B_VISIT2_WEIGHT_KG),
    ):
        subject = f"{generate.PATIENT_B}@{date}"
        assert by_subject.get(subject) is not None
        expected = weight_kg / ((height_cm / 100) ** 2)
        assert abs(by_subject[subject] - expected) < 0.01


async def test_a_warm_pass_leaves_the_same_derived_rows(records_server: RecordsServer) -> None:
    """Full-vs-warm parity (documents-plan-v3, D4), over this example's own
    bundle rather than the two-page fixture `test_extract_server.py` uses."""
    http = records_server.http
    await _load_bundle(http)

    before = await http.get("/tenants/t1/results/patient.bmi")
    before_rows = {s["id"]: s["value"] for s in before.json()["subjects"]}

    warm = await http.post("/tenants/t1/runs", json={})
    assert warm.status_code == 200, warm.text
    after_warm = await http.get("/tenants/t1/results/patient.bmi")
    assert {s["id"]: s["value"] for s in after_warm.json()["subjects"]} == before_rows

    full = await http.post("/tenants/t1/runs", json={"full": True})
    assert full.status_code == 200, full.text
    after_full = await http.get("/tenants/t1/results/patient.bmi")
    assert {s["id"]: s["value"] for s in after_full.json()["subjects"]} == before_rows


async def test_the_audit_runs_under_the_fake_provider_and_disputes_two_pages(
    records_server: RecordsServer,
) -> None:
    """The example's `audit` (documents-plan-v3, D6), run under
    `uratori.audit.fake.FakeAuditProvider` -- no network, no model, the
    same posture `URATORI_AUDIT_PROVIDER=fake` gives a real deployment
    that wants to exercise the worker without a provider. Two pages are
    forced, via the fake's own `focus` override, into the two verdicts a
    reader actually has to act on: a genuine `disagrees` (the reader
    says the weight it is told about is not on a page that plainly
    carries one) and a genuine `missed` (the reader reports a value on
    the one page the extract's own matcher could not read -- the
    deliberate no-printed-unit failure the failures route already
    names)."""
    from uratori.audit.fake import FakeAnswer, FakeAuditProvider
    from uratori.server.audit_worker import run_worker_sweep

    http = records_server.http
    await _load_bundle(http)

    # The disagreeing page: patient A's second visit, a plain-text
    # relabelling the evidence test above already walks to a real page
    # and a real "74" -- so forcing the reader to say "not on this page"
    # is forcing a genuine disagreement, not a coincidence of the fake's
    # own crude scan.
    disagree_subject = f"{generate.PATIENT_A}@{generate.A_VISIT2_DATE}"
    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": disagree_subject}
    )
    assert weight_evidence.status_code == 200, weight_evidence.text
    disagree_page = weight_evidence.json()["members"][0]["sources"][0]["page_key"]

    # The missed page: the deliberate "weight with no printed unit"
    # failure -- the extract stores no `weight_kg` for it at all, so a
    # reader claiming it found one is a genuine miss, not a
    # disagreement with a number that exists.
    failures = await http.get("/tenants/t1/extracts/measurement/failures")
    assert failures.status_code == 200, failures.text
    [no_unit_failure] = [
        r for r in failures.json()["failures"] if r["field"] == "weight_kg"
    ]
    miss_page = no_unit_failure["page_key"]

    state = records_server.state
    assert state.world is not None
    provider = FakeAuditProvider(
        focus={
            (disagree_page, "measurement", "weight_kg"): [FakeAnswer(status="not_on_page")],
            (miss_page, "measurement", "weight_kg"): [
                FakeAnswer(status="seen", seen_text="not a real weight", anchored=False)
            ],
        }
    )
    read = await run_worker_sweep(state, state.world, state.world.library, provider)
    assert read > 0

    findings = await http.get(
        "/tenants/t1/audits/medical_record_page.vitals_audit/findings"
    )
    assert findings.status_code == 200, findings.text
    body = findings.json()
    # Keyed by (page, field): other verified fields on these same pages
    # (measured_at, patient_id, height_cm) are read too, under the fake's
    # default scan, and may dispute for reasons that have nothing to do
    # with the one field this test forced -- the page's overall verdict
    # is deliberately the worst of all of them, so asserting on the one
    # forced field is what actually pins this test's own claim.
    by_page_field = {(f["page_key"], f["field"]): f["verdict"] for f in body["findings"]}
    assert by_page_field.get((disagree_page, "weight_kg")) == "disagrees"
    assert by_page_field.get((miss_page, "weight_kg")) == "missed"
    assert body["verdict_counts"].get("disagrees", 0) >= 1
    assert body["verdict_counts"].get("missed", 0) >= 1


def test_the_bundle_is_byte_stable_across_runs() -> None:
    """Finding H (whole-review, section 3): `generate.py`'s module
    docstring already claims "`build_bundle()` returns byte-identical
    PDFs on every call", but reportlab's `Canvas` stamps the real
    wall-clock `CreationDate`/`ModDate` into every PDF it writes unless
    told `invariant=1` -- so the claim held only *within* one process's
    use of one `datetime.now()` call, never across two actual runs of
    `generate.py` (which is exactly how the README's own pasted
    `document_id`s were produced and then failed to reproduce). A real
    time gap between the two calls here is what makes a timestamp-based
    regression visible rather than coincidentally passing because both
    calls landed in the same wall-clock second."""
    import hashlib
    import time

    first = generate.build_bundle()
    time.sleep(1.1)
    second = generate.build_bundle()

    assert set(first.documents) == set(second.documents)
    for filename in first.documents:
        first_sha = hashlib.sha256(first.documents[filename]).hexdigest()
        second_sha = hashlib.sha256(second.documents[filename]).hexdigest()
        assert first_sha == second_sha, f"{filename} is not byte-stable across runs"
