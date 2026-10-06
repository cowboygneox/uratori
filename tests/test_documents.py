"""Documents end to end: upload, read back, render, search, delete.

Package 1b of the documents plan (`~/.claude/notes/uratori/
documents-plan-v3.md`, D1): the server's own provider for `as document` /
`as page of` kinds. Goes through the same HTTP door a host uses, against a
real Postgres and a real filesystem blob store under a temp directory --
this package's whole claim is "upload a file, get back pages, a rendered
image and a searchable word layer", and that is only worth what an
end-to-end test says it is.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
import pytest

from uratori import Schema
from uratori.server import create_app

from .pdf_fixtures import blank_page_pdf, rotated_page_pdf, sample_bundle_pdf

WORLD = Schema(kinds=frozenset())

DOCUMENT_SOURCE = """
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title

# One page of one.
fact medical_record_page as page of medical_record
"""


@dataclass
class DocServer:
    http: httpx.AsyncClient
    pg_schema: str
    blob_dir: str


async def _make_server(
    pg_dsn: str, tmp_path: Path, *, blob_dir: bool = True
) -> AsyncIterator[DocServer]:
    name = f"uratori_docs_{os.urandom(4).hex()}"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"create schema {name}")
    finally:
        await connection.close()

    blobs = str(tmp_path / "blobs")
    app = create_app(
        dsn=pg_dsn,
        pg_schema=name,
        token=None,
        version="test",
        blob_dir=blobs if blob_dir else None,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://uratori") as http:
            put = await http.put("/schema", json=WORLD.to_document())
            assert put.status_code == 200, put.text
            put = await http.put("/definitions", json={"source": DOCUMENT_SOURCE})
            assert put.status_code == 200, put.text
            yield DocServer(http=http, pg_schema=name, blob_dir=blobs)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def docs(pg_dsn: str, tmp_path: Path) -> AsyncIterator[DocServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


@pytest.fixture
async def docs_no_blob_dir(pg_dsn: str, tmp_path: Path) -> AsyncIterator[DocServer]:
    async for server in _make_server(pg_dsn, tmp_path, blob_dir=False):
        yield server


async def _upload(
    http: httpx.AsyncClient, data: bytes, *, filename: str = "chart.pdf"
) -> httpx.Response:
    return await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": (filename, data, "application/pdf")},
    )


async def test_uploading_without_url_blob_dir_is_a_409(docs_no_blob_dir: DocServer) -> None:
    resp = await _upload(docs_no_blob_dir.http, sample_bundle_pdf())
    assert resp.status_code == 409, resp.text
    assert "URATORI_BLOB_DIR" in resp.json()["detail"]


async def test_upload_lists_reads_renders_and_searches(docs: DocServer) -> None:
    http = docs.http
    # reportlab's own output is not byte-stable across calls (it stamps an
    # object-id/creation time per build), so the dedupe check below reuses
    # one call's bytes rather than generating the "same" document twice.
    pdf = sample_bundle_pdf()
    up = await _upload(http, pdf)
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["written"] == 1
    assert body["pages"] == 3
    document_id = body["id"]

    # Re-uploading the identical bytes is a dedupe, not a second record.
    again = await _upload(http, pdf)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == document_id
    assert again.json()["written"] == 0
    assert again.json()["pages"] == 3

    listed = await http.get("/tenants/t1/documents/medical_record")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["documents"][0]["id"] == document_id
    assert listed.json()["documents"][0]["held"] is True

    one = await http.get(f"/tenants/t1/documents/medical_record/{document_id}")
    assert one.status_code == 200, one.text
    assert one.json()["pages"] == 3
    assert one.json()["mime"] == "application/pdf"

    # Page 1 has a real text layer.
    png1 = await http.get(f"/tenants/t1/documents/medical_record/{document_id}/pages/1.png")
    assert png1.status_code == 200, png1.text
    assert png1.headers["content-type"] == "image/png"
    assert png1.content[:8] == b"\x89PNG\r\n\x1a\n"

    words1 = await http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words"
    )
    assert words1.status_code == 200
    texts1 = [w["text"] for w in words1.json()["words"]]
    assert "Weight:" in texts1
    assert all(w["source"] == "pdf" for w in words1.json()["words"])
    for w in words1.json()["words"]:
        assert 0.0 <= w["x0"] <= w["x1"] <= 1.0
        assert 0.0 <= w["y0"] <= w["y1"] <= 1.0

    # Page 2 is image-only: the word layer must come from OCR.
    words2 = await http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/2/words"
    )
    assert words2.status_code == 200
    texts2 = [w["text"] for w in words2.json()["words"]]
    assert texts2  # tesseract found something
    assert all(w["source"] == "ocr" for w in words2.json()["words"])

    # A page past the end is a 404, not a 500.
    missing = await http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/99.png"
    )
    assert missing.status_code == 404

    # Rendering again hits the on-disk cache rather than re-parsing.
    png1_again = await http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1.png"
    )
    assert png1_again.content == png1.content


async def test_rotated_page_boxes_stay_in_bounds(docs: DocServer) -> None:
    up = await _upload(docs.http, rotated_page_pdf(), filename="rotated.pdf")
    assert up.status_code == 200, up.text
    document_id = up.json()["id"]
    words = await docs.http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words"
    )
    body = words.json()["words"]
    assert any(w["text"] == "77" for w in body)
    for w in body:
        assert 0.0 <= w["x0"] <= w["x1"] <= 1.0
        assert 0.0 <= w["y0"] <= w["y1"] <= 1.0
    png = await docs.http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1.png"
    )
    assert png.status_code == 200


async def test_a_blank_page_reports_no_text_source_and_no_words(docs: DocServer) -> None:
    up = await _upload(docs.http, blank_page_pdf(), filename="blank.pdf")
    assert up.status_code == 200, up.text
    document_id = up.json()["id"]
    words = await docs.http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words"
    )
    assert words.json()["words"] == []


async def test_a_host_field_beyond_the_shape_is_verified(docs: DocServer) -> None:
    """The `record` form field is JSON, verified against the declaration
    exactly as a facts-route write is -- a field the kind never declared is
    refused, not silently stored."""
    resp = await docs.http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart.pdf", sample_bundle_pdf(), "application/pdf")},
        data={"record": '{"nonsense_field": "x"}'},
    )
    assert resp.status_code == 422, resp.text


async def test_the_facts_route_refuses_direct_writes_and_deletes(docs: DocServer) -> None:
    write = await docs.http.post(
        "/tenants/t1/facts",
        json={"writes": {"medical_record": {"d1": {"title": "sneaky"}}}},
    )
    assert write.status_code == 422, write.text
    assert "documents routes" in write.json()["detail"]

    delete = await docs.http.post(
        "/tenants/t1/facts", json={"deletes": {"medical_record_page": ["d1/p0001"]}}
    )
    assert delete.status_code == 422, delete.text


async def test_reocr_rebuilds_the_word_layer(docs: DocServer) -> None:
    up = await _upload(docs.http, sample_bundle_pdf())
    document_id = up.json()["id"]
    reocr = await docs.http.post(
        f"/tenants/t1/documents/medical_record/{document_id}/reocr"
    )
    assert reocr.status_code == 200, reocr.text
    # An identical re-ingest of unchanged bytes moves nothing.
    assert reocr.json()["pages_changed"] == 0


async def test_delete_removes_facts_words_and_the_blob(docs: DocServer) -> None:
    import pathlib

    up = await _upload(docs.http, sample_bundle_pdf())
    document_id = up.json()["id"]
    assert any(p.is_file() for p in pathlib.Path(docs.blob_dir).rglob("*"))

    deleted = await docs.http.delete(f"/tenants/t1/documents/medical_record/{document_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True

    gone = await docs.http.get(f"/tenants/t1/documents/medical_record/{document_id}")
    assert gone.status_code == 404

    words = await docs.http.get(
        f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words"
    )
    assert words.json()["words"] == []

    # The blob file itself is gone from disk (rows went first, then the
    # file, D1's stated ordering), not just the fact.
    blob_files = [p for p in pathlib.Path(docs.blob_dir).rglob("*") if p.is_file()]
    assert blob_files == []

    listing = await docs.http.get("/tenants/t1/documents/medical_record")
    assert listing.json()["total"] == 0


async def test_remove_tenant_cascades_documents_and_blobs(docs: DocServer) -> None:
    import pathlib

    up = await _upload(docs.http, sample_bundle_pdf())
    assert up.status_code == 200, up.text

    before = list(pathlib.Path(docs.blob_dir).rglob("*"))
    assert any(p.is_file() for p in before)

    removed = await docs.http.delete("/tenants/t1")
    assert removed.status_code == 200, removed.text
    assert removed.json()["documents_removed"] == 1

    listing = await docs.http.get("/tenants/t1/documents/medical_record")
    assert listing.json()["total"] == 0

    after = [p for p in pathlib.Path(docs.blob_dir).rglob("*") if p.is_file()]
    assert after == []
