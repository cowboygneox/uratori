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
from ..facade import DEFAULT_TRAILING, RunReport, Uratori
from ..lang.check import WorldConflict, compile_source
from ..lang.plan import Library
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
        self.teach = asyncio.Lock()
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
