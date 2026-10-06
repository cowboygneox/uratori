"""The `audit` worker, end to end, with the fake provider.

Package 5d of the documents plan (documents-plan-v3, D6): `run_worker_sweep`
claims a lease, calls a provider OUTSIDE `lock_for(tenant)`, then re-checks
and writes under it -- `uratori.audit.fake.FakeAuditProvider` stands in for
a model so this runs with no network and no API key, over a real Postgres
and a real filesystem blob store.
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
from uratori.audit.fake import FakeAnswer, FakeAuditProvider
from uratori.server import create_app, db
from uratori.server.audit_worker import run_worker_sweep
from uratori.server.runtime import State, World

WORLD = Schema(kinds=frozenset())

SOURCE = """
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title

# One page of one.
fact medical_record_page as page of medical_record

# One set of vitals read off one page.
fact measurement:
    page as text
    weight_kg as number

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


@dataclass
class WorkerServer:
    http: httpx.AsyncClient
    state: State
    world: World


async def _make_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[WorkerServer]:
    name = f"uratori_worker_{os.urandom(4).hex()}"
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
            state = app.state.uratori
            assert state.world is not None
            yield WorkerServer(http=http, state=state, world=state.world)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def worker_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[WorkerServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


async def _fact_rows(http: httpx.AsyncClient, kind: str) -> dict[str, dict]:
    resp = await http.get(f"/ui/api/tenants/t1/facts/{kind}")
    assert resp.status_code == 200, resp.text
    return {row["key"]: row["value"] for row in resp.json()["records"]}


async def _upload(http: httpx.AsyncClient, weight_kg: str) -> str:
    pdf = vitals_pdf(weight_kg)
    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart.pdf", pdf, "application/pdf")},
    )
    assert up.status_code == 200, up.text
    pages = await _fact_rows(http, "medical_record_page")
    assert len(pages) == 1
    return next(iter(pages))


async def _audit_value(state: State, page_key: str, audit_version: str) -> object:
    row = await state.pool.fetchrow(
        "select value from figure_value where tenant_id = 't1' and name = $1 "
        "and version = $2 and subject_id = $3",
        "medical_record_page.vitals_audit",
        audit_version,
        page_key,
    )
    if row is None:
        return None
    import json

    return json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]


async def _audit_version(http: httpx.AsyncClient) -> str:
    defs = await http.get("/definitions")
    return str(defs.json()["audits"][0]["version"])


async def test_the_worker_reads_a_page_and_agrees(worker_server: WorkerServer) -> None:
    http = worker_server.http
    page_key = await _upload(http, "82")
    audit_version = await _audit_version(http)

    provider = FakeAuditProvider()
    read = await run_worker_sweep(worker_server.state, worker_server.world, worker_server.world.library, provider)
    assert read == 1

    value = await _audit_value(worker_server.state, page_key, audit_version)
    assert value == "agrees"

    reading = await db.audit_reading(
        worker_server.state.pool, "t1", "medical_record_page.vitals_audit", audit_version, page_key
    )
    assert reading is not None
    assert reading["model"] == "fake-v1"
    assert reading["parsed"][0]["status"] == "seen"


async def test_a_lease_prevents_a_concurrent_second_read(worker_server: WorkerServer) -> None:
    """Two sweeps racing for the same page: the second finds the page
    already leased and skips it, rather than paying for the model call
    twice."""
    http = worker_server.http
    page_key = await _upload(http, "82")
    audit_version = await _audit_version(http)

    claimed = await db.claim_audit_lease(
        worker_server.state.pool, "t1", "medical_record_page.vitals_audit", audit_version,
        page_key, ttl_seconds=300.0,
    )
    assert claimed

    provider = FakeAuditProvider()
    read = await run_worker_sweep(worker_server.state, worker_server.world, worker_server.world.library, provider)
    assert read == 0  # the only candidate page is leased


async def test_a_focused_disagreement_judges_disagrees(worker_server: WorkerServer) -> None:
    http = worker_server.http
    page_key = await _upload(http, "82")

    provider = FakeAuditProvider(
        focus={
            (page_key, "measurement", "weight_kg"): [
                FakeAnswer(status="seen", seen_text="90", words=())
            ]
        }
    )
    read = await run_worker_sweep(worker_server.state, worker_server.world, worker_server.world.library, provider)
    assert read == 1

    audit_version = await _audit_version(http)
    value = await _audit_value(worker_server.state, page_key, audit_version)
    # The forced reading cites no words (unanchored), so D6's rule applies:
    # presence only, never a value comparison -- "disagrees" about presence
    # since the extract *does* have a value.
    assert value in ("disagrees", "unreadable")


async def test_a_missing_reading_leaves_the_page_unaudited(worker_server: WorkerServer) -> None:
    http = worker_server.http
    page_key = await _upload(http, "82")
    audit_version = await _audit_version(http)
    # No worker ran at all -- the pass itself already stored "unaudited"
    # when the page was extracted (5c); this just pins that the worker
    # test module agrees with that without having to run the worker.
    value = await _audit_value(worker_server.state, page_key, audit_version)
    assert value == "unaudited"


async def test_the_reaudit_verb_discards_the_reading_and_the_worker_takes_it_again(
    worker_server: WorkerServer,
) -> None:
    http = worker_server.http
    page_key = await _upload(http, "82")
    audit_version = await _audit_version(http)
    provider = FakeAuditProvider()

    read = await run_worker_sweep(worker_server.state, worker_server.world, worker_server.world.library, provider)
    assert read == 1
    assert await _audit_value(worker_server.state, page_key, audit_version) == "agrees"

    run = await http.post(
        "/tenants/t1/runs", json={"audit": "medical_record_page.vitals_audit"}
    )
    assert run.status_code == 200, run.text
    assert await _audit_value(worker_server.state, page_key, audit_version) == "unaudited"
    assert (
        await db.audit_reading(
            worker_server.state.pool, "t1", "medical_record_page.vitals_audit",
            audit_version, page_key,
        )
        is None
    )

    read_again = await run_worker_sweep(
        worker_server.state, worker_server.world, worker_server.world.library, provider
    )
    assert read_again == 1
    assert await _audit_value(worker_server.state, page_key, audit_version) == "agrees"


async def test_the_reaudit_verb_refuses_an_unknown_audit_name(worker_server: WorkerServer) -> None:
    http = worker_server.http
    run = await http.post("/tenants/t1/runs", json={"audit": "not_a_real_audit"})
    assert run.status_code == 422, run.text
