"""`extract` end to end: upload, classify, extract, compute, trace.

Package 3c of the documents plan (documents-plan-v3, D4): `run_pass` wired
into every server entry point, over a real Postgres and a real filesystem
blob store, with fixture PDFs built by reportlab (as `tests/pdf_fixtures.py`
does for package 1b). This is the BMI story the MR's release notes walk:
upload a page, `page_identity` + `page_class` + `measurement` extract it,
`patient.bmi` computes from the derived records, and a reader can trace the
number back to the words it came from.

**Finding, not a deviation of this package's own scope:** D5's
`patient.height`/`patient.weight`/`patient.bmi` figures are scoped to a
`patient` fact kind (`figure patient.height bucketed: ...`), and the real
checker requires that scope to be a *declared* fact kind -- confirmed here,
and separately, that a bucketed figure's roster is the subject kind's own
fact records: a `measurement` record naming `patient_id "004412"` with no
`fact patient "004412"` ever written serves **zero subjects**, silently.
Nothing in D4 or D5 as written populates that roster (`page_identity`
extracts the identifier onto `measurement`'s own rows, never onto a
`patient` record). This test supplies it directly, as an ordinary host
write, because closing that gap for real (a fourth extract? a roster
derived from `page_identity` itself?) is a package 4/5 design decision, not
a package 3 one -- flagged in the package report.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import pytest

from uratori import Schema
from uratori.server import create_app, db

WORLD = Schema(kinds=frozenset())

SOURCE = """
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title

# One page of one.
fact medical_record_page as page of medical_record

# Who a page says it is about: the identifier printed in its header.
# Read once, from the printed identifier.
extract page_identity from medical_record_page:
    patient_id = text after any of ["MRN:", "Patient ID:"]

# What kind of page this is. A page is "vitals" if it says so anywhere on
# it.
extract page_class from medical_record_page:
    type = "vitals" if page contains any of ["VITAL SIGNS", "Vitals", "Wt:"]

filter page_class.vitals keyed as medical_record_page where type == "vitals"

# Vitals read off one page: as many rows as the page carries.
extract measurement from medical_record_page:
    over page_class.vitals
    many by row up to 5
    patient_id  = page_identity.patient_id
    measured_at = date after any of ["Date:", "Visit date", "DOS"]
    weight_kg   = number after any of ["Weight:", "Wt:", "Wt", "WEIGHT"] in kg or lb
    height_cm   = number after any of ["Height:", "Ht:", "Ht", "HEIGHT"] in cm or in or ft_in

# Who vitals are tracked for -- see the module docstring: nothing in D4/D5
# populates this roster today, so the test writes it directly.
fact patient:
    mrn as text

group measurement.by_patient_day from (patient_id, measured_at by day in "UTC")
filter measurement.weighed where weight_kg is set
filter measurement.heighted where height_cm is set

# The height in force each day: the latest one measured, carried across the
# days nobody measured it.
figure patient.height bucketed:
    display "{patient} height"
    unit decimal
    depends:
        measured = measurement.by_patient_day:{patient} & measurement.heighted
    calculate:
        latest(measurement.height_cm over measured) carried forward

# Weight on each day one was taken.
figure patient.weight bucketed:
    display "{patient} weight"
    unit decimal
    depends:
        weighed = measurement.by_patient_day:{patient} & measurement.weighed
    calculate:
        latest(measurement.weight_kg over weighed)

# Body-mass index on each day, from that day's weight and the height in
# force.
figure patient.bmi bucketed:
    display "{patient} BMI"
    unit decimal
    calculate:
        patient.weight:{bucket} / ((patient.height:{bucket} / 100) * (patient.height:{bucket} / 100))
"""

SOURCE_WITH_AUDIT = (
    SOURCE
    + """
# A second reader over the vitals on each page -- for the evidence-surface
# staleness test below (review finding C, residual on Source.audits).
audit medical_record_page.vitals_audit:
    verifies measurement
    model "fake-v1"
    display "{medical_record_page} {value}"
"""
)


def vitals_pdf(rows: list[tuple[str, str, str | None]]) -> bytes:
    """One page per row: `MRN:`, `VITAL SIGNS`, `Date:`, `Wt:` and
    (optionally) `Ht:` -- real reportlab PDF bytes, a real text layer, real
    word boxes once ingested, so the trace this test makes all the way to
    `Source.boxes` is over the genuine pipeline, not a stand-in for it."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for date, weight, height in rows:
        c.drawString(72, 700, "Patient Chart")
        c.drawString(72, 680, "VITAL SIGNS")
        c.drawString(72, 660, "MRN: 004412")
        line = f"Date: {date}  Wt: {weight} kg"
        if height is not None:
            line += f"  Ht: {height} cm"
        c.drawString(72, 640, line)
        c.showPage()
    c.save()
    return buf.getvalue()


@dataclass
class ExtractServer:
    http: httpx.AsyncClient
    pg_dsn: str
    schema: str


async def _make_server(
    pg_dsn: str, tmp_path: Path, *, source: str = SOURCE
) -> AsyncIterator[ExtractServer]:
    name = f"uratori_extract_{os.urandom(4).hex()}"
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
            put = await http.put("/definitions", json={"source": source})
            assert put.status_code == 200, put.text
            yield ExtractServer(http=http, pg_dsn=pg_dsn, schema=name)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def extract_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[ExtractServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


@pytest.fixture
async def extract_server_with_audit(
    pg_dsn: str, tmp_path: Path
) -> AsyncIterator[ExtractServer]:
    async for server in _make_server(pg_dsn, tmp_path, source=SOURCE_WITH_AUDIT):
        yield server


async def _upload_and_identify_patient(http: httpx.AsyncClient) -> str:
    """Upload the two-visit bundle, then write the `patient` roster record
    the module docstring explains -- a host write, through the ordinary
    facts door, same as any fact a provider pushes."""
    pdf = vitals_pdf(
        [
            ("2024-01-02", "80", "178"),
            ("2024-06-01", "82", None),
        ]
    )
    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart.pdf", pdf, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["pages"] == 2
    put = await http.post(
        "/tenants/t1/facts", json={"writes": {"patient": {"004412": {"mrn": "004412"}}}}
    )
    assert put.status_code == 200, put.text
    return str(up.json()["id"])


async def _fact_rows(http: httpx.AsyncClient, kind: str) -> dict[str, dict]:
    """Every record of a kind, read back through the built-in UI's own
    facts page (`/ui/api/tenants/{t}/facts/{kind}`) -- the authenticated
    API has no symmetric GET for a whole kind (a host already knows what
    it wrote; reading a *derived* kind back this way is exactly this
    test's job). The dev UI is mounted unconditionally here: `create_app`
    is given no token, and `URATORI_UI` defaults on whenever none is set.
    """
    resp = await http.get(f"/ui/api/tenants/t1/facts/{kind}")
    assert resp.status_code == 200, resp.text
    return {row["key"]: row["value"] for row in resp.json()["records"]}


async def test_pages_are_classified_and_identified(extract_server: ExtractServer) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)

    bodies = await _fact_rows(http, "page_identity")
    assert len(bodies) == 2
    assert {v["patient_id"] for v in bodies.values()} == {"004412"}

    class_bodies = await _fact_rows(http, "page_class")
    assert {v["type"] for v in class_bodies.values()} == {"vitals"}


async def test_measurement_rows_are_extracted_with_the_right_units(
    extract_server: ExtractServer,
) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)

    rows = await _fact_rows(http, "measurement")
    assert len(rows) == 2
    by_date = {v["measured_at"][:10]: v for v in rows.values()}
    assert by_date["2024-01-02"]["weight_kg"] == 80.0
    assert by_date["2024-01-02"]["height_cm"] == 178.0
    assert by_date["2024-06-01"]["weight_kg"] == 82.0
    assert "height_cm" not in by_date["2024-06-01"] or by_date["2024-06-01"]["height_cm"] is None
    for v in rows.values():
        assert v["patient_id"] == "004412"
        assert v["page"]


async def test_bmi_computes_with_height_carried_forward(extract_server: ExtractServer) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)

    # A bucketed figure answers one `Subject` row per (subject, bucket) --
    # `id` is the composite `<subject>@<bucket label>` key, `dimension`
    # carries the bucket label on its own. `subject`/`trailing` are a
    # *reading*'s pooling and windowing arguments and are refused on a
    # figure (`Uratori.answer`'s own docstring); the client filters the
    # flat list for the one row it wants.
    bmi = await http.get("/tenants/t1/results/patient.bmi")
    assert bmi.status_code == 200, bmi.text
    body = bmi.json()
    day2 = next(s for s in body["subjects"] if s["id"] == "004412@2024-06-01")
    assert day2["value"] is not None
    expected = 82.0 / ((178.0 / 100) ** 2)
    assert abs(day2["value"] - expected) < 0.05


async def test_evidence_walks_bmi_to_weight_and_height_to_the_page_and_its_boxes(
    extract_server: ExtractServer,
) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)

    bmi_day2 = "004412@2024-06-01"
    evidence = await http.get(
        "/tenants/t1/evidence/patient.bmi", params={"subject": bmi_day2}
    )
    assert evidence.status_code == 200, evidence.text
    bmi_evidence = evidence.json()
    assert bmi_evidence["parts"] is True
    figures_cited = {m["figure"] for m in bmi_evidence["members"]}
    assert figures_cited == {"patient.weight", "patient.height"}

    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": bmi_day2}
    )
    assert weight_evidence.status_code == 200, weight_evidence.text
    w_members = weight_evidence.json()["members"]
    assert len(w_members) == 1
    [w_member] = w_members
    assert w_member["held"] is True
    [source] = w_member["sources"]
    assert source["field"] == "weight_kg"
    assert source["anchored"] is True
    assert len(source["boxes"]) >= 1
    assert "82" in (source["printed"] or "")

    # Height was measured on day 1, carried forward to day 2 -- its evidence
    # must cite the ORIGINAL record (day 1's page), not a day-2 fabrication.
    height_evidence = await http.get(
        "/tenants/t1/evidence/patient.height", params={"subject": bmi_day2}
    )
    assert height_evidence.status_code == 200, height_evidence.text
    h_members = height_evidence.json()["members"]
    [h_member] = h_members
    [h_source] = h_member["sources"]
    assert h_source["field"] == "height_cm"
    assert "178" in (h_source["printed"] or "")


async def test_the_facts_route_refuses_a_direct_write_or_delete_of_a_derived_kind(
    extract_server: ExtractServer,
) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)

    write = await http.post(
        "/tenants/t1/facts",
        json={"writes": {"measurement": {"fake": {"patient_id": "x"}}}},
    )
    assert write.status_code == 422, write.text
    assert "extract" in write.json()["detail"]

    delete = await http.post(
        "/tenants/t1/facts", json={"deletes": {"page_identity": ["d1/p0001"]}}
    )
    assert delete.status_code == 422, delete.text


async def test_a_warm_pass_after_the_upload_leaves_the_same_derived_rows(
    extract_server: ExtractServer,
) -> None:
    """Full-vs-warm parity (documents-plan-v3, D4): a pass that extracts
    nothing new must not rewrite -- or lose -- what the upload's own pass
    already produced."""
    http = extract_server.http
    await _upload_and_identify_patient(http)

    before_rows = await _fact_rows(http, "measurement")
    assert before_rows  # the fixture upload must actually have produced rows

    warm = await http.post("/tenants/t1/runs", json={})
    assert warm.status_code == 200, warm.text
    assert await _fact_rows(http, "measurement") == before_rows

    full = await http.post("/tenants/t1/runs", json={"full": True})
    assert full.status_code == 200, full.text
    assert await _fact_rows(http, "measurement") == before_rows


async def test_extract_failures_route_answers_with_the_pages_word_layer(
    extract_server: ExtractServer,
) -> None:
    """A page with no vitals produces no measurement row and no failure
    (`over` simply excludes it) -- but a page gated IN that is missing a
    required field does, and the failures route hands back enough to fix
    the declaration: the reason, the field, and the page's own words."""
    http = extract_server.http
    await _upload_and_identify_patient(http)
    # A second document whose only page says VITAL SIGNS/Wt but never
    # Date -- the date alternative is simply never on this page.
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "VITAL SIGNS")
    c.drawString(72, 680, "MRN: 004412")
    c.drawString(72, 660, "Wt: 80 kg")
    c.showPage()
    c.save()

    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart2.pdf", buf.getvalue(), "application/pdf")},
    )
    assert up.status_code == 200, up.text

    failures = await http.get("/tenants/t1/extracts/measurement/failures")
    assert failures.status_code == 200, failures.text
    body = failures.json()
    assert body["extract"] == "measurement"
    assert len(body["failures"]) >= 1
    failure = body["failures"][0]
    assert failure["field"] == "measured_at"
    assert "no alternative matched" in failure["reason"]
    assert any(w["text"] == "VITAL" or w["text"] == "SIGNS" for w in failure["words"])


# ----------------------------------------------- facade-level unit tests --
#
# No server, no Postgres: `Uratori`'s own construction-time refusal, and
# `availability`'s `behind-deploy` for a figure over a derived kind with a
# cold extract pointer. Both are engine-side claims and are checked against
# the engine directly, over the in-memory pair.

_SMALL_SOURCE = """
# A document kind, so `medical_record_page` is a page shape.
fact medical_record as document:
    name title

# Its one page kind.
fact medical_record_page as page of medical_record

# What kind of page this is. A page is "vitals" if it says so.
extract page_class from medical_record_page:
    type = "vitals" if page contains any of ["Wt:"]

filter page_class.vitals keyed as medical_record_page where type == "vitals"
group medical_record_page.classification keyed as medical_record_page from document_id

# How many pages of this document are vitals pages.
figure medical_record.vitals_pages:
    display "{medical_record} vitals pages"
    depends:
        mine = medical_record_page.classification:{medical_record} & page_class.vitals
    calculate:
        count(mine)
"""


def test_uratori_refuses_a_library_with_extracts() -> None:
    from uratori import MemoryEngineStore, MemoryFactStore, Uratori, compile_source

    library = compile_source(_SMALL_SOURCE, WORLD)
    assert library.extracts  # the fixture is pointless if this compiled to none
    with pytest.raises(ValueError, match="server feature"):
        Uratori(
            schema=WORLD,
            library=library,
            store=MemoryEngineStore(),
            facts=MemoryFactStore(),
        )


async def test_a_figure_over_a_derived_kind_with_a_cold_extract_pointer_is_behind_deploy() -> None:
    from uratori import MemoryEngineStore, MemoryFactStore, Pointer, compile_source
    from uratori.engine.serve import availability

    library = compile_source(_SMALL_SOURCE, WORLD)
    store = MemoryEngineStore()
    facts = MemoryFactStore()
    tenant = "t1"
    figure_plan = library.figure("medical_record.vitals_pages")
    assert figure_plan is not None

    facts.put(tenant, "medical_record", "d1", {"title": "chart"})
    facts.put(tenant, "medical_record_page", "d1/p0001", {"document_id": "d1", "number": 1})
    facts.put(tenant, "page_class", "d1/p0001", {"type": "vitals"})
    extract_plan = library.extracts["page_class"]

    # No pointer at all yet: never-computed, not behind-deploy.
    never = await availability(store, library, tenant, figure_plan)
    assert never.ok is False
    assert never.because == "never-computed"  # type: ignore[union-attr]

    # The pointer names a version the current build does not hold.
    await store.set_pointer(
        tenant, "page_class", Pointer(version="stale-version", settings_fingerprint="")
    )
    await store.set_pointer(tenant, figure_plan.name, Pointer(version=figure_plan.version, settings_fingerprint=""))
    await store.set_buckets(tenant, "page_class.vitals", "d1/p0001", [""])
    await store.set_buckets(
        tenant, "medical_record_page.classification", "d1/p0001", ["d1"]
    )

    stale = await availability(store, library, tenant, figure_plan)
    assert stale.ok is False
    assert stale.because == "behind-deploy"  # type: ignore[union-attr]
    assert "page_class" in (stale.detail or "")  # type: ignore[union-attr]

    # Moving the pointer to the current version clears it.
    await store.set_pointer(
        tenant, "page_class", Pointer(version=extract_plan.version, settings_fingerprint="")
    )
    fresh = await availability(store, library, tenant, figure_plan)
    assert fresh.ok is True


_RETIRED_EXTRACT_BLOCK = """
# Vitals read off one page: as many rows as the page carries.
extract measurement from medical_record_page:
    over page_class.vitals
    many by row up to 5
    patient_id  = page_identity.patient_id
    measured_at = date after any of ["Date:", "Visit date", "DOS"]
    weight_kg   = number after any of ["Weight:", "Wt:", "Wt", "WEIGHT"] in kg or lb
    height_cm   = number after any of ["Height:", "Ht:", "Ht", "HEIGHT"] in cm or in or ft_in

# Who vitals are tracked for -- see the module docstring: nothing in D4/D5
# populates this roster today, so the test writes it directly.
fact patient:
    mrn as text

group measurement.by_patient_day from (patient_id, measured_at by day in "UTC")
filter measurement.weighed where weight_kg is set
filter measurement.heighted where height_cm is set

# The height in force each day: the latest one measured, carried across the
# days nobody measured it.
figure patient.height bucketed:
    display "{patient} height"
    unit decimal
    depends:
        measured = measurement.by_patient_day:{patient} & measurement.heighted
    calculate:
        latest(measurement.height_cm over measured) carried forward

# Weight on each day one was taken.
figure patient.weight bucketed:
    display "{patient} weight"
    unit decimal
    depends:
        weighed = measurement.by_patient_day:{patient} & measurement.weighed
    calculate:
        latest(measurement.weight_kg over weighed)

# Body-mass index on each day, from that day's weight and the height in
# force.
figure patient.bmi bucketed:
    display "{patient} BMI"
    unit decimal
    calculate:
        patient.weight:{bucket} / ((patient.height:{bucket} / 100) * (patient.height:{bucket} / 100))
"""

# Retiring `measurement` must retire every declaration that reads it too,
# not just swap out its `extract`: under D4.4 the kind lives only inside
# the extract that defines it, so there is no `fact measurement:` left
# standing once the extract is gone for a group, filter or figure to
# type-check against -- unlike the old fact-plus-extract shape, where the
# fact stayed declared (and so did everything reading it) while only the
# extract's own production stopped. `patient` stays declared, since the
# test still writes it directly through the facts route; nothing else in
# this tail survives the retirement.
_RETIRED_REPLACEMENT = """
# Who vitals are tracked for -- see the module docstring: nothing in D4/D5
# populates this roster today, so the test writes it directly.
fact patient:
    mrn as text
"""
RETIRED_SOURCE = SOURCE.replace(_RETIRED_EXTRACT_BLOCK, _RETIRED_REPLACEMENT)
assert RETIRED_SOURCE != SOURCE, "the block to strip must match the source exactly"


async def test_retiring_an_extract_deletes_its_rows_at_the_next_pass(
    extract_server: ExtractServer,
) -> None:
    http = extract_server.http
    await _upload_and_identify_patient(http)
    assert await _fact_rows(http, "measurement")  # something to retire

    redeploy = await http.put("/definitions", json={"source": RETIRED_SOURCE})
    assert redeploy.status_code == 200, redeploy.text

    run = await http.post("/tenants/t1/runs", json={})
    assert run.status_code == 200, run.text

    assert await _fact_rows(http, "measurement") == {}
    # page_identity and page_class are untouched -- only the retired
    # extract's own kind is swept.
    assert await _fact_rows(http, "page_identity")


async def test_a_redefined_audits_stale_finding_does_not_render_on_the_evidence_surface(
    extract_server_with_audit: ExtractServer,
) -> None:
    """Residual of review finding C: `provenance.py::audits_for_field`
    feeds `Source.audits` (the evidence route's per-field citation), and
    reads `db.audit_findings_citing` by record alone, exactly like
    `ui.py`'s `cited_audits` did before finding C's fix -- the identical
    staleness bug on a second surface. Redefine the audit and a weight
    cell's evidence must stop showing the previous version's `disagrees`
    finding as current, the moment the redefinition takes effect, not
    only once an operator happens to run a full pass."""
    http = extract_server_with_audit.http
    await _upload_and_identify_patient(http)

    bmi_day2 = "004412@2024-06-01"
    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": bmi_day2}
    )
    assert weight_evidence.status_code == 200, weight_evidence.text
    [w_member] = weight_evidence.json()["members"]
    record_key = w_member["key"]
    [source] = w_member["sources"]
    page_key = source["page_key"]

    defs = await http.get("/definitions")
    [audit] = defs.json()["audits"]
    old_version = audit["version"]

    rows = await _fact_rows(http, "medical_record_page")
    words_sha = str(rows[page_key]["words_sha"])

    connection = await asyncpg.connect(
        extract_server_with_audit.pg_dsn,
        server_settings={"search_path": extract_server_with_audit.schema},
    )
    try:
        await db.replace_audit_reading(
            connection,
            "t1",
            "medical_record_page.vitals_audit",
            old_version,
            page_key,
            words_sha=words_sha,
            prompt="read the weight",
            model="fake-v1",
            response="...",
            parsed=[
                {
                    "extract": "measurement",
                    "field": "weight_kg",
                    "row": 0,
                    "status": "not_on_page",
                    "words": [],
                    "box": None,
                    "seen_text": None,
                    "anchored": True,
                }
            ],
        )
    finally:
        await connection.close()

    run = await http.post("/tenants/t1/runs", json={"full": True})
    assert run.status_code == 200, run.text

    # Sanity: the stale-version check below is meaningful only if the
    # evidence surface actually shows the finding while it is current.
    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": bmi_day2}
    )
    [w_member] = weight_evidence.json()["members"]
    assert w_member["key"] == record_key
    [source] = w_member["sources"]
    assert [a["verdict"] for a in source["audits"]] == ["disagrees"]

    redefined = SOURCE_WITH_AUDIT.replace('model "fake-v1"', 'model "fake-v2"')
    put = await http.put("/definitions", json={"source": redefined})
    assert put.status_code == 200, put.text
    [new_audit] = put.json()["audits"]
    assert new_audit["version"] != old_version

    # A warm pass: no reading exists yet for the new version, so nothing
    # has re-judged this page under it at all.
    run = await http.post("/tenants/t1/runs", json={})
    assert run.status_code == 200, run.text

    weight_evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": bmi_day2}
    )
    [w_member] = weight_evidence.json()["members"]
    [source] = w_member["sources"]
    assert source["audits"] == []
