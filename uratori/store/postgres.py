"""The default persistence: Postgres, for the engine's state and for facts.

The key shapes and their reasons live on the protocols in `base.py`; this file
is only their SQL. `SCHEMA_SQL` at the bottom is the DDL a fresh host applies
from its own migration mechanism -- this package deliberately has none, because
a library that migrates a database it does not own is a library that races its
host's migrations.

`tenant_id` is `text` here. A host with narrower tenant keys (the project this
grew out of uses uuids, with foreign keys into its own tenant table) can keep
its own DDL as long as the columns read and written below exist; the store only
ever passes tenant ids through.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import asyncpg

from ..lang.plan import Value
from .base import BucketChange, FactRow, Pointer, StoredValue


class PostgresEngineStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # --------------------------------------------------------- definitions --

    async def ensure_definition(
        self,
        version: str,
        name: str,
        declaration: str,
        doc: str,
        display: str,
        source: str,
        plan: Mapping[str, Any],
    ) -> None:
        """Record a definition, source and all, keyed by its content hash.

        The **source** is stored rather than only the plan, because a value
        outlives the build that produced it: a citation of `name@version` has to
        be able to show the formula that computed it even after the definition
        has moved on. A plan is a description of a definition, and a description
        is a second place for the truth to live.
        """
        await self._pool.execute(
            """
            insert into figure_definition
              (version, name, declaration, doc, display, source, plan)
            values ($1, $2, $3, $4, $5, $6, $7)
            on conflict (version) do nothing
            """,
            version,
            name,
            declaration,
            doc,
            display,
            source,
            json.dumps(plan),
        )

    async def pointer(self, tenant: str, name: str) -> Pointer | None:
        row = await self._pool.fetchrow(
            "select version, settings_fingerprint from figure_pointer "
            "where tenant_id = $1 and name = $2",
            tenant,
            name,
        )
        if row is None:
            return None
        return Pointer(version=row["version"], settings_fingerprint=row["settings_fingerprint"])

    async def pointers(self, tenant: str) -> dict[str, Pointer]:
        rows = await self._pool.fetch(
            "select name, version, settings_fingerprint from figure_pointer where tenant_id = $1",
            tenant,
        )
        return {
            r["name"]: Pointer(
                version=r["version"], settings_fingerprint=r["settings_fingerprint"]
            )
            for r in rows
        }

    async def set_pointer(self, tenant: str, name: str, pointer: Pointer) -> bool:
        """Returns whether it actually moved, which is the release event."""
        row = await self._pool.fetchrow(
            """
            insert into figure_pointer (tenant_id, name, version, settings_fingerprint, moved_at)
            values ($1, $2, $3, $4, now())
            on conflict (tenant_id, name) do update
              set version = excluded.version,
                  settings_fingerprint = excluded.settings_fingerprint,
                  moved_at = now()
              where figure_pointer.version is distinct from excluded.version
                 or figure_pointer.settings_fingerprint
                    is distinct from excluded.settings_fingerprint
            returning 1 as moved
            """,
            tenant,
            name,
            pointer.version,
            pointer.settings_fingerprint,
        )
        return row is not None

    async def index_versions(self, tenant: str) -> dict[str, str]:
        rows = await self._pool.fetch(
            "select index_name, version from index_built where tenant_id = $1", tenant
        )
        return {row["index_name"]: row["version"] for row in rows}

    async def set_index_version(self, tenant: str, index: str, version: str) -> None:
        await self._pool.execute(
            """
            insert into index_built (tenant_id, index_name, version, moved_at)
            values ($1, $2, $3, now())
            on conflict (tenant_id, index_name) do update
              set version = excluded.version, moved_at = now()
              where index_built.version is distinct from excluded.version
            """,
            tenant,
            index,
            version,
        )

    async def legacy_index_set(self, tenant: str) -> str | None:
        held = await self._pool.fetchval(
            "select version from index_state where tenant_id = $1", tenant
        )
        return held if isinstance(held, str) else None

    async def drop_legacy_index_set(self, tenant: str) -> None:
        await self._pool.execute(
            "delete from index_state where tenant_id = $1", tenant
        )

    # -------------------------------------------------------------- indexes --

    async def set_buckets(
        self, tenant: str, index: str, member: str, buckets: Sequence[str]
    ) -> BucketChange:
        held = {
            r["bucket"]
            for r in await self._pool.fetch(
                "select bucket from figure_index "
                "where tenant_id = $1 and index_name = $2 and member = $3",
                tenant,
                index,
                member,
            )
        }
        wanted = set(buckets)
        added = sorted(wanted - held)
        removed = sorted(held - wanted)

        if removed:
            await self._pool.execute(
                "delete from figure_index where tenant_id = $1 and index_name = $2 "
                "and member = $3 and bucket = any($4::text[])",
                tenant,
                index,
                member,
                removed,
            )
        if added:
            await self._pool.executemany(
                "insert into figure_index (tenant_id, index_name, bucket, member) "
                "values ($1, $2, $3, $4) on conflict do nothing",
                [(tenant, index, bucket, member) for bucket in added],
            )
        return BucketChange(
            index=index, member=member, added=tuple(added), removed=tuple(removed)
        )

    async def set_buckets_many(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> list[BucketChange]:
        """One round trip for a batch, because a sync writes hundreds of records
        and a query per record is the shape that turns a poll into a minute."""
        if not wanted:
            return []
        members = list(wanted)
        held: dict[str, set[str]] = {m: set() for m in members}
        for row in await self._pool.fetch(
            "select member, bucket from figure_index "
            "where tenant_id = $1 and index_name = $2 and member = any($3::text[])",
            tenant,
            index,
            members,
        ):
            held[row["member"]].add(row["bucket"])

        changes: list[BucketChange] = []
        to_remove: list[tuple[str, str, str, str]] = []
        to_add: list[tuple[str, str, str, str]] = []
        for member in members:
            want = set(wanted[member])
            added = sorted(want - held[member])
            removed = sorted(held[member] - want)
            if not added and not removed:
                continue
            changes.append(
                BucketChange(index=index, member=member, added=tuple(added), removed=tuple(removed))
            )
            to_remove.extend((tenant, index, bucket, member) for bucket in removed)
            to_add.extend((tenant, index, bucket, member) for bucket in added)

        if to_remove:
            await self._pool.executemany(
                "delete from figure_index where tenant_id = $1 and index_name = $2 "
                "and bucket = $3 and member = $4",
                to_remove,
            )
        if to_add:
            await self._pool.executemany(
                "insert into figure_index (tenant_id, index_name, bucket, member) "
                "values ($1, $2, $3, $4) on conflict do nothing",
                to_add,
            )
        return changes

    async def buckets_holding(self, tenant: str, index: str, member: str) -> list[str]:
        rows = await self._pool.fetch(
            "select bucket from figure_index "
            "where tenant_id = $1 and index_name = $2 and member = $3",
            tenant,
            index,
            member,
        )
        return [r["bucket"] for r in rows]

    async def members(self, tenant: str, index: str, bucket: str) -> frozenset[str]:
        rows = await self._pool.fetch(
            "select member from figure_index "
            "where tenant_id = $1 and index_name = $2 and bucket = $3",
            tenant,
            index,
            bucket,
        )
        return frozenset(r["member"] for r in rows)

    async def all_buckets(self, tenant: str, index: str) -> dict[str, frozenset[str]]:
        rows = await self._pool.fetch(
            "select bucket, member from figure_index where tenant_id = $1 and index_name = $2",
            tenant,
            index,
        )
        out: dict[str, set[str]] = {}
        for row in rows:
            out.setdefault(row["bucket"], set()).add(row["member"])
        return {k: frozenset(v) for k, v in out.items()}

    async def bucket_keys(self, tenant: str, index: str) -> list[str]:
        rows = await self._pool.fetch(
            "select distinct bucket from figure_index where tenant_id = $1 and index_name = $2",
            tenant,
            index,
        )
        return [r["bucket"] for r in rows]

    async def index_has_rows(self, tenant: str, index: str) -> bool:
        row = await self._pool.fetchrow(
            "select 1 from figure_index where tenant_id = $1 and index_name = $2 limit 1",
            tenant,
            index,
        )
        return row is not None

    async def drop_index(self, tenant: str, index: str) -> None:
        # One transaction: rows without a stamp read as never-built (honest),
        # but a stamp without rows would read as built-and-empty -- a lie
        # about a grouping that is gone.
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "delete from figure_index where tenant_id = $1 and index_name = $2",
                tenant,
                index,
            )
            await conn.execute(
                "delete from index_built where tenant_id = $1 and index_name = $2",
                tenant,
                index,
            )

    async def remove_member(self, tenant: str, index: str, member: str) -> None:
        await self._pool.execute(
            "delete from figure_index where tenant_id = $1 and index_name = $2 and member = $3",
            tenant,
            index,
            member,
        )

    async def replace_index(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> None:
        """Prune and rebuild inside one transaction.

        A rebuild that deleted first and then failed half way would leave an
        index empty, and an empty index is a figure reading zero for everybody
        rather than an error anybody sees.

        The insert is chunked because the pool carries a per-statement
        timeout: a million-member index (an era of plays) rewritten as one
        executemany is one statement, and it crossing the timeout killed the
        pass that would have rebuilt it. Chunks keep every statement short;
        the transaction still makes the rebuild all-or-nothing.
        """
        rows = [
            (tenant, index, bucket, member)
            for member, buckets in wanted.items()
            for bucket in buckets
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "delete from figure_index where tenant_id = $1 and index_name = $2", tenant, index
            )
            for start in range(0, len(rows), 50_000):
                await conn.executemany(
                    "insert into figure_index (tenant_id, index_name, bucket, member) "
                    "values ($1, $2, $3, $4) on conflict do nothing",
                    rows[start : start + 50_000],
                )

    # --------------------------------------------------------------- values --

    async def value(self, tenant: str, name: str, version: str, subject: str) -> StoredValue | None:
        row = await self._pool.fetchrow(
            "select subject_id, value, members, subject_label from figure_value "
            "where tenant_id = $1 and name = $2 and version = $3 and subject_id = $4",
            tenant,
            name,
            version,
            subject,
        )
        return _stored(row) if row is not None else None

    async def values(self, tenant: str, name: str, version: str) -> list[StoredValue]:
        rows = await self._pool.fetch(
            "select subject_id, value, members, subject_label from figure_value "
            "where tenant_id = $1 and name = $2 and version = $3 order by subject_id",
            tenant,
            name,
            version,
        )
        return [_stored(r) for r in rows]

    async def values_under(
        self, tenant: str, name: str, version: str, prefix: str
    ) -> list[StoredValue]:
        rows = await self._pool.fetch(
            "select subject_id, value, members, subject_label from figure_value "
            "where tenant_id = $1 and name = $2 and version = $3 and subject_id like $4 "
            "order by subject_id",
            tenant,
            name,
            version,
            f"{prefix}%",
        )
        return [_stored(r) for r in rows]

    async def values_in_range(
        self, tenant: str, name: str, version: str, frm: str, to: str
    ) -> list[StoredValue]:
        """Time-keyed values whose bucket label falls inside a range.

        The comparison is on the text after the separator, which works because
        every label is fixed-width local ISO time -- `2026-08-23`, or
        `2026-08-23T14:30` at a sub-day grain -- and those sort
        lexicographically -- which is the reason the truncation produces a
        label rather than an epoch. The caller supplies bounds in the stored grain's own
        shape: bare days for a day figure, `T00:00`/`T23:59` suffixes for a
        sub-day one, since every label on a day sorts after the bare day
        string.
        """
        rows = await self._pool.fetch(
            """
            select subject_id, value, members, subject_label from figure_value
            where tenant_id = $1 and name = $2 and version = $3
              and position('@' in subject_id) > 0
              and split_part(subject_id, '@', 2) between $4 and $5
            order by subject_id
            """,
            tenant,
            name,
            version,
            frm,
            to,
        )
        return [_stored(r) for r in rows]

    async def subjects(self, tenant: str, name: str, version: str) -> list[str]:
        rows = await self._pool.fetch(
            "select subject_id from figure_value where tenant_id = $1 and name = $2 and version = $3",
            tenant,
            name,
            version,
        )
        return [r["subject_id"] for r in rows]

    async def save(
        self,
        tenant: str,
        name: str,
        version: str,
        subject: str,
        value: Value,
        members: Iterable[str],
        label: str,
    ) -> None:
        await self._pool.execute(
            """
            insert into figure_value
              (tenant_id, name, version, subject_id, value, members, subject_label, computed_at)
            values ($1, $2, $3, $4, $5, $6, $7, now())
            on conflict (tenant_id, name, version, subject_id) do update
              set value = excluded.value,
                  members = excluded.members,
                  subject_label = excluded.subject_label,
                  computed_at = now()
            """,
            tenant,
            name,
            version,
            subject,
            json.dumps(value),
            json.dumps(list(members)),
            label,
        )

    async def remove(self, tenant: str, name: str, version: str, subject: str) -> None:
        await self._pool.execute(
            "delete from figure_value where tenant_id = $1 and name = $2 and version = $3 "
            "and subject_id = $4",
            tenant,
            name,
            version,
            subject,
        )


def _stored(row: asyncpg.Record) -> StoredValue:
    return StoredValue(
        subject=row["subject_id"],
        value=json.loads(row["value"]),
        members=tuple(json.loads(row["members"])),
        label=row["subject_label"],
    )


class PostgresFactStore:
    """The reference fact table: enough for the engine, and honestly no more.

    A real product's facts carry more than this -- provenance, freshness,
    frozen-at-source markers, a connection to cascade from -- and a host with
    that kind of table should implement `FactSource` over it directly (it is
    two reads) and keep its own write path. This store exists for the host that
    has nothing yet: `upsert` is the whole ingestion API, and what it returns is
    the thing the engine actually needs to hear about.

    **`upsert` reports only the keys whose value moved.** A reconcile rewrites
    thousands of records that have not changed, and treating those as changes
    would recompute the whole engine on every pass. The store decides what
    moved, because it is the only thing holding both versions.
    """

    def __init__(
        self,
        pool: asyncpg.Pool | asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    ) -> None:
        # A connection (or a pool's proxy for one) is accepted alongside a
        # pool so the facts route can run one batch's deletes and writes
        # inside a single transaction -- asyncpg gives all three the same
        # query surface, and this store only queries.
        self._pool = pool

    async def of_kind(self, tenant: str, kind: str) -> list[FactRow]:
        rows = await self._pool.fetch(
            "select kind, key, value from fact where tenant_id = $1 and kind = $2",
            tenant,
            kind,
        )
        return [_fact(r) for r in rows]

    async def some(self, tenant: str, kind: str, keys: Sequence[str]) -> list[FactRow]:
        if not keys:
            return []
        rows = await self._pool.fetch(
            "select kind, key, value from fact "
            "where tenant_id = $1 and kind = $2 and key = any($3::text[])",
            tenant,
            kind,
            list(keys),
        )
        return [_fact(r) for r in rows]

    async def upsert(
        self,
        tenant: str,
        kind: str,
        records: Mapping[str, Mapping[str, Any]],
        stamps: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Write records, and answer the keys whose value actually moved.

        `stamps` are the provider's own updated-at instants, and they are the
        stale-write guard: a batch built from a snapshot read *before* another
        batch's event must not put the pre-event record back -- which is
        exactly what a reconcile racing a webhook produces, with the board
        then carrying the wrong figure until the next reconcile "discovers"
        the change all over again.

        The comparison is `>=`, not `>`, so a rewrite at the same version
        still lands -- that is how a parser change reaches records nobody has
        touched. Either side missing a stamp means there is nothing to
        compare, so the write goes through: a guard that cannot see the
        versions must never be the thing that drops data.
        """
        held = dict(stamps or {})
        moved: list[str] = []
        for key, value in records.items():
            row = await self._pool.fetchrow(
                """
                insert into fact (tenant_id, kind, key, value, source_stamp)
                values ($1, $2, $3, $4, $5)
                on conflict (tenant_id, kind, key) do update
                  set value = excluded.value,
                      source_stamp = excluded.source_stamp
                  where fact.value is distinct from excluded.value
                    and (
                      fact.source_stamp is null
                      or excluded.source_stamp is null
                      or excluded.source_stamp >= fact.source_stamp
                    )
                returning key
                """,
                tenant,
                kind,
                key,
                json.dumps(value),
                _instant(held.get(key)),
            )
            if row is not None:
                moved.append(key)
        return moved

    async def delete(self, tenant: str, kind: str, keys: Sequence[str]) -> None:
        if not keys:
            return
        await self._pool.execute(
            "delete from fact where tenant_id = $1 and kind = $2 and key = any($3::text[])",
            tenant,
            kind,
            list(keys),
        )


def _instant(text: str | None) -> Any:
    """An ISO stamp as a datetime, or nothing.

    Unparseable answers None rather than raising: a malformed stamp means
    there is nothing to compare, and the guard must never be the thing that
    drops data over its own input's spelling.
    """
    if not text:
        return None
    from datetime import UTC, datetime

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _fact(row: asyncpg.Record) -> FactRow:
    value = row["value"]
    return FactRow(
        kind=row["kind"],
        key=row["key"],
        value=value if isinstance(value, dict) else json.loads(value),
    )


SCHEMA_SQL = """
-- uratori's tables. Apply from the host's own migration mechanism; this
-- package never migrates a database it does not own.

create table if not exists fact (
  tenant_id    text not null,
  kind         text not null,
  key          text not null,
  value        jsonb not null,
  -- The provider's own updated timestamp, never ours: the question a write
  -- has to answer is *which version of the record is this*, and only the
  -- provider can say. See PostgresFactStore.upsert for the stale-write guard
  -- built on it.
  source_stamp timestamptz,
  primary key (tenant_id, kind, key)
);

-- For databases built before the stamp existed; a no-op everywhere else.
alter table fact add column if not exists source_stamp timestamptz;

create table if not exists figure_definition (
  version     text primary key,
  name        text not null,
  declaration text not null,
  display     text not null,
  doc         text not null,
  -- The source as written. A citation of name@version has to be able to show
  -- the formula that computed it even after the definition has moved on.
  source      text not null,
  plan        jsonb not null,
  created_at  timestamptz not null default now()
);

create index if not exists figure_definition_name_idx on figure_definition (name);

create table if not exists figure_pointer (
  tenant_id  text not null,
  name       text not null,
  version    text not null references figure_definition(version),
  settings_fingerprint text not null default '',
  moved_at   timestamptz not null default now(),
  primary key (tenant_id, name)
);

-- Which spec version each grouping's stored buckets were built under,
-- recorded only after that index's rebuild actually ran. Per index rather
-- than one stamp over the set: staleness is the unit of work, and a single
-- stamp made one new filter re-bucket every grouping over every record.
-- It exists for every index, including the ones no figure reads (a
-- projection's population), whose redefinition moves no figure_pointer row.
create table if not exists index_built (
  tenant_id  text not null,
  index_name text not null,
  version    text not null,
  moved_at   timestamptz not null default now(),
  primary key (tenant_id, index_name)
);

-- The pre-0.7 whole-set stamp, kept only so an upgraded deployment's first
-- pass can tell "fully built yesterday" from "never built": a row matching
-- the current library seeds index_built and retires; any row retires. New
-- installs never write it.
create table if not exists index_state (
  tenant_id  text primary key,
  version    text not null,
  moved_at   timestamptz not null default now()
);

create table if not exists figure_index (
  tenant_id  text not null,
  index_name text not null,
  -- '' for a predicate index, which has exactly one bucket.
  bucket     text not null,
  member     text not null,
  primary key (tenant_id, index_name, bucket, member)
);

create index if not exists figure_index_member_idx
  on figure_index (tenant_id, index_name, member);

create table if not exists figure_value (
  tenant_id     text not null,
  name          text not null,
  version       text not null,
  subject_id    text not null,
  -- A number, a word, a list, or null. Null is a real answer meaning "we
  -- cannot tell", and it is not nought.
  value         jsonb,
  members       jsonb not null default '[]'::jsonb,
  subject_label text not null default '',
  computed_at   timestamptz not null default now(),
  primary key (tenant_id, name, version, subject_id)
);
"""
"""The DDL, as one script.

Kept beside the store whose SQL it must agree with, so a column rename is one
diff. `if not exists` throughout, because the host applies this from its own
migrations and a re-run must be a no-op rather than a crash."""

