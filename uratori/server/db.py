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

-- One row per engine pass: the RunOut a caller was answered with, frozen.
-- This is the activity log the built-in UI reads -- "I sent a fact; what did
-- it cascade to" is only answerable later if somebody wrote it down at the
-- time. `shown` rows are rendered text and never re-derived, for the same
-- reason as the engine's own activity module: a figure redefined next week
-- must not rewrite the history of what moved.
create table if not exists run_log (
  id        bigint generated always as identity primary key,
  tenant_id text not null,
  at        timestamptz not null default now(),
  -- 'facts' or 'run': which door the pass came through. Named cause rather
  -- than trigger so nobody ever has to remember which keyword class TRIGGER
  -- falls into.
  cause     text not null,
  full_pass boolean not null,
  written   int not null,
  deleted   int not null,
  changed   int not null,
  rebuilt   jsonb not null,
  covered   jsonb not null,
  shown     jsonb not null
);

create index if not exists run_log_tenant_idx on run_log (tenant_id, id desc);
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


# ---------------------------------------------------------------- run log --

RUN_KEEP = 1000
"""Runs kept per tenant. The cap is on rows, not days: a busy tenant's history
is deep enough to investigate a bad sync, and an idle one's is never reaped by
a clock it does not share. Pruned on insert, so the table cannot outgrow the
cap between any two writes."""


async def record_run(
    pool: asyncpg.Pool[Any],
    tenant: str,
    cause: str,
    *,
    full: bool,
    written: int,
    deleted: int,
    changed: int,
    rebuilt: list[str],
    covered: list[str],
    shown: list[dict[str, Any]],
    keep: int = RUN_KEEP,
) -> None:
    """Freeze what one pass did. `shown` rows arrive already rendered -- this
    writes them down and nothing ever re-derives them."""
    async with pool.acquire() as connection:
        await connection.execute(
            """
            insert into run_log
              (tenant_id, cause, full_pass, written, deleted, changed,
               rebuilt, covered, shown)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            tenant,
            cause,
            full,
            written,
            deleted,
            changed,
            json.dumps(rebuilt),
            json.dumps(covered),
            json.dumps(shown),
        )
        await connection.execute(
            """
            delete from run_log
            where tenant_id = $1
              and id < (
                select min(id) from (
                  select id from run_log
                  where tenant_id = $1
                  order by id desc
                  limit $2
                ) newest
              )
            """,
            tenant,
            keep,
        )


_LOUD = "(written > 0 or deleted > 0 or changed > 0)"
"""The one predicate deciding which runs the log lists by default. A run that
neither landed a fact nor moved a value is a scheduled pass finding nothing --
kept, because an empty pass at a surprising time is itself a finding, but
hidden behind `quiet` so the log reads as cause and effect."""


async def page_runs(
    pool: asyncpg.Pool[Any], tenant: str, *, limit: int, quiet: bool
) -> tuple[list[dict[str, Any]], int]:
    """Newest runs first, and the honest count of quiet runs a default listing
    hid -- a filtered list that does not say what it filtered reads as
    complete."""
    where = "tenant_id = $1" if quiet else f"tenant_id = $1 and {_LOUD}"
    rows = await pool.fetch(
        f"""
        select id, at, cause, full_pass, written, deleted, changed,
               rebuilt, covered, shown
        from run_log
        where {where}
        order by id desc
        limit $2
        """,
        tenant,
        limit,
    )
    hidden = 0
    if not quiet:
        hidden = int(
            await pool.fetchval(
                f"select count(*) from run_log where tenant_id = $1 and not {_LOUD}",
                tenant,
            )
            or 0
        )
    return (
        [
            {
                "id": row["id"],
                "at": row["at"].isoformat(),
                "cause": row["cause"],
                "full": row["full_pass"],
                "written": row["written"],
                "deleted": row["deleted"],
                "changed": row["changed"],
                "rebuilt": _loaded(row["rebuilt"]),
                "covered": _loaded(row["covered"]),
                "shown": _loaded(row["shown"]),
            }
            for row in rows
        ],
        hidden,
    )


def _loaded(value: Any) -> Any:
    return value if isinstance(value, (list, dict)) else json.loads(value)


# ------------------------------------------------------------- browsing --


async def list_tenants(pool: asyncpg.Pool[Any]) -> list[dict[str, Any]]:
    """Every tenant the database knows, however it got there: facts pushed,
    settings stored, or runs logged. The union matters -- a tenant taught
    settings but never fed is exactly the misconfiguration an investigator
    comes looking for."""
    rows = await pool.fetch(
        """
        select tenant_id, sum(facts)::int as facts
        from (
          select tenant_id, count(*) as facts from fact group by tenant_id
          union all
          select tenant_id, 0 from tenant_settings
          union all
          select distinct tenant_id, 0 from run_log
        ) sources
        group by tenant_id
        order by tenant_id
        """
    )
    return [{"tenant": row["tenant_id"], "facts": row["facts"]} for row in rows]


async def fact_kind_counts(pool: asyncpg.Pool[Any], tenant: str) -> dict[str, int]:
    rows = await pool.fetch(
        "select kind, count(*)::int as records from fact where tenant_id = $1 group by kind",
        tenant,
    )
    return {row["kind"]: row["records"] for row in rows}


async def page_facts(
    pool: asyncpg.Pool[Any],
    tenant: str,
    kind: str,
    *,
    after: str | None,
    q: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """One keyset page of stored records, key order.

    Keyset rather than offset because the table moves under the reader: a sync
    landing mid-scroll shifts every offset, and a page that repeats or skips a
    record breaks the "what does the server actually hold" question this
    exists to answer. `q` is a substring match over key and record text --
    investigation-grade search, deliberately no cleverer than what it claims.
    """
    conditions = ["tenant_id = $1", "kind = $2"]
    args: list[Any] = [tenant, kind]
    if after is not None:
        args.append(after)
        conditions.append(f"key > ${len(args)}")
    if q:
        args.append(f"%{q}%")
        conditions.append(f"(key ilike ${len(args)} or value::text ilike ${len(args)})")
    where = " and ".join(conditions)

    args.append(limit + 1)  # one past the page is how `more` is a fact, not a guess
    rows = await pool.fetch(
        f"""
        select key, value, source_stamp
        from fact
        where {where}
        order by key
        limit ${len(args)}
        """,
        *args,
    )
    more = len(rows) > limit
    page = [
        {
            "key": row["key"],
            "value": row["value"]
            if isinstance(row["value"], dict)
            else json.loads(row["value"]),
            "source_stamp": row["source_stamp"].isoformat()
            if row["source_stamp"] is not None
            else None,
        }
        for row in rows[:limit]
    ]

    # The count ignores `after` (the total is the whole match, not the rest of
    # it) but honours `q` -- a search's total is the number of hits.
    count_conditions = ["tenant_id = $1", "kind = $2"]
    count_args: list[Any] = [tenant, kind]
    if q:
        count_args.append(f"%{q}%")
        count_conditions.append(
            f"(key ilike ${len(count_args)} or value::text ilike ${len(count_args)})"
        )
    total = int(
        await pool.fetchval(
            f"select count(*) from fact where {' and '.join(count_conditions)}",
            *count_args,
        )
        or 0
    )
    return page, more, total


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
        ("run_log", "tenant_id"),
    ):
        await pool.execute(f"delete from {table} where {column} = $1", tenant)
    return int(facts or 0), int(values or 0)
