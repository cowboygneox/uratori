"""The facts route's `provenance` map, end to end (documents-plan-v3, D2).

Package 2a: `document_provenance` is a sibling table, coupled to the guarded
write. These tests go through the real HTTP door against a real Postgres and
a real filesystem blob store -- a page to cite has to actually exist, with a
real word layer, which only an upload produces.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from uratori import Schema
from uratori.server import create_app

from .pdf_fixtures import sample_bundle_pdf

WORLD = Schema(kinds=frozenset())

SOURCE = """
# A patient's uploaded records, one file at a time.
fact medical_record as document:
    name title

# One page of one.
fact medical_record_page as page of medical_record

# A patient, so `figure patient.weight` below has a fact kind to scope
# against -- the figure itself never writes or reads one of these; it only
# needs the kind to exist.
fact patient:
    name name
    name as text

# An ordinary host-written fact -- not a D4 extract (package 3), just a
# record whose fields a write's `provenance` map can cite.
fact measurement:
    patient_id as text
    page as text
    measured_at as moment
    weight_kg as number
    one visit:
        many events:
            kind as text

group measurement.by_patient_day from (patient_id, measured_at by day in "UTC")

# D5's shape, narrowed to one figure: the read path (2b) decorates this
# figure's evidence and worksheet with the `weight_kg` provenance a write
# cited, via `FieldPick` -- `evidence_field`'s whole reason to exist.
figure patient.weight bucketed:
    display "{patient} weight"
    unit decimal
    depends:
        weighed = measurement.by_patient_day:{patient}
    calculate:
        latest(measurement.weight_kg over weighed)
"""


@dataclass
class ProvServer:
    http: httpx.AsyncClient
    pool: asyncpg.Pool[Any]
    pg_schema: str


async def _make_server(pg_dsn: str, tmp_path: Path) -> AsyncIterator[ProvServer]:
    name = f"uratori_prov_{os.urandom(4).hex()}"
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
            yield ProvServer(http=http, pool=app.state.uratori.pool, pg_schema=name)

    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema {name} cascade")
    finally:
        await connection.close()


@pytest.fixture
async def srv(pg_dsn: str, tmp_path: Path) -> AsyncIterator[ProvServer]:
    async for server in _make_server(pg_dsn, tmp_path):
        yield server


async def _upload_page_one(http: httpx.AsyncClient) -> tuple[str, str, list[dict[str, Any]]]:
    """Upload the sample bundle and answer `(document_id, page 1's key,
    page 1's words)` -- a real page with a real word layer to cite."""
    up = await http.post(
        "/tenants/t1/documents/medical_record",
        files={"file": ("chart.pdf", sample_bundle_pdf(), "application/pdf")},
    )
    assert up.status_code == 200, up.text
    document_id = up.json()["id"]
    page_key = f"{document_id}/p0001"
    words = await http.get(f"/tenants/t1/documents/medical_record/{document_id}/pages/1/words")
    assert words.status_code == 200, words.text
    return document_id, page_key, words.json()["words"]


def _word_ids(words: Sequence[dict[str, Any]], text: str) -> list[int]:
    return [w["id"] for w in words if w["text"] == text]


async def _provenance_rows(pool: asyncpg.Pool[Any], kind: str, key: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "select field, page_key, word_ids, boxes, printed, value, anchored "
        "from document_provenance where tenant_id = $1 and kind = $2 and key = $3 "
        "order by field",
        "t1",
        kind,
        key,
    )
    out = []
    for r in rows:
        row = dict(r)
        row["boxes"] = json.loads(row["boxes"]) if isinstance(row["boxes"], str) else row["boxes"]
        row["value"] = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
        out.append(row)
    return out


async def test_a_word_citation_is_stored_with_derived_boxes_and_printed_text(
    srv: ProvServer,
) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    assert weight_ids, "the sample bundle's page 1 must print the weight as its own word"

    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    assert resp.status_code == 200, resp.text

    rows = await _provenance_rows(srv.pool, "measurement", "m1")
    assert len(rows) == 1
    row = rows[0]
    assert row["field"] == "weight_kg"
    assert row["page_key"] == page_key
    assert row["printed"] == "82"
    assert row["anchored"] is True
    assert row["value"] == 82
    boxes = row["boxes"]
    assert len(boxes) == len(weight_ids)


async def test_the_boxes_fallback_is_stored_unanchored(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, _words = await _upload_page_one(http)

    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {
                    "m1": {"weight_kg": {"page": page_key, "boxes": [[0.1, 0.2, 0.3, 0.25]]}}
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    rows = await _provenance_rows(srv.pool, "measurement", "m1")
    assert len(rows) == 1
    assert rows[0]["anchored"] is False
    assert rows[0]["printed"] is None


async def test_provenance_refused_on_a_stale_stamped_write(srv: ProvServer) -> None:
    """The stale-write guard refuses the body; the row it would have
    carried must never land either -- a box beside a value that was never
    stored would vouch for the wrong thing."""
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    first_ids = _word_ids(words, "82")
    second_ids = _word_ids(words, "Weight:")
    assert first_ids and second_ids

    first = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": first_ids}}}
            },
            "stamps": {"measurement": {"m1": "2026-08-24T12:00:00Z"}},
        },
    )
    assert first.status_code == 200, first.text
    before = await _provenance_rows(srv.pool, "measurement", "m1")
    assert before and before[0]["word_ids"] == first_ids

    # An older stamp, a different value, a different citation: the guard
    # refuses the body (this is the webhook-vs-reconcile race itself), and
    # the response says nothing moved.
    stale = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 91}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": second_ids}}}
            },
            "stamps": {"measurement": {"m1": "2026-08-24T11:00:00Z"}},
        },
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["written"] == 0

    after = await _provenance_rows(srv.pool, "measurement", "m1")
    assert after == before, "a refused write must not replace the provenance it never earned"


async def test_absent_provenance_map_keeps_what_is_held(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")

    first = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    assert first.status_code == 200, first.text
    held = await _provenance_rows(srv.pool, "measurement", "m1")
    assert held

    # The record moves (a new value), but this batch says nothing at all
    # about its provenance -- the rows from the first write must survive.
    second = await http.post(
        "/tenants/t1/facts",
        json={"writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 85}}}},
    )
    assert second.status_code == 200, second.text
    assert second.json()["written"] == 1

    after = await _provenance_rows(srv.pool, "measurement", "m1")
    assert after == held


async def test_provenance_is_replaced_wholesale_on_an_admitted_write(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    patient_ids = _word_ids(words, "004412")

    first = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}
            },
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    assert first.status_code == 200, first.text
    assert len(await _provenance_rows(srv.pool, "measurement", "m1")) == 1

    # A second, admitted write (a real value change) whose map now cites a
    # DIFFERENT field and drops the first -- the old row must be gone, not
    # merged with the new one.
    second = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "measurement": {"m1": {"patient_id": "p1", "weight_kg": 85}}
            },
            "provenance": {
                "measurement": {"m1": {"patient_id": {"page": page_key, "words": patient_ids}}}
            },
        },
    )
    assert second.status_code == 200, second.text
    rows = await _provenance_rows(srv.pool, "measurement", "m1")
    assert [r["field"] for r in rows] == ["patient_id"]


async def test_a_many_path_is_refused(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")

    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "measurement": {
                    "m1": {
                        "patient_id": "p1",
                        "weight_kg": 82,
                        "visit": {"events": [{"kind": "weigh-in"}]},
                    }
                }
            },
            "provenance": {
                "measurement": {
                    "m1": {"visit.events.kind": {"page": page_key, "words": weight_ids}}
                }
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert "measurement" in resp.json()["detail"]
    assert "m1" in resp.json()["detail"]
    # Nothing landed: the whole batch is refused, body included.
    found = await srv.pool.fetchval(
        "select 1 from fact where tenant_id = 't1' and kind = 'measurement' and key = 'm1'"
    )
    assert found is None


async def test_a_citation_for_a_key_this_batch_does_not_write_is_refused(
    srv: ProvServer,
) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")

    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert "measurement" in resp.json()["detail"]


async def test_a_page_not_held_is_refused(srv: ProvServer) -> None:
    http = srv.http
    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {
                    "m1": {"weight_kg": {"page": "nonexistent/p0001", "words": [0]}}
                }
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert "page" in resp.json()["detail"].lower()


async def test_provenance_deleted_with_the_record(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")

    await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    assert await _provenance_rows(srv.pool, "measurement", "m1")

    deleted = await http.post(
        "/tenants/t1/facts", json={"deletes": {"measurement": ["m1"]}}
    )
    assert deleted.status_code == 200, deleted.text
    assert await _provenance_rows(srv.pool, "measurement", "m1") == []


async def _write_weighed_measurement(http: httpx.AsyncClient, page_key: str, word_ids: list[int]) -> None:
    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {
                "measurement": {
                    "m1": {
                        "patient_id": "p1",
                        "measured_at": "2026-01-01T00:00:00Z",
                        "weight_kg": 82,
                    }
                }
            },
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": word_ids}}}
            },
        },
    )
    assert resp.status_code == 200, resp.text


async def test_the_evidence_route_attaches_sources_to_the_field_it_read(
    srv: ProvServer,
) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    await _write_weighed_measurement(http, page_key, weight_ids)

    evidence = await http.get(
        "/tenants/t1/evidence/patient.weight", params={"subject": "p1@2026-01-01"}
    )
    assert evidence.status_code == 200, evidence.text
    body = evidence.json()
    assert body["kind"] == "measurement"
    members = {m["key"]: m for m in body["members"]}
    assert "m1" in members
    sources = members["m1"]["sources"]
    assert sources and sources[0]["field"] == "weight_kg"
    assert sources[0]["page_key"] == page_key
    assert sources[0]["printed"] == "82"
    assert sources[0]["agrees"] is True
    assert sources[0]["page_url"] is not None


async def test_the_working_route_attaches_sources_to_the_winning_line(
    srv: ProvServer,
) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    await _write_weighed_measurement(http, page_key, weight_ids)

    working = await http.get(
        "/ui/api/tenants/t1/working/patient.weight", params={"subject": "p1@2026-01-01"}
    )
    assert working.status_code == 200, working.text
    root = working.json()["root"]
    assert root["op"] == "field-pick"
    assert root["field"] == "weight_kg"
    winner = next(r for r in root["records"] if r["role"] == "winner")
    assert winner["key"] == "m1"
    assert winner["sources"] and winner["sources"][0]["page_key"] == page_key


async def test_the_record_page_lists_where_it_came_from(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    await _write_weighed_measurement(http, page_key, weight_ids)

    record = await http.get("/ui/api/tenants/t1/facts/measurement/m1")
    assert record.status_code == 200, record.text
    provenance = record.json()["provenance"]
    assert [p["field"] for p in provenance] == ["weight_kg"]
    assert provenance[0]["page_url"] is not None


async def test_a_disagreeing_value_renders_a_note(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")
    await _write_weighed_measurement(http, page_key, weight_ids)

    # Re-attest the same field with a different value, same citation --
    # the row now disagrees with the record it describes.
    resp = await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 90}}},
        },
    )
    assert resp.status_code == 200, resp.text

    record = await http.get("/ui/api/tenants/t1/facts/measurement/m1")
    source = record.json()["provenance"][0]
    assert source["agrees"] is False
    assert source["note"]


async def test_provenance_removed_with_the_tenant(srv: ProvServer) -> None:
    http = srv.http
    _document_id, page_key, words = await _upload_page_one(http)
    weight_ids = _word_ids(words, "82")

    await http.post(
        "/tenants/t1/facts",
        json={
            "writes": {"measurement": {"m1": {"patient_id": "p1", "weight_kg": 82}}},
            "provenance": {
                "measurement": {"m1": {"weight_kg": {"page": page_key, "words": weight_ids}}}
            },
        },
    )
    removed = await http.delete("/tenants/t1")
    assert removed.status_code == 200, removed.text
    assert removed.json()["provenance_removed"] == 1
    assert await _provenance_rows(srv.pool, "measurement", "m1") == []
