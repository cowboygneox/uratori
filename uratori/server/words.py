"""The word layer: one page's text, positioned, server-owned.

A document page's text does not live in the fact body -- `concepts.md` and
`language.md:300-306` are specific about what the provider sends but nothing
reads: it is left out of the mapping, and no definition reads a page's raw
text or its word boxes structurally (a box is a list of scalars, which
`language.md` makes undeclarable). It lives here instead, keyed generically
by the page kind the host named (`as page of`), never by a fixed name, so
one deployment's `medical_record_page` and another's `invoice_page` are both
served by the same table.

`WordStore` is the protocol, mirroring `BlobStore`'s shape: a memory twin
that keeps the protocol honest, and a Postgres implementation backing the
`document_page_words` table (`db.py` `_SERVER_SQL`). Both are async for the
same reason `FactSource`'s memory twin is -- callers always `await`, whether
or not there is a real database underneath.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import asyncpg


@dataclass(frozen=True)
class Word:
    """One word of a page's word layer.

    `id` is the word's reading-order index on its own page -- 0, 1, 2, ... --
    which is what provenance cites (`{"page": "<page key>", "words": [41,
    42]}`) and what the word-search route answers. The box is page-normalised
    `[0,1]` in the *rendered* frame: CropBox and `/Rotate` applied, origin
    top-left, y down (`docs/http-api.md`'s coordinate contract) -- the same
    frame at any render scale, so a box drawn today still lands correctly on
    a page re-rendered at a different zoom tomorrow.
    """

    id: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int
    """Which text line this word sits on, 0-based in reading order. For a
    PDF text layer, lines are a deterministic y-band clustering over the
    page's own characters (there is no structural "line" in PDF content);
    for OCR, Tesseract's own `line_num` is used directly."""

    block: int
    """Tesseract's `block_num` for an OCR'd page (a layout region); 0 for
    every word of a PDF text layer, which has no equivalent structure."""

    source: Literal["pdf", "ocr"]
    confidence: float | None
    """Tesseract's word confidence, 0-100, for an OCR'd word. `None` for a
    PDF text-layer word -- it was read, not guessed, so there is no
    confidence to report."""


class WordStore(Protocol):
    async def put(self, tenant: str, kind: str, key: str, words: Sequence[Word]) -> None:
        """Replace-set: every word this page previously held under this
        kind and key is gone, replaced by exactly `words`. Mirrors how a
        re-extraction or a re-OCR replaces a page's whole word layer rather
        than patching it -- the layer is the output of one deterministic
        pass over the page's bytes, never edited in place."""
        ...

    async def words_of(self, tenant: str, kind: str, key: str) -> list[Word]:
        """Every word of one page, in reading (id) order."""
        ...

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        """Drop every word of the named pages -- called alongside the page
        facts' own deletion, never on its own."""
        ...


class MemoryWordStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], list[Word]] = {}

    async def put(self, tenant: str, kind: str, key: str, words: Sequence[Word]) -> None:
        self._rows[(tenant, kind, key)] = list(words)

    async def words_of(self, tenant: str, kind: str, key: str) -> list[Word]:
        return list(self._rows.get((tenant, kind, key), ()))

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        for key in keys:
            self._rows.pop((tenant, kind, key), None)


_INSERT = """
insert into document_page_words
    (tenant_id, kind, key, word_id, text, x0, y0, x1, y1, line_no, block_no, source, confidence)
values
    ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""


class PostgresWordStore:
    """Backed by `document_page_words` (`db.py` `_SERVER_SQL`). Takes a pool
    or a connection -- the documents route writes the word layer in the same
    transaction as the page facts, so it passes the open connection; a
    read-only route (the words endpoint) passes the pool."""

    def __init__(self, conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy) -> None:
        self._conn = conn

    async def put(self, tenant: str, kind: str, key: str, words: Sequence[Word]) -> None:
        await self._conn.execute(
            "delete from document_page_words where tenant_id = $1 and kind = $2 and key = $3",
            tenant,
            kind,
            key,
        )
        if not words:
            return
        await self._conn.executemany(
            _INSERT,
            [
                (
                    tenant,
                    kind,
                    key,
                    w.id,
                    w.text,
                    w.x0,
                    w.y0,
                    w.x1,
                    w.y1,
                    w.line,
                    w.block,
                    w.source,
                    w.confidence,
                )
                for w in words
            ],
        )

    async def words_of(self, tenant: str, kind: str, key: str) -> list[Word]:
        rows = await self._conn.fetch(
            "select word_id, text, x0, y0, x1, y1, line_no, block_no, source, confidence "
            "from document_page_words where tenant_id = $1 and kind = $2 and key = $3 "
            "order by word_id",
            tenant,
            kind,
            key,
        )
        return [
            Word(
                id=int(row["word_id"]),
                text=str(row["text"]),
                x0=float(row["x0"]),
                y0=float(row["y0"]),
                x1=float(row["x1"]),
                y1=float(row["y1"]),
                line=int(row["line_no"]),
                block=int(row["block_no"]),
                source=cast('Literal["pdf", "ocr"]', row["source"]),
                confidence=(
                    float(row["confidence"]) if row["confidence"] is not None else None
                ),
            )
            for row in rows
        ]

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        if not keys:
            return
        await self._conn.execute(
            "delete from document_page_words where tenant_id = $1 and kind = $2 and key = any($3)",
            tenant,
            kind,
            list(keys),
        )
