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
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

from ..engine.activity import shown_changes
from ..facade import RunReport, Uratori
from ..lang.check import WorldConflict, compile_source
from ..lang.plan import Library
from ..results import Result
from ..schema import Schema
from ..store.postgres import PostgresEngineStore, PostgresFactStore
from . import db
from .contract import RunOut, ShownChange, schema_out
from .hub import Hub

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

    def __init__(self, pool: asyncpg.Pool[Any], token: str | None, version: str) -> None:
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


def taught_schema(world: World) -> Schema:
    """The world as taught, whichever door taught it.

    A fact-taught library carries the kinds and name fields; the declared
    schema then holds only settings. Every surface that *describes* the world
    (the UI's kind list, its record names) reads through this, so those
    surfaces cannot disagree -- the facade does the same completion
    internally. The one deliberate exception is `GET /schema`, which answers
    the stored document in exactly the PUT shape: in a fact-taught world the
    kinds live on `GET /definitions`, and the docs say so.
    """
    if world.library is None:
        return world.schema
    return world.schema.taught_by(world.library)


def compile_for_teach(source: str, world: World) -> tuple[Library, Schema, dict[str, Any]]:
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
    """
    schema = world.schema
    try:
        return compile_source(source, schema), schema, world.schema_document
    except WorldConflict:
        stripped = dataclasses.replace(
            schema, kinds=frozenset(), name_fields={}, url_fields={}
        )
        library = compile_source(source, stripped)
        return library, stripped, schema_out(stripped).model_dump()


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


def facade_for(s: State, world: World, library: Library) -> Uratori:
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
