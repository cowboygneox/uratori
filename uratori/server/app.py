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


import asyncio
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ..engine.activity import shown_changes
from ..facade import DEFAULT_TRAILING, RunReport, Uratori
from ..lang.check import CheckError, compile_source
from ..lang.plan import Library
from ..results import Result
from ..schema import Schema
from ..store.postgres import PostgresEngineStore, PostgresFactStore
from . import db
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
from .hub import Client, Hub

log = logging.getLogger("uratori.server")


@dataclass
class World:
    """What this deployment has been taught, held compiled in memory."""

    schema: Schema
    schema_document: dict[str, Any]
    source: str | None
    library: Library | None


class State:
    """Typed app state -- `app.state` is `Any`, and `Any` is how a renamed
    attribute becomes a request-time AttributeError instead of a mypy error."""

    def __init__(self, pool: "asyncpg.Pool[Any]", token: str | None, version: str) -> None:
        self.pool = pool
        self.token = token
        self.version = version
        self.world: World | None = None
        self.hub = Hub()
        self.locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, tenant: str) -> asyncio.Lock:
        held = self.locks.get(tenant)
        if held is None:
            held = asyncio.Lock()
            self.locks[tenant] = held
        return held


def create_app(
    *,
    dsn: str | None = None,
    token: str | None = None,
    version: str | None = None,
    pg_schema: str | None = None,
) -> FastAPI:
    """Build the service. Parameters override the environment, for tests and
    for embedding; production reads DATABASE_URL / URATORI_TOKEN / APP_VERSION."""
    resolved_dsn = dsn or os.environ.get("DATABASE_URL")
    resolved_token = token if token is not None else os.environ.get("URATORI_TOKEN")
    resolved_version = version or os.environ.get("APP_VERSION", "dev")

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
            library = compile_source(source, schema) if source else None
            state.world = World(
                schema=schema, schema_document=document, source=source, library=library
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

    def state(request: Request) -> State:
        return request.app.state.uratori  # type: ignore[no-any-return]

    async def authed(request: Request) -> None:
        expected = request.app.state.uratori.token
        if expected is None:
            return
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, f"Bearer {expected}"):
            raise HTTPException(status_code=401, detail="Bad or missing bearer token")

    S = Annotated[State, Depends(state)]
    auth = Depends(authed)

    def _ready(s: State) -> tuple[World, Library]:
        """The world, or the 409 that explains what is missing.

        409 rather than 500: an unconfigured server is a state the client can
        fix (declare a schema, load definitions), and it must be told which.
        """
        if s.world is None:
            raise HTTPException(status_code=409, detail="No schema has been declared yet")
        if s.world.library is None:
            raise HTTPException(status_code=409, detail="No definitions have been loaded yet")
        return s.world, s.world.library

    def _facade(s: State, world: World, library: Library) -> Uratori:
        facade = Uratori(
            schema=world.schema,
            library=library,
            store=PostgresEngineStore(s.pool),
            facts=PostgresFactStore(s.pool),
        )

        # The socket is fed through the same hook an embedding host gets, so
        # a listener bug is caught by whichever of the two hits it first.
        async def push(tenant: str, _outcome: Any, results: tuple[Result, ...]) -> None:
            await s.hub.publish(tenant, results)

        facade.subscribe(push)
        return facade

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
        if source:
            try:
                library = compile_source(source, schema)
            except CheckError as refusal:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "the loaded definitions do not compile under this schema: "
                        f"{refusal}"
                    ),
                ) from refusal

        document = body.model_dump()
        await db.save_world(s.pool, document, source)
        s.world = World(
            schema=schema, schema_document=document, source=source, library=library
        )
        return Ack(ok=True)

    @app.get("/definitions", response_model=LibraryOut, dependencies=[auth])
    async def get_definitions(s: S) -> LibraryOut:
        _world, library = _ready(s)
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
        except CheckError as refusal:
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
        world, library = _ready(s)
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
            report = await _facade(s, world, library).run(
                tenant,
                settings,
                written=moved,
                deleted={k: list(v) for k, v in body.deletes.items()},
                full=body.full,
            )
        return _run_out(
            report,
            world,
            library,
            settings,
            written=written,
            deleted=sum(len(v) for v in body.deletes.values()),
        )

    @app.post("/tenants/{tenant}/runs", response_model=RunOut, dependencies=[auth])
    async def post_run(tenant: str, body: RunIn, s: S) -> RunOut:
        """A pass with no new facts: pick up a settings change, a redeployed
        definition, or (with `full`) rebuild everything from what is stored."""
        world, library = _ready(s)
        async with s.lock_for(tenant):
            settings = await db.load_settings(s.pool, tenant)
            report = await _facade(s, world, library).run(tenant, settings, full=body.full)
        return _run_out(report, world, library, settings, written=0, deleted=0)

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
        world, library = _ready(s)
        facade = _facade(s, world, library)
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
        world, library = _ready(s)
        facade = _facade(s, world, library)
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
                # Closed before accept, with the 4401 convention: a socket that
                # accepts and then drops reads as a network fault, and the
                # client retries for ever against an auth problem.
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
                    facade = _facade(s, world, world.library)
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

    return app


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
