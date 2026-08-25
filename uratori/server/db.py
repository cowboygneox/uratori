"""The server's own database: the engine's tables, plus what makes it a service.

Three server-only tables sit beside the engine's four:

- `uratori_meta` -- the ownership marker. The engine's table names are generic
  enough to exist in other products (the project this grew out of has a
  `figure_value` of its own, with different column types), so pointing this
  server at somebody else's database must be a loud refusal at boot, not a
  wrong answer later.
- `engine_world` -- the schema and the definitions source, one row. Stored so a
  restarted container comes back knowing its world; **the source is stored and
  the plans are recompiled at boot**, because the source is the truth and a
  compiled artifact read back would let a stale copy decide what the server
  computes.
- `tenant_settings` -- each tenant's sparse dial document.

Schema management is one idempotent `ensure_schema` under an advisory lock
rather than numbered migration files: the schema is young and additive. The
day it needs a destructive change, numbered files arrive and this function
becomes their `001`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import asyncpg

from ..store.postgres import SCHEMA_SQL

log = logging.getLogger("uratori.server")

ADVISORY_LOCK = 0x7572_6174  # arbitrary; every booting process asks for the same one

OWNER = "uratori"

_SERVER_SQL = """
create table if not exists uratori_meta (
  key   text primary key,
  value text not null
);

create table if not exists engine_world (
  id      int primary key check (id = 1),
  schema  jsonb not null,
  source  text,
  updated_at timestamptz not null default now()
);

create table if not exists tenant_settings (
  tenant_id  text primary key,
  document   jsonb not null,
  updated_at timestamptz not null default now()
);
"""


class DatabaseBelongsToSomethingElse(RuntimeError):
    pass


async def open_server_pool(
    dsn: str, *, pg_schema: str | None = None, timeout: float = 60.0
) -> asyncpg.Pool[Any]:
    """Connect, waiting for a database that may still be starting.

    Boot order is not something this process gets to decide: under an
    orchestrator (or `docker compose up`) Postgres is routinely seconds behind
    us. Waiting quietly and then dying loudly distinguishes "not ready yet"
    from "misconfigured".

    `pg_schema` pins `search_path`, so a test run can keep this server's tables
    in a schema of their own inside a database other suites also use.
    """
    settings = {"search_path": pg_schema} if pg_schema else None
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        try:
            pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=1,
                max_size=10,
                statement_cache_size=0,
                command_timeout=30.0,
                server_settings=settings,
            )
            assert pool is not None
            if attempt:
                log.info("connected after %d retries", attempt)
            return pool
        except (OSError, asyncpg.PostgresError):
            attempt += 1
            if time.monotonic() >= deadline:
                raise
            if attempt == 1:
                log.info("waiting for the database")
            await asyncio.sleep(1.0)


async def ensure_schema(pool: asyncpg.Pool[Any]) -> None:
    """Create anything missing, refusing a database that is not ours.

    The refusal has to come **before** the `if not exists` DDL runs: applying
    our tables into another product's database would interleave two schemas
    that share table names, and every later error would point at data rather
    than at this moment.
    """
    async with pool.acquire() as connection:
        await connection.execute("select pg_advisory_lock($1)", ADVISORY_LOCK)
        try:
            marker = None
            has_meta = await connection.fetchval("select to_regclass('uratori_meta')")
            if has_meta is not None:
                marker = await connection.fetchval(
                    "select value from uratori_meta where key = 'owner'"
                )
            if marker is not None and marker != OWNER:
                raise DatabaseBelongsToSomethingElse(
                    f"this database belongs to {marker!r}. Point DATABASE_URL at a "
                    "database of uratori's own."
                )
            if marker is None:
                suspicious = await connection.fetchval(
                    "select to_regclass('figure_definition')"
                )
                if suspicious is not None:
                    raise DatabaseBelongsToSomethingElse(
                        "this database already holds a figure_definition table that "
                        "uratori did not create -- it is probably another product's. "
                        "Sharing would interleave two schemas that reuse table names; "
                        "point DATABASE_URL at a database of uratori's own."
                    )
            async with connection.transaction():
                await connection.execute(SCHEMA_SQL)
                await connection.execute(_SERVER_SQL)
                await connection.execute(
                    "insert into uratori_meta (key, value) values ('owner', $1) "
                    "on conflict (key) do nothing",
                    OWNER,
                )
        finally:
            await connection.execute("select pg_advisory_unlock($1)", ADVISORY_LOCK)


# ----------------------------------------------------------------- world --


async def load_world(pool: asyncpg.Pool[Any]) -> tuple[dict[str, Any], str | None] | None:
    """The stored schema document and definitions source, or None on first boot."""
    row = await pool.fetchrow("select schema, source from engine_world where id = 1")
    if row is None:
        return None
    schema = row["schema"]
    return (
        schema if isinstance(schema, dict) else json.loads(schema),
        row["source"],
    )


async def save_world(
    pool: asyncpg.Pool[Any], schema_document: dict[str, Any], source: str | None
) -> None:
    await pool.execute(
        """
        insert into engine_world (id, schema, source, updated_at)
        values (1, $1, $2, now())
        on conflict (id) do update
          set schema = excluded.schema, source = excluded.source, updated_at = now()
        """,
        json.dumps(schema_document),
        source,
    )


# -------------------------------------------------------------- settings --


async def load_settings(pool: asyncpg.Pool[Any], tenant: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        "select document from tenant_settings where tenant_id = $1", tenant
    )
    if row is None:
        return {}
    document = row["document"]
    return document if isinstance(document, dict) else json.loads(document)


async def save_settings(pool: asyncpg.Pool[Any], tenant: str, document: dict[str, Any]) -> None:
    await pool.execute(
        """
        insert into tenant_settings (tenant_id, document, updated_at)
        values ($1, $2, now())
        on conflict (tenant_id) do update
          set document = excluded.document, updated_at = now()
        """,
        tenant,
        json.dumps(document),
    )


# ---------------------------------------------------------------- tenants --


async def remove_tenant(pool: asyncpg.Pool[Any], tenant: str) -> tuple[int, int]:
    """Every row a tenant owns, gone. Returns (facts, values) removed, because
    a destructive route answering only "ok" would be the least useful true
    thing it could say."""
    facts = await pool.fetchval("select count(*) from fact where tenant_id = $1", tenant)
    values = await pool.fetchval(
        "select count(*) from figure_value where tenant_id = $1", tenant
    )
    for table, column in (
        ("fact", "tenant_id"),
        ("figure_pointer", "tenant_id"),
        ("figure_index", "tenant_id"),
        ("index_state", "tenant_id"),
        ("figure_value", "tenant_id"),
        ("tenant_settings", "tenant_id"),
    ):
        await pool.execute(f"delete from {table} where {column} = $1", tenant)
    return int(facts or 0), int(values or 0)
