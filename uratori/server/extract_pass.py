"""`run_pass`'s other half: the `extract` pre-pass (documents-plan-v3, D4).

Computes which pages are stale for which extracts, runs the pure runner
(`uratori.documents.extract.run_extract`) over each, and writes what it
produces -- derived facts and their provenance -- through the same verified
upsert a host write uses, before the engine ever runs. The engine itself
never calls any of this; `uratori.server.runtime.run_pass` is the one
caller, inside the tenant's lock, in one transaction with the rest of this
module's writes (pointers are the one exception -- see `run_extracts`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from ..documents.extract import ExtractedRecord, PageExtractResult, run_extract
from ..lang.ast import SetExpr, SetIndex, SetOp, SetRef
from ..lang.plan import ExtractPlan, Library
from ..lang.source import declaration_source
from ..store.base import EngineStore, Pointer
from ..store.postgres import PostgresFactStore
from . import db
from .provenance import PostgresProvenanceStore, ProvenanceRow
from .words import WordStore

log = logging.getLogger("uratori.server")

Connection = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy


class ExtractKindError(Exception):
    """A direct write or delete against an extract's target kind through
    the facts route -- refused in the route, exactly as a document or
    page kind is (`uratori.server.documents.DocumentKindError`): a derived
    record's only writer is the extract that computes it."""


def refuse_extract_kind_writes(
    library: Library,
    writes: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    deletes: Mapping[str, Sequence[str]] | None,
) -> None:
    """The facts route's door for extract-produced kinds. Called beside
    `refuse_document_kind_writes`, before anything else runs, for the same
    reason: `verify_writes` ignores deletes by design and is shared with
    embedding hosts that have no extract to point at, so the refusal lives
    here, naming the extract."""
    offending = sorted((set(writes or {}) | set(deletes or {})) & set(library.extracts))
    if offending:
        names = ", ".join(offending)
        raise ExtractKindError(
            f'"{names}" {"is" if len(offending) == 1 else "are"} produced by an '
            f'`extract` declaration. {"It" if len(offending) == 1 else "They"} '
            "move only through the extract's own pass, over the page it reads -- "
            "the facts route never writes or deletes a derived kind directly."
        )


def _extract_order(library: Library) -> list[str]:
    """Every extract, in copy-dependency order -- the checker already
    refused a cycle, so a plain walk suffices here."""
    order: list[str] = []
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        plan = library.extracts.get(name)
        if plan is None:
            return
        for copied in plan.copies:
            visit(copied)
        done.add(name)
        order.append(name)

    for name in library.extracts:
        visit(name)
    return order


def _over_kinds(expr: SetExpr | None, library: Library) -> set[str]:
    """Every fact kind an extract's `over` reads -- a derived kind among
    them when the filter it names is over an extract's own target, which
    is how a page whose classification just changed reaches the extracts
    it gates even though they never copy from it."""
    if expr is None:
        return set()
    if isinstance(expr, SetOp):
        return _over_kinds(expr.left, library) | _over_kinds(expr.right, library)
    if isinstance(expr, SetIndex):
        index = library.indexes.get(expr.index)
        return {index.kind} if index is not None else set()
    assert isinstance(expr, SetRef)  # pragma: no cover - refused at check time
    return set()


async def _stale_pages(
    connection: Connection,
    facts: PostgresFactStore,
    engine_store: EngineStore,
    tenant: str,
    library: Library,
    plan: ExtractPlan,
    written: Mapping[str, Sequence[str]],
    full: bool,
) -> tuple[set[str], bool]:
    """Which pages of `plan.source` need re-extracting, and whether this
    extract's own pointer is cold (so the caller knows to re-check `full`
    pages come what may, even ones `written` says nothing about)."""
    pointer = await engine_store.pointer(tenant, plan.name)
    cold = pointer is None or pointer.version != plan.version
    if full or cold:
        rows = await facts.of_kind(tenant, plan.source)
        return {r.key for r in rows}, cold
    trigger_kinds = {plan.source, *plan.copies, *_over_kinds(plan.over, library)}
    stale: set[str] = set()
    for kind in trigger_kinds:
        stale |= set(written.get(kind, ()))
    return stale, cold


async def _resolve_derived(
    facts: PostgresFactStore,
    provenance_store: PostgresProvenanceStore,
    tenant: str,
    needed_kinds: set[str],
    page_key: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[ProvenanceRow]]]:
    bodies: dict[str, Mapping[str, Any]] = {}
    provenance: dict[str, list[ProvenanceRow]] = {}
    for kind in needed_kinds:
        rows = await facts.some(tenant, kind, [page_key])
        if rows:
            bodies[kind] = rows[0].value
            provenance[kind] = await provenance_store.for_record(tenant, kind, page_key)
    return bodies, provenance


def _with_page_field(result: PageExtractResult, page_key: str) -> PageExtractResult:
    """The built-in `page` field (D4: `page = source`, set by the engine,
    never by a matcher) -- added here, server-side, because the pure
    runner is handed an `ExtractPlan` with no view of the target fact's
    own declared fields, only the matchers. Every row of a `many`
    extract's page gets the same value: the *page's* key, never the row's.
    """
    return PageExtractResult(
        records=tuple(
            ExtractedRecord(
                key=record.key, body={**record.body, "page": page_key}, provenance=record.provenance
            )
            for record in result.records
        ),
        failures=result.failures,
    )


async def _write_page(
    connection: Connection,
    facts: PostgresFactStore,
    provenance_store: PostgresProvenanceStore,
    tenant: str,
    plan: ExtractPlan,
    page_key: str,
    result: PageExtractResult,
) -> tuple[set[str], set[str]]:
    """Replace-set per (page, extract): every key this page's extraction
    used to hold that this run did not produce is deleted; everything it
    did produce is upserted, provenance rewritten unconditionally (derived
    facts carry no stamp -- there is no stale-write race to guard against,
    only a deterministic recompute)."""
    old_keys = set(
        await db.extract_keys_for_page(connection, tenant, plan.name, page_key, many=plan.many)
    )
    new_records = {r.key: r for r in result.records}
    moved: set[str] = set()
    if new_records:
        bodies = {key: record.body for key, record in new_records.items()}
        changed = await facts.upsert(tenant, plan.name, bodies)
        moved = set(changed)
        for key, record in new_records.items():
            await provenance_store.replace(tenant, plan.name, key, list(record.provenance))
    vanished = old_keys - set(new_records)
    if vanished:
        ordered = sorted(vanished)
        await facts.delete(tenant, plan.name, ordered)
        await provenance_store.delete(tenant, plan.name, ordered)
    return moved, vanished


async def _retract_page(
    connection: Connection,
    facts: PostgresFactStore,
    provenance_store: PostgresProvenanceStore,
    tenant: str,
    plan: ExtractPlan,
    page_key: str,
) -> set[str]:
    """The page itself is gone (deleted, or simply no longer there): every
    record and failure this extract held for it, gone too."""
    old_keys = set(
        await db.extract_keys_for_page(connection, tenant, plan.name, page_key, many=plan.many)
    )
    if old_keys:
        ordered = sorted(old_keys)
        await facts.delete(tenant, plan.name, ordered)
        await provenance_store.delete(tenant, plan.name, ordered)
    await db.clear_extract_failures_for_page(connection, tenant, plan.name, plan.version, page_key)
    return old_keys


async def _retire(
    connection: Connection,
    facts: PostgresFactStore,
    provenance_store: PostgresProvenanceStore,
    engine_store: EngineStore,
    tenant: str,
    library: Library,
) -> dict[str, list[str]]:
    """An extract no longer in the definitions has its derived records
    deleted at the next pass, as a retired grouping's rows are. Any bare
    (dot-free) pointer name is unambiguously a past-or-present extract's --
    no other declaration kind is ever named bare, and a fact kind never
    gets a pointer at all."""
    pointers = await engine_store.pointers(tenant)
    retired = [name for name in pointers if "." not in name and name not in library.extracts]
    vanished: dict[str, list[str]] = {}
    for name in retired:
        rows = await facts.of_kind(tenant, name)
        keys = [r.key for r in rows]
        if keys:
            await facts.delete(tenant, name, keys)
            await provenance_store.delete(tenant, name, keys)
            vanished[name] = sorted(keys)
        await db.delete_all_extract_failures(connection, tenant, name)
    return vanished


async def run_extracts(
    connection: Connection,
    engine_store: EngineStore,
    word_store: WordStore,
    library: Library,
    tenant: str,
    *,
    written: Mapping[str, Sequence[str]],
    deleted: Mapping[str, Sequence[str]],
    full: bool,
    now_ms: float,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Run every extract, in copy order, over the pages it needs to; return
    the moved and vanished derived keys the caller merges into `written`/
    `deleted` before `facade.run`. Empty when the library declares none."""
    if not library.extracts:
        return {}, {}

    facts = PostgresFactStore(connection)
    provenance_store = PostgresProvenanceStore(connection)
    moved: dict[str, list[str]] = {}
    vanished: dict[str, list[str]] = {}

    for name in _extract_order(library):
        plan = library.extracts[name]
        target = library.facts[plan.name]
        writes_page_field = any(f.name == "page" and f.type == "text" for f in target.fields)
        stale, cold = await _stale_pages(
            connection, facts, engine_store, tenant, library, plan, written, full
        )
        deleted_pages = set(deleted.get(plan.source, ()))
        needed_kinds = {*plan.copies, *_over_kinds(plan.over, library)}

        page_moved: set[str] = set()
        page_vanished: set[str] = set()

        for page_key in sorted(deleted_pages):
            page_vanished |= await _retract_page(
                connection, facts, provenance_store, tenant, plan, page_key
            )

        for page_key in sorted(stale - deleted_pages):
            page_rows = await facts.some(tenant, plan.source, [page_key])
            if not page_rows:
                page_vanished |= await _retract_page(
                    connection, facts, provenance_store, tenant, plan, page_key
                )
                continue
            words = await word_store.words_of(tenant, plan.source, page_key)
            derived_on_page, derived_provenance_on_page = await _resolve_derived(
                facts, provenance_store, tenant, needed_kinds, page_key
            )
            result = run_extract(
                plan,
                page_key=page_key,
                page_record=page_rows[0].value,
                words=words,
                derived_on_page=derived_on_page,
                derived_provenance_on_page=derived_provenance_on_page,
                indexes=library.indexes,
                now_ms=now_ms,
            )
            if writes_page_field:
                result = _with_page_field(result, page_key)
            produced, page_level_vanished = await _write_page(
                connection, facts, provenance_store, tenant, plan, page_key, result
            )
            page_moved |= produced
            page_vanished |= page_level_vanished

            succeeded = {r.key for r in result.records}
            if result.failures:
                await db.replace_extract_failures(
                    connection,
                    tenant,
                    plan.name,
                    plan.version,
                    [(f.subject, f.field, f.reason) for f in result.failures],
                )
            await db.clear_extract_failures(
                connection, tenant, plan.name, plan.version, sorted(succeeded)
            )

        if page_moved:
            moved.setdefault(plan.name, []).extend(sorted(page_moved))
        if page_vanished:
            vanished.setdefault(plan.name, []).extend(sorted(page_vanished))

        # `figure_pointer.version` is a foreign key into `figure_definition`
        # (`uratori/store/postgres.py`'s `SCHEMA_SQL`) -- the engine's own
        # pointer table, reused here for a bare extract name (D4.1's
        # design note in `db.py`). `ensure_definition` is idempotent
        # (`on conflict do nothing`) and every figure's own pointer write
        # calls it right beside `set_pointer` for the same reason: the row
        # the FK needs must exist before the pointer can name its version.
        await engine_store.ensure_definition(
            plan.version,
            plan.name,
            "extract",
            plan.doc,
            "",
            declaration_source(library, plan.name, "extract") or "",
            {"source": plan.source, "many": plan.many},
        )
        pointer_moved = await engine_store.set_pointer(
            tenant, plan.name, Pointer(version=plan.version, settings_fingerprint="")
        )
        if pointer_moved:
            await db.prune_extract_failures(connection, tenant, plan.name, plan.version)
        elif cold:  # pragma: no cover - set_pointer reports False only when unchanged
            log.warning(
                "extract %s ran a cold pass for %s but its pointer did not move", name, tenant
            )

    retired_vanished = await _retire(
        connection, facts, provenance_store, engine_store, tenant, library
    )
    for kind, keys in retired_vanished.items():
        vanished.setdefault(kind, []).extend(keys)

    return moved, vanished
