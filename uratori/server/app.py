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
- **The websocket is fed by the facade's listener hook** -- the same mechanism
  an embedding host would use -- rather than by a special path inside the
  routes. The socket therefore carries exactly the objects the routes return.
- **Configuration survives restart, compiled state does not.** The schema
  document and the definitions *source* are persisted; the library is
  recompiled from source at boot, because the source is the truth and a stored
  artifact read back would let a stale copy decide what the server computes.
"""


import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ..engine.activity import shown_changes
from ..facade import DEFAULT_TRAILING, RunReport
from ..lang.check import compile_source
from ..lang.lex import DefinitionError
from ..lang.plan import Library
from ..results import Evidence, Result
from ..store.postgres import PostgresFactStore
from . import db
from . import ui as builtin_ui
from .contract import (
    Ack,
    DeclarationRef,
    DefinitionsIn,
    Envelope,
    FactsIn,
    Health,
    LibraryOut,
    RunIn,
    RunOut,
    SchemaIn,
    SettingsIn,
    ShownChange,
    Subscribe,
    TenantRemoved,
    schema_out,
)
from .hub import Client
from .runtime import State, World, facade_for, ready, state_of

log = logging.getLogger("uratori.server")


def create_app(
    *,
    dsn: str | None = None,
    token: str | None = None,
    version: str | None = None,
    pg_schema: str | None = None,
    ui: bool | None = None,
    frame_ancestors: str | None = None,
) -> FastAPI:
    """Build the service. Parameters override the environment, for tests and
    for embedding; production reads DATABASE_URL / URATORI_TOKEN / APP_VERSION,
    plus URATORI_UI / URATORI_UI_FRAME_ANCESTORS for the built-in UI."""
    resolved_dsn = dsn or os.environ.get("DATABASE_URL")
    resolved_token = token if token is not None else os.environ.get("URATORI_TOKEN")
    resolved_version = version or os.environ.get("APP_VERSION", "dev")
    resolved_ui = (
        ui if ui is not None else _ui_default(os.environ.get("URATORI_UI"), resolved_token)
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
        state = State(pool, resolved_token, resolved_version)
        held = await db.load_world(pool)
        if held is not None:
            document, source = held
            schema = SchemaIn(**document).build()
            boot_refusal: str | None = None
            try:
                library = compile_source(source, schema) if source else None
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
        try:
            yield
        finally:
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
        try:
            library = compile_source(body.source, s.world.schema)
        except DefinitionError as refusal:
            raise HTTPException(status_code=422, detail=str(refusal)) from refusal
        await db.save_world(s.pool, s.world.schema_document, body.source)
        s.world = World(
            schema=s.world.schema,
            schema_document=s.world.schema_document,
            source=body.source,
            library=library,
        )
        return _library_out(library)

    # ----------------------------------------------------------- settings --

    @app.put("/tenants/{tenant}/settings", response_model=Ack, dependencies=[auth])
    async def put_settings(tenant: str, body: SettingsIn, s: S) -> Ack:
        """Store the tenant's sparse document. Deliberately does not run a
        pass: whether to pay for the rebuild now or at the next sync is the
        host's call, and `POST /tenants/{t}/runs` is how it says now."""
        await db.save_settings(s.pool, tenant, body.document)
        return Ack(ok=True)

    # -------------------------------------------------------------- facts --

    @app.post("/tenants/{tenant}/facts", response_model=RunOut, dependencies=[auth])
    async def post_facts(tenant: str, body: FactsIn, s: S) -> RunOut:
        """Apply one batch of fact movement and run the pass it implies.

        Deletes before writes, writes before the engine: the engine reads the
        buckets a deleted record held to work out whose numbers move, and the
        fact table is what the departed-subject sweep walks -- both need the
        table to already say what the batch said.
        """
        world, library = ready(s)
        facts = PostgresFactStore(s.pool)
        async with s.lock_for(tenant):
            for kind, keys in body.deletes.items():
                await facts.delete(tenant, kind, keys)
            moved: dict[str, list[str]] = {}
            written = 0
            for kind, records in body.writes.items():
                if not records:
                    continue
                changed = await facts.upsert(
                    tenant, kind, records, stamps=body.stamps.get(kind)
                )
                written += len(changed)
                if changed:
                    moved[kind] = changed
            settings = await db.load_settings(s.pool, tenant)
            report = await facade_for(s, world, library).run(
                tenant,
                settings,
                written=moved,
                deleted={k: list(v) for k, v in body.deletes.items()},
                full=body.full,
            )
            out = _run_out(
                report,
                world,
                library,
                settings,
                written=written,
                deleted=sum(len(v) for v in body.deletes.values()),
            )
            await _record(s, tenant, "facts", full=body.full, out=out)
        return out

    @app.post("/tenants/{tenant}/runs", response_model=RunOut, dependencies=[auth])
    async def post_run(tenant: str, body: RunIn, s: S) -> RunOut:
        """A pass with no new facts: pick up a settings change, a redeployed
        definition, or (with `full`) rebuild everything from what is stored."""
        world, library = ready(s)
        async with s.lock_for(tenant):
            settings = await db.load_settings(s.pool, tenant)
            report = await facade_for(s, world, library).run(tenant, settings, full=body.full)
            out = _run_out(report, world, library, settings, written=0, deleted=0)
            await _record(s, tenant, "run", full=body.full, out=out)
        return out

    async def _record(s: State, tenant: str, cause: str, *, full: bool, out: RunOut) -> None:
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

    def _run_out(
        report: RunReport,
        world: World,
        library: Library,
        settings: dict[str, Any],
        *,
        written: int,
        deleted: int,
    ) -> RunOut:
        # The sample is rendered against the tenant's own dials as they stand
        # now -- an effort formatted with the wrong working day is a wrong
        # sentence frozen into the caller's activity log for ever.
        document = world.schema.settings_for(settings)
        return RunOut(
            written=written,
            deleted=deleted,
            changed=len(report.outcome.changes),
            rebuilt=list(report.outcome.rebuilt),
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
                for c in shown_changes(list(report.outcome.changes), library, document)
            ],
            results=list(report.results),
        )

    # ------------------------------------------------------------ results --

    @app.get("/tenants/{tenant}/results", response_model=list[Result], dependencies=[auth])
    async def get_results(
        tenant: str,
        s: S,
        trailing: Annotated[list[int] | None, Query()] = None,
    ) -> list[Result]:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        return list(
            await facade.results(
                tenant,
                await db.load_settings(s.pool, tenant),
                trailing=trailing or DEFAULT_TRAILING,
            )
        )

    @app.get("/tenants/{tenant}/results/{name}", response_model=Result, dependencies=[auth])
    async def get_result(
        tenant: str,
        name: str,
        s: S,
        trailing: Annotated[list[int] | None, Query()] = None,
    ) -> Result:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            result = await facade.answer(
                tenant,
                name,
                await db.load_settings(s.pool, tenant),
                trailing=trailing or DEFAULT_TRAILING,
            )
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
                tenant, name, subject, await db.load_settings(s.pool, tenant)
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
        return answer

    # ------------------------------------------------------------ tenants --

    @app.delete("/tenants/{tenant}", response_model=TenantRemoved, dependencies=[auth])
    async def delete_tenant(tenant: str, s: S) -> TenantRemoved:
        async with s.lock_for(tenant):
            facts, values = await db.remove_tenant(s.pool, tenant)
        return TenantRemoved(facts_removed=facts, values_removed=values)

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
                if frame.tenant is None:
                    await s.hub.send(
                        client, Envelope(type="error", message="subscribe names a tenant")
                    )
                    continue
                client.tenant_id = frame.tenant
                # First paint: the current answer for everything, so a client
                # never renders from a partial world while waiting for a pass.
                world = s.world
                if world is not None and world.library is not None:
                    facade = facade_for(s, world, world.library)
                    results = await facade.results(
                        frame.tenant, await db.load_settings(s.pool, frame.tenant)
                    )
                    for result in results:
                        await s.hub.send(
                            client,
                            Envelope(type="result", tenant=frame.tenant, result=result),
                        )
        except WebSocketDisconnect:
            pass
        finally:
            await s.hub.leave(client)

    if resolved_ui:
        app.include_router(builtin_ui.router(resolved_ancestors))

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


def _parse(raw: str) -> Subscribe | None:
    """A malformed frame is ignored rather than closing the socket: a client
    that sends nonsense is a bug in that client, and taking the connection down
    makes the bug look like an outage."""
    try:
        return Subscribe.model_validate_json(raw)
    except ValueError:
        return None


def _library_out(library: Library) -> LibraryOut:
    return LibraryOut(
        figures=[DeclarationRef(name=p.name, version=p.version) for p in library.figures],
        readings=[DeclarationRef(name=p.name, version=p.version) for p in library.readings],
        projections=[
            DeclarationRef(name=p.name, version=p.version) for p in library.projections
        ],
        summaries=[DeclarationRef(name=p.name, version=p.version) for p in library.summaries],
        indexes=len(library.indexes),
        measures=len(library.measures),
    )


__all__ = ["create_app"]
