"""What a running service holds, shared by every router.

Split out of `app.py` the day the built-in UI arrived: its routes live in a
module of their own but must answer from the same world, the same 409s and
the same facade wiring as the API proper. Two copies of "what does ready
mean" would let the two surfaces disagree about whether the server is taught,
which is exactly the kind of ambient drift `World` exists to prevent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastapi import HTTPException, Request

from ..facade import Uratori
from ..lang.plan import Library
from ..results import Result
from ..schema import Schema
from ..store.postgres import PostgresEngineStore, PostgresFactStore
from .hub import Hub


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
