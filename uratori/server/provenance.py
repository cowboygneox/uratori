"""Provenance: sibling metadata beside a record, never in it.

A fact's body is what a definition may read; provenance is the server's own
claim about where a field's value came from, and no construct in the
language can see it (`verify.py` never checks it, `FactSource` never serves
it -- `store/base.py` is "what the engine needs from storage, and nothing
more", and the engine needs none of this). It lives here, beside `blobs.py`
and `words.py`, for the same reason they do: a protocol with a Postgres
implementation and a memory twin, owned by the server, never by the engine.

**Coupled to the guarded write, not a door of its own.** The facts route's
`provenance` map is parsed and verified alongside `writes`, and a row for
`(kind, key, field)` is written in the *same transaction* as the body, and
only when the stale-write guard actually admits that key's write -- a
provenance row for a write the guard refused would vouch for a value that
was never stored. `document_provenance` (`db.py` `_SERVER_SQL`) is a
replace-set table per `(tenant, kind, key)`: a batch that writes a record but
says nothing about its provenance leaves whatever rows are already held
exactly as they are; a batch that *does* name `(kind, key)` in its map
replaces every row that record held wholesale, because a stale box beside a
value nobody re-attested is worse than no box.

**Cites words, falls back to a raw box.** The ordinary case names word ids
off the page's own word layer (`words.py`); the server derives each word's
box and the printed text from the word table itself, so "the extractor said
so" shrinks to "the extractor pointed at these words", checkable by eye. The
fallback -- `boxes` given directly, no `words` -- is for a value nothing in
the word layer anchors (a tick-box, handwriting OCR missed): stored and
rendered `anchored: false`, "region asserted, not matched to page text".

**Paths cross `one` blocks only.** `resolve_field_path` below mirrors
`lang/check.py`'s `_record_field` at request time: a dotted path that does
not resolve, or crosses a `many` block (a repeating position is not a stable
place to point a box at), is refused.

The read path -- joining this table back onto a figure's evidence and a
worksheet's record lines (D3) -- lives in this same module too, added once
`uratori.results` carries the `Source`/`Box` wire shapes it decorates onto.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from ..lang.plan import CompiledFact, CompiledFactField, Library
from . import db
from .contract import ProvenanceCiteIn
from .documents import document_kinds
from .words import Word, WordStore


class ProvenanceError(Exception):
    """A `provenance` map entry that does not check out -- an undeclared or
    `many`-crossing field, a page not held, a word id the page's layer does
    not have, a malformed box fallback, or a citation naming a (kind, key)
    this batch is not also writing. Refused whole, by kind/key/field, like
    `verify.FactError` -- the fix is in the host's mapping either way."""


@dataclass(frozen=True)
class StoredBox:
    """A box as held in `document_provenance.boxes` -- the same four
    numbers as `results.Box`, the wire shape, kept as a separate type so
    this module never has to import the wire layer to hold a row."""

    x0: float
    y0: float
    x1: float
    y1: float

    def as_json(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]


@dataclass(frozen=True)
class ProvenanceRow:
    """One `(kind, key, field)`'s citation, as stored. `value` is the value
    this row attests -- read off the write batch at the moment this row was
    written -- so a read-time compare against the record's *current* value
    is what renders a disagreement (D2); this row never updates itself."""

    field: str
    page_key: str
    word_ids: tuple[int, ...]
    boxes: tuple[StoredBox, ...]
    printed: str | None
    value: Any
    anchored: bool
    extractor: str | None = None
    parser: str | None = None
    matcher: Any | None = None
    reproducible: bool = True
    at: str = ""


class ProvenanceStore(Protocol):
    async def replace(
        self, tenant: str, kind: str, key: str, rows: Sequence[ProvenanceRow]
    ) -> None:
        """Every row this record held under `kind`/`key` is gone, replaced by
        exactly `rows` -- including an empty `rows`, which clears the record's
        provenance entirely (a batch that names the key with an empty field
        map says "nothing is cited any more", as distinct from not naming
        the key at all, which the caller never reaches this for)."""
        ...

    async def for_record(self, tenant: str, kind: str, key: str) -> list[ProvenanceRow]: ...

    async def for_many(
        self, tenant: str, kind: str, keys: Sequence[str]
    ) -> dict[str, list[ProvenanceRow]]:
        """Every held row for several records of one kind, grouped by key --
        the bulk form the evidence/working decoration uses so a member list
        of twenty records costs one query, not twenty."""
        ...

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        """Drop every row of these records -- called beside the fact
        deletes a `deletes` batch, a document delete or a tenant removal
        makes, never on its own."""
        ...

    async def delete_tenant(self, tenant: str) -> int:
        """Every row this tenant holds, gone; returns how many."""
        ...


class MemoryProvenanceStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], list[ProvenanceRow]] = {}

    async def replace(
        self, tenant: str, kind: str, key: str, rows: Sequence[ProvenanceRow]
    ) -> None:
        # Stamped here, matching `now()` on the Postgres side -- the twin
        # that left `at` at the caller's own default would answer a
        # different row for the same write, and the parity suite exists
        # exactly to catch that.
        now = datetime.now(UTC).isoformat()
        self._rows[(tenant, kind, key)] = [dc_replace(row, at=now) for row in rows]

    async def for_record(self, tenant: str, kind: str, key: str) -> list[ProvenanceRow]:
        return list(self._rows.get((tenant, kind, key), ()))

    async def for_many(
        self, tenant: str, kind: str, keys: Sequence[str]
    ) -> dict[str, list[ProvenanceRow]]:
        return {
            key: list(self._rows[(tenant, kind, key)])
            for key in keys
            if (tenant, kind, key) in self._rows
        }

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        for key in keys:
            self._rows.pop((tenant, kind, key), None)

    async def delete_tenant(self, tenant: str) -> int:
        gone = [k for k in self._rows if k[0] == tenant]
        total = sum(len(self._rows[k]) for k in gone)
        for k in gone:
            del self._rows[k]
        return total


_UPSERT = """
insert into document_provenance
    (tenant_id, kind, key, field, page_key, word_ids, boxes, printed, value,
     anchored, extractor, parser, matcher, reproducible, at)
values
    ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())
"""


class PostgresProvenanceStore:
    """Backed by `document_provenance` (`db.py` `_SERVER_SQL`). Takes a pool
    or a connection, like `PostgresWordStore` -- the facts route's write path
    passes the open transaction's connection so the body and its provenance
    land atomically; a read-only route passes the pool."""

    def __init__(self, conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy) -> None:
        self._conn = conn

    async def replace(
        self, tenant: str, kind: str, key: str, rows: Sequence[ProvenanceRow]
    ) -> None:
        await self._conn.execute(
            "delete from document_provenance where tenant_id = $1 and kind = $2 and key = $3",
            tenant,
            kind,
            key,
        )
        if not rows:
            return
        await self._conn.executemany(
            _UPSERT,
            [
                (
                    tenant,
                    kind,
                    key,
                    row.field,
                    row.page_key,
                    list(row.word_ids),
                    json.dumps([b.as_json() for b in row.boxes]),
                    row.printed,
                    json.dumps(row.value),
                    row.anchored,
                    row.extractor,
                    row.parser,
                    json.dumps(row.matcher) if row.matcher is not None else None,
                    row.reproducible,
                )
                for row in rows
            ],
        )

    async def for_record(self, tenant: str, kind: str, key: str) -> list[ProvenanceRow]:
        rows = await self._conn.fetch(
            "select field, page_key, word_ids, boxes, printed, value, anchored, "
            "extractor, parser, matcher, reproducible, at "
            "from document_provenance where tenant_id = $1 and kind = $2 and key = $3 "
            "order by field",
            tenant,
            kind,
            key,
        )
        return [_row(r) for r in rows]

    async def for_many(
        self, tenant: str, kind: str, keys: Sequence[str]
    ) -> dict[str, list[ProvenanceRow]]:
        if not keys:
            return {}
        rows = await self._conn.fetch(
            "select key, field, page_key, word_ids, boxes, printed, value, anchored, "
            "extractor, parser, matcher, reproducible, at "
            "from document_provenance where tenant_id = $1 and kind = $2 and key = any($3::text[]) "
            "order by key, field",
            tenant,
            kind,
            list(keys),
        )
        out: dict[str, list[ProvenanceRow]] = {}
        for r in rows:
            out.setdefault(r["key"], []).append(_row(r))
        return out

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        if not keys:
            return
        await self._conn.execute(
            "delete from document_provenance where tenant_id = $1 and kind = $2 "
            "and key = any($3::text[])",
            tenant,
            kind,
            list(keys),
        )

    async def delete_tenant(self, tenant: str) -> int:
        count = await self._conn.fetchval(
            "select count(*) from document_provenance where tenant_id = $1", tenant
        )
        await self._conn.execute(
            "delete from document_provenance where tenant_id = $1", tenant
        )
        return int(count or 0)


def _row(r: asyncpg.Record) -> ProvenanceRow:
    boxes_raw = r["boxes"]
    boxes_list = boxes_raw if isinstance(boxes_raw, list) else json.loads(boxes_raw)
    value_raw = r["value"]
    value = value_raw if not isinstance(value_raw, str) else json.loads(value_raw)
    matcher_raw = r["matcher"]
    matcher = (
        matcher_raw
        if matcher_raw is None or not isinstance(matcher_raw, str)
        else json.loads(matcher_raw)
    )
    return ProvenanceRow(
        field=r["field"],
        page_key=r["page_key"],
        word_ids=tuple(r["word_ids"] or ()),
        boxes=tuple(StoredBox(*b) for b in boxes_list),
        printed=r["printed"],
        value=value,
        anchored=r["anchored"],
        extractor=r["extractor"],
        parser=r["parser"],
        matcher=matcher,
        reproducible=r["reproducible"],
        at=r["at"].isoformat() if r["at"] is not None else "",
    )


# ------------------------------------------------------------ field paths --


def resolve_field_path(fact: CompiledFact, path: str) -> CompiledFactField | None:
    """The declared field a dotted path lands on, or `None` when it does not
    resolve or crosses a `many` block -- the request-time twin of
    `lang/check.py`'s `_record_field`, which runs at compile time against a
    `Checker`'s own tables and cannot be called here. A page citation and a
    raw-box fallback both name a field this way; `many` is refused because a
    repeating position is not a stable place to point a box at
    (documents-plan-v3, D2)."""
    at: dict[str, CompiledFactField] = {f.name: f for f in fact.fields}
    found: CompiledFactField | None = None
    for segment in path.split("."):
        found = at.get(segment)
        if found is None or found.many:
            return None
        at = {c.name: c for c in found.children}
    if found is None or found.type is None:
        return None
    return found


# -------------------------------------------------------------- write path --


async def validate_and_build(
    pool: asyncpg.Pool[Any],
    word_store: WordStore,
    library: Library,
    tenant: str,
    writes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    provenance: Mapping[str, Mapping[str, Mapping[str, ProvenanceCiteIn]]],
) -> dict[tuple[str, str], list[ProvenanceRow]]:
    """Parse and verify one batch's `provenance` map, deriving every row it
    implies -- raises `ProvenanceError`, naming kind/key/field, the moment
    anything does not check out, so the facts route can 422 the whole batch
    before touching the database (documents-plan-v3, D2). Read-only: no
    write happens here, and the transaction decides, key by key, whether the
    stale-write guard actually admits the write this citation is coupled to.
    """
    if not provenance:
        return {}
    page_to_document = {page: doc for doc, page in document_kinds(library).items()}
    page_kinds = list(page_to_document)
    out: dict[tuple[str, str], list[ProvenanceRow]] = {}
    word_cache: dict[tuple[str, str], dict[int, Word]] = {}

    for kind, by_key in provenance.items():
        fact = library.facts.get(kind)
        if fact is None:
            raise ProvenanceError(f'provenance names "{kind}", which is not a fact kind.')
        kind_writes = writes.get(kind, {})
        for key, by_field in by_key.items():
            if key not in kind_writes:
                raise ProvenanceError(
                    f'provenance for {kind} "{key}" names a record this batch is not '
                    "writing. Provenance is coupled to the body write (D2): write the "
                    "record in the same batch, or omit this key to leave its provenance "
                    "as it stands."
                )
            record = kind_writes[key]
            rows: list[ProvenanceRow] = []
            for fieldname, cite in by_field.items():
                where = f'{kind} "{key}".{fieldname}'
                if record.get(fieldname) in (None, ""):
                    raise ProvenanceError(
                        f"provenance for {where} names a field this batch's write does "
                        "not carry a value for."
                    )
                if resolve_field_path(fact, fieldname) is None:
                    raise ProvenanceError(
                        f'provenance for {where}: "{fieldname}" is not a declared field '
                        f"of {kind}, or its path crosses a `many` block -- provenance "
                        "paths cross `one` blocks only."
                    )
                page_kind = await db.held_page_kind(pool, tenant, page_kinds, cite.page)
                if page_kind is None:
                    raise ProvenanceError(
                        f'provenance for {where} cites page "{cite.page}", which is not '
                        "a held page fact of a declared page kind in this tenant."
                    )
                has_words = bool(cite.words)
                has_boxes = bool(cite.boxes)
                if has_words == has_boxes:
                    raise ProvenanceError(
                        f"provenance for {where} must give exactly one of `words` "
                        "(the ordinary citation) or `boxes` (the no-text-layer "
                        "fallback) -- never both, never neither."
                    )
                if has_words:
                    assert cite.words is not None
                    cache_key = (page_kind, cite.page)
                    by_id = word_cache.get(cache_key)
                    if by_id is None:
                        held_words = await word_store.words_of(tenant, page_kind, cite.page)
                        by_id = {w.id: w for w in held_words}
                        word_cache[cache_key] = by_id
                    missing = sorted(w for w in cite.words if w not in by_id)
                    if missing:
                        raise ProvenanceError(
                            f"provenance for {where} cites word ids {missing} that page "
                            f'"{cite.page}" does not hold.'
                        )
                    ordered = sorted(set(cite.words))
                    matched = [by_id[w] for w in ordered]
                    rows.append(
                        ProvenanceRow(
                            field=fieldname,
                            page_key=cite.page,
                            word_ids=tuple(ordered),
                            boxes=tuple(StoredBox(w.x0, w.y0, w.x1, w.y1) for w in matched),
                            printed=" ".join(w.text for w in matched) or None,
                            value=record[fieldname],
                            anchored=True,
                        )
                    )
                else:
                    assert cite.boxes is not None
                    boxes: list[StoredBox] = []
                    for raw in cite.boxes:
                        if len(raw) != 4:
                            raise ProvenanceError(
                                f"provenance for {where}: each box in the `boxes` "
                                "fallback is four numbers, [x0, y0, x1, y1]."
                            )
                        x0, y0, x1, y1 = (float(n) for n in raw)
                        if not (0.0 <= x0 <= x1 <= 1.0 and 0.0 <= y0 <= y1 <= 1.0):
                            raise ProvenanceError(
                                f"provenance for {where}: box {raw} is not "
                                "page-normalised [0,1] with x0<=x1 and y0<=y1."
                            )
                        boxes.append(StoredBox(x0, y0, x1, y1))
                    rows.append(
                        ProvenanceRow(
                            field=fieldname,
                            page_key=cite.page,
                            word_ids=(),
                            boxes=tuple(boxes),
                            printed=None,
                            value=record[fieldname],
                            anchored=False,
                        )
                    )
            out[(kind, key)] = rows
    return out
