"""`audit` end to end: an upload, an extract, a stored reading, a verdict.

Package 5c of the documents plan (documents-plan-v3, D6): `run_audits`
wired into `run_pass`, over a real Postgres and a real filesystem blob
store. No provider and no worker here (5d) -- the reading a worker would
have produced is written directly through `uratori.server.db`, exactly the
shape `uratori/audit/judge.py`'s `AuditReading`/`FieldReading` describe, so
this pins the pass-side half independently of whether a model ever runs.

Verdicts are read back straight from `figure_value` (an audit's value
lives there, like a figure's -- `Engine.accept` calls the same
`EngineStore.save`) rather than through a results route, because the
surfaces that would render one nicely (`GET /tenants/{t}/audits/.../
findings`, the record page) are 5e's work, not yet built.
"""

from __future__ import annotations

import io
import json
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

# Vitals read off one page.
extract measurement from medical_record_page:
    weight_kg = number after any of ["Weight:", "Wt:"] in kg

# A second reader over the vitals on each page.
audit medical_record_page.vitals_audit:
    verifies measurement
    model "fake-v1"
    display "{medical_record_page} {value}"
"""


def vitals_pdf(weight_kg: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, f"Weight: {weight_kg} kg")
    c.showPage()
    c.save()
    return buf.getvalue()


def blank_pdf() -> bytes:
    """A page with nothing the `measurement` extract's patterns can match --
    the recall-gap case D6 exists to catch."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "Patient Chart")
    c.drawString(72, 680, "No vitals recorded this visit.")
    c.showPage()
    c.save()
    return buf.getvalue()


@dataclass
class AuditServer:
    http: httpx.AsyncClient
    pg_dsn: str
    schema: str
    audit_version: str


async def _make_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[AuditServer]:
    name = f"uratori_audit_{os.urandom(4).hex()}"
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
            put = await http.put("/definitions", json={"source": SOURCE})
            assert put.status_code == 200, put.text
            defs = await http.get("/definitions")
            assert defs.status_code == 200, defs.text
            audits = defs.json()["audits"]
            assert len(audits) == 1
            yield AuditServer(
                http=http, pg_dsn=pg_dsn, schema=name, audit_version=audits[0]["version"]
            )

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def audit_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[AuditServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


async def _fact_rows(http: httpx.AsyncClient, kind: str) -> dict[str, dict]:
    resp = await http.get(f"/ui/api/tenants/t1/facts/{kind}")
    assert resp.status_code == 200, resp.text
    return {row["key"]: row["value"] for row in resp.json()["records"]}


async def _upload(http: httpx.AsyncClient, weight_kg: str) -> tuple[str, str]:
    pdf = vitals_pdf(weight_kg)
    before = set(await _fact_rows(http, "medical_record_page"))
    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart.pdf", pdf, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    document_id = str(up.json()["id"])
    pages = await _fact_rows(http, "medical_record_page")
    new_pages = set(pages) - before
    assert len(new_pages) == 1
    page_key = next(iter(new_pages))
    return document_id, page_key


async def _word_id(http: httpx.AsyncClient, document_id: str, text: str) -> int:
    resp = await http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words"
    )
    assert resp.status_code == 200, resp.text
    for w in resp.json()["words"]:
        if w["text"].rstrip(".,") == text:
            return int(w["id"])
    raise AssertionError(f"no word {text!r} on the page: {resp.json()}")


async def _words_sha(http: httpx.AsyncClient, kind: str, key: str) -> str:
    rows = await _fact_rows(http, kind)
    return str(rows[key]["words_sha"])


async def _insert_reading(
    server: AuditServer,
    *,
    page_key: str,
    words_sha: str,
    status: str,
    words: list[int] | None = None,
    seen_text: str | None = None,
) -> None:
    connection = await asyncpg.connect(server.pg_dsn, server_settings={"search_path": server.schema})
    try:
        await db.replace_audit_reading(
            connection,
            "t1",
            "medical_record_page.vitals_audit",
            server.audit_version,
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
                    "status": status,
                    "words": words or [],
                    "box": None,
                    "seen_text": seen_text,
                    "anchored": True,
                }
            ],
        )
    finally:
        await connection.close()


async def _audit_value(server: AuditServer, page_key: str) -> object:
    connection = await asyncpg.connect(server.pg_dsn, server_settings={"search_path": server.schema})
    try:
        row = await connection.fetchrow(
            "select value from figure_value where tenant_id = 't1' and name = $1 "
            "and version = $2 and subject_id = $3",
            "medical_record_page.vitals_audit",
            server.audit_version,
            page_key,
        )
    finally:
        await connection.close()
    if row is None:
        return None
    return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]


async def _findings(server: AuditServer, page_key: str) -> list[dict]:
    connection = await asyncpg.connect(server.pg_dsn, server_settings={"search_path": server.schema})
    try:
        rows = await connection.fetch(
            "select extract, field, verdict, seen, extracted from audit_finding "
            "where tenant_id = 't1' and audit = $1 and page_key = $2",
            "medical_record_page.vitals_audit",
            page_key,
        )
    finally:
        await connection.close()
    out = []
    for r in rows:
        d = dict(r)
        d["seen"] = json.loads(d["seen"]) if d["seen"] is not None else None
        d["extracted"] = json.loads(d["extracted"]) if d["extracted"] is not None else None
        out.append(d)
    return out


async def test_a_page_with_no_reading_is_unaudited(audit_server: AuditServer) -> None:
    http = audit_server.http
    _document_id, page_key = await _upload(http, "82")
    value = await _audit_value(audit_server, page_key)
    assert value == "unaudited"


async def test_a_matching_reading_agrees(audit_server: AuditServer) -> None:
    http = audit_server.http
    document_id, page_key = await _upload(http, "82")
    word_id = await _word_id(http, document_id, "82")
    words_sha = await _words_sha(http, "medical_record_page", page_key)

    await _insert_reading(
        audit_server,
        page_key=page_key,
        words_sha=words_sha,
        status="seen",
        words=[word_id],
        seen_text="82",
    )

    run = await http.post("/tenants/t1/runs", json={"full": True})
    assert run.status_code == 200, run.text

    assert await _audit_value(audit_server, page_key) == "agrees"
    findings = await _findings(audit_server, page_key)
    assert len(findings) == 1
    assert findings[0]["verdict"] == "agrees"
    assert findings[0]["seen"] in (82, 82.0)


async def test_a_disagreeing_reading_judges_disagrees(audit_server: AuditServer) -> None:
    http = audit_server.http
    _document_id, page_key = await _upload(http, "82")
    words_sha = await _words_sha(http, "medical_record_page", page_key)

    await _insert_reading(
        audit_server,
        page_key=page_key,
        words_sha=words_sha,
        status="not_on_page",
    )

    run = await http.post("/tenants/t1/runs", json={"full": True})
    assert run.status_code == 200, run.text

    assert await _audit_value(audit_server, page_key) == "disagrees"


async def test_the_findings_route_serves_disputed_pages_only(audit_server: AuditServer) -> None:
    http = audit_server.http
    _document_id, agreeing_page = await _upload(http, "82")
    word_id = await _word_id(http, _document_id, "82")
    words_sha = await _words_sha(http, "medical_record_page", agreeing_page)
    await _insert_reading(
        audit_server, page_key=agreeing_page, words_sha=words_sha, status="seen",
        words=[word_id], seen_text="82",
    )

    _document_id_2, disputed_page = await _upload(http, "90")
    words_sha_2 = await _words_sha(http, "medical_record_page", disputed_page)
    await _insert_reading(
        audit_server, page_key=disputed_page, words_sha=words_sha_2, status="not_on_page",
    )

    run = await http.post("/tenants/t1/runs", json={"full": True})
    assert run.status_code == 200, run.text
    assert await _audit_value(audit_server, agreeing_page) == "agrees"
    assert await _audit_value(audit_server, disputed_page) == "disagrees"

    findings = await http.get(
        "/tenants/t1/audits/medical_record_page.vitals_audit/findings"
    )
    assert findings.status_code == 200, findings.text
    body = findings.json()
    assert body["audit"] == "medical_record_page.vitals_audit"
    assert body["verdict_counts"].get("agrees") == 1
    assert body["verdict_counts"].get("disagrees") == 1
    pages = {f["page_key"] for f in body["findings"]}
    assert pages == {disputed_page}


async def test_deleting_the_page_removes_its_audit_value(audit_server: AuditServer) -> None:
    http = audit_server.http
    document_id, page_key = await _upload(http, "82")
    word_id = await _word_id(http, document_id, "82")
    words_sha = await _words_sha(http, "medical_record_page", page_key)
    await _insert_reading(
        audit_server, page_key=page_key, words_sha=words_sha, status="seen",
        words=[word_id], seen_text="82",
    )
    await http.post("/tenants/t1/runs", json={"full": True})
    assert await _audit_value(audit_server, page_key) == "agrees"

    delete = await http.delete(f"/tenants/t1/documents/medical_record/{document_id}")
    assert delete.status_code == 200, delete.text

    assert await _audit_value(audit_server, page_key) is None


async def test_a_page_with_nothing_to_match_is_unaudited_after_a_warm_pass(
    audit_server: AuditServer,
) -> None:
    """Finding A (review F1): a page whose verified extract produces zero
    rows never lands in `moved`/`vanished` (those are keyed by the
    extract's own output kind, never the page kind), so the ordinary,
    non-full upload pass must not skip the page's audit roster -- rule 3
    says `unaudited` is owed the moment the pass touches the page, not
    only after the next explicit full pass."""
    http = audit_server.http
    before = set(await _fact_rows(http, "medical_record_page"))
    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("blank.pdf", blank_pdf(), "application/pdf")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["run"]["changed"] >= 0
    pages = await _fact_rows(http, "medical_record_page")
    new_pages = set(pages) - before
    assert len(new_pages) == 1
    page_key = next(iter(new_pages))

    # The upload route's own pass is a warm one (no `full`); nothing else
    # runs a pass here.
    assert await _audit_value(audit_server, page_key) == "unaudited"


async def test_redefining_an_audit_gives_every_page_a_row_on_the_next_warm_pass(
    audit_server: AuditServer,
) -> None:
    """Finding A (review F1), second half: redefining an audit (a new
    version, same pages in scope) must not leave the new version's row
    absent until an operator happens to run a `full` pass -- the next
    warm pass must roster every page the audit covers."""
    http = audit_server.http
    _document_id, page_key = await _upload(http, "82")
    assert await _audit_value(audit_server, page_key) == "unaudited"

    redefined = SOURCE.replace('model "fake-v1"', 'model "fake-v2"')
    put = await http.put("/definitions", json={"source": redefined})
    assert put.status_code == 200, put.text
    new_version = put.json()["audits"][0]["version"]
    assert new_version != audit_server.audit_version

    run = await http.post("/tenants/t1/runs", json={})
    assert run.status_code == 200, run.text

    connection = await asyncpg.connect(
        audit_server.pg_dsn, server_settings={"search_path": audit_server.schema}
    )
    try:
        row = await connection.fetchrow(
            "select value from figure_value where tenant_id = 't1' and name = $1 "
            "and version = $2 and subject_id = $3",
            "medical_record_page.vitals_audit",
            new_version,
            page_key,
        )
    finally:
        await connection.close()
    assert row is not None, "no row for the redefined audit's version after a warm pass"
    assert json.loads(row["value"]) == "unaudited"


async def test_a_redefined_audits_stale_finding_does_not_render_as_current(
    audit_server: AuditServer,
) -> None:
    """Finding C (review F3): `AboutOut.cited_audits` (`db.audit_findings_
    citing`) read every `audit_finding` row naming this record with no
    version check at all. Redefine the audit and the record's "verdicts
    citing it" section kept showing the previous version's `disagrees`
    finding as if it were live, with no way for a reader to know the
    audit has since moved and not yet re-read this page."""
    http = audit_server.http
    _document_id, page_key = await _upload(http, "82")
    words_sha = await _words_sha(http, "medical_record_page", page_key)
    await _insert_reading(
        audit_server, page_key=page_key, words_sha=words_sha, status="not_on_page",
    )
    run = await http.post("/tenants/t1/runs", json={"full": True})
    assert run.status_code == 200, run.text
    assert await _audit_value(audit_server, page_key) == "disagrees"

    # Sanity: the stale-version check below is meaningful only if the
    # record page actually shows the finding while it is current.
    about = await http.get(f"/ui/api/tenants/t1/about/measurement/{page_key}")
    assert about.status_code == 200, about.text
    assert len(about.json()["cited_audits"]) == 1

    redefined = SOURCE.replace('model "fake-v1"', 'model "fake-v2"')
    put = await http.put("/definitions", json={"source": redefined})
    assert put.status_code == 200, put.text
    new_version = put.json()["audits"][0]["version"]
    assert new_version != audit_server.audit_version

    # A warm pass: no reading exists yet for the new version, so nothing
    # has re-judged this page under the new version at all.
    run = await http.post("/tenants/t1/runs", json={})
    assert run.status_code == 200, run.text

    about = await http.get(f"/ui/api/tenants/t1/about/measurement/{page_key}")
    assert about.status_code == 200, about.text
    assert about.json()["cited_audits"] == []
