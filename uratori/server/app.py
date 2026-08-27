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
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ..facade import DEFAULT_TRAILING
from ..lang.check import compile_source
from ..lang.lex import DefinitionError
from ..lang.plan import CompiledFactField, Library
from ..results import BundleResult, Evidence, Result
from ..store.postgres import PostgresFactStore
from ..verify import FactError
from ..windows import WindowError, WindowSpec, as_window_spec
from . import db
from . import ui as builtin_ui
from .contract import (
    Ack,
    DeclarationOut,
    DefinitionsIn,
    Envelope,
    FactFieldOut,
    FactOut,
    FactsIn,
    Health,
    LibraryOut,
    RunIn,
    RunOut,
    SchemaIn,
    SettingsIn,
    Subscribe,
    TenantRemoved,
    schema_out,
)
from .hub import Client
from .runtime import (
    State,
    World,
    compile_for_teach,
    facade_for,
    ready,
    record_pass,
    run_out,
    state_of,
)

log = logging.getLogger("uratori.server")


def create_app(
    *,
    dsn: str | None = None,
    token: str | None = None,
    version: str | None = None,
    pg_schema: str | None = None,
    ui: bool | None = None,
    ui_edit: bool | None = None,
    frame_ancestors: str | None = None,
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
            facade.verify(body.writes, body.deletes)
        except FactError as refusal:
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
            settings = await db.load_settings(s.pool, tenant)
            full = body.full or await db.deferred(s.pool, tenant)
            report = await facade.run(
                tenant,
                settings,
                written=moved,
                deleted={k: list(v) for k, v in body.deletes.items()},
                full=full,
            )
            if full:
                # Settled after the pass actually ran: a debt cleared up
                # front would be forgiven, not paid, if the pass died.
                await db.clear_deferred(s.pool, tenant)
            out = run_out(
                report,
                world,
                library,
                settings,
                written=written,
                deleted=sum(len(v) for v in body.deletes.values()),
            )
            await record_pass(s, tenant, "facts", full=full, out=out)
        return out

    @app.post("/tenants/{tenant}/runs", response_model=RunOut, dependencies=[auth])
    async def post_run(tenant: str, body: RunIn, s: S) -> RunOut:
        """A pass with no new facts: pick up a settings change, a redeployed
        definition, or (with `full`) rebuild everything from what is stored."""
        world, library = ready(s)
        async with s.lock_for(tenant):
            settings = await db.load_settings(s.pool, tenant)
            full = body.full or await db.deferred(s.pool, tenant)
            report = await facade_for(s, world, library).run(tenant, settings, full=full)
            if full:
                await db.clear_deferred(s.pool, tenant)
            out = run_out(report, world, library, settings, written=0, deleted=0)
            await record_pass(s, tenant, "run", full=full, out=out)
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

        Each value is a bucket span: `30` (the last 30 days, unchanged from
        when this parameter was an integer), `31-60` (the 30 before them),
        or with a unit suffix `1-48h` / `1-90m`. Malformed specs are 422s,
        never coerced -- a coerced window is a plausible population nobody
        asked for. The unit-versus-grain check happens deeper, where the
        reading's source is known, and surfaces as the same 422.
        """
        if trailing is None:
            return None
        try:
            return [as_window_spec(token) for token in trailing]
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

    @app.get("/tenants/{tenant}/results", response_model=list[Result], dependencies=[auth])
    async def get_results(
        tenant: str,
        s: S,
        trailing: Annotated[list[str] | None, Query()] = None,
        at: Annotated[str | None, Query()] = None,
    ) -> list[Result]:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            return list(
                await facade.results(
                    tenant,
                    await db.load_settings(s.pool, tenant),
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
    # field rather than sniffing. Bundles are deliberately absent from the
    # bulk route above: every member already serves there under its own name.
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
    ) -> Result | BundleResult:
        world, library = ready(s)
        facade = facade_for(s, world, library)
        try:
            result = await facade.answer(
                tenant,
                name,
                await db.load_settings(s.pool, tenant),
                trailing=windows_of(trailing) or DEFAULT_TRAILING,
                at=anchor_of(at),
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
        app.include_router(builtin_ui.router(resolved_ancestors, edit=resolved_edit))

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


def _parse(raw: str) -> Subscribe | None:
    """A malformed frame is ignored rather than closing the socket: a client
    that sends nonsense is a bug in that client, and taking the connection down
    makes the bug look like an outage."""
    try:
        return Subscribe.model_validate_json(raw)
    except ValueError:
        return None


def _library_out(library: Library) -> LibraryOut:
    """The library, described -- see `DeclarationOut` for why this is rich.

    Prose and formula come from the same scanners an embedding host would
    call (`declaration_prose`/`declaration_source`), so the HTTP door and the
    library door describe one library identically and cannot drift.
    """
    from ..lang.ast import ByAge
    from ..lang.check import _index_fields
    from ..lang.source import declaration_prose, declaration_source

    def described(
        name: str,
        *,
        declaration: Literal[
            "group", "filter", "measure", "figure", "reading", "projection", "summary", "bundle"
        ],
        version: str | None = None,
        display: str | None = None,
        unit: str | None = None,
        kind: str | None = None,
        id_space: str | None = None,
        mode: Literal["window", "live"] | None = None,
        grain: Literal["day", "minute", "15 minutes"] | None = None,
        across: str | None = None,
        banded: bool | None = None,
        over: str | None = None,
        indexes: list[str] | None = None,
        measures: list[str] | None = None,
        reads: list[str] | None = None,
        settings: list[str] | None = None,
        band_settings: list[str] | None = None,
        statistics: list[str] | None = None,
        fields: list[str] | None = None,
        through: list[str] | None = None,
        members: list[str] | None = None,
    ) -> DeclarationOut:
        # Spelled out rather than **kwargs, so pydantic-mypy's init guard
        # reaches every call site: routed through Any, a misspelled field
        # here was silently dropped at runtime and invisible to the checker.
        return DeclarationOut(
            name=name,
            prose=declaration_prose(library, name),
            source=declaration_source(library, name) or "",
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
            indexes=indexes or [],
            measures=measures or [],
            reads=reads or [],
            settings=settings or [],
            band_settings=band_settings or [],
            statistics=statistics or [],
            fields=fields or [],
            through=through or [],
            members=members or [],
        )

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
            )
            for f in library.facts.values()
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
                settings=list(p.settings),
                band_settings=list(p.band_settings),
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
                settings=list(p.settings),
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
                settings=list(p.settings),
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
                settings=list(p.settings),
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
                    (part.truncate for part in parts if part.truncate is not None), None
                ),
                fields=[part.field for part in parts],
                through=sorted(
                    {
                        f"{part.through.kind}.{part.through.path}"
                        for part in parts
                        if part.through is not None
                    }
                ),
                # The dials this declaration itself reads: an age filter's
                # threshold, a time bucket's calendar. Moving one re-buckets
                # a tenant's whole history, and a host reading the
                # declaration alone must not conclude it rests on nothing.
                settings=sorted(
                    {
                        *(part.zone for part in parts if part.zone is not None),
                        *([i.spec.setting] if isinstance(i.spec, ByAge) else []),
                    }
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
