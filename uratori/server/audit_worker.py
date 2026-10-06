"""The `audit` worker: one background task, one page at a time.

The provider call is the expensive, slow, fallible step, so it happens
*outside* `lock_for(tenant)` -- a model call must never hold up every fact
push and every pass for that tenant. Everything that touches storage
happens under the lock, in one short critical section: re-check the page
is still held with the same `words_sha` (otherwise the reading arrived for
a page that has since moved, and is discarded rather than stored against
a word layer it no longer describes), store the reading, judge it, and
`accept` the verdict.

Work discovery is a query (`db.unread_pages`), not a durable queue, backed
by `audit_lease` so a restart or a second replica cannot double-pay for
the same page (`documents-plan-v3`, D6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..audit.fake import FakeAuditProvider
from ..audit.judge import AuditReading, judge
from ..audit.prompt import build_prompt
from ..audit.provider import AuditProvider, FieldToRead
from ..lang.ast import NumberAfter
from ..lang.plan import AuditPlan, Library
from ..store.postgres import PostgresEngineStore, PostgresFactStore
from . import db
from .audit_pass import dump_fields, dump_finding, page_of, verified_fields_of
from .blobs import BlobStore
from .documents import render_page_png
from .runtime import State, World, facade_for, push_pass

log = logging.getLogger("uratori.audit.worker")

LEASE_TTL_SECONDS = 300.0
"""How long a claimed page stays claimed before another sweep may retry
it -- long enough for a slow provider call, short enough that a crashed
worker's leases age out inside one operator's coffee break."""

POLL_SECONDS = 15.0


def fields_to_read(library: Library, audit: AuditPlan) -> list[FieldToRead]:
    """What the model is told, D6's own list: name, type, unit, the `#`
    prose of each verified field -- never an extract's alternatives."""
    out: list[FieldToRead] = []
    for extract_name in audit.verifies:
        extract = library.extracts.get(extract_name)
        fact = library.facts.get(extract_name)
        if extract is None or fact is None:  # pragma: no cover - checker refuses this
            continue
        declared = {f.name: f for f in fact.fields}
        for field in extract.fields:
            units = field.matcher.units if isinstance(field.matcher, NumberAfter) else ()
            decl = declared.get(field.name)
            out.append(
                FieldToRead(
                    extract=extract_name,
                    field=field.name,
                    type=decl.type if decl is not None else None,
                    units=tuple(units),
                    prose=decl.doc if decl is not None else "",
                )
            )
    return out


async def resolve_bindings(
    facts: PostgresFactStore,
    engine_store: PostgresEngineStore,
    tenant: str,
    library: Library,
    audit: AuditPlan,
    page_key: str,
) -> dict[str, str]:
    """The `read:` block's bindings, resolved to this page's actual
    values -- what the template's `{name}` placeholders print."""
    out: dict[str, str] = {}
    page_rows = await facts.some(tenant, audit.scope, [page_key])
    page_body = page_rows[0].value if page_rows else {}
    for b in audit.reads:
        value: object = None
        if b.kind == "extract_field" and b.field is not None:
            rows = await facts.some(tenant, b.source, [page_key])
            value = rows[0].value.get(b.field) if rows else None
        elif b.kind == "document_field" and b.field is not None:
            doc_id = page_body.get("document_id")
            if doc_id:
                doc_rows = await facts.some(tenant, b.source, [str(doc_id)])
                value = doc_rows[0].value.get(b.field) if doc_rows else None
        elif b.kind == "verdict":
            other = library.audit(b.source)
            if other is not None:
                stored = await engine_store.value(tenant, b.source, other.version, page_key)
                value = stored.value if stored is not None else "unaudited"
        out[b.name] = "" if value is None else str(value)
    return out


async def render_page_image(
    blobs: BlobStore,
    facts: PostgresFactStore,
    tenant: str,
    library: Library,
    audit: AuditPlan,
    page_key: str,
    page_body: dict[str, Any],
) -> bytes | None:
    """The page's own rendered image, for the provider -- `None` rather
    than raised when a piece is missing (an unrendered page audits blind
    on the word layer alone rather than failing the whole reading)."""
    page_fact = library.facts.get(audit.scope)
    doc_kind = page_fact.page_of if page_fact is not None else None
    number = page_body.get("number")
    if doc_kind is None or number is None:
        return None
    doc_id = page_body.get("document_id")
    if doc_id is None:
        return None
    doc_rows = await facts.some(tenant, doc_kind, [str(doc_id)])
    if not doc_rows:
        return None
    sha256 = doc_rows[0].value.get("sha256")
    if not sha256:
        return None
    data = await blobs.open(tenant, str(sha256))
    if data is None:
        return None
    try:
        return await asyncio.to_thread(render_page_png, data, int(number), 1.0)
    except Exception:  # pragma: no cover - a malformed page must not stop the worker
        log.warning("could not render %s/%s for an audit reading", tenant, page_key, exc_info=True)
        return None


async def read_one_page(
    s: State,
    world: World,
    library: Library,
    audit: AuditPlan,
    tenant: str,
    page_key: str,
    provider: AuditProvider,
) -> bool:
    """One page, start to finish. Returns whether a reading was actually
    stored (false when the page moved out from under the provider call and
    the reading was discarded)."""
    from .runtime import documents_ready  # local import: avoids a cycle at module load

    blobs, word_store, _cache = documents_ready(s)
    facts = PostgresFactStore(s.pool)
    page_rows = await facts.some(tenant, audit.scope, [page_key])
    if not page_rows:
        return False
    words_sha = str(page_rows[0].value.get("words_sha") or "")
    words = await word_store.words_of(tenant, audit.scope, page_key)
    fields = fields_to_read(library, audit)
    engine_store = PostgresEngineStore(s.pool)
    bound = await resolve_bindings(facts, engine_store, tenant, library, audit, page_key)
    prompt = build_prompt(audit, fields, bound)
    page_png = await render_page_image(
        blobs, facts, tenant, library, audit, page_key, dict(page_rows[0].value)
    )

    # The provider call: outside `lock_for(tenant)`, deliberately -- this
    # function is never itself called from inside one.
    reading = await provider.read_page(
        prompt=prompt, model=audit.model, page_png=page_png, words=words, fields=fields,
        page_key=page_key,
    )

    async with s.lock_for(tenant):
        current = await facts.some(tenant, audit.scope, [page_key])
        if not current or str(current[0].value.get("words_sha") or "") != words_sha:
            # The page moved (a re-ingest, a re-OCR, a deletion) while the
            # provider call was in flight: this reading describes a word
            # layer that is no longer the page's current one, and storing
            # it would silently mislabel the next pass's judging.
            return False
        await db.replace_audit_reading(
            s.pool,
            tenant,
            audit.name,
            audit.version,
            page_key,
            words_sha=words_sha,
            prompt=prompt,
            model=audit.model,
            response=reading.response,
            parsed=dump_fields(reading.fields),
        )
        verified_fields = verified_fields_of(library, audit)
        words_by_id = {w.id: w for w in words}
        current_rows: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for extract_name in audit.verifies:
            for row in await facts.of_kind(tenant, extract_name):
                if page_of(row.key) == page_key:
                    current_rows.setdefault(extract_name, []).append((row.key, dict(row.value)))
        verdict, members, findings = judge(
            AuditReading(
                page_key=page_key,
                words_sha=words_sha,
                prompt=prompt,
                model=audit.model,
                response=reading.response,
                fields=reading.fields,
            ),
            current_rows,
            verified_fields,
            words_by_id,
        )
        await db.replace_audit_findings(
            s.pool,
            tenant,
            audit.name,
            audit.version,
            page_key,
            [dump_finding(f) for f in findings],
        )
        facade = facade_for(s, world, library)
        report = await facade.accept(tenant, audit.name, page_key, verdict, members, page_key)
        await push_pass(s, tenant, facade, report)
    return True


async def run_worker_sweep(s: State, world: World, library: Library, provider: AuditProvider) -> int:
    """One pass over every tenant's unread pages, for every declared
    audit. Returns the count actually read, so the caller's loop can back
    off when there is nothing to do."""
    if not library.audits:
        return 0
    read = 0
    facts = PostgresFactStore(s.pool)
    for row in await db.list_tenants(s.pool):
        tenant = str(row["tenant"])
        for audit in library.audits.values():
            candidates = [
                (r.key, str(r.value.get("words_sha") or ""))
                for r in await facts.of_kind(tenant, audit.scope)
            ]
            todo = await db.unread_pages(s.pool, tenant, audit.name, audit.version, candidates)
            for page_key in todo:
                claimed = await db.claim_audit_lease(
                    s.pool, tenant, audit.name, audit.version, page_key, ttl_seconds=LEASE_TTL_SECONDS
                )
                if not claimed:
                    continue
                try:
                    if await read_one_page(s, world, library, audit, tenant, page_key, provider):
                        read += 1
                finally:
                    await db.release_audit_lease(s.pool, tenant, audit.name, audit.version, page_key)
    return read


def provider_from_env(env: dict[str, str] | None = None) -> AuditProvider | None:
    """`URATORI_AUDIT_PROVIDER`: `"fake"` (deterministic, no network, for
    tests and demos), `"claude"` (the Anthropic SDK, behind the `audit`
    extra), or unset -- no worker runs, every page stays `unaudited`, and
    the declaration page says why (D6)."""
    import os

    name = (env or os.environ).get("URATORI_AUDIT_PROVIDER")
    if not name:
        return None
    if name == "fake":
        return FakeAuditProvider()
    if name == "claude":
        from ..audit.claude import ClaudeAuditProvider

        return ClaudeAuditProvider()
    raise ValueError(
        f"URATORI_AUDIT_PROVIDER={name!r} is not one of \"fake\", \"claude\"."
    )


async def run_worker_loop(
    s: State, get_world: Any, provider: AuditProvider, *, poll_seconds: float = POLL_SECONDS
) -> None:
    """The lifespan task: sweep, sleep, repeat, forever, swallowing one
    sweep's failure rather than taking the server down with it -- a
    provider outage must not become a server outage."""
    while True:
        try:
            world = get_world()
            if world is not None and world.library is not None:
                await run_worker_sweep(s, world, world.library, provider)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - exercised via run_worker_sweep directly
            log.exception("audit worker sweep failed")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(poll_seconds)
