"""What a running service holds, shared by every router.

Split out of `app.py` the day the built-in UI arrived: its routes live in a
module of their own but must answer from the same world, the same 409s and
the same facade wiring as the API proper. Two copies of "what does ready
mean" would let the two surfaces disagree about whether the server is taught,
which is exactly the kind of ambient drift `World` exists to prevent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

from ..engine.activity import shown_changes
from ..facade import DEFAULT_TRAILING, RunReport, Uratori
from ..lang.check import WorldConflict, compile_source
from ..lang.plan import Library
from ..schema import Schema
from ..store.postgres import PostgresEngineStore, PostgresFactStore
from . import db
from .blobs import BlobStore
from .contract import RunOut, ShownChange, schema_out
from .documents import RenderCache
from .extract_pass import run_extracts
from .hub import Hub
from .provenance import PostgresProvenanceStore, ProvenanceStore
from .words import WordStore

log = logging.getLogger("uratori.server")


@dataclass
class World:
    """What this deployment has been taught, held compiled in memory."""

    schema: Schema
    schema_document: dict[str, Any]
    source: str | None
    library: Library | None
    refusal: str | None = None
    """Why `library` is None when a source IS stored: this build's compiler
    refused it (an upgrade across a language change). Carried so `ready` can
    say the truth -- "no definitions have been loaded" points the operator at
    the wrong fix when the real one is a corrected PUT /definitions."""


class State:
    """Typed app state -- `app.state` is `Any`, and `Any` is how a renamed
    attribute becomes a request-time AttributeError instead of a mypy error."""

    def __init__(
        self,
        pool: asyncpg.Pool[Any],
        token: str | None,
        version: str,
        *,
        blob_store: BlobStore | None = None,
        word_store: WordStore | None = None,
        render_cache: RenderCache | None = None,
        blob_dir: str | None = None,
        max_upload_bytes: int = 50 * 1024 * 1024,
        provenance_store: ProvenanceStore | None = None,
    ) -> None:
        self.pool = pool
        self.token = token
        self.version = version
        self.world: World | None = None
        self.hub = Hub()
        self.locks: dict[str, asyncio.Lock] = {}
        self.teach = asyncio.Lock()
        self.blob_store = blob_store
        self.word_store = word_store
        self.render_cache = render_cache
        self.provenance_store: ProvenanceStore = provenance_store or PostgresProvenanceStore(pool)
        """Needs only `pool` (`document_provenance` is a plain server table,
        D2) -- unlike `blob_store`/`word_store`, never gated behind
        `URATORI_BLOB_DIR`: a tenant with no documents feature simply never
        has a page to cite, and the facts route's own write-time validation
        is what actually refuses a citation naming one."""
        self.blob_dir = blob_dir
        """Set only to say whether `URATORI_BLOB_DIR` was configured --
        `blob_store`/`render_cache` are `None` exactly when this is, and the
        documents routes 409 naming the variable when they are."""
        self.max_upload_bytes = max_upload_bytes
        """Serialises every write to the world (schema, definitions, the
        editor's save). The check-then-write in each of those awaits the
        database between reading `self.world` and swapping it, and two
        writers interleaving across that await would both pass their
        preconditions -- the editor's edited-since-loaded refusal exists
        precisely to prevent the silent overwrite that allows."""

    def lock_for(self, tenant: str) -> asyncio.Lock:
        held = self.locks.get(tenant)
        if held is None:
            held = asyncio.Lock()
            self.locks[tenant] = held
        return held


def state_of(request: Request) -> State:
    return request.app.state.uratori  # type: ignore[no-any-return]


def ready(s: State) -> tuple[World, Library]:
    """The world, or the 409 that explains what is missing.

    409 rather than 500: an unconfigured server is a state the client can
    fix (declare a schema, load definitions), and it must be told which.
    """
    if s.world is None:
        raise HTTPException(status_code=409, detail="No schema has been declared yet")
    if s.world.library is None:
        if s.world.refusal is not None:
            # Definitions ARE stored; saying "none loaded" would send the
            # operator hunting a data loss when the fix is a re-PUT.
            raise HTTPException(
                status_code=409,
                detail=(
                    "The stored definitions do not compile under this build: "
                    f"{s.world.refusal}. PUT /definitions with corrected source."
                ),
            )
        raise HTTPException(status_code=409, detail="No definitions have been loaded yet")
    return s.world, s.world.library


def documents_ready(s: State) -> tuple[BlobStore, WordStore, RenderCache]:
    """The document runtime, or the 409 that names the missing variable.

    `URATORI_BLOB_DIR` is required the moment any document kind is declared
    -- a document-shaped fact with nowhere to put its bytes is a
    configuration gap, not a 500 the first time somebody uploads something.
    """
    if s.blob_store is None or s.word_store is None or s.render_cache is None:
        raise HTTPException(
            status_code=409,
            detail="URATORI_BLOB_DIR is not set. A document-shaped fact (`as "
            "document` / `as page of`) needs somewhere to put its bytes; set it "
            "to a writable directory and restart.",
        )
    return s.blob_store, s.word_store, s.render_cache


def taught_schema(world: World) -> Schema:
    """The world as taught, whichever door taught it.

    A fact-taught library carries the kinds and name fields; the declared
    schema then holds nothing of its own. Every surface that *describes* the world
    (the UI's kind list, its record names) reads through this, so those
    surfaces cannot disagree -- the facade does the same completion
    internally. The one deliberate exception is `GET /schema`, which answers
    the stored document in exactly the PUT shape: in a fact-taught world the
    kinds live on `GET /definitions`, and the docs say so.
    """
    if world.library is None:
        return world.schema
    return world.schema.taught_by(world.library)


def compile_for_teach(
    source: str, world: World
) -> tuple[Library, Schema, dict[str, Any], bool]:
    """Compile a candidate source exactly the way `PUT /definitions` teaches.

    Shared between the API door and the built-in editor so the two cannot
    drift: a source the editor's dry-run check accepts must be a source the
    save (and the API) accepts, adoption rules included. A source that brings
    its own facts refuses a schema that also declares kinds -- but a live
    schema-taught deployment must be able to adopt facts without blanking its
    definitions first, so the conflict retires the schema's kinds, name fields
    and url fields in the same compile: the source is the truth, and the retry
    proves the new world whole before anything is persisted. Any other refusal
    propagates verbatim as the `DefinitionError` it is; each door shapes its
    own response from it.

    The final element says whether that retirement happened. It is part of
    what a save changes -- record names and links stop coming from the schema
    -- and a diff that walked only the declarations would state none of it.
    """
    schema = world.schema
    try:
        return compile_source(source, schema), schema, world.schema_document, False
    except WorldConflict:
        stripped = dataclasses.replace(
            schema, kinds=frozenset(), name_fields={}, url_fields={}
        )
        library = compile_source(source, stripped)
        return library, stripped, schema_out(stripped).model_dump(), True


async def record_pass(s: State, tenant: str, cause: str, *, full: bool, out: RunOut) -> None:
    """Freeze the pass into the run log, inside the tenant's lock so the
    log's order is the order the passes actually ran in. The log is data,
    not a UI feature -- it records whether or not the UI is mounted,
    because the question it answers ("what did that fact cascade to")
    is asked after the fact by definition.

    A logging failure is swallowed, loudly: by the time this runs the
    facts and values are committed, so raising would answer 500 for a
    pass that happened -- and the retry that provokes would find nothing
    changed, log a quiet run, and bury the real cascade for good. A hole
    in the log is the smaller lie, and the error log says where it is.
    """
    try:
        await db.record_run(
            s.pool,
            tenant,
            cause,
            full=full,
            written=out.written,
            deleted=out.deleted,
            changed=out.changed,
            rebuilt=out.rebuilt,
            covered=out.covered,
            shown=[c.model_dump() for c in out.shown],
        )
    except Exception:
        log.exception("the pass for %s ran but could not be recorded", tenant)


def run_out(
    report: RunReport,
    world: World,
    library: Library,
    *,
    written: int,
    deleted: int,
    include_results: bool = True,
) -> RunOut:
    # `include_results=False` is the `serve: false` caller: the pass may
    # still have evaluated results (the server's own socket subscribers need
    # them), but the caller asked for the moved names instead of the
    # payloads, and handing both would make the lean request pay the fat
    # response.
    return RunOut(
        written=written,
        deleted=deleted,
        changed=len(report.outcome.changes),
        rebuilt=list(report.outcome.rebuilt),
        carried=list(report.outcome.carried),
        covered=sorted(report.outcome.covered),
        shown=[
            ShownChange(
                figure=c.figure,
                subject_id=c.subject_id,
                kind=c.kind,
                label=c.label,
                before_display=c.before_display,
                after_display=c.after_display,
                unit=c.unit,
                weight=c.weight,
            )
            for c in shown_changes(list(report.outcome.changes), library)
        ],
        results=list(report.results) if include_results else [],
        moved=sorted(report.moved),
    )


def known_names(library: Library) -> frozenset[str]:
    """Every name a subscription entry can stand on in this library -- what
    a teach hands the hub so entries on retired definitions are ended with a
    stated reason instead of going quiet for ever."""
    return frozenset(
        [plan.name for plan in library.figures]
        + [reading.name for reading in library.readings]
        + [plan.name for plan in library.projections]
        + [summary.name for summary in library.summaries]
        + [bundle.name for bundle in library.bundles]
    )


async def run_pass(
    s: State,
    world: World,
    library: Library,
    tenant: str,
    *,
    written: Mapping[str, Sequence[str]] | None = None,
    deleted: Mapping[str, Sequence[str]] | None = None,
    full: bool = False,
    serve: bool = True,
) -> RunReport:
    """The one way a pass starts (documents-plan-v3, D4). Every route that
    moves facts or asks for a bare pass -- the facts route, `POST /runs`,
    the UI's run button, and the documents upload/delete/reocr routes --
    calls this instead of `facade.run` directly, so `extract`'s pre-pass
    (run here, inside the tenant's lock the caller already holds, before
    the engine ever sees the batch) is never skipped by a route that
    forgot it.

    Runs every `extract` over the pages that need it, writes what they
    produce through the verified upsert in its own transaction, then
    merges the moved and vanished derived keys into `written`/`deleted`
    before calling `facade.run`. A library that declares no `extract` pays
    nothing beyond the one dict copy -- this is exactly `facade.run` for
    every host before this MR.
    """
    # `facade.run` treats `written`/`deleted` being *present at all* (even
    # an empty dict), not merely non-empty, as the sync moment every
    # projection re-serves on (`is not None`, not truthiness -- a batch
    # that deduplicated to nothing is still the sync). `or {}` below would
    # silently collapse "the facts door was used and reported nothing
    # moved" into "no door was used at all", so the two cases are tracked
    # separately and only folded back together at the very end.
    written_opened = written is not None
    deleted_opened = deleted is not None
    merged_written = {k: list(v) for k, v in (written or {}).items()}
    merged_deleted = {k: list(v) for k, v in (deleted or {}).items()}
    if library.extracts:
        _blobs, word_store, _cache = documents_ready(s)
        engine_store = PostgresEngineStore(s.pool)
        async with s.pool.acquire() as connection, connection.transaction():
            moved, vanished = await run_extracts(
                connection,
                engine_store,
                word_store,
                library,
                tenant,
                written=merged_written,
                deleted=merged_deleted,
                full=full,
                now_ms=time.time() * 1000.0,
            )
        for kind, keys in moved.items():
            merged_written[kind] = sorted(set(merged_written.get(kind, ())) | set(keys))
            written_opened = True
        for kind, keys in vanished.items():
            merged_deleted[kind] = sorted(set(merged_deleted.get(kind, ())) | set(keys))
            deleted_opened = True
    facade = facade_for(s, world, library)
    return await facade.run(
        tenant,
        written=merged_written if written_opened else None,
        deleted=merged_deleted if deleted_opened else None,
        full=full,
        serve=serve,
    )


def facade_for(s: State, world: World, library: Library) -> Uratori:
    # No listener is wired here any more, deliberately: the facade's listener
    # hook carries the default-argument results and nothing else, and a
    # subscription entry is answered at ITS OWN arguments -- which needs the
    # facade and the pass's moved set together. `push_pass` is that delivery,
    # called by every route that runs a pass; the listener hook remains the
    # embedding host's mechanism.
    return Uratori(
        schema=world.schema,
        library=library,
        store=PostgresEngineStore(s.pool),
        facts=PostgresFactStore(s.pool),
        # `facade_for` is the server's one construction site, and the
        # server as a whole is what provides `run_pass`'s `extract`
        # pre-pass -- this facade may be built here between passes too
        # (for `verify`, `answer`, delivery), never only in the instant
        # after a pre-pass just ran. See `Uratori.__init__` for why that
        # is what this flag means and why nothing outside this module may
        # ever pass it.
        _extract_pass=bool(library.extracts),
    )


async def push_pass(
    s: State,
    tenant: str,
    facade: Uratori,
    report: RunReport,
) -> None:
    """Deliver one pass to the socket, both interests at once.

    Firehose subscribers get the pass's own re-served answers -- the same
    objects the HTTP response carries. Entry subscribers get each
    watched-and-moved entry, evaluated once per distinct entry at the entry's
    own arguments. Called inside the tenant's pass lock by every route that
    runs a pass, so two passes cannot interleave their deliveries out of
    order.
    """
    await s.hub.publish(tenant, report.results)

    from .hub import Entry

    async def evaluate(entry: Entry) -> Any:
        return await facade.answer(
            tenant,
            entry.name,
            trailing=entry.windows if entry.windows is not None else DEFAULT_TRAILING,
        )

    await s.hub.serve_entries(tenant, report.moved, evaluate)
