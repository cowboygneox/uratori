"""The service: the engine behind an HTTP API and a websocket.

One container, one Postgres database, one world. A host starts it, declares
its schema, loads its definitions, and from then on pushes facts and reads
answers -- every calculation, every cascade and every served number happens in
here, so the host's own code never holds a second copy of any arithmetic.

Design decisions a reader should not have to rediscover:

- **One world per deployment.** The schema and the definitions are global;
  tenants are data partitions under them. Two products get two containers,
  because "which definitions" is exactly the kind of ambient state that must
  not vary per request.
- **Passes are serialised per tenant.** The engine's warm path reads bucket
  membership before writing it, so two concurrent passes over one tenant could
  interleave those reads and writes into a state neither pass computed. A lock
  per tenant is the whole fix; passes for different tenants still overlap.
- **The websocket carries exactly the objects the routes return**, delivered
  by `push_pass` from every route that runs a pass. The facade's listener
  hook remains the embedding host's mechanism, but this server outgrew it
  the day subscriptions arrived: an entry is re-answered at ITS OWN
  arguments, which needs the facade and the pass's moved set together, and
  the listener carries neither.
- **Configuration survives restart, compiled state does not.** The schema
  document and the definitions *source* are persisted; the library is
  recompiled from source at boot, because the source is the truth and a stored
  artifact read back would let a stale copy decide what the server computes.
"""


import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)

from ..facade import DEFAULT_TRAILING
from ..lang.check import compile_source
from ..lang.lex import DefinitionError
from ..lang.plan import CompiledFactField, Library
from ..results import BundleResult, Evidence, Result
from ..store.postgres import PostgresFactStore
from ..verify import FactError
from ..windows import WindowError, WindowSpec, expand_window_args
from . import db
from . import ui as builtin_ui
from .audit_worker import provider_from_env, run_worker_loop
from .blobs import BlobStore, FilesystemBlobStore
from .contract import (
    Ack,
    AnyResult,
    AuditFindingOut,
    AuditFindingsOut,
    DeclarationOut,
    DefinitionsIn,
    DeleteDocumentOut,
    DocumentOut,
    DocumentsOut,
    Envelope,
    ExtractFailureOut,
    ExtractFailuresOut,
    FactFieldOut,
    FactOut,
    FactsIn,
    Health,
    LibraryOut,
    PageWordsOut,
    ReocrOut,
    RunIn,
    RunOut,
    SchemaIn,
    Subscribe,
    SubscribeEntry,
    TenantRemoved,
    UploadOut,
    WordOut,
    schema_out,
)
from .documents import (
    DEFAULT_MAX_UPLOAD_BYTES,
    DocumentKindError,
    DocumentParseError,
    RenderCache,
    document_id_of,
    document_kinds,
    ingest_pdf,
    page_key,
    refuse_document_kind_writes,
    render_page_png,
    sha256_hex,
    uploaded_at_now,
    words_sha_of,
)
from .extract_pass import ExtractKindError, refuse_extract_kind_writes
from .hub import Client, Entry
from .provenance import (
    PostgresProvenanceStore,
    ProvenanceError,
    ProvenanceRow,
    decorate_evidence,
    validate_and_build,
)
from .runtime import (
    State,
    World,
    compile_for_teach,
    documents_ready,
    facade_for,
    known_names,
    push_pass,
    ready,
    record_pass,
    run_out,
    run_pass,
    state_of,
)
from .words import PostgresWordStore, Word

log = logging.getLogger("uratori.server")


async def _document_out(
    blob_store: BlobStore, tenant: str, kind: str, document_id: str, value: dict[str, Any]
) -> DocumentOut:
    """One document fact, decorated with whether its blob is actually on
    disk -- a row whose file is missing renders `held: false` with a
    reason, never a 500."""
    sha = str(value.get("sha256") or "")
    held = True
    reason: str | None = None
    if not sha or not await blob_store.exists(tenant, sha):
        held = False
        reason = "this document's bytes are missing from blob storage"
    return DocumentOut(
        kind=kind,
        id=document_id,
        title=cast('str | None', value.get("title")),
        mime=str(value.get("mime") or ""),
        sha256=sha,
        pages=int(value.get("pages") or 0),
        uploaded_at=cast('str | None', value.get("uploaded_at")),
        held=held,
        reason=reason,
    )


def create_app(
    *,
    dsn: str | None = None,
    token: str | None = None,
    version: str | None = None,
    pg_schema: str | None = None,
    ui: bool | None = None,
    ui_edit: bool | None = None,
    ui_documents: bool | None = None,
    frame_ancestors: str | None = None,
    blob_dir: str | None = None,
) -> FastAPI:
    """Build the service. Parameters override the environment, for tests and
    for embedding; production reads DATABASE_URL / URATORI_TOKEN / APP_VERSION,
    plus URATORI_UI / URATORI_UI_EDIT / URATORI_UI_FRAME_ANCESTORS for the
    built-in UI."""
    resolved_dsn = dsn or os.environ.get("DATABASE_URL")
    resolved_token = token if token is not None else os.environ.get("URATORI_TOKEN")
    resolved_version = version or os.environ.get("APP_VERSION", "dev")
    resolved_ui = (
        ui if ui is not None else _ui_default(os.environ.get("URATORI_UI"), resolved_token)
    )
    resolved_edit = (
        ui_edit
        if ui_edit is not None
        else _edit_default(os.environ.get("URATORI_UI_EDIT"), resolved_token, resolved_ui)
    )
    if resolved_edit and not resolved_ui:
        # A grant for an editor that is not mounted is a configuration
        # contradiction: whoever set it believes editing is on somewhere.
        # Refused rather than ignored, for the same reason as a junk value --
        # a security-relevant flag that silently does nothing is a surprise
        # deferred to the worst moment.
        raise RuntimeError("URATORI_UI_EDIT is granted but the UI itself is off")
    resolved_ui_documents = (
        ui_documents
        if ui_documents is not None
        else _ui_documents_default(os.environ.get("URATORI_UI_DOCUMENTS"), resolved_token, resolved_ui)
    )
    if resolved_ui_documents and not resolved_ui:
        raise RuntimeError("URATORI_UI_DOCUMENTS is granted but the UI itself is off")
    resolved_blob_dir = blob_dir or os.environ.get("URATORI_BLOB_DIR")
    resolved_max_upload = int(
        os.environ.get("URATORI_DOCUMENT_MAX_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    )
    resolved_ancestors = (
        frame_ancestors or os.environ.get("URATORI_UI_FRAME_ANCESTORS") or "'self'"
    )
    if any(forbidden in resolved_ancestors for forbidden in ("\r", "\n")):
        # The value is pasted into a response header; a newline in it would
        # let configuration smuggle arbitrary headers past every proxy.
        raise RuntimeError("URATORI_UI_FRAME_ANCESTORS must not contain newlines")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not resolved_dsn:
            raise RuntimeError(
                "DATABASE_URL is not set. uratori keeps facts, computed values and its "
                "own configuration in Postgres; there is no file-based fallback."
            )
        pool = await db.open_server_pool(resolved_dsn, pg_schema=pg_schema)
        await db.ensure_schema(pool)
        blob_store = FilesystemBlobStore(resolved_blob_dir) if resolved_blob_dir else None
        render_cache = (
            RenderCache(Path(resolved_blob_dir) / ".render-cache")
            if resolved_blob_dir
            else None
        )
        state = State(
            pool,
            resolved_token,
            resolved_version,
            blob_store=blob_store,
            word_store=PostgresWordStore(pool) if resolved_blob_dir else None,
            render_cache=render_cache,
            blob_dir=resolved_blob_dir,
            max_upload_bytes=resolved_max_upload,
        )
        held = await db.load_world(pool)
        if held is not None:
            document, source = held
            schema = SchemaIn(**document).build()
            boot_refusal: str | None = None
            try:
                # `is not None`, not truthiness: an explicitly saved empty
                # source is a taught (empty) library that answered ready
                # before the restart, and a boot that quietly demoted it to
                # "no definitions loaded" would make the same stored state
                # ready on one side of a restart and unready on the other.
                library = compile_source(source, schema) if source is not None else None
            except DefinitionError as refusal:
                # A stored source an older engine wrote, refused by this
                # build's compiler -- an upgrade across a language change.
                # Re-raising would crash-loop the container with the only
                # fix, a corrected PUT /definitions, locked out behind the
                # crash; unready-with-a-schema is a state every client
                # already knows how to repair.
                library = None
                boot_refusal = str(refusal)
                log.error(
                    "stored definitions no longer compile under this build: %s "
                    "-- serving unready; PUT /definitions with corrected source",
                    refusal,
                )
            state.world = World(
                schema=schema,
                schema_document=document,
                source=source,
                library=library,
                refusal=boot_refusal,
            )
            log.info(
                "world restored: %d figures, %d readings",
                len(library.figures) if library else 0,
                len(library.readings) if library else 0,
            )
        app.state.uratori = state
        # The audit worker (documents-plan-v3, D6): one task, started here
        # like every other long-lived piece of server state, reading
        # `state.world` fresh on every sweep so a redeployed definition (a
        # new auditor, a changed one) is picked up without a restart.
        # `provider_from_env` returns `None` with no env var set, and the
        # declaration page is what then says why every page stays
        # `unaudited` -- there is no 409 and no log spam for the common
        # case of a deployment that never configured one.
        worker_provider = provider_from_env()
        worker_task = (
            asyncio.create_task(run_worker_loop(state, lambda: state.world, worker_provider))
            if worker_provider is not None
            else None
        )
        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            await pool.close()

    app = FastAPI(title="uratori", version=resolved_version, lifespan=lifespan)

    async def authed(request: Request) -> None:
        expected = request.app.state.uratori.token
        if expected is None:
            return
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, f"Bearer {expected}"):
            raise HTTPException(status_code=401, detail="Bad or missing bearer token")

    S = Annotated[State, Depends(state_of)]
    auth = Depends(authed)

    # `ready` and `facade_for` live in runtime.py, shared with the built-in
    # UI's router, so the two surfaces cannot disagree about what "taught"
    # means or how the facade is wired.

    # ------------------------------------------------------------- health --

    @app.get("/health", response_model=Health)
    async def health(s: S) -> Health:
        world = s.world
        library = world.library if world is not None else None
        return Health(
            ok=True,
            version=s.version,
            ready=library is not None,
            figures=len(library.figures) if library else 0,
            readings=len(library.readings) if library else 0,
        )

    # -------------------------------------------------------------- world --

    @app.get("/schema", response_model=SchemaIn, dependencies=[auth])
    async def get_schema(s: S) -> SchemaIn:
        if s.world is None:
            raise HTTPException(status_code=404, detail="No schema has been declared yet")
        return schema_out(s.world.schema)

    @app.put("/schema", response_model=Ack, dependencies=[auth])
    async def put_schema(body: SchemaIn, s: S) -> Ack:
        """Declare (or replace) the world.

        When definitions are already loaded they are recompiled against the new
        schema *before* anything is persisted: a schema change that breaks the
        definitions is refused whole, because persisting it would leave a server
        that cannot rebuild its own library at the next boot.
        """
        try:
            schema = body.build()
        except ValueError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

        # Under the teach lock like every other world writer: this reads the
        # held source and swaps the world across an await, and interleaving
        # with a definitions save would revert whichever landed first.
        async with s.teach:
            source = s.world.source if s.world is not None else None
            library = s.world.library if s.world is not None else None
            held_refusal: str | None = None
            if source:
                try:
                    library = compile_source(source, schema)
                except DefinitionError as refusal:
                    if library is not None:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                "the loaded definitions do not compile under this schema: "
                                f"{refusal}"
                            ),
                        ) from refusal
                    # The stored source already failed this build's compiler --
                    # there is no working library the schema could break. Refusing
                    # here would lock the host's own teach order (schema first,
                    # then definitions) out of the repair the boot path promised.
                    library = None
                    held_refusal = str(refusal)

            document = body.model_dump()
            await db.save_world(s.pool, document, source)
            s.world = World(
                schema=schema,
                schema_document=document,
                source=source,
                library=library,
                refusal=held_refusal,
            )
        return Ack(ok=True)

    @app.get("/definitions", response_model=LibraryOut, dependencies=[auth])
    async def get_definitions(s: S) -> LibraryOut:
        _world, library = ready(s)
        return _library_out(library)

    @app.put("/definitions", response_model=LibraryOut, dependencies=[auth])
    async def put_definitions(body: DefinitionsIn, s: S) -> LibraryOut:
        if s.world is None:
            raise HTTPException(
                status_code=409,
                detail="Declare a schema before loading definitions; they compile against it",
            )
        # The compile, adoption rules included, is `compile_for_teach` --
        # shared with the built-in editor so a source its dry-run check
        # accepts is a source this door accepts, and vice versa. The teach
        # lock serialises the read-compile-write against every other world
        # writer; without it two teaches interleaving across the save's
        # await would each persist over the other's swap.
        async with s.teach:
            try:
                library, schema, document, _adopted = compile_for_teach(body.source, s.world)
            except DefinitionError as refusal:
                raise HTTPException(status_code=422, detail=str(refusal)) from refusal
            await db.save_world(s.pool, document, body.source)
            s.world = World(
                schema=schema,
                schema_document=document,
                source=body.source,
                library=library,
            )
        # Entries standing on definitions this teach removed would never
        # appear in a moved set again -- ended now, with the same refusal a
        # fresh subscribe would earn, instead of going quiet for ever.
        await s.hub.retire_entries(known_names(library))
        return _library_out(library)

    # -------------------------------------------------------------- facts --

    @app.post("/tenants/{tenant}/facts", response_model=RunOut, dependencies=[auth])
    async def post_facts(tenant: str, body: FactsIn, s: S) -> RunOut:
        """Apply one batch of fact movement and run the pass it implies.

        Deletes before writes, writes before the engine: the engine reads the
        buckets a deleted record held to work out whose numbers move, and the
        fact table is what the departed-subject sweep walks -- both need the
        table to already say what the batch said.

        Verification comes before any of it, and refuses the batch whole: a
        record that does not match the declared world must land nowhere, and
        landing its batch-mates while dropping it would narrow a population
        by a cheap path. The 422 names the kind, key and field, because the
        fix is in the host's mapping.
        """
        world, library = ready(s)
        facade = facade_for(s, world, library)
        if body.defer and body.full:
            # `full` demands the most expensive pass and `defer` demands none;
            # honouring either would silently ignore the other.
            raise HTTPException(
                status_code=422,
                detail="defer and full contradict each other: defer skips the pass, "
                "full forces the biggest one. Send the batches with defer, then "
                'close the import with POST /tenants/{tenant}/runs {"full": true}.',
            )
        try:
            refuse_document_kind_writes(library, body.writes, body.deletes)
            refuse_extract_kind_writes(library, body.writes, body.deletes)
            facade.verify(body.writes, body.deletes)
        except DocumentKindError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal
        except ExtractKindError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal
        except FactError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

        # Parsed and verified before anything is written, same as the body:
        # a citation naming a field this batch's write does not carry, a
        # page not held, or a word id the page's layer does not have 422s
        # the whole batch (documents-plan-v3, D2). Needs the documents
        # runtime only because a citation needs a page to resolve against --
        # a tenant with no `provenance` in this batch never reaches it.
        provenance_rows: dict[tuple[str, str], list[ProvenanceRow]] = {}
        if body.provenance:
            _blobs, word_store, _cache = documents_ready(s)
            try:
                provenance_rows = await validate_and_build(
                    s.pool, word_store, library, tenant, body.writes, body.provenance
                )
            except ProvenanceError as refusal:
                raise HTTPException(status_code=422, detail=str(refusal)) from refusal

        async with s.lock_for(tenant):
            # One transaction for the whole mutation: verification is the
            # first line of defence, but a value it missed (or a database
            # refusal it could not foresee) must fail the batch whole, not
            # leave the kinds written before the bad one persisted and the
            # rest gone -- the half-applied batch is the same narrowed
            # population as a quarantined record.
            async with s.pool.acquire() as connection, connection.transaction():
                facts = PostgresFactStore(connection)
                provenance_store = PostgresProvenanceStore(connection)
                for kind, keys in body.deletes.items():
                    await facts.delete(tenant, kind, keys)
                    await provenance_store.delete(tenant, kind, keys)
                moved: dict[str, list[str]] = {}
                written = 0
                for kind, records in body.writes.items():
                    if not records:
                        continue
                    # Read before the write, not after (`admitted_keys`'s own
                    # docstring): the guard's comparison is against the
                    # *pre-write* stamp, and provenance must land only for
                    # the keys the guard actually admitted -- a stale write
                    # that the body refused must not carry a fresh box for a
                    # value that was never stored.
                    admitted = set(
                        await facts.admitted_keys(
                            tenant, kind, records, stamps=body.stamps.get(kind)
                        )
                    )
                    changed = await facts.upsert(
                        tenant, kind, records, stamps=body.stamps.get(kind)
                    )
                    written += len(changed)
                    if changed:
                        moved[kind] = changed
                    for key in records:
                        if key not in admitted:
                            continue
                        cited = provenance_rows.get((kind, key))
                        if cited is not None:
                            await provenance_store.replace(tenant, kind, key, cited)
            if body.defer:
                # The batch is landed and verified; the pass is the caller's
                # to run. No results are re-served because nothing recomputed
                # -- an empty list is the honest shape, where re-serving the
                # stored answers would present pre-import values as this
                # batch's outcome. The debt row is what makes the obligation
                # enforceable rather than documentary: a caller who closes
                # the import any way other than the documented full run
                # would otherwise be served stale values as current, for
                # ever, with nothing to see.
                await db.mark_deferred(s.pool, tenant)
                out = RunOut(
                    written=written,
                    deleted=sum(len(v) for v in body.deletes.values()),
                    changed=0,
                    rebuilt=[],
                    covered=[],
                    shown=[],
                    results=[],
                )
                await record_pass(s, tenant, "facts-deferred", full=False, out=out)
                return out
            full = body.full or await db.deferred(s.pool, tenant)
            # `serve: false` skips evaluating the full default results ONLY
            # when no firehose subscriber needs them anyway: the caller may
            # own its own delivery, but the server's socket owns its own
            # subscribers, and their paint must not depend on which HTTP
            # client happened to trigger the pass.
            serve = body.serve or s.hub.wants_everything(tenant)
            report = await run_pass(
                s,
                world,
                library,
                tenant,
                written=moved,
                deleted={k: list(v) for k, v in body.deletes.items()},
                full=full,
                serve=serve,
            )
            if full:
                # Settled after the pass actually ran: a debt cleared up
                # front would be forgiven, not paid, if the pass died.
                await db.clear_deferred(s.pool, tenant)
            out = run_out(
                report,
                world,
                library,
                written=written,
                deleted=sum(len(v) for v in body.deletes.values()),
                include_results=body.serve,
            )
            await record_pass(s, tenant, "facts", full=full, out=out)
            await push_pass(s, tenant, facade, report)
        return out

    @app.post("/tenants/{tenant}/runs", response_model=RunOut, dependencies=[auth])
    async def post_run(tenant: str, body: RunIn, s: S) -> RunOut:
        """A pass with no new facts: pick up a redeployed
        definition, or (with `full`) rebuild everything from what is stored."""
        world, library = ready(s)
        async with s.lock_for(tenant):
            if body.audit:
                if body.audit is True:
                    names = list(library.audits)
                else:
                    if body.audit not in library.audits:
                        raise HTTPException(
                            status_code=422,
                            detail=f'no audit named "{body.audit}". Declared: '
                            f'{", ".join(sorted(library.audits)) or "none"}.',
                        )
                    names = [body.audit]
                for name in names:
                    await db.discard_audit_readings(s.pool, tenant, name)
            # A discarded reading leaves a stale verdict sitting in
            # `figure_value` until something re-judges that page; forcing
            # `full` here is what makes the re-audit verb immediately
            # answer `unaudited` for every page it just cleared, rather
            # than a stale "agrees" surviving until the worker's next
            # sweep produces a fresh reading.
            full = body.full or bool(body.audit) or await db.deferred(s.pool, tenant)
            serve = body.serve or s.hub.wants_everything(tenant)
            report = await run_pass(s, world, library, tenant, full=full, serve=serve)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(
                report,
                world,
                library,
                written=0,
                deleted=0,
                include_results=body.serve,
            )
            await record_pass(s, tenant, "run", full=full, out=out)
            facade = facade_for(s, world, library)
            await push_pass(s, tenant, facade, report)
        return out

    # ------------------------------------------------------------ results --

    # `at` anchors window readings on a chosen day instead of today: an ISO
    # date, resolved by the engine to that day's end in each reading's own
    # zone. An argument like `trailing` -- windows and their anchor are the
    # things a client may choose, because both only move which stored days
    # take part; the calculation itself is hashed into the version and no
    # query parameter reaches it.
    def windows_of(trailing: list[str] | None) -> list[WindowSpec] | None:
        """The window parameter, validated or refused as a 422.

        Each value is a span of positions in the reading's own bucket
        sequence: `30` (the last 30 buckets, bucket 1 the anchor bucket),
        `31-60` (the 30 before them), or `each:1-12` (one window per
        bucket, expanded here so the sugar and the enumerated spelling are
        one request). Malformed specs are 422s, never coerced -- a coerced
        window is a plausible population nobody asked for. The reach check
        happens deeper, where the reading's bucket rule is known, and
        surfaces as the same 422.
        """
        if trailing is None:
            return None
        try:
            return list(expand_window_args(trailing))
        except WindowError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

    def anchor_of(at: str | None) -> str | None:
        """The anchor, validated as a bare calendar day or refused as a 422.

        A `str` validated by hand rather than a pydantic `date`, because
        pydantic's lax mode coerces bare integers as unix timestamps --
        `?at=1782000000` would quietly become 2026-06-21 and serve a
        plausible window nobody asked for, when the caller who sends epoch
        seconds (or millis, same acceptance) needs to be told the parameter
        is a day. The spelling is pinned to YYYY-MM-DD first, so ISO's
        undashed basic form is refused too rather than depending on which
        parser this build's `fromisoformat` happens to be.
        """
        if at is None:
            return None
        refusal = HTTPException(
            status_code=422,
            detail=f"`at` must be a calendar day, YYYY-MM-DD; got {at!r}",
        )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", at):
            raise refusal
        try:
            return date.fromisoformat(at).isoformat()
        except ValueError:
            raise refusal from None

    # Bundles serve here too since the push made them a first-paint surface
    # -- except on an anchored read, where the facade leaves them off for the
    # same reason `answer` refuses `at` on a bundle by name (`results`'s own
    # docstring carries the full argument).
    @app.get(
        "/tenants/{tenant}/results", response_model=list[AnyResult], dependencies=[auth]
    )
    async def get_results(
        tenant: str,
        s: S,
        trailing: Annotated[list[str] | None, Query()] = None,
        at: Annotated[str | None, Query()] = None,
    ) -> list[Result | BundleResult]:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            return list(
                await facade.results(
                    tenant,
                    trailing=windows_of(trailing) or DEFAULT_TRAILING,
                    at=anchor_of(at),
                )
            )
        except WindowError as refusal:
            # A span whose unit cannot slice a served reading's storage --
            # only discoverable here, where the library is. Refused whole
            # rather than serving the list with that reading quietly absent:
            # a response silently shorter than the library is a narrowed
            # population wearing a clean status code.
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

    # A bundle answers here too, as a `BundleResult` -- the wrapper carrying
    # its members' ordinary Results in declaration order. `kind` is the
    # discriminator between the two shapes, so a typed client branches on a
    # field rather than sniffing.
    @app.get(
        "/tenants/{tenant}/results/{name}",
        response_model=Result | BundleResult,
        dependencies=[auth],
    )
    async def get_result(
        tenant: str,
        name: str,
        s: S,
        trailing: Annotated[list[str] | None, Query()] = None,
        at: Annotated[str | None, Query()] = None,
        subject: Annotated[list[str] | None, Query()] = None,
    ) -> Result | BundleResult:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            result = await facade.answer(
                tenant,
                name,
                trailing=windows_of(trailing) or DEFAULT_TRAILING,
                at=anchor_of(at),
                subject=subject,
            )
        except WindowError as refusal:
            # Before the ValueError arm, deliberately: a WindowError is a
            # ValueError, and a window the caller can fix is a 422 where a
            # 400 says the engine refused the request in its own terms.
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal
        except ValueError as refusal:
            raise HTTPException(status_code=400, detail=str(refusal)) from refusal
        except NotImplementedError as gap:
            raise HTTPException(status_code=501, detail=str(gap)) from gap
        if result is None:
            raise HTTPException(status_code=404, detail=f"No definition called {name}")
        return result

    @app.get(
        "/tenants/{tenant}/evidence/{name}", response_model=Evidence, dependencies=[auth]
    )
    async def get_evidence(tenant: str, name: str, subject: str, s: S) -> Evidence:
        """The records behind one stored value.

        The engine has always stored the citation -- every value is written
        with the record ids it was computed from -- and this serves it: a
        bucket of durations read "1.0h, 2.0h" and this is what says which
        records those were. Figures only, because a figure is the only
        declaration that stores; the facade's refusals each say where the
        evidence actually lives, and they travel as the 404 detail.
        """
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            answer = await facade.evidence(
                tenant, name, subject
            )
        except LookupError as refusal:
            raise HTTPException(status_code=404, detail=str(refusal)) from refusal
        if answer is None:
            plan = library.figure(name)
            version = plan.version if plan is not None else "?"
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Nothing is stored for {subject} under {name}@{version}. If the "
                    "row you came from showed a value, a rebuild has landed between "
                    "the two reads."
                ),
            )
        plan = library.figure(name)
        if plan is None:
            return answer
        return await decorate_evidence(
            s.pool,
            s.provenance_store,
            library,
            tenant,
            plan,
            answer,
            base=f"/tenants/{tenant}/documents",
        )

    # ----------------------------------------------------------- documents --
    #
    # `fact <kind> as document:` / `fact <kind> as page of <kind>`
    # (docs/documents.md, D1). This is the one provider for document and
    # page kinds -- the facts route above refuses a direct write or delete
    # against either (`refuse_document_kind_writes`). Parsing, rendering and
    # OCR run outside `s.lock_for(tenant)`; only the database write of facts
    # and the word layer takes the lock.

    def _document_kind_or_404(library: Library, kind: str) -> str:
        kinds = document_kinds(library)
        page_kind = kinds.get(kind)
        if page_kind is None:
            raise HTTPException(
                status_code=404,
                detail=f'"{kind}" is not a document kind. Those are: '
                f'{", ".join(sorted(kinds)) or "none"}.',
            )
        return page_kind

    @app.post(
        "/tenants/{tenant}/documents/{kind}", response_model=UploadOut, dependencies=[auth]
    )
    async def upload_document(
        tenant: str,
        kind: str,
        s: S,
        file: Annotated[UploadFile, File()],
        record: Annotated[str | None, Form()] = None,
    ) -> UploadOut:
        """Upload one file. `record` is an optional JSON object of host
        fields beside the shape's own, verified against the kind's
        declaration exactly as the facts route verifies a write. A re-upload
        of bytes already held under this kind is `written: 0` -- the
        document id is content-derived, so it is the same upload, not a
        second record of the same file."""
        world, library = ready(s)
        page_kind = _document_kind_or_404(library, kind)
        blob_store, _words, _cache = documents_ready(s)

        data = await file.read()
        if len(data) > s.max_upload_bytes:
            raise HTTPException(
                status_code=422,
                detail=f"the upload is {len(data)} bytes, over the "
                f"{s.max_upload_bytes}-byte limit (URATORI_DOCUMENT_MAX_BYTES).",
            )
        host_fields: dict[str, Any] = {}
        if record is not None:
            try:
                parsed = json.loads(record)
            except json.JSONDecodeError as refusal:
                raise HTTPException(
                    status_code=422, detail=f"record is not valid JSON: {refusal}"
                ) from refusal
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=422, detail="record must be a JSON object")
            host_fields = parsed

        sha = sha256_hex(data)
        document_id = document_id_of(sha)
        existing = await db.document_by_sha(s.pool, tenant, kind, sha)
        if existing is not None:
            held = await db.fact_record(s.pool, tenant, kind, existing)
            pages = int(held["value"].get("pages") or 0) if held is not None else 0
            empty = RunOut(
                written=0, deleted=0, changed=0, rebuilt=[], covered=[], shown=[], results=[]
            )
            return UploadOut(id=existing, written=0, pages=pages, run=empty)

        # The slow part: parsing the PDF's own text layer and, for any page
        # without one, rendering it and running OCR. A pure function of the
        # bytes, run off the event loop and outside any lock.
        try:
            ingested = await asyncio.to_thread(ingest_pdf, data)
        except DocumentParseError as refusal:
            # A clean 4xx naming the file, never a 500 off an unhandled
            # `pypdfium2.PdfiumError` for a non-PDF or corrupt upload
            # (review finding F).
            raise HTTPException(
                status_code=422,
                detail=f"{file.filename or 'the uploaded file'} is {refusal}",
            ) from refusal

        document_fields: dict[str, Any] = {
            **host_fields,
            "title": host_fields.get("title") or file.filename or document_id,
            "mime": file.content_type or "application/pdf",
            "sha256": sha,
            "pages": len(ingested.pages),
            "uploaded_at": uploaded_at_now(),
        }
        page_records: dict[str, dict[str, Any]] = {}
        page_words: dict[str, list[Word]] = {}
        for number, page in enumerate(ingested.pages, start=1):
            key = page_key(document_id, number)
            page_records[key] = {
                "document_id": document_id,
                "number": number,
                "text_source": page.text_source,
                "words_sha": words_sha_of(page.words),
            }
            page_words[key] = list(page.words)

        writes = {kind: {document_id: document_fields}, page_kind: page_records}
        facade = facade_for(s, world, library)
        try:
            facade.verify(writes, None)
        except FactError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

        # Bytes land first, then the DB rows in one transaction (D1): a
        # crash between the two leaves an unreferenced file, swept by
        # tenant removal or an explicit admin sweep, never silently served.
        await blob_store.put(tenant, data)

        async with s.lock_for(tenant):
            async with s.pool.acquire() as connection, connection.transaction():
                await db.record_document(connection, tenant, kind, document_id, sha)
                facts = PostgresFactStore(connection)
                moved: dict[str, list[str]] = {}
                for write_kind, records in writes.items():
                    changed = await facts.upsert(tenant, write_kind, records)
                    if changed:
                        moved[write_kind] = changed
                word_rows = PostgresWordStore(connection)
                for key, words in page_words.items():
                    await word_rows.put(tenant, page_kind, key, words)
            full = await db.deferred(s.pool, tenant)
            report = await run_pass(s, world, library, tenant, written=moved, full=full)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(
                report, world, library, written=sum(len(v) for v in moved.values()), deleted=0
            )
            await record_pass(s, tenant, "documents", full=full, out=out)
            await push_pass(s, tenant, facade, report)

        return UploadOut(id=document_id, written=1, pages=len(ingested.pages), run=out)

    @app.get(
        "/tenants/{tenant}/documents/{kind}", response_model=DocumentsOut, dependencies=[auth]
    )
    async def list_documents(
        tenant: str,
        kind: str,
        s: S,
        after: Annotated[str | None, Query()] = None,
        q: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> DocumentsOut:
        _world, library = ready(s)
        _document_kind_or_404(library, kind)
        blob_store, _words, _cache = documents_ready(s)
        rows, more, total = await db.page_facts(
            s.pool, tenant, kind, after=after, q=q, limit=limit
        )
        documents = [
            await _document_out(blob_store, tenant, kind, row["key"], row["value"])
            for row in rows
        ]
        return DocumentsOut(documents=documents, more=more, total=total)

    @app.get(
        "/tenants/{tenant}/documents/{kind}/{document_id}",
        response_model=DocumentOut,
        dependencies=[auth],
    )
    async def get_document(tenant: str, kind: str, document_id: str, s: S) -> DocumentOut:
        _world, library = ready(s)
        _document_kind_or_404(library, kind)
        blob_store, _words, _cache = documents_ready(s)
        row = await db.fact_record(s.pool, tenant, kind, document_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no document {document_id!r} of kind {kind!r}"
            )
        return await _document_out(blob_store, tenant, kind, document_id, row["value"])

    @app.get(
        "/tenants/{tenant}/documents/{kind}/{document_id}/pages/{number}.png",
        dependencies=[auth],
    )
    async def get_page_png(
        tenant: str,
        kind: str,
        document_id: str,
        number: int,
        s: S,
        scale: Annotated[float, Query(gt=0, le=10)] = 1.5,
    ) -> Response:
        _world, library = ready(s)
        _document_kind_or_404(library, kind)
        blob_store, _words, render_cache = documents_ready(s)
        row = await db.fact_record(s.pool, tenant, kind, document_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no document {document_id!r} of kind {kind!r}"
            )
        pages = int(row["value"].get("pages") or 0)
        if not 1 <= number <= pages:
            raise HTTPException(
                status_code=404, detail=f"document {document_id!r} has {pages} page(s)"
            )
        sha = str(row["value"]["sha256"])
        cached = await asyncio.to_thread(render_cache.get, tenant, sha, number, scale)
        if cached is not None:
            return Response(content=cached, media_type="image/png")
        data = await blob_store.open(tenant, sha)
        if data is None:
            # Never a 500: a row whose file is missing on disk is a stated
            # gap, not an opaque failure.
            raise HTTPException(
                status_code=404,
                detail="this document's bytes are missing from blob storage",
            )
        png = await asyncio.to_thread(render_page_png, data, number, scale)
        await asyncio.to_thread(render_cache.put, tenant, sha, number, scale, png)
        return Response(content=png, media_type="image/png")

    @app.get(
        "/tenants/{tenant}/documents/{kind}/{document_id}/pages/{number}/words",
        response_model=PageWordsOut,
        dependencies=[auth],
    )
    async def get_page_words(
        tenant: str, kind: str, document_id: str, number: int, s: S
    ) -> PageWordsOut:
        _world, library = ready(s)
        page_kind = _document_kind_or_404(library, kind)
        _blobs, word_store, _cache = documents_ready(s)
        key = page_key(document_id, number)
        words = await word_store.words_of(tenant, page_kind, key)
        return PageWordsOut(
            words=[
                WordOut(
                    id=w.id,
                    text=w.text,
                    x0=w.x0,
                    y0=w.y0,
                    x1=w.x1,
                    y1=w.y1,
                    line=w.line,
                    source=w.source,
                    confidence=w.confidence,
                )
                for w in words
            ]
        )

    @app.delete(
        "/tenants/{tenant}/documents/{kind}/{document_id}",
        response_model=DeleteDocumentOut,
        dependencies=[auth],
    )
    async def delete_document(
        tenant: str, kind: str, document_id: str, s: S
    ) -> DeleteDocumentOut:
        world, library = ready(s)
        page_kind = _document_kind_or_404(library, kind)
        blob_store, _words, _cache = documents_ready(s)
        facade = facade_for(s, world, library)

        async with s.lock_for(tenant):
            row = await db.fact_record(s.pool, tenant, kind, document_id)
            if row is None:
                raise HTTPException(
                    status_code=404, detail=f"no document {document_id!r} of kind {kind!r}"
                )
            pages = int(row["value"].get("pages") or 0)
            sha = str(row["value"]["sha256"])
            page_keys = [page_key(document_id, n) for n in range(1, pages + 1)]

            async with s.pool.acquire() as connection, connection.transaction():
                facts = PostgresFactStore(connection)
                await facts.delete(tenant, page_kind, page_keys)
                await facts.delete(tenant, kind, [document_id])
                await PostgresWordStore(connection).delete(tenant, page_kind, page_keys)
                # Document and page kinds are refused from facts-route
                # writes entirely (`refuse_document_kind_writes`), so
                # neither ever holds provenance of its own today -- this is
                # defensive, matching "deleted with the record" literally
                # against the day a derived kind (D4) is keyed as a page.
                await PostgresProvenanceStore(connection).delete(
                    tenant, page_kind, page_keys
                )
                await PostgresProvenanceStore(connection).delete(tenant, kind, [document_id])
                # `_remove_departed`'s own audit sweep (engine.py) removes a
                # deleted page's *value*; the reading and findings it was
                # judged from are server-owned storage the engine never
                # sees, so they are cleared here, alongside the page's own
                # facts and word layer, the same way provenance is.
                await db.delete_audit_readings_for_pages(connection, tenant, page_keys)
                await db.delete_documents(connection, tenant, kind, [document_id])
                # Checked AFTER this row is gone, in the same transaction:
                # blobs are keyed `(tenant, sha256)` alone, no kind, so two
                # document kinds holding identical bytes (same sha256,
                # therefore the same content-derived document_id) share one
                # blob. Unlinking it here unconditionally would destroy an
                # unrelated, still-live document under another kind the
                # moment the two happened to collide on content.
                still_referenced = await db.document_sha_referenced(connection, tenant, sha)

            # Rows before the file (D1): a reader racing this delete sees
            # the fact gone before the bytes are, never the other way.
            # Only when no other document row of this tenant -- any kind --
            # still names these bytes.
            if not still_referenced:
                await blob_store.delete(tenant, sha)

            # A warm pass with the deleted keys, named here as the intent --
            # `engine.py`'s `_remove_departed` already handles a deletion
            # without a full rebuild. NOTE (deviation, see the package
            # report): `Uratori.execute` (`facade.py`) currently escalates
            # to a full pass whenever `deleted` is non-empty, a pre-existing
            # cross-cutting safety rule outside this package's scope to
            # change, so this delete runs full today regardless of the
            # `full=False` stated below.
            deleted = {kind: [document_id], page_kind: page_keys}
            full = await db.deferred(s.pool, tenant)
            report = await run_pass(s, world, library, tenant, deleted=deleted, full=full)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(report, world, library, written=0, deleted=len(page_keys) + 1)
            await record_pass(s, tenant, "documents", full=full, out=out)
            await push_pass(s, tenant, facade, report)

        return DeleteDocumentOut(ok=True, run=out)

    @app.post(
        "/tenants/{tenant}/documents/{kind}/{document_id}/reocr",
        response_model=ReocrOut,
        dependencies=[auth],
    )
    async def reocr_document(
        tenant: str, kind: str, document_id: str, s: S
    ) -> ReocrOut:
        """Re-run word extraction (text layer, then OCR where there is none)
        against the stored bytes -- the operator verb for "re-ingest under a
        better renderer or OCR pass". A page whose word layer actually
        changes gets a new `words_sha`, which is a page-fact change every
        extract downstream notices; a page whose layer comes back identical
        moves nothing."""
        world, library = ready(s)
        page_kind = _document_kind_or_404(library, kind)
        blob_store, _words, _cache = documents_ready(s)
        facade = facade_for(s, world, library)

        row = await db.fact_record(s.pool, tenant, kind, document_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no document {document_id!r} of kind {kind!r}"
            )
        sha = str(row["value"]["sha256"])
        data = await blob_store.open(tenant, sha)
        if data is None:
            raise HTTPException(
                status_code=404,
                detail="this document's bytes are missing from blob storage",
            )

        try:
            ingested = await asyncio.to_thread(ingest_pdf, data)
        except DocumentParseError as refusal:
            # Same boundary rule as the upload route (review finding F):
            # the stored bytes themselves are what failed to parse this
            # time, never a 500.
            raise HTTPException(
                status_code=422,
                detail=f"document {document_id!r}'s stored bytes are {refusal}",
            ) from refusal
        page_records: dict[str, dict[str, Any]] = {}
        page_words: dict[str, list[Word]] = {}
        for number, page in enumerate(ingested.pages, start=1):
            key = page_key(document_id, number)
            page_records[key] = {
                "document_id": document_id,
                "number": number,
                "text_source": page.text_source,
                "words_sha": words_sha_of(page.words),
            }
            page_words[key] = list(page.words)

        try:
            facade.verify({page_kind: page_records}, None)
        except FactError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal

        async with s.lock_for(tenant):
            async with s.pool.acquire() as connection, connection.transaction():
                facts = PostgresFactStore(connection)
                changed = await facts.upsert(tenant, page_kind, page_records)
                word_rows = PostgresWordStore(connection)
                for key, words in page_words.items():
                    await word_rows.put(tenant, page_kind, key, words)
            moved = {page_kind: changed} if changed else {}
            full = await db.deferred(s.pool, tenant)
            report = await run_pass(s, world, library, tenant, written=moved, full=full)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(report, world, library, written=len(changed), deleted=0)
            await record_pass(s, tenant, "documents", full=full, out=out)
            await push_pass(s, tenant, facade, report)

        return ReocrOut(pages_changed=len(changed), run=out)

    @app.get(
        "/tenants/{tenant}/extracts/{name}/failures",
        response_model=ExtractFailuresOut,
        dependencies=[auth],
    )
    async def get_extract_failures(tenant: str, name: str, s: S) -> ExtractFailuresOut:
        """Every subject this extract could not read, under its *current*
        version -- the authoring loop's input: read these back, hand them
        with each page's word layer to whoever is revising the
        declaration, and iterate (documents-plan-v3, D4). A version other
        than this build's own is never served here; a failure under a
        retired version explains nothing a reader of today's declaration
        can act on, and `run_pass` prunes those rows the moment the
        extract's pointer actually moves to the current one."""
        from ..lang.source import declaration_source

        _world, library = ready(s)
        plan = library.extracts.get(name)
        if plan is None:
            raise HTTPException(status_code=404, detail=f'no extract named "{name}"')
        _blobs, word_store, _cache = documents_ready(s)
        rows = await db.extract_failures(s.pool, tenant, name, plan.version)
        out: list[ExtractFailureOut] = []
        for row in rows:
            subject = str(row["subject"])
            page = subject.split("#r", 1)[0]
            words = await word_store.words_of(tenant, plan.source, page)
            out.append(
                ExtractFailureOut(
                    subject=subject,
                    field=row["field"],
                    reason=str(row["reason"]),
                    page_key=page,
                    words=[
                        WordOut(
                            id=w.id,
                            text=w.text,
                            x0=w.x0,
                            y0=w.y0,
                            x1=w.x1,
                            y1=w.y1,
                            line=w.line,
                            source=w.source,
                            confidence=w.confidence,
                        )
                        for w in words
                    ],
                )
            )
        return ExtractFailuresOut(
            extract=name,
            version=plan.version,
            declaration=declaration_source(library, name, "extract") or "",
            failures=out,
        )

    @app.get(
        "/tenants/{tenant}/audits/{name}/findings",
        response_model=AuditFindingsOut,
        dependencies=[auth],
    )
    async def get_audit_findings(tenant: str, name: str, s: S) -> AuditFindingsOut:
        """Every page this audit currently disputes (`disagrees` or
        `missed`), under its *current* version, with each disputed field's
        finding and the page's own word layer -- `ExtractFailuresOut`'s
        twin for the auditor half of the authoring loop
        (documents-plan-v3, D6). Driven off `figure_value` (the audit's
        own stored verdicts), not off `audit_finding` directly, so a page
        re-defined out from under a lingering pre-version finding row
        never surfaces as a false positive."""
        from ..lang.source import declaration_source

        _world, library = ready(s)
        plan = library.audits.get(name)
        if plan is None:
            raise HTTPException(status_code=404, detail=f'no audit named "{name}"')
        _blobs, word_store, _cache = documents_ready(s)
        counts = await db.audit_verdict_counts(s.pool, tenant, name, plan.version)
        disputed = await db.audit_disputed_pages(
            s.pool, tenant, name, plan.version, ["disagrees", "missed"]
        )
        out: list[AuditFindingOut] = []
        for disputed_page in sorted(disputed):
            rows = await db.audit_findings_for_page(s.pool, tenant, name, disputed_page)
            words = await word_store.words_of(tenant, plan.scope, disputed_page)
            words_by_id = {w.id: w for w in words}
            for row in rows:
                if row["verdict"] not in ("disagrees", "missed"):
                    continue
                cited = [words_by_id[i] for i in row["word_ids"] if i in words_by_id]
                out.append(
                    AuditFindingOut(
                        page_key=disputed_page,
                        extract=row["extract"],
                        field=row["field"],
                        record=row["record"],
                        row=row["row_index"],
                        verdict=row["verdict"],
                        seen=row["seen"],
                        extracted=row["extracted"],
                        anchored=row["anchored"],
                        seen_text=row["seen_text"],
                        note=row["note"],
                        words=[
                            WordOut(
                                id=w.id,
                                text=w.text,
                                x0=w.x0,
                                y0=w.y0,
                                x1=w.x1,
                                y1=w.y1,
                                line=w.line,
                                source=w.source,
                                confidence=w.confidence,
                            )
                            for w in cited
                        ],
                    )
                )
        return AuditFindingsOut(
            audit=name,
            version=plan.version,
            declaration=declaration_source(library, name, "audit") or "",
            verdict_counts=counts,
            unaudited=counts.get("unaudited", 0),
            findings=out,
        )

    # ------------------------------------------------------------ tenants --

    @app.delete("/tenants/{tenant}", response_model=TenantRemoved, dependencies=[auth])
    async def delete_tenant(tenant: str, s: S) -> TenantRemoved:
        async with s.lock_for(tenant):
            # Read before `remove_tenant` deletes the rows that name them:
            # blobs are tenant-namespaced, so every sha256 this tenant's
            # `document` rows hold is a file only this delete can orphan.
            shas = await db.tenant_document_shas(s.pool, tenant)
            (
                facts,
                values,
                documents,
                provenance,
                extract_failures,
                audit_readings,
            ) = await db.remove_tenant(s.pool, tenant)
            if s.blob_store is not None:
                for sha in shas:
                    await s.blob_store.delete(tenant, sha)
        return TenantRemoved(
            facts_removed=facts,
            values_removed=values,
            documents_removed=documents,
            provenance_removed=provenance,
            extract_failures_removed=extract_failures,
            audit_readings_removed=audit_readings,
        )

    # ------------------------------------------------------------- socket --

    @app.websocket("/stream")
    async def stream(socket: WebSocket) -> None:
        s: State = socket.app.state.uratori
        if s.token is not None:
            # Header only, never a query parameter: a query string lands in
            # every access and proxy log between here and the client, and a
            # logged credential is a stored one.
            offered = socket.headers.get("authorization", "")
            if not hmac.compare_digest(offered, f"Bearer {s.token}"):
                # Accepted, then closed with 4401, and the order matters:
                # uvicorn renders a close-before-accept as a bare 403
                # handshake rejection, which a browser's WebSocket API cannot
                # tell from a network fault -- so a client retrying network
                # faults would retry an auth problem for ever. Accepting
                # first is what delivers the code; nothing is sent in
                # between, so nothing leaks.
                await socket.accept()
                await socket.close(code=4401)
                return
        await socket.accept()
        client = Client(socket=socket)
        await s.hub.join(client)
        try:
            while True:
                raw = await socket.receive_text()
                frame = _parse(raw)
                if frame is None:
                    continue
                if frame.type == "ping":
                    await s.hub.send(client, Envelope(type="pong"))
                    continue
                if frame.type == "unsubscribe":
                    # By the same identity subscribe added, or everything when
                    # the frame names nothing -- a client going quiet on
                    # purpose. An identity that never parses never matched an
                    # added entry either, so it is simply not there to remove.
                    if frame.entries is None:
                        client.firehose = False
                        client.entries.clear()
                        continue
                    for asked in frame.entries:
                        entry = _entry_of(asked)
                        if entry is not None:
                            client.entries.pop(entry.key(), None)
                    continue
                tenant = frame.tenant or client.tenant_id
                if tenant is None:
                    await s.hub.send(
                        client, Envelope(type="error", message="subscribe names a tenant")
                    )
                    continue
                if tenant != client.tenant_id:
                    # A tenant switch resets the interest wholesale: entries
                    # made while watching one board silently following
                    # another is exactly the cross-board leak a reset makes
                    # impossible by construction.
                    client.tenant_id = tenant
                    client.firehose = False
                    client.entries.clear()
                world = s.world
                if frame.entries is None:
                    # The original contract, unchanged: everything, at the
                    # serving defaults -- the current answers now, every
                    # re-served answer thereafter. Under the tenant's pass
                    # lock, deliberately: a pass landing between this fetch
                    # and the flag taking effect would push to a client the
                    # hub does not know yet and then be overwritten by this
                    # older paint -- a screen stale until the next movement,
                    # with nothing saying so.
                    client.firehose = True
                    if world is not None and world.library is not None:
                        facade = facade_for(s, world, world.library)
                        async with s.lock_for(tenant):
                            results = await facade.results(
                                tenant
                            )
                            for result in results:
                                await s.hub.send(
                                    client,
                                    Envelope(type="result", tenant=tenant, result=result),
                                )
                    continue
                # Named entries: each is a standing GET. Fetch now (the same
                # answer the route would serve for these arguments), follow on
                # every pass that impacts it. Refusals are per entry, by name,
                # in the API's own vocabulary; the valid entries beside a
                # refused one proceed -- a whole frame dropped over one typo
                # would leave a screen half-subscribed with nothing saying so.
                if world is None or world.library is None:
                    for asked in frame.entries:
                        await s.hub.send(
                            client,
                            Envelope(
                                type="error",
                                tenant=tenant,
                                name=asked.name,
                                message="No definitions have been loaded yet",
                            ),
                        )
                    continue
                facade = facade_for(s, world, world.library)
                # The fetch-and-register runs under the tenant's pass lock,
                # and the entry is registered BEFORE its fetch: a pass landing
                # in between would otherwise push to an interest map that does
                # not know this client yet and then be shadowed by this older
                # fetch -- a subscription born stale, with nothing saying so.
                # Under the lock neither order matters, but registering first
                # keeps the window shut even if the locking ever changes.
                async with s.lock_for(tenant):
                    for asked in frame.entries:
                        refusal = _refuse_entry(world.library, asked)
                        if refusal is not None:
                            await s.hub.send(
                                client,
                                Envelope(
                                    type="error",
                                    tenant=tenant,
                                    name=asked.name,
                                    message=refusal,
                                ),
                            )
                            continue
                        entry = _entry_of(asked)
                        if entry is None:  # pragma: no cover - _refuse_entry caught it
                            continue
                        client.entries[entry.key()] = entry
                        try:
                            answer = await facade.answer(
                                tenant,
                                entry.name,
                                trailing=entry.windows
                                if entry.windows is not None
                                else DEFAULT_TRAILING,
                            )
                        except (WindowError, ValueError, NotImplementedError) as failure:
                            # The API's own refusals (a span over the wrong
                            # grain, a live reading), carried to the entry
                            # that asked. The entry is removed again:
                            # following something that cannot be fetched
                            # would push the same error on every pass for
                            # ever.
                            client.entries.pop(entry.key(), None)
                            await s.hub.send(
                                client,
                                Envelope(
                                    type="error",
                                    tenant=tenant,
                                    name=asked.name,
                                    message=str(failure),
                                ),
                            )
                            continue
                        if answer is None:  # pragma: no cover - _refuse_entry caught it
                            client.entries.pop(entry.key(), None)
                            continue
                        # `name` on the envelope is the entry's own address,
                        # so a client can attribute the frame even where the
                        # payload answers under another name -- a summary
                        # serves its projection's Result, and without this
                        # the tile asking for the summary never fills.
                        await s.hub.send(
                            client,
                            Envelope(
                                type="result",
                                tenant=tenant,
                                name=entry.name,
                                result=answer,
                            ),
                        )
        except WebSocketDisconnect:
            pass
        finally:
            await s.hub.leave(client)

    if resolved_ui:
        app.include_router(
            builtin_ui.router(
                resolved_ancestors, edit=resolved_edit, documents=resolved_ui_documents
            )
        )

    return app


def _ui_default(env: str | None, token: str | None) -> bool:
    """Whether to mount the built-in UI when the caller did not say.

    The UI is unauthenticated by design, so the default follows the token: an
    open server gets the UI, a token-protected one does not -- mounting an
    open window beside a locked door would hand every fact and figure to
    anyone who can reach the port. `URATORI_UI` overrides in either direction,
    and a value that is neither a yes nor a no is refused at boot rather than
    guessed at: a typo'd `URATORI_UI=fales` silently meaning "the default"
    would surface as a security surprise, not a config error.
    """
    if env is None or env.strip() == "":
        # Empty is how compose files and manifests spell "unset"
        # (`- URATORI_UI=` or an `-e URATORI_UI` pass-through of nothing);
        # refusing it would fail boots that never chose anything.
        return token is None
    value = env.strip().lower()
    if value in {"1", "true", "on", "yes"}:
        return True
    if value in {"0", "false", "off", "no"}:
        return False
    raise RuntimeError(f"URATORI_UI={env!r} is neither a yes nor a no")


def _edit_default(env: str | None, token: str | None, ui: bool) -> bool:
    """Whether the built-in UI may edit definitions when the caller did not say.

    The same contract as `_ui_default`, one notch stricter: an open server's
    API already accepts an unauthenticated `PUT /definitions`, so its UI
    editing too grants nothing new -- but beside a token the API's writes are
    gated, and a UI that could still save would hand "redefine every figure"
    to anyone who can reach the port. So the default is editing only where
    the UI is on AND the API itself is open; `URATORI_UI_EDIT` overrides in
    either direction, and junk refuses to boot rather than guessing.
    """
    if env is None or env.strip() == "":
        return ui and token is None
    value = env.strip().lower()
    if value in {"1", "true", "on", "yes"}:
        return True
    if value in {"0", "false", "off", "no"}:
        return False
    raise RuntimeError(f"URATORI_UI_EDIT={env!r} is neither a yes nor a no")


def _ui_documents_default(env: str | None, token: str | None, ui: bool) -> bool:
    """Whether the built-in UI's document viewer (page images, word
    layers) is granted when the caller did not say.

    The same shape as `_edit_default`, for the same reason: this is a
    sub-grant of the UI, not a sibling of it, so its default must follow
    the UI's own resolved value rather than the token alone -- `_ui_default`
    on its own could default `True` while `URATORI_UI` was explicitly
    turned off, the exact contradiction `_edit_default` already avoids.
    Default is on only where the UI is on AND the API itself is open (an
    `<img src>` cannot carry a bearer token, so page images beside a token
    stay behind the authenticated API only); `URATORI_UI_DOCUMENTS`
    overrides in either direction, and junk refuses to boot.
    """
    if env is None or env.strip() == "":
        return ui and token is None
    value = env.strip().lower()
    if value in {"1", "true", "on", "yes"}:
        return True
    if value in {"0", "false", "off", "no"}:
        return False
    raise RuntimeError(f"URATORI_UI_DOCUMENTS={env!r} is neither a yes nor a no")


def _parse(raw: str) -> Subscribe | None:
    """A malformed frame is ignored rather than closing the socket: a client
    that sends nonsense is a bug in that client, and taking the connection down
    makes the bug look like an outage."""
    try:
        return Subscribe.model_validate_json(raw)
    except ValueError:
        return None


def _entry_of(asked: SubscribeEntry) -> Entry | None:
    """The entry as the hub holds it: windows parsed to specs, or None when
    the spelling does not parse -- the caller has already refused (or is
    removing, where an unparseable identity matches nothing). An EMPTY window
    list is the bare entry: it named no windows, and a `()` identity distinct
    from `None`'s would let two spellings of one question shadow each other
    in the interest map."""
    if not asked.trailing:
        return Entry(name=asked.name, windows=None)
    try:
        return Entry(
            name=asked.name,
            windows=expand_window_args(asked.trailing),
        )
    except WindowError:
        return None


def _refuse_entry(library: Library, asked: SubscribeEntry) -> str | None:
    """The refusals a subscribe frame can earn per entry, before any fetch --
    the same vocabulary the HTTP routes answer with, so a client sees one
    validation language on both surfaces.

    Serve-time refusals (a span whose unit cannot slice the reading's
    storage, a live reading) surface from the fetch itself; this is only what
    the frame alone can be wrong about. `trailing` is accepted exactly where
    it means something -- a windowed reading. The HTTP route quietly ignores
    it elsewhere; a standing entry cannot afford that generosity, because the
    ignored argument would become part of the entry's identity and one
    question would fork into two subscriptions serving identical frames."""
    known = (
        library.figure(asked.name) is not None
        or library.reading(asked.name) is not None
        or library.projection(asked.name) is not None
        or library.summary(asked.name) is not None
        or library.bundle(asked.name) is not None
    )
    if not known:
        return f"No definition called {asked.name}"
    if asked.subject:
        return (
            f"{asked.name} was asked with a subject list: pooling is served over HTTP "
            "only, for now -- `GET .../results/{name}?subject=` -- and not yet followed "
            "over this socket. Subscribe to it bare, or poll the HTTP route for the "
            "pooled row."
        )
    if asked.trailing:
        if library.bundle(asked.name) is not None:
            return (
                f"{asked.name} is a bundle: its windows are declared in the "
                "definition, so a subscription names it bare -- an entry that "
                "could move them would be a different tile under the same hash."
            )
        reading = library.reading(asked.name)
        if reading is None or reading.mode != "window":
            return (
                f"{asked.name} takes no windows: `trailing` selects the spans a "
                "windowed reading is served over, and nothing else serves over "
                "windows. Subscribe to it bare."
            )
        try:
            expand_window_args(asked.trailing)
        except WindowError as refusal:
            return str(refusal)
    return None


def _library_out(library: Library) -> LibraryOut:
    """The library, described -- see `DeclarationOut` for why this is rich.

    Prose and formula come from the same scanners an embedding host would
    call (`declaration_prose`/`declaration_source`), so the HTTP door and the
    library door describe one library identically and cannot drift.
    """
    from ..lang.ast import ByAge, IndexBy, SetExpr, SetIndex, SetOp, SetRef
    from ..lang.check import _index_fields
    from ..lang.source import declaration_prose, declaration_source

    def _age_join(spec: IndexBy) -> set[str]:
        """The owner record an age filter reads its threshold off, if any."""
        if isinstance(spec, ByAge) and spec.through is not None:
            return {f"{spec.through.kind}.{spec.through.path}"}
        return set()

    def described(
        name: str,
        *,
        declaration: Literal[
            "group",
            "filter",
            "measure",
            "figure",
            "reading",
            "projection",
            "summary",
            "bundle",
            "extract",
            "audit",
        ],
        version: str | None = None,
        display: str | None = None,
        unit: str | None = None,
        kind: str | None = None,
        id_space: str | None = None,
        mode: Literal["window", "live"] | None = None,
        grain: str | None = None,
        across: str | None = None,
        banded: bool | None = None,
        over: str | None = None,
        many: bool | None = None,
        many_up_to: int | None = None,
        copies: list[str] | None = None,
        verifies: list[str] | None = None,
        model: str | None = None,
        indexes: list[str] | None = None,
        measures: list[str] | None = None,
        reads: list[str] | None = None,
        band_reads: list[str] | None = None,
        statistics: list[str] | None = None,
        fields: list[str] | None = None,
        through: list[str] | None = None,
        members: list[str] | None = None,
    ) -> DeclarationOut:
        # Spelled out rather than **kwargs, so pydantic-mypy's init guard
        # reaches every call site: routed through Any, a misspelled field
        # here was silently dropped at runtime and invisible to the checker.
        # `kind=` for `extract` and `audit`: `extract` is the one declaration
        # kind that may share a name with another (the `fact` it targets,
        # D4), so a plain name lookup would silently resolve to whichever
        # header sorts first in the source; `audit` never collides (its own
        # namespace, D6), but passing it costs nothing and keeps one rule
        # ("the kinds `_HEADER_BY_KIND` knows, pass their own name") rather
        # than two. Every other kind passes no kind, exactly as before
        # `extract` existed.
        source_kind = declaration if declaration in ("extract", "audit") else None
        return DeclarationOut(
            name=name,
            prose=declaration_prose(library, name, source_kind),
            source=declaration_source(library, name, source_kind) or "",
            declaration=declaration,
            version=version,
            display=display,
            unit=unit,
            kind=kind,
            id_space=id_space,
            mode=mode,
            grain=grain,
            across=across,
            banded=banded,
            over=over,
            many=many,
            many_up_to=many_up_to,
            copies=copies or [],
            verifies=verifies or [],
            model=model,
            indexes=indexes or [],
            measures=measures or [],
            reads=reads or [],
            band_reads=band_reads or [],
            statistics=statistics or [],
            fields=fields or [],
            through=through or [],
            members=members or [],
        )

    def set_text(expr: SetExpr) -> str:
        """An extract's `over`, rendered back to the words it was written
        with -- the manifest's `source` splits formula from display
        template, and `over` is neither, so it travels on its own field."""
        if isinstance(expr, SetOp):
            symbol = {"intersect": "&", "union": "|", "difference": "-"}[expr.op]
            return f"{set_text(expr.left)} {symbol} {set_text(expr.right)}"
        if isinstance(expr, SetRef):
            return expr.name
        assert isinstance(expr, SetIndex)
        return expr.index

    def measure_unit(shape: str, unit: str | None) -> str | None:
        # A duration or a moment is its own unit; only a field measure
        # declares one (`in effort`).
        if shape == "duration":
            return "duration"
        if shape == "moment":
            return "moment"
        return unit

    def fact_leaves(
        fields: tuple[CompiledFactField, ...], prefix: str, repeats: bool
    ) -> list[FactFieldOut]:
        """The body flattened to its leaves, dotted the way a definition
        reads them, so the manifest and the paths in `fields`/`through`
        speak one spelling."""
        out: list[FactFieldOut] = []
        for f in fields:
            path = f"{prefix}{f.name}"
            if f.type is None:
                out.extend(fact_leaves(f.children, f"{path}.", repeats or f.many))
            else:
                leaf_type = cast('Literal["text", "number", "flag", "moment"]', f.type)
                out.append(
                    FactFieldOut(path=path, type=leaf_type, repeats=repeats, prose=f.doc)
                )
        return out

    return LibraryOut(
        facts=[
            FactOut(
                name=f.name,
                version=f.version,
                prose=declaration_prose(library, f.name),
                source=declaration_source(library, f.name) or "",
                name_field=f.name_field,
                url_field=f.url_field,
                fields=fact_leaves(f.fields, "", False),
                shape=f.shape,
                page_of=f.page_of,
            )
            for f in library.facts.values()
        ],
        extracts=[
            described(
                e.name,
                declaration="extract",
                version=e.version,
                kind=e.source,
                many=e.many,
                many_up_to=e.many_up_to,
                copies=list(e.copies),
                over=set_text(e.over) if e.over is not None else None,
                fields=[f.name for f in e.fields],
            )
            for e in library.extracts.values()
        ],
        audits=[
            described(
                a.name,
                declaration="audit",
                version=a.version,
                display=a.display,
                unit="level",
                kind=a.scope,
                verifies=list(a.verifies),
                model=a.model,
                fields=[r.name for r in a.reads],
            )
            for a in library.audits.values()
        ],
        figures=[
            described(
                p.name,
                declaration="figure",
                version=p.version,
                display=p.display,
                unit=p.unit,
                kind=p.scope,
                grain=p.grain,
                across=p.across,
                banded=p.band is not None,
                indexes=list(p.indexes),
                measures=list(p.measures),
                reads=list(p.reads),
                band_reads=list(p.band_reads),
            )
            for p in library.figures
        ],
        readings=[
            described(
                p.name,
                declaration="reading",
                version=p.version,
                display=p.display,
                unit=p.unit,
                kind=p.scope,
                mode=p.mode,
                banded=p.band is not None,
                indexes=list(p.indexes),
                measures=[p.live_measure] if p.live_measure else [],
                reads=[p.source] if p.source else [],
                band_reads=list(p.band_reads),
                statistics=[stat.fn for stat in p.calculate],
            )
            for p in library.readings
        ],
        projections=[
            described(
                p.name,
                declaration="projection",
                version=p.version,
                kind=p.kind,
                indexes=list(p.indexes),
                reads=list(p.figures),
                # For a joined field the path on OUR record is the join's
                # linking field; the declared path is read off the other
                # kind and travels in `through`. Serving the remote path
                # under `fields` broke the drift guard both ways: a false
                # alarm on the other kind's field, and blindness to the
                # local one going missing.
                fields=[
                    join.field if join is not None else path
                    for _name, path, _type, join in p.fields
                ],
                through=sorted({f"{j.kind}.{j.path}" for j in p.joins}),
            )
            for p in library.projections
        ],
        summaries=[
            described(
                p.name,
                declaration="summary",
                version=p.version,
                over=p.over,
            )
            for p in library.summaries
        ],
        bundles=[
            described(
                p.name,
                declaration="bundle",
                version=p.version,
                members=[m.name for m in p.members],
            )
            for p in library.bundles
        ],
        indexes=[
            described(
                i.name,
                # The declaration word carries the split: a group fans out,
                # a filter narrows to one bucket.
                declaration="group" if i.bucketed else "filter",
                kind=i.kind,
                id_space=i.id_space,
                display=i.label,
                grain=next(
                    (
                        part.truncate or part.select
                        for part in parts
                        if part.truncate is not None or part.select is not None
                    ),
                    None,
                ),
                # Both ends of a span, not just the near one. A host reading
                # the declaration alone is being told which fields moving
                # would refile this grouping's records, and a span's far end
                # decides its last bucket exactly as the near end decides its
                # first -- listed singly, changing a due date looked like a
                # change nothing was filed against.
                fields=[
                    field
                    for part in parts
                    for field in ((part.field, part.until) if part.until else (part.field,))
                ],
                # Every record this grouping rests on besides the one it
                # buckets: the identity hop, the calendar it cuts on, and an
                # age filter's owner join. All three used to be dials, which
                # a host could enumerate from the settings list; they are
                # facts now, and this is where a host reading the declaration
                # alone finds out what moving them would move. Leaving the
                # calendar out was the last of them, and the comment here
                # already claimed it was included.
                through=sorted(
                    {
                        f"{part.through.kind}.{part.through.path}"
                        for part in parts
                        if part.through is not None
                    }
                    | {
                        f"{part.zone.kind}.{part.zone.field}"
                        for part in parts
                        # A calendar written in the definition (`in "UTC"`)
                        # resolves through no record, and `kind` is None for
                        # exactly that case. Formatting it anyway produced the
                        # literal string "None.None" -- a host's drift guard
                        # reads this list as fact kinds it must carry, and
                        # cannot tell that from a definition naming a kind the
                        # host retired. Nothing to resolve is an empty set, not
                        # a placeholder.
                        if part.zone is not None and part.zone.kind is not None
                    }
                    | _age_join(i.spec)
                ),
            )
            for i in library.indexes.values()
            for parts in [_index_fields(i.spec)]
        ],
        measures=[
            described(
                m.name,
                declaration="measure",
                kind=m.kind,
                unit=measure_unit(m.shape, m.unit),
                # `now` is the clock, not a field: a drift guard told to
                # look for a record field called "now" alarms for ever.
                fields=[
                    path
                    for path in (m.field_path, m.moment, m.later, m.earlier)
                    if path is not None and path != "now"
                ],
            )
            for m in library.measures.values()
        ],
    )


__all__ = ["create_app"]
