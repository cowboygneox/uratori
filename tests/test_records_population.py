"""The twelve-patient population (documents-plan-v3, D4/D5) pinned the way
`tests/test_records_example.py` pins the two-chart bundle: generate, assert
byte-stability, upload one file per patient, run, assert.

What this file adds beyond the sibling test:

- one upload per patient (12 documents, not one bundle carrying several);
- a page count pinned per file;
- a weighed-day count pinned per patient, derived from the roster
  (`generate_population.ROSTER`) rather than duplicated as bare numbers --
  except the three OCR outcomes below, which this environment's Tesseract
  decided, not this file;
- the one patient (`600007`) whose BMI is absent, stated, on every day she
  is weighed, never a silent zero;
- the two failures `generate_population.py`'s docstring says actually land
  on `measurement`'s own failures route (the unitless weight and the
  OCR-degraded page), plus the third, unplanned one Tesseract added on its
  own (patient 9's plain OCR page) -- and, separately, the two quirks that
  do *not* produce a failure by the language's own design (patient 6's
  spelling miss is a quiet absence; patient 10's misfiled page reads and
  copies cleanly, just under the wrong patient);
- no cross-patient leakage, with that one misfiled page named as the
  deliberate, documented exception rather than silently excluded.

**Tesseract dependency.** Three assertions below (`_OCR_OUTCOMES`) pin
whatever this environment's installed Tesseract actually produces for
three synthetic, low-fidelity scanned pages. `tests/test_records_example.py`
already takes this posture for its own OCR page ("not pinned numerically
... a documents-package concern"); this file pins the *outcome* (did a
weight land or not, and if not, absent or a hard failure) without pinning
the OCR'd text itself, so a Tesseract upgrade that reads the same shapes
differently is the one way this file could need updating for a reason
that has nothing to do with the engine.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import time
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


def _load_module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, EXAMPLE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generate_population = _load_module("records_generate_population", "generate_population.py")

# ----------------------------------------------------------------- data --

EXPECTED_PAGES: dict[str, int] = {
    "600001": 22, "600002": 19, "600003": 11, "600004": 13,
    "600005": 11, "600006": 13, "600007": 11, "600008": 11,
    "600009": 12, "600010": 2, "600011": 1, "600012": 1,
}
"""From `generate_population.build_population()`'s own pdfium page counts --
asserted directly against a fresh build below, not just copied here."""

_NON_VITALS_KINDS = ("lab", "discharge", "fax_cover")

_OCR_OUTCOMES: dict[tuple[str, str], str] = {
    # (mrn, date) -> "succeeds" | "absent" | "fails"
    ("600001", "2024-01-27"): "succeeds",  # template A, plain OCR: reads fine.
    ("600005", "2023-11-28"): "absent",  # template C: "WEIGHT" merges into
    # the number, so `Weight:`'s alternative never matches at all --
    # `_Absent`, not a failure.
    ("600009", "2024-05-25"): "fails",  # template A: "Wt" survives OCR but
    # "57.4kg" does not parse as a number -- `_match_record` aborts the
    # whole row, a genuine `ExtractFailure`.
}
_DEGRADED_FAILS = True
"""Patient 1's deliberately degraded page (2024-07-01): low-contrast,
slightly rotated text. In this environment Tesseract misreads "Date:" as
"Date," (a comma, not a colon) badly enough that `measured_at`'s own
alternative never matches anywhere on the page -- the whole-page anchor
failure `uratori/documents/extract.py::run_extract` raises for a `many by
row` extract with no candidate row at all."""


def _expected_weighed_dates(patient: Any) -> set[str]:
    """Every date this patient's *own* file should contribute a weight
    for, replicating the engine's own rules rather than restating its
    answer: an administrative page (lab/discharge/fax cover) never
    reaches `measurement` (`page_class` never calls it vitals); a
    unitless or spelling-missed weight contributes nothing (a hard
    failure aborts the whole row; a miss is merely absent); a page whose
    header carries another patient's MRN contributes to *that* patient's
    set, never this one's; and an OCR page's fate is `_OCR_OUTCOMES`."""
    dates: set[str] = set()
    for visit in patient.visits:
        if visit.kind in _NON_VITALS_KINDS:
            continue
        if visit.header_mrn_override is not None:
            continue
        if visit.weight_unitless or visit.spelling_miss:
            continue
        if visit.kind == "ocr_degraded":
            if not _DEGRADED_FAILS:
                dates.add(visit.date)
            continue
        if visit.kind == "ocr_good":
            outcome = _OCR_OUTCOMES[(patient.mrn, visit.date)]
            if outcome == "succeeds":
                dates.add(visit.date)
            continue
        dates.add(visit.date)
    return dates


def _misfiled_dates(patient: Any) -> dict[str, str]:
    """date -> the MRN actually printed on that page, for every visit in
    this patient's *file* whose header lies about whose chart it is."""
    return {
        visit.date: visit.header_mrn_override
        for visit in patient.visits
        if visit.header_mrn_override is not None
    }


# ------------------------------------------------------------------ pins --


def test_the_population_is_byte_stable_across_runs() -> None:
    """Same claim, same reason, as `test_records_example.py`'s own: every
    `canvas.Canvas` takes `invariant=1` and every post-hoc `/Rotate` save
    has its pdfium-stamped id neutralised -- checked here with a real
    time gap between the two builds."""
    first = generate_population.build_population()
    time.sleep(1.1)
    second = generate_population.build_population()

    assert set(first.documents) == set(second.documents)
    for filename in first.documents:
        first_sha = hashlib.sha256(first.documents[filename]).hexdigest()
        second_sha = hashlib.sha256(second.documents[filename]).hexdigest()
        assert first_sha == second_sha, f"{filename} is not byte-stable across runs"


def test_the_roster_totals_match_the_page_budget() -> None:
    """The module docstring promises ~120-180 pages total, one file per
    patient, with a page count per file this test does not let drift
    without someone noticing."""
    population = generate_population.build_population()
    assert set(population.documents) == {
        f"patient-{p.mrn}.pdf" for p in generate_population.ROSTER
    }

    import pypdfium2 as pdfium

    total = 0
    for patient in generate_population.ROSTER:
        filename = f"patient-{patient.mrn}.pdf"
        doc = pdfium.PdfDocument(population.documents[filename])
        try:
            pages = len(doc)
        finally:
            doc.close()
        assert pages == EXPECTED_PAGES[patient.mrn], f"{filename}: {pages} pages"
        total += pages
    assert 120 <= total <= 180, f"total pages {total} outside the ~120-180 budget"


@dataclass
class RecordsServer:
    http: httpx.AsyncClient
    state: Any


async def _make_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[RecordsServer]:
    name = f"uratori_records_pop_{os.urandom(4).hex()}"
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


async def _load_population(http: httpx.AsyncClient) -> dict[str, str]:
    """Push the roster, upload one document per patient, return
    filename -> document id."""
    population = generate_population.build_population()
    roster = await http.post(
        "/tenants/t1/facts", json={"writes": {"patient": population.patients}}
    )
    assert roster.status_code == 200, roster.text
    document_ids: dict[str, str] = {}
    for filename, data in population.documents.items():
        up = await http.post(
            "/tenants/t1/documents/medical_record",
            files={"file": (filename, io.BytesIO(data), "application/pdf")},
        )
        assert up.status_code == 200, f"{filename}: {up.text}"
        document_ids[filename] = up.json()["id"]
    return document_ids


async def test_twelve_documents_one_per_patient(records_server: RecordsServer) -> None:
    http = records_server.http
    document_ids = await _load_population(http)
    assert len(document_ids) == 12

    docs = await http.get("/tenants/t1/documents/medical_record")
    assert docs.status_code == 200, docs.text
    by_title = {d["title"]: d for d in docs.json()["documents"]}
    for patient in generate_population.ROSTER:
        filename = f"patient-{patient.mrn}.pdf"
        assert filename in by_title, filename
        assert by_title[filename]["id"] == document_ids[filename]


async def test_measurements_per_patient_match_the_roster(records_server: RecordsServer) -> None:
    """`patient.weight`'s non-null days, per patient, against
    `_expected_weighed_dates` -- which is the roster's own rules, not a
    second copy of the answer."""
    http = records_server.http
    await _load_population(http)

    weight = await http.get("/tenants/t1/results/patient.weight")
    assert weight.status_code == 200, weight.text
    by_subject = {s["id"]: s["value"] for s in weight.json()["subjects"]}

    for patient in generate_population.ROSTER:
        expected = _expected_weighed_dates(patient)
        actual = {
            date.split("@", 1)[1]
            for date, value in by_subject.items()
            if date.startswith(f"{patient.mrn}@") and value is not None
        }
        # Add in whatever other patients' misfiled pages print this
        # patient's own MRN.
        for other in generate_population.ROSTER:
            for date, wrong_mrn in _misfiled_dates(other).items():
                if wrong_mrn == patient.mrn:
                    expected.add(date)
        assert actual == expected, (
            f"{patient.mrn}: expected weighed days {sorted(expected)}, got {sorted(actual)}"
        )


async def test_patient_ten_misfiled_page_lands_under_patient_one(
    records_server: RecordsServer,
) -> None:
    """The deliberate demonstration, named explicitly rather than merely
    absorbed into the roster-wide check above: patient 10's second visit
    prints patient 1's MRN, so `page_identity` reads patient 1's MRN,
    `measurement.patient_id` copies it verbatim, and the weight lands in
    patient 1's own series -- not because anything failed, but because
    nothing in the engine cross-checks a page's printed identity against
    the file it arrived in (D4)."""
    http = records_server.http
    await _load_population(http)

    weight = await http.get("/tenants/t1/results/patient.weight")
    by_subject = {s["id"]: s["value"] for s in weight.json()["subjects"]}

    assert by_subject.get("600001@2025-09-10") is not None, (
        "patient 10's misfiled page should land under patient 1"
    )
    assert "600010@2025-09-10" not in {
        sid for sid, v in by_subject.items() if v is not None
    }, "patient 10's own series must not also carry the misfiled page's weight"


async def test_patient_seven_bmi_is_absent_not_zero_every_day(
    records_server: RecordsServer,
) -> None:
    """Patient 7 never has a `height_cm` on record at all -- `latest(...)
    carried forward` has nothing to carry, and `patient.bmi`'s division
    (`uratori/engine/evaluate.py::_arith`) answers `None` for a
    non-numeric operand, never `0`. She is still weighed on every one of
    her 11 visits, so this is the sharpest version of the check: a
    present weight, an explicit `None` BMI, every single day."""
    http = records_server.http
    await _load_population(http)

    weight = await http.get("/tenants/t1/results/patient.weight")
    weight_rows = {
        s["id"]: s["value"]
        for s in weight.json()["subjects"]
        if s["id"].startswith("600007@")
    }
    assert sum(1 for v in weight_rows.values() if v is not None) == 11

    height = await http.get("/tenants/t1/results/patient.height")
    height_rows = [
        s for s in height.json()["subjects"] if s["id"].startswith("600007@")
    ]
    assert height_rows and all(s["value"] is None for s in height_rows)

    bmi = await http.get("/tenants/t1/results/patient.bmi")
    bmi_rows = {
        s["id"]: s["value"] for s in bmi.json()["subjects"] if s["id"].startswith("600007@")
    }
    # The rows exist -- stated -- for every weighed day; every one is None.
    for date in _expected_weighed_dates(
        next(p for p in generate_population.ROSTER if p.mrn == "600007")
    ):
        subject = f"600007@{date}"
        assert subject in bmi_rows, f"missing (not just null) bmi row for {subject}"
        assert bmi_rows[subject] is None


async def test_bmi_present_for_eleven_patients_absent_for_one(
    records_server: RecordsServer,
) -> None:
    http = records_server.http
    await _load_population(http)

    bmi = await http.get("/tenants/t1/results/patient.bmi")
    rows = bmi.json()["subjects"]
    non_null_mrns = {
        s["id"].split("@", 1)[0] for s in rows if s["value"] is not None
    }
    all_mrns = {p.mrn for p in generate_population.ROSTER}
    assert non_null_mrns == all_mrns - {"600007"}


async def test_the_three_measurement_failures_land_with_their_reasons(
    records_server: RecordsServer,
) -> None:
    """Exactly three hard failures on `measurement`'s own failures route
    in this environment -- the unitless weight (patient 9, by design) and
    the OCR-degraded page (patient 1, by design), plus the one Tesseract
    added on its own (patient 9's plain OCR page, `_OCR_OUTCOMES`). The
    other two roster quirks (patient 6's spelling miss, patient 10's
    misfile) are checked elsewhere because they are not failures at all
    -- an absence and a clean misfile, never an `ExtractFailure`."""
    http = records_server.http
    document_ids = await _load_population(http)

    failures = await http.get("/tenants/t1/extracts/measurement/failures")
    assert failures.status_code == 200, failures.text
    rows = failures.json()["failures"]
    assert len(rows) == 3, rows

    patient_1_id = document_ids["patient-600001.pdf"]
    patient_9_id = document_ids["patient-600009.pdf"]

    unitless = [
        r for r in rows if r["page_key"].startswith(f"{patient_9_id}/") and r["field"] == "weight_kg"
        and "no printed unit" in r["reason"]
    ]
    assert unitless, "expected patient 9's unitless-weight page to fail"

    degraded = [
        r for r in rows if r["page_key"].startswith(f"{patient_1_id}/")
    ]
    assert len(degraded) == 1
    assert degraded[0]["field"] == "measured_at"
    assert "no alternative matched" in degraded[0]["reason"]

    ocr_fail = [
        r
        for r in rows
        if r["page_key"].startswith(f"{patient_9_id}/")
        and r["field"] == "weight_kg"
        and "no number followed" in r["reason"]
    ]
    assert ocr_fail, "expected patient 9's plain OCR page to fail in this environment"


async def test_the_spelling_miss_is_an_absence_not_a_failure(
    records_server: RecordsServer,
) -> None:
    """Patient 6's `Body-Wt:` page: the label is a single token precisely
    so the per-word matcher's own case/colon-insensitive normalisation
    cannot rescue it by accidentally satisfying `Wt:` -- confirmed
    separately in `test_body_wt_would_have_matched_the_wt_alternative`
    below -- and it never shows up on the failures route, because a
    never-found alternative is `_Absent`, not an `ExtractFailure`."""
    http = records_server.http
    await _load_population(http)

    failures = await http.get("/tenants/t1/extracts/measurement/failures")
    rows = failures.json()["failures"]
    assert not any(
        r["reason"] for r in rows if "2023-12-01" in str(r.get("subject", ""))
    )

    weight = await http.get("/tenants/t1/results/patient.weight")
    by_subject = {s["id"]: s["value"] for s in weight.json()["subjects"]}
    # The bucketed day itself still exists (patient.weight enumerates every
    # day in its own patient-day universe) -- it is the *value* that is
    # absent, same posture as patient 7's BMI, never a missing row.
    assert by_subject.get("600006@2023-12-01") is None


def test_body_wt_would_have_matched_the_wt_alternative_if_not_hyphenated() -> None:
    """Why the spelling-miss page prints `Body-Wt:` and not `Body wt:`:
    the per-word matcher's own normalisation (`uratori/documents/
    extract.py::_normalize`) lowercases and strips a trailing colon, so a
    *bare* `wt` token anywhere on a line already equals `Wt:`'s own
    alternative -- `Body wt: 83 kg` would read as a completely ordinary
    weight, not a miss. Hyphenating it into one token (`Body-Wt:`) is what
    keeps this page a genuine spelling the bundle's three templates do
    not cover."""
    # `uratori.server` loaded first: importing `uratori.documents.extract`
    # as the very first thing trips a circular partial-import, since it
    # reaches back into `uratori.server.provenance` -> `uratori.server`
    # -> ... -> `uratori.audit.judge` -> `uratori.documents.extract` again.
    import uratori.server  # noqa: F401
    from uratori.documents.extract import _find_alternative
    from uratori.server.words import Word

    def words(texts: list[str]) -> list[Word]:
        return [
            Word(id=i, line=0, text=t, x0=0, y0=0, x1=1, y1=1, block=0, source="pdf", confidence=1.0)
            for i, t in enumerate(texts)
        ]

    assert _find_alternative(words(["Body", "wt:", "83", "kg"]), "Wt:") is not None
    assert _find_alternative(words(["Body-Wt:", "83", "kg"]), "Wt:") is None


async def test_no_cross_patient_leakage_except_the_documented_misfile(
    records_server: RecordsServer,
) -> None:
    """Every weighed day in every patient's own series is a day that
    patient's own file actually printed -- except the one page this
    bundle deliberately misfiles (patient 10's second visit, read above),
    named here rather than silently excluded."""
    http = records_server.http
    await _load_population(http)

    weight = await http.get("/tenants/t1/results/patient.weight")
    by_subject = {s["id"]: s["value"] for s in weight.json()["subjects"]}

    known_exception = {("600001", "2025-09-10")}

    for patient in generate_population.ROSTER:
        own_dates = {v.date for v in patient.visits if v.header_mrn_override is None}
        for sid, value in by_subject.items():
            if not sid.startswith(f"{patient.mrn}@") or value is None:
                continue
            mrn, date = sid.split("@", 1)
            if (mrn, date) in known_exception:
                continue
            assert date in own_dates, (
                f"{sid}: a weight attributed to {mrn} on a day its own file never printed"
            )
