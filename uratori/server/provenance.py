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
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import asyncpg

from ..lang.ast import Extreme, FieldPick, ListOf
from ..lang.ast import Sum as LangSum
from ..lang.plan import CompiledFact, CompiledFactField, FigurePlan, Library
from ..results import Box as WireBox
from ..results import Evidence, Source, SourceAudit
from ..store.postgres import PostgresFactStore
from . import db
from .contract import ProvenanceCiteIn
from .documents import document_kinds, parse_page_key
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


def read_field_value(record: Mapping[str, Any], path: str) -> Any:
    """The raw value at a dotted path, crossing only `one` blocks -- read
    generically (no number/instant parsing) because this is used only to
    compare against a provenance row's own attested `value`, which was
    stored the same way at write time. `None` for a path that does not
    resolve, same as absent."""
    node: Any = record
    for segment in path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return None
        node = node[segment]
    return node


# -------------------------------------------------------------- read path --


def evidence_field(plan: FigurePlan, library: Library) -> str | None:
    """The one field this figure's calculation reads off each of its
    leaf-kind members, when there is a single one to name without touching
    the engine (D3). Mirrors `engine/serve.py`'s own `_measure_read` for the
    shapes it already names (a measure-backed `list`/`sum`/`latest`), and
    adds `FieldPick` (`latest(kind.field over set)`), which that function
    never had to answer because the evidence panel did not yet need to know
    *which* field -- only whether to show a live re-read. `None` for a count
    (reads no field), a rollup (handled before this is ever reached), and
    anything reading more than one field (arithmetic, a ladder, a
    duration/moment measure) -- D3's own stated deferral; the general
    `_members_of` fix for mixed evidence is out of this package's scope.
    """
    calc = plan.calculate
    if isinstance(calc, FieldPick):
        return calc.field
    measure_name: str | None = None
    if isinstance(calc, ListOf):
        measure_name = calc.measure
    elif isinstance(calc, LangSum) and calc.measure is not None:
        measure_name = calc.measure
    elif isinstance(calc, Extreme):
        measure_name = calc.measure
    if measure_name is None:
        return None
    measure = library.measures.get(measure_name)
    if measure is None or measure.shape != "field" or measure.field_path is None:
        return None
    return measure.field_path


@dataclass(frozen=True)
class ResolvedPage:
    page_kind: str
    document_kind: str
    document_id: str
    number: int
    document_title: str | None


async def resolve_page(
    pool: asyncpg.Pool[Any], tenant: str, library: Library, key: str
) -> ResolvedPage | None:
    """The page a provenance row's `page_key` names, or `None` when it is no
    longer held -- deleted with its document (or, in principle, never
    written). The caller renders a `Source` with no boxes and a note saying
    so in that case, the same honesty `EvidenceMember.held` already states
    for a deleted record -- never a 404 for a citation that still exists as
    a row, only as a dangling reference.

    Ambiguous on purpose, in one narrow case: two document kinds that share
    a blob (identical bytes uploaded under both, `document_sha_referenced`'s
    own scenario) can share a page key too, and this citation shape names
    only the page, not its kind. `held_page_kind` breaks the tie by sorted
    kind name, deterministically, and the package report names this as a
    known limitation rather than a silent one.
    """
    parsed = parse_page_key(key)
    if parsed is None:
        return None
    document_id, number = parsed
    page_to_document = {page: doc for doc, page in document_kinds(library).items()}
    page_kind = await db.held_page_kind(pool, tenant, list(page_to_document), key)
    if page_kind is None:
        return None
    document_kind = page_to_document[page_kind]
    doc_row = await db.fact_record(pool, tenant, document_kind, document_id)
    title = doc_row["value"].get("title") if doc_row is not None else None
    return ResolvedPage(
        page_kind=page_kind,
        document_kind=document_kind,
        document_id=document_id,
        number=number,
        document_title=title if isinstance(title, str) else None,
    )


def page_image_url(resolved: ResolvedPage, base: str | None) -> str | None:
    """`base` is the surface's own documents root -- the authenticated API's
    `/tenants/{t}/documents` for `GET /evidence`, or the unauthenticated
    `/ui/api/tenants/{t}/documents` mirror for the UI's own routes -- so a
    `<img src>` built from a UI response never needs a bearer token it has
    nowhere to attach (D3, "Image routes and the UI posture"). `None` when
    the caller has no documents root to link against -- the UI with
    `URATORI_UI_DOCUMENTS` off, where every other field of the source still
    renders, just with nothing to click through to."""
    if base is None:
        return None
    return (
        f"{base}/{quote(resolved.document_kind, safe='')}"
        f"/{quote(resolved.document_id, safe='')}"
        f"/pages/{resolved.number}.png"
    )


def _compare(field: str, current: Any, attested: Any) -> tuple[bool, str | None]:
    """Whether the record's value at `field` now, read live, still matches
    what this row attested when it was written. `None`/`None` still agrees
    -- a field neither side ever set is not a disagreement."""
    if current == attested:
        return True, None
    now = "nothing" if current is None else repr(current)
    return False, f"this page attested {field} as {attested!r}; the record now holds {now}"


async def source_of(
    pool: asyncpg.Pool[Any],
    library: Library,
    tenant: str,
    row: ProvenanceRow,
    current_value: Any,
    *,
    base: str | None,
    record_key: str | None = None,
) -> Source:
    """One provenance row, read for a reader: resolve its page, compare its
    attested value against the record's value now, and render the one
    sentence that matters either way -- a disagreement, or that the page
    behind the citation is gone.

    `record_key` (the record this field belongs to -- not `row.page_key`,
    which is where the field was *read from*) is what `audits_for_field`
    looks a second reader's finding up by; `None` skips that lookup for a
    caller with no record key in hand."""
    agrees, note = _compare(row.field, current_value, row.value)
    audits = (
        await audits_for_field(pool, library, tenant, record_key, row.field)
        if record_key is not None
        else []
    )
    resolved = await resolve_page(pool, tenant, library, row.page_key)
    if resolved is None:
        return Source(
            field=row.field,
            page_key=row.page_key,
            printed=row.printed,
            anchored=row.anchored,
            agrees=agrees,
            note=note or "the cited page is no longer held",
            audits=audits,
        )
    return Source(
        field=row.field,
        page_key=row.page_key,
        page_label=f"page {resolved.number}",
        document_title=resolved.document_title,
        page_url=page_image_url(resolved, base),
        printed=row.printed,
        boxes=[WireBox(x0=b.x0, y0=b.y0, x1=b.x1, y1=b.y1) for b in row.boxes],
        anchored=row.anchored,
        agrees=agrees,
        note=note,
        audits=audits,
    )


async def audits_for_field(
    pool: asyncpg.Pool[Any], library: Library, tenant: str, record_key: str, field: str
) -> list[SourceAudit]:
    """Every auditor with a current finding naming this exact (record,
    field) -- `Source.audits` (documents-plan-v3, D6). Each finding's own
    `verdict` is the field-level comparison `judge` made, which is also
    what a `Source` wants beside the box it already draws.

    Scoped to each auditor's *current* version exactly as `ui.py`'s
    `cited_audits` is (review finding C): `db.audit_findings_citing`'s
    rows are rewritten wholesale only once a reading lands under a
    redefined audit's new version, so a row from the version just
    replaced can still be sitting there when nothing has judged the page
    under the new one yet. A stale version's finding must not render as
    current on this surface either."""
    rows = await db.audit_findings_citing(pool, tenant, record_key)
    out = []
    for r in rows:
        if r["field"] != field:
            continue
        citing_audit = library.audit(r["audit"])
        if citing_audit is None or citing_audit.version != r["version"]:
            continue
        out.append(
            SourceAudit(auditor=r["audit"], verdict=r["verdict"], seen=r["seen"], note=r["note"])
        )
    return out


async def sources_for_record(
    pool: asyncpg.Pool[Any],
    store: ProvenanceStore,
    library: Library,
    tenant: str,
    kind: str,
    key: str,
    value: Mapping[str, Any],
    *,
    base: str | None,
) -> list[Source]:
    """Every field of one record a write's `provenance` map ever cited --
    the record page's "where it came from" block."""
    rows = await store.for_record(tenant, kind, key)
    return [
        await source_of(
            pool, library, tenant, row, read_field_value(value, row.field), base=base,
            record_key=key,
        )
        for row in rows
    ]


async def sources_for_members(
    pool: asyncpg.Pool[Any],
    store: ProvenanceStore,
    library: Library,
    tenant: str,
    kind: str,
    field: str,
    keys: Sequence[str],
    *,
    base: str | None,
) -> dict[str, list[Source]]:
    """`{key: [Source]}` for every one of `keys` that holds a provenance row
    for `field` -- the bulk form `GET /evidence` and the worksheet decorate
    their member/record lists with, one query for the whole list rather
    than one per row."""
    if not keys:
        return {}
    rows_by_key = await store.for_many(tenant, kind, keys)
    current = {
        r.key: r.value for r in await PostgresFactStore(pool).some(tenant, kind, list(keys))
    }
    out: dict[str, list[Source]] = {}
    for key, rows in rows_by_key.items():
        row = next((r for r in rows if r.field == field), None)
        if row is None:
            continue
        current_value = read_field_value(current.get(key, {}), field)
        out[key] = [
            await source_of(
                pool, library, tenant, row, current_value, base=base, record_key=key
            )
        ]
    return out


async def decorate_evidence(
    pool: asyncpg.Pool[Any],
    store: ProvenanceStore,
    library: Library,
    tenant: str,
    plan: FigurePlan,
    evidence: Evidence,
    *,
    base: str | None,
) -> Evidence:
    """`GET /evidence`'s decoration, shared by the authenticated API route
    and the UI's own mirror (D3): attach `sources` to every held member,
    for the one field (if any) `evidence_field` can name without touching
    the engine. `serve_evidence` itself is untouched -- this only reshapes
    the answer it already returned."""
    if evidence.kind is None or not evidence.members:
        return evidence
    field = evidence_field(plan, library)
    if field is None:
        return evidence
    keys = [m.key for m in evidence.members if m.held]
    sources_by_key = await sources_for_members(
        pool, store, library, tenant, evidence.kind, field, keys, base=base
    )
    if not sources_by_key:
        return evidence
    return evidence.model_copy(
        update={
            "members": [
                m.model_copy(update={"sources": sources_by_key[m.key]})
                if m.key in sources_by_key
                else m
                for m in evidence.members
            ]
        }
    )


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
