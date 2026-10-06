"""The server's own database: the engine's tables, plus what makes it a service.

The server-only tables sit beside the engine's own (counted in neither
place on purpose -- a number here rots the day either side grows a table;
`_SERVER_SQL` below and the store's `SCHEMA_SQL` are the lists). The three
that carry decisions:

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
from collections.abc import Mapping, Sequence
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
  -- 'facts', 'facts-deferred' or 'run': which door the pass came through
  -- (deferred batches log too -- an import that wrote a million records and
  -- left no trace would be an unexplainable jump in every later cascade).
  -- Named cause rather than trigger so nobody ever has to remember which
  -- keyword class TRIGGER falls into.
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

-- Tenants holding deferred (written, never computed) batches. A row here is
-- a debt: the next pass for the tenant runs full whatever shape its caller
-- asked for, because the alternative is stored answers served as current
-- while silently describing the pre-import world. Cleared by the pass that
-- settles it.
create table if not exists import_debt (
  tenant_id text primary key,
  at        timestamptz not null default now()
);

-- Document-shaped facts (`as document`, docs/documents.md, D1): one row per
-- uploaded file, server-owned bookkeeping beside the fact itself. The fact
-- row (kind=document kind, key=document_id) carries title/mime/sha256/
-- pages/uploaded_at for any definition to read; this table exists so a
-- dedupe lookup (`sha256 already seen for this kind?`) and an orphan sweep
-- never have to decode a JSONB body to ask. `document_id` is the fact key:
-- the first 16 hex characters of the file's sha256, so a re-upload of the
-- same bytes is idempotent by construction.
create table if not exists document (
  tenant_id    text not null,
  kind         text not null,
  document_id  text not null,
  sha256       text not null,
  created_at   timestamptz not null default now(),
  primary key (tenant_id, kind, document_id)
);

create index if not exists document_sha_idx on document (tenant_id, kind, sha256);

-- The word layer: one row per word of one page, keyed generically by the
-- page kind the host named (`as page of`) rather than a fixed name --
-- `uratori/server/words.py` is the store this backs. Coordinates are
-- page-normalised [0,1] in the rendered frame (CropBox and `/Rotate`
-- applied, origin top-left, y down; `docs/http-api.md`'s coordinate
-- contract), so a box drawn against them is correct at any render scale.
-- Replace-set per (tenant, kind, key): a re-extraction or re-OCR deletes a
-- page's rows and reinserts the new layer whole, never patches one word.
create table if not exists document_page_words (
  tenant_id  text not null,
  kind       text not null,
  key        text not null,
  word_id    int not null,
  text       text not null,
  x0         double precision not null,
  y0         double precision not null,
  x1         double precision not null,
  y1         double precision not null,
  line_no    int not null,
  block_no   int not null,
  source     text not null check (source in ('pdf', 'ocr')),
  confidence double precision,
  primary key (tenant_id, kind, key, word_id)
);

-- Provenance (documents-plan-v3, D2): sibling metadata beside a record, never
-- in its body and never readable by a definition. One row per (kind, key,
-- field) a write's `provenance` map cited. Replace-set per (tenant, kind,
-- key): a write that is admitted by the stale-write guard (`uratori/server/
-- provenance.py`) replaces every row this record held wholesale, so a row
-- never outlives the body write that attested its value, and a batch with no
-- `provenance` entry for a key leaves that key's rows untouched. `value` is
-- the field's value *as attested*, read at write time -- a field whose
-- current value later disagrees with it is a finding the read path renders,
-- never silently repaired here. `word_ids`/`boxes` are both page-normalised
-- to the rendered frame (`docs/http-api.md`); `boxes` is populated either way
-- -- derived from the cited words' own boxes, or, for the no-text-layer
-- fallback, exactly the caller's own boxes (`anchored = false` then).
-- `matcher`/`reproducible` exist from the start for D4/D6 (a deterministic
-- matcher or a model-backed one); a host write through the facts route
-- leaves both at their defaults (null, true).
create table if not exists document_provenance (
  tenant_id    text not null,
  kind         text not null,
  key          text not null,
  field        text not null,
  page_key     text not null,
  word_ids     int[] not null default '{}',
  boxes        jsonb not null,
  printed      text,
  value        jsonb,
  anchored     boolean not null default true,
  extractor    text,
  parser       text,
  matcher      jsonb,
  reproducible boolean not null default true,
  at           timestamptz not null default now(),
  primary key (tenant_id, kind, key, field)
);

-- No separate index for (tenant_id, kind, key): the primary key above is a
-- composite btree on exactly those three columns plus `field`, so a lookup
-- by record (every field a record holds) already uses it as a prefix scan.

-- `extract` pointers (documents-plan-v3, D4) are NOT a table of their own:
-- an extract's name is bare, the same name its target `fact` carries, and
-- no figure, reading, projection or summary may ever be named bare (every
-- one of those is dotted) -- so the engine's own generic `figure_pointer`
-- (`EngineStore.pointer`/`set_pointer`, keyed `(tenant_id, name)`) already
-- has no collision to worry about. `run_pass` (`uratori/server/
-- extract_pass.py`) reads and writes an extract's pointer through that
-- same protocol method a figure's pointer uses, and `availability()`
-- (`uratori/engine/serve.py`) reads it the same way to answer
-- `behind-deploy` for a figure over a derived kind with a cold extract.
-- Retirement needs no `source_kind` of its own either: a retired extract's
-- derived records are just `fact` rows of kind = the extract's own name,
-- deleted the ordinary way.

-- A subject the patterns could not read (documents-plan-v3, D4): no record
-- is written, and this is written instead -- `(extract, version, subject,
-- field, reason)`, pruned whole for an extract whenever its version moves
-- (an old failure under a retired version explains nothing a current
-- reader can act on, and keeping it would double-count a page that now
-- fails for a different reason). `field` is null for a `many by row`
-- extract's own anchor failure (no row was ever found to fail on a named
-- field). Replace-set per (tenant, extract, version, subject): a subject
-- that starts succeeding is removed by the same statement that would have
-- rewritten it.
create table if not exists extract_failure (
  tenant_id text not null,
  extract   text not null,
  version   text not null,
  subject   text not null,
  field     text,
  reason    text not null,
  at        timestamptz not null default now(),
  primary key (tenant_id, extract, version, subject)
);

create index if not exists extract_failure_lookup
  on extract_failure (tenant_id, extract, version);

-- An `audit`'s reading (documents-plan-v3, D6): the model's blind pass
-- over one page, taken once per (tenant, audit, version, page) and
-- re-taken only for a new page, a new auditor version, or a rebuilt word
-- layer (`words_sha` moved -- carried here, not joined against the page's
-- own current one, so a stale reading is a fact this table can state
-- rather than something every reader has to join to notice). `parsed` is
-- `uratori.audit.judge.FieldReading`s as JSON, in the shape
-- `uratori.server.audit_pass._dump_fields` writes and
-- `_load_fields` reads back -- the prompt and response are kept verbatim
-- beside it, because a reviewer asking "what did the model actually see
-- and say" must never be answered with a derived summary.
create table if not exists audit_reading (
  tenant_id text not null,
  audit     text not null,
  version   text not null,
  page_key  text not null,
  words_sha text not null,
  prompt    text not null,
  model     text not null,
  response  text not null,
  parsed    jsonb not null,
  at        timestamptz not null default now(),
  primary key (tenant_id, audit, version, page_key)
);

create index if not exists audit_reading_lookup
  on audit_reading (tenant_id, audit, version);

-- One row per (extract, field, row) a verdict was judged from -- the
-- server-facing detail behind a page's single stored verdict word
-- (documents-plan-v3 D6's `AuditFinding`). Replace-set per (tenant, audit,
-- page): rewritten whole whenever the verdict is re-judged, which is
-- every pass the page's derived rows move and every time its reading
-- lands.
create table if not exists audit_finding (
  tenant_id text not null,
  audit     text not null,
  version   text not null,
  page_key  text not null,
  extract   text not null,
  field     text not null,
  row_index int not null,
  record    text,
  verdict   text not null,
  seen      jsonb,
  extracted jsonb,
  word_ids  int[] not null default '{}',
  boxes     jsonb not null default '[]',
  anchored  boolean not null default true,
  seen_text text,
  note      text,
  at        timestamptz not null default now(),
  primary key (tenant_id, audit, page_key, extract, field, row_index)
);

create index if not exists audit_finding_lookup
  on audit_finding (tenant_id, audit, page_key);

create index if not exists audit_finding_by_record
  on audit_finding (tenant_id, record) where record is not null;

-- A claimed lease on a (tenant, audit, version, page) the worker is
-- taking a reading for -- insert-on-conflict, expiring, so a restart or a
-- replica neither loses nor double-pays the model call (documents-plan-v3
-- D6's worker boundary, 5d). The work list is a query over
-- audit_reading/audit_lease, never a durable queue: a lease that expires
-- (the worker crashed mid-call) is simply claimable again by the next
-- sweep.
create table if not exists audit_lease (
  tenant_id  text not null,
  audit      text not null,
  version    text not null,
  page_key   text not null,
  claimed_at timestamptz not null default now(),
  expires_at timestamptz not null,
  primary key (tenant_id, audit, version, page_key)
);

create index if not exists audit_lease_expiry on audit_lease (expires_at);

-- One row per (tenant, audit, version, page) the worker last failed to
-- read -- a provider exception or timeout, isolated to the one page it
-- happened on (review finding D/F4) rather than left to abort the whole
-- sweep. Replace-one-row: a later successful read deletes it, so this
-- table only ever holds a page's *current* failure, never a history.
create table if not exists audit_read_failure (
  tenant_id text not null,
  audit     text not null,
  version   text not null,
  page_key  text not null,
  reason    text not null,
  at        timestamptz not null default now(),
  primary key (tenant_id, audit, version, page_key)
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


async def mark_deferred(pool: asyncpg.Pool[Any], tenant: str) -> None:
    await pool.execute(
        "insert into import_debt (tenant_id) values ($1) on conflict do nothing", tenant
    )


async def deferred(pool: asyncpg.Pool[Any], tenant: str) -> bool:
    return (
        await pool.fetchval("select 1 from import_debt where tenant_id = $1", tenant)
    ) is not None


async def clear_deferred(pool: asyncpg.Pool[Any], tenant: str) -> None:
    await pool.execute("delete from import_debt where tenant_id = $1", tenant)


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
    pool: asyncpg.Pool[Any],
    tenant: str,
    *,
    limit: int,
    quiet: bool,
    after: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Newest runs first, plus the two counts that keep the listing honest:
    the true total of runs the filter matches (a limit-capped list under no
    total reads as complete at every size) and, when quiet runs are being
    hidden, how many the default view is not showing.

    `after` is the keyset cursor -- the id of the last run the previous page
    showed, so the next page starts strictly below it. Ids because the log
    is ordered by them (assigned by insert, so newest-first is descending
    id), and a keyset over the display order is the one pager that neither
    drops nor doubles a row when a pass lands between two clicks."""
    where = "tenant_id = $1" if quiet else f"tenant_id = $1 and {_LOUD}"
    page_where = where if after is None else f"{where} and id < $3"
    rows = await pool.fetch(
        f"""
        select id, at, cause, full_pass, written, deleted, changed,
               rebuilt, covered, shown
        from run_log
        where {page_where}
        order by id desc
        limit $2
        """,
        tenant,
        limit,
        *([] if after is None else [after]),
    )
    total = int(
        await pool.fetchval(f"select count(*) from run_log where {where}", tenant) or 0
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
        total,
    )


def _loaded(value: Any) -> Any:
    return value if isinstance(value, (list, dict)) else json.loads(value)


# ------------------------------------------------------------- browsing --


def _contains(q: str) -> str:
    """`q` as a substring ILIKE pattern, with the pattern language disarmed.

    `%` and `_` are ILIKE's own operators; a reader searching for `PROJ_123`
    means those five characters, not "PROJ, anything, 123" -- and an unescaped
    `%` would match the whole table while reporting the count as if it were a
    real search. Backslash is Postgres's default LIKE escape, so it goes first.
    """
    disarmed = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{disarmed}%"


async def tenant_exists(pool: asyncpg.Pool[Any], tenant: str) -> bool:
    """Whether anything anywhere belongs to this tenant. The existence
    question only: the run door asks it as a guard, and answering it by
    counting every fact taxed each editor pass with a scan of the whole
    table for a yes/no."""
    held = await pool.fetchval(
        """
        select exists (select 1 from fact where tenant_id = $1)
            or exists (select 1 from run_log where tenant_id = $1)
        """,
        tenant,
    )
    return bool(held)


async def list_tenants(pool: asyncpg.Pool[Any]) -> list[dict[str, Any]]:
    """Every tenant the database knows, however it got there: facts pushed or
    runs logged. The union matters -- a tenant that has run and holds nothing
    is exactly the misconfiguration an investigator comes looking for.

    It used to count a third way in, a stored settings document. There are no
    settings; the table went with them, and existing deployments keep theirs
    untouched and unread rather than paying a destructive migration for a
    tidier list."""
    rows = await pool.fetch(
        """
        select tenant_id, sum(facts)::int as facts
        from (
          select tenant_id, count(*) as facts from fact group by tenant_id
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
        args.append(_contains(q))
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
        count_args.append(_contains(q))
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


# ------------------------------------------------------------ membership --
#
# Read-only views over the engine's own figure_index and index_built tables,
# for the UI's drill-down. They live here rather than on EngineStore because
# they are presentation reads (paged, counted, joined to names) and the store
# protocol is deliberately too narrow to grow them -- every method a store
# grows is a way a calculation could start depending on where records live.


async def index_versions(pool: asyncpg.Pool[Any], tenant: str) -> dict[str, str]:
    """Per grouping, the spec version its stored buckets were built under --
    empty if no pass has ever bucketed this tenant. The version half only:
    the UI compares specs; the dial fingerprint beside it is the engine's
    own staleness signal, and the membership pages state the dial caveat in
    prose instead."""
    rows = await pool.fetch(
        "select index_name, version from index_built where tenant_id = $1", tenant
    )
    return {row["index_name"]: row["version"] for row in rows}


async def legacy_index_set_version(pool: asyncpg.Pool[Any], tenant: str) -> str | None:
    """The pre-0.7 whole-set stamp, still standing only in the window between
    an upgrade and the tenant's first pass. Read so the UI can honour the
    same proof of currency the pass's seed will accept."""
    held = await pool.fetchval(
        "select version from index_state where tenant_id = $1", tenant
    )
    return held if isinstance(held, str) else None


async def bucket_counts(
    pool: asyncpg.Pool[Any], tenant: str, index: str, *, after: str | None, limit: int
) -> tuple[list[dict[str, Any]], bool, int, int]:
    """One keyset page of buckets with member counts, bucket order, plus the
    two honest totals: distinct members across every bucket (a record can be
    in several), and how many buckets exist. Paged like every other list on
    this surface -- a group can hold thousands of buckets (one per player per
    day), and a capped list with no way forward makes most of them
    unreachable from the page whose claim is that everything is."""
    conditions = ["tenant_id = $1", "index_name = $2"]
    args: list[Any] = [tenant, index]
    if after is not None:
        args.append(after)
        conditions.append(f"bucket > ${len(args)}")
    args.append(limit + 1)  # one past the page is how `more` is a fact, not a guess
    rows = await pool.fetch(
        f"""
        select bucket, count(*)::int as members
        from figure_index
        where {" and ".join(conditions)}
        group by bucket
        order by bucket
        limit ${len(args)}
        """,
        *args,
    )
    more = len(rows) > limit
    members = await pool.fetchval(
        "select count(distinct member) from figure_index "
        "where tenant_id = $1 and index_name = $2",
        tenant,
        index,
    )
    buckets_total = await pool.fetchval(
        "select count(distinct bucket) from figure_index "
        "where tenant_id = $1 and index_name = $2",
        tenant,
        index,
    )
    return (
        [{"bucket": row["bucket"], "members": row["members"]} for row in rows[:limit]],
        more,
        int(members or 0),
        int(buckets_total or 0),
    )


async def page_members(
    pool: asyncpg.Pool[Any],
    tenant: str,
    index: str,
    bucket: str,
    kind: str,
    *,
    after: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """One keyset page of a bucket's members, each joined to its record.

    `kind` is the index's **id_space**, not its fact kind: `keyed as` files one
    kind's records under another kind's ids, and joining the wrong table marks
    every member missing. A member with no record still lists (`value` None) --
    the membership row is the engine's claim and hiding it would un-say it.
    """
    conditions = ["i.tenant_id = $1", "i.index_name = $2", "i.bucket = $3"]
    args: list[Any] = [tenant, index, bucket, kind]
    if after is not None:
        args.append(after)
        conditions.append(f"i.member > ${len(args)}")
    args.append(limit + 1)  # one past the page is how `more` is a fact, not a guess
    rows = await pool.fetch(
        f"""
        select i.member, f.value
        from figure_index i
        left join fact f
          on f.tenant_id = i.tenant_id and f.kind = $4 and f.key = i.member
        where {" and ".join(conditions)}
        order by i.member
        limit ${len(args)}
        """,
        *args,
    )
    more = len(rows) > limit
    page = [
        {
            "member": row["member"],
            "value": (
                row["value"]
                if row["value"] is None or isinstance(row["value"], dict)
                else json.loads(row["value"])
            ),
        }
        for row in rows[:limit]
    ]
    total = int(
        await pool.fetchval(
            "select count(*) from figure_index "
            "where tenant_id = $1 and index_name = $2 and bucket = $3",
            tenant,
            index,
            bucket,
        )
        or 0
    )
    return page, more, total


async def memberships_of(
    pool: asyncpg.Pool[Any], tenant: str, member: str, indexes: list[str]
) -> dict[str, list[str]]:
    """Every bucket holding this member, per index, restricted to the given
    index names. The caller passes the current library's indexes, which is
    the guard against dropped-index leftovers; the `any()` clause here only
    keeps this query from returning rows the caller would have to filter."""
    rows = await pool.fetch(
        """
        select index_name, bucket
        from figure_index
        where tenant_id = $1 and member = $2 and index_name = any($3::text[])
        order by index_name, bucket
        """,
        tenant,
        member,
        indexes,
    )
    held: dict[str, list[str]] = {}
    for row in rows:
        held.setdefault(row["index_name"], []).append(row["bucket"])
    return held


async def count_kind(pool: asyncpg.Pool[Any], tenant: str, kind: str) -> int:
    return int(
        await pool.fetchval(
            "select count(*) from fact where tenant_id = $1 and kind = $2",
            tenant,
            kind,
        )
        or 0
    )


async def extract_pages_done(pool: asyncpg.Pool[Any], tenant: str, kind: str) -> int:
    """Distinct source pages behind this extract's current records -- the
    built-in `page` field (documents-plan-v3, D4) names the page every row
    was read from, so counting its distinct values counts pages with at
    least one surviving record without re-deriving anything the runner
    already decided."""
    return int(
        await pool.fetchval(
            "select count(distinct value->>'page') from fact "
            "where tenant_id = $1 and kind = $2",
            tenant,
            kind,
        )
        or 0
    )


async def fact_record(
    pool: asyncpg.Pool[Any], tenant: str, kind: str, key: str
) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "select value, source_stamp from fact "
        "where tenant_id = $1 and kind = $2 and key = $3",
        tenant,
        kind,
        key,
    )
    if row is None:
        return None
    return {
        "value": row["value"] if isinstance(row["value"], dict) else json.loads(row["value"]),
        "source_stamp": (
            row["source_stamp"].isoformat() if row["source_stamp"] is not None else None
        ),
    }


async def held_page_kind(
    pool: asyncpg.Pool[Any], tenant: str, page_kinds: Sequence[str], page_key: str
) -> str | None:
    """Which of this tenant's declared page kinds holds a page fact under
    this key -- the provenance write path's "is this page held" check
    (documents-plan-v3, D2). Candidates are tried in a fixed order (sorted
    by name) so two document kinds that happen to share a page key
    (identical bytes uploaded under both -- `document_sha_referenced`'s own
    scenario) resolve the same way on every call rather than racing; a known
    limitation of a citation shape that names the page but not its kind,
    recorded in the package report rather than hidden."""
    for kind in sorted(page_kinds):
        found = await pool.fetchval(
            "select 1 from fact where tenant_id = $1 and kind = $2 and key = $3",
            tenant,
            kind,
            page_key,
        )
        if found:
            return kind
    return None


# ---------------------------------------------------------------- tenants --


async def tenant_document_shas(pool: asyncpg.Pool[Any], tenant: str) -> list[str]:
    """Every distinct sha256 a tenant's `document` rows name -- read before
    `remove_tenant` deletes those rows, so the caller can unlink the tenant's
    blobs (`BlobStore.delete`) after the database side is gone. Blobs are
    tenant-namespaced, so this is every file `remove_tenant` is about to
    orphan for this tenant alone."""
    rows = await pool.fetch(
        "select distinct sha256 from document where tenant_id = $1", tenant
    )
    return [row["sha256"] for row in rows]


async def remove_tenant(
    pool: asyncpg.Pool[Any], tenant: str
) -> tuple[int, int, int, int, int, int]:
    """Every row a tenant owns, gone. Returns (facts, values, documents,
    provenance, extract_failures, audit_readings) removed, because a
    destructive route answering only "ok" would be the least useful true
    thing it could say. Derived facts are counted under `facts` already
    (they are ordinary `fact` rows); an audit's own *values* are counted
    under `values` the same way (`figure_value`, D6: `accept` saves through
    the same `EngineStore.save` a figure's recompute does); `audit_finding`
    carries no count of its own, for the same reason `extract_failure`'s
    own detail rows inside it never did. The blobs themselves are not this
    function's job -- it has no `BlobStore` to delete through -- so a
    caller that owns documents calls `tenant_document_shas` first and
    unlinks them after this returns."""
    facts = await pool.fetchval("select count(*) from fact where tenant_id = $1", tenant)
    values = await pool.fetchval(
        "select count(*) from figure_value where tenant_id = $1", tenant
    )
    documents = await pool.fetchval(
        "select count(*) from document where tenant_id = $1", tenant
    )
    provenance = await pool.fetchval(
        "select count(*) from document_provenance where tenant_id = $1", tenant
    )
    extract_failure_count = await pool.fetchval(
        "select count(*) from extract_failure where tenant_id = $1", tenant
    )
    audit_reading_count = await pool.fetchval(
        "select count(*) from audit_reading where tenant_id = $1", tenant
    )
    for table, column in (
        ("fact", "tenant_id"),
        ("figure_pointer", "tenant_id"),
        ("figure_index", "tenant_id"),
        ("index_built", "tenant_id"),
        ("index_state", "tenant_id"),
        ("figure_value", "tenant_id"),
        ("run_log", "tenant_id"),
        ("import_debt", "tenant_id"),
        ("document", "tenant_id"),
        ("document_page_words", "tenant_id"),
        ("document_provenance", "tenant_id"),
        ("extract_failure", "tenant_id"),
        ("audit_reading", "tenant_id"),
        ("audit_finding", "tenant_id"),
        ("audit_lease", "tenant_id"),
        ("audit_read_failure", "tenant_id"),
    ):
        await pool.execute(f"delete from {table} where {column} = $1", tenant)
    return (
        int(facts or 0),
        int(values or 0),
        int(documents or 0),
        int(provenance or 0),
        int(extract_failure_count or 0),
        int(audit_reading_count or 0),
    )


# --------------------------------------------------------------- documents --


async def document_by_sha(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    kind: str,
    sha256: str,
) -> str | None:
    """The `document_id` already holding this tenant's copy of these bytes
    under this kind, or `None` -- the dedupe check an upload makes before
    doing any parsing: a re-upload of the same file is `written: 0`, and
    this is cheaper than decoding every fact body of the kind to find out."""
    row = await conn.fetchrow(
        "select document_id from document where tenant_id = $1 and kind = $2 and sha256 = $3",
        tenant,
        kind,
        sha256,
    )
    return row["document_id"] if row is not None else None


async def record_document(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    kind: str,
    document_id: str,
    sha256: str,
) -> None:
    """Record a newly ingested document. Called in the same transaction as
    its fact and page rows -- `on conflict do nothing` because the dedupe
    check above already means this is only reached for bytes not seen
    before, and a concurrent duplicate upload racing it should lose quietly
    rather than with a constraint-violation 500."""
    await conn.execute(
        "insert into document (tenant_id, kind, document_id, sha256) values ($1, $2, $3, $4) "
        "on conflict (tenant_id, kind, document_id) do nothing",
        tenant,
        kind,
        document_id,
        sha256,
    )


async def delete_documents(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    kind: str,
    document_ids: Sequence[str],
) -> None:
    """Drop this tenant's bookkeeping rows for these document ids -- called
    beside the fact and word-layer deletes a document delete makes, in the
    same transaction."""
    if not document_ids:
        return
    await conn.execute(
        "delete from document where tenant_id = $1 and kind = $2 and document_id = any($3)",
        tenant,
        kind,
        list(document_ids),
    )


async def document_sha_referenced(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    sha256: str,
) -> bool:
    """Whether any `document` row of this tenant -- **any kind** -- still
    names this sha256.

    Content-addressed blobs are keyed `(tenant, sha256)` alone, with no
    kind in the key (`uratori/server/blobs.py`): two document kinds that
    happen to hold the same bytes share one blob. A delete must call this
    *after* removing its own row (same transaction, so the row it just
    deleted does not count itself) and only unlink the blob when it comes
    back false -- otherwise deleting `medical_record`'s copy orphans
    `insurance_form`'s, which still cites the same bytes.
    """
    return bool(
        await conn.fetchval(
            "select exists(select 1 from document where tenant_id = $1 and sha256 = $2)",
            tenant,
            sha256,
        )
    )


# ----------------------------------------------------------------- extract --


async def replace_extract_failures(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
    version: str,
    failures: Sequence[tuple[str, str | None, str]],
) -> None:
    """Replace-set per (tenant, extract, version): every subject named in
    `failures` is written (or rewritten, if its reason changed), and every
    OTHER subject this extract/version previously failed is cleared --
    exactly the subjects this pass actually looked at and found unreadable,
    never a wider or narrower set. Deliberately not scoped any finer (a
    whole page's worth of rows is cheap next to the pass that just read
    every one of that page's words)."""
    subjects = [subject for subject, _field, _reason in failures]
    await conn.execute(
        "delete from extract_failure where tenant_id = $1 and extract = $2 and version = $3 "
        "and subject = any($4::text[])",
        tenant,
        extract,
        version,
        subjects,
    )
    for subject, field, reason in failures:
        await conn.execute(
            "insert into extract_failure (tenant_id, extract, version, subject, field, reason, at) "
            "values ($1, $2, $3, $4, $5, $6, now())",
            tenant,
            extract,
            version,
            subject,
            field,
            reason,
        )


async def clear_extract_failures(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
    version: str,
    subjects: Sequence[str],
) -> None:
    """A page's subjects that produced a record this pass: whatever
    failures they held under this version are stale, cleared without a
    replacement row."""
    if not subjects:
        return
    await conn.execute(
        "delete from extract_failure where tenant_id = $1 and extract = $2 and version = $3 "
        "and subject = any($4::text[])",
        tenant,
        extract,
        version,
        list(subjects),
    )


async def clear_extract_failures_for_page(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
    version: str,
    page_key: str,
) -> None:
    """Every failure this extract/version holds under this one page --
    its own bare key (a non-`many` extract's anchor failure, or a `many`
    extract's "no row found" page-level failure) and every `#r...` row key
    -- gone, called when the page itself is retracted (deleted, or simply
    no longer there to extract)."""
    await conn.execute(
        "delete from extract_failure where tenant_id = $1 and extract = $2 and version = $3 "
        "and (subject = $4 or subject like $5)",
        tenant,
        extract,
        version,
        page_key,
        page_key + "#r%",
    )


async def delete_all_extract_failures(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
) -> None:
    """Every failure row this extract holds, under any version -- called
    when the extract itself is retired."""
    await conn.execute(
        "delete from extract_failure where tenant_id = $1 and extract = $2", tenant, extract
    )


async def extract_keys_for_page(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    kind: str,
    page_key: str,
    *,
    many: bool,
) -> list[str]:
    """Every key currently stored for this extract's kind that belongs to
    this one page: the page's own key when `many` is false, or every
    `<page key>#r...` row key when it is true. `#` is `uratori.documents.
    extract.ROW_SEPARATOR`, duplicated here as a literal rather than
    imported -- `uratori.documents.extract` imports this module's own
    sibling `uratori.server.provenance`, and a reverse import would cycle.
    """
    if not many:
        held = await conn.fetchval(
            "select 1 from fact where tenant_id = $1 and kind = $2 and key = $3",
            tenant,
            kind,
            page_key,
        )
        return [page_key] if held else []
    rows = await conn.fetch(
        "select key from fact where tenant_id = $1 and kind = $2 and key like $3",
        tenant,
        kind,
        page_key + "#r%",
    )
    return [r["key"] for r in rows]


async def prune_extract_failures(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
    current_version: str,
) -> None:
    """Every failure row for this extract under any OTHER version, gone --
    called once the pointer actually moves to `current_version`. An old
    version's failure explains nothing a reader of today's declaration can
    act on, and keeping it would double-count a page that now fails (or
    succeeds) for a different reason entirely."""
    await conn.execute(
        "delete from extract_failure where tenant_id = $1 and extract = $2 and version != $3",
        tenant,
        extract,
        current_version,
    )


async def extract_failures(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    extract: str,
    version: str,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "select subject, field, reason, at from extract_failure "
        "where tenant_id = $1 and extract = $2 and version = $3 order by subject",
        tenant,
        extract,
        version,
    )
    return [dict(r) for r in rows]


async def extract_failure_counts(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    version: str,
    names: Sequence[str],
) -> dict[str, int]:
    if not names:
        return {}
    rows = await conn.fetch(
        "select extract, count(*) as n from extract_failure "
        "where tenant_id = $1 and version = $2 and extract = any($3::text[]) "
        "group by extract",
        tenant,
        version,
        list(names),
    )
    return {r["extract"]: int(r["n"]) for r in rows}


# ------------------------------------------------------------------ audit --


async def replace_audit_reading(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    version: str,
    page_key: str,
    *,
    words_sha: str,
    prompt: str,
    model: str,
    response: str,
    parsed: Any,
) -> None:
    """One reading, written whole -- there is nothing to merge: a new
    reading for this (tenant, audit, version, page) replaces the old one
    outright, because the old one answered a question (what does this page
    say) that only has one honest answer at a time."""
    await conn.execute(
        "insert into audit_reading "
        "(tenant_id, audit, version, page_key, words_sha, prompt, model, response, parsed, at) "
        "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now()) "
        "on conflict (tenant_id, audit, version, page_key) do update set "
        "words_sha = excluded.words_sha, prompt = excluded.prompt, model = excluded.model, "
        "response = excluded.response, parsed = excluded.parsed, at = excluded.at",
        tenant,
        audit,
        version,
        page_key,
        words_sha,
        prompt,
        model,
        response,
        json.dumps(parsed),
    )


async def audit_reading(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    version: str,
    page_key: str,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "select words_sha, prompt, model, response, parsed, at from audit_reading "
        "where tenant_id = $1 and audit = $2 and version = $3 and page_key = $4",
        tenant,
        audit,
        version,
        page_key,
    )
    if row is None:
        return None
    out = dict(row)
    out["parsed"] = json.loads(out["parsed"])
    return out


async def pages_with_readings(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    version: str,
) -> set[str]:
    """Every page this audit already has a current-version reading for --
    the worker's own "what is left to do" query is the complement of this
    against the pages in scope (`documents-plan-v3` D6's worker boundary,
    5d)."""
    rows = await conn.fetch(
        "select page_key from audit_reading where tenant_id = $1 and audit = $2 and version = $3",
        tenant,
        audit,
        version,
    )
    return {r["page_key"] for r in rows}


async def prune_audit_readings(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    current_version: str,
) -> None:
    """Every reading (and the findings judged from it) for this audit under
    any OTHER version, gone -- called once the pointer moves to
    `current_version`, the same moment `prune_extract_failures` acts on."""
    await conn.execute(
        "delete from audit_reading where tenant_id = $1 and audit = $2 and version != $3",
        tenant,
        audit,
        current_version,
    )


async def delete_audit_readings_for_pages(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    page_keys: Sequence[str],
) -> None:
    """Every reading, under any audit or version, for pages that are gone
    -- called alongside a page's own deletion, never on its own."""
    if not page_keys:
        return
    await conn.execute(
        "delete from audit_reading where tenant_id = $1 and page_key = any($2::text[])",
        tenant,
        list(page_keys),
    )
    await conn.execute(
        "delete from audit_finding where tenant_id = $1 and page_key = any($2::text[])",
        tenant,
        list(page_keys),
    )
    await conn.execute(
        "delete from audit_lease where tenant_id = $1 and page_key = any($2::text[])",
        tenant,
        list(page_keys),
    )


async def replace_audit_findings(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    version: str,
    page_key: str,
    findings: Sequence[Mapping[str, Any]],
) -> None:
    """Replace-set per (tenant, audit, page): every finding this verdict
    was just judged from, and nothing this page's last judging left behind
    -- a verdict rewritten from a changed extract or a changed reading must
    not go on showing a finding that explained the previous one. The
    delete is version-agnostic (every prior version's rows for this page
    go too) -- `version` is written on each surviving row so a *reader*
    (`audit_findings_citing`) can tell a stale row from a current one in
    the window between a redefinition and this page's next judging,
    without waiting for that judging to land (review finding C/F3)."""
    await conn.execute(
        "delete from audit_finding where tenant_id = $1 and audit = $2 and page_key = $3",
        tenant,
        audit,
        page_key,
    )
    if not findings:
        return
    await conn.executemany(
        "insert into audit_finding "
        "(tenant_id, audit, version, page_key, extract, field, row_index, record, verdict, "
        "seen, extracted, word_ids, boxes, anchored, seen_text, note, at) "
        "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, now())",
        [
            (
                tenant,
                audit,
                version,
                page_key,
                f["extract"],
                f["field"],
                f["row"],
                f["record"],
                f["verdict"],
                json.dumps(f["seen"]),
                json.dumps(f["extracted"]),
                list(f["words"]),
                json.dumps(f["boxes"]),
                f["anchored"],
                f["seen_text"],
                f["note"],
            )
            for f in findings
        ],
    )


async def audit_findings_for_page(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    audit: str,
    page_key: str,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "select extract, field, row_index, record, verdict, seen, extracted, word_ids, "
        "boxes, anchored, seen_text, note, at from audit_finding "
        "where tenant_id = $1 and audit = $2 and page_key = $3 order by extract, field, row_index",
        tenant,
        audit,
        page_key,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["seen"] = json.loads(d["seen"]) if d["seen"] is not None else None
        d["extracted"] = json.loads(d["extracted"]) if d["extracted"] is not None else None
        d["boxes"] = json.loads(d["boxes"])
        out.append(d)
    return out


async def audit_findings_citing(
    conn: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    tenant: str,
    record: str,
) -> list[dict[str, Any]]:
    """Every finding that names this record -- the derived record page's
    "verdicts citing it" (documents-plan-v3 D6's surfaces, 5e). Carries
    each row's own `version`: a redefined audit's rows for this page are
    rewritten wholesale only once a reading lands under the new version
    (`replace_audit_findings`), so a caller must compare `version` against
    the audit's *current* version itself before treating a row as live
    (review finding C/F3) -- this function does not know which version is
    current, only the library the caller already has does."""
    rows = await conn.fetch(
        "select audit, version, page_key, extract, field, row_index, verdict, seen, "
        "extracted, word_ids, boxes, anchored, seen_text, note, at from audit_finding "
        "where tenant_id = $1 and record = $2 order by audit, field",
        tenant,
        record,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["seen"] = json.loads(d["seen"]) if d["seen"] is not None else None
        d["extracted"] = json.loads(d["extracted"]) if d["extracted"] is not None else None
        d["boxes"] = json.loads(d["boxes"])
        out.append(d)
    return out


async def unread_pages(
    pool: asyncpg.Pool[Any],
    tenant: str,
    audit: str,
    version: str,
    candidates: Sequence[tuple[str, str]],
) -> list[str]:
    """The worker's own work list: every candidate `(page_key, words_sha)`
    with no current-version reading matching that `words_sha`, and no live
    lease -- a query, not a queue, so a restart or a replica re-derives it
    rather than losing or duplicating work (`documents-plan-v3` D6)."""
    if not candidates:
        return []
    rows = await pool.fetch(
        "select page_key, words_sha from audit_reading "
        "where tenant_id = $1 and audit = $2 and version = $3 "
        "and page_key = any($4::text[])",
        tenant,
        audit,
        version,
        [p for p, _ in candidates],
    )
    current = {r["page_key"]: r["words_sha"] for r in rows}
    leased = await pool.fetch(
        "select page_key from audit_lease where tenant_id = $1 and audit = $2 "
        "and version = $3 and expires_at > now() and page_key = any($4::text[])",
        tenant,
        audit,
        version,
        [p for p, _ in candidates],
    )
    leased_keys = {r["page_key"] for r in leased}
    return [
        page_key
        for page_key, words_sha in candidates
        if current.get(page_key) != words_sha and page_key not in leased_keys
    ]


async def claim_audit_lease(
    pool: asyncpg.Pool[Any],
    tenant: str,
    audit: str,
    version: str,
    page_key: str,
    *,
    ttl_seconds: float,
) -> bool:
    """Claim a page for this worker, or say no -- insert-on-conflict against
    an expired or absent lease, so two workers (a restart racing the
    process it is replacing, or two replicas) cannot both pay for the same
    model call. Returns whether the claim was this call's."""
    row = await pool.fetchrow(
        "insert into audit_lease (tenant_id, audit, version, page_key, claimed_at, expires_at) "
        "values ($1, $2, $3, $4, now(), now() + $5 * interval '1 second') "
        "on conflict (tenant_id, audit, version, page_key) do update set "
        "claimed_at = now(), expires_at = now() + $5 * interval '1 second' "
        "where audit_lease.expires_at <= now() "
        "returning 1",
        tenant,
        audit,
        version,
        page_key,
        ttl_seconds,
    )
    return row is not None


async def release_audit_lease(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str, page_key: str
) -> None:
    await pool.execute(
        "delete from audit_lease where tenant_id = $1 and audit = $2 and version = $3 "
        "and page_key = $4",
        tenant,
        audit,
        version,
        page_key,
    )


async def record_audit_read_failure(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str, page_key: str, reason: str
) -> None:
    """A provider call for this page raised or timed out -- recorded
    against the page's reading attempt, never left to abort every other
    page's turn in the sweep (review finding D/F4). Upsert: the latest
    attempt's reason is the one that matters."""
    await pool.execute(
        "insert into audit_read_failure (tenant_id, audit, version, page_key, reason, at) "
        "values ($1, $2, $3, $4, $5, now()) "
        "on conflict (tenant_id, audit, version, page_key) do update set "
        "reason = excluded.reason, at = excluded.at",
        tenant,
        audit,
        version,
        page_key,
        reason,
    )


async def clear_audit_read_failure(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str, page_key: str
) -> None:
    await pool.execute(
        "delete from audit_read_failure where tenant_id = $1 and audit = $2 and version = $3 "
        "and page_key = $4",
        tenant,
        audit,
        version,
        page_key,
    )


async def audit_read_failures(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str
) -> dict[str, str]:
    """Every page this audit's current version last failed to read, with
    the reason -- the declaration page's own "could not read" list,
    beside its unaudited backlog."""
    rows = await pool.fetch(
        "select page_key, reason from audit_read_failure "
        "where tenant_id = $1 and audit = $2 and version = $3",
        tenant,
        audit,
        version,
    )
    return {str(r["page_key"]): str(r["reason"]) for r in rows}


async def audit_verdict_counts(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str
) -> dict[str, int]:
    """How many pages hold each verdict word, under this audit's *current*
    version -- the declaration page's own counts (documents-plan-v3 D6's
    surfaces, 5e). Read off `figure_value`, the same table `accept` writes
    through: an audit's value is to the engine exactly what a figure's is,
    so there is no second count to keep in step."""
    rows = await pool.fetch(
        "select value, count(*) as n from figure_value "
        "where tenant_id = $1 and name = $2 and version = $3 group by value",
        tenant,
        audit,
        version,
    )
    out: dict[str, int] = {}
    for r in rows:
        value = r["value"]
        word = json.loads(value) if isinstance(value, str) else value
        if isinstance(word, str):
            out[word] = int(r["n"])
    return out


async def audit_disputed_pages(
    pool: asyncpg.Pool[Any], tenant: str, audit: str, version: str, verdicts: Sequence[str]
) -> list[str]:
    """Every page currently holding one of `verdicts` (typically
    `("disagrees", "missed")`) under this audit's current version -- the
    findings route drives off this, rather than off `audit_finding`
    directly, so a page re-defined out from under a lingering pre-version
    finding row never surfaces as a false positive."""
    rows = await pool.fetch(
        "select subject_id from figure_value "
        "where tenant_id = $1 and name = $2 and version = $3 and value = any($4::jsonb[])",
        tenant,
        audit,
        version,
        [json.dumps(v) for v in verdicts],
    )
    return [r["subject_id"] for r in rows]


async def discard_audit_readings(
    pool: asyncpg.Pool[Any], tenant: str, audit: str
) -> None:
    """Every reading this audit holds for this tenant, under any version,
    gone -- the re-audit operator verb (`POST /tenants/{t}/runs {"audit":
    "<name>"}`): the worker takes every page again, at no cost to the
    extract's own derived facts, which this never touches. Findings go
    with their readings (`delete_audit_readings_for_pages` deletes both by
    page; this deletes both by audit, since a re-audit is "take every page
    again", not "this page is gone")."""
    await pool.execute(
        "delete from audit_finding where tenant_id = $1 and audit = $2", tenant, audit
    )
    await pool.execute(
        "delete from audit_reading where tenant_id = $1 and audit = $2", tenant, audit
    )
