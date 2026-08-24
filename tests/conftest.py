"""Shared fixtures, and the rule about the database.

**A database test fails when there is no database. It never skips.** A suite
that skips its Postgres half and one that passes look identical from outside,
which is how the origin project once shipped months of schema changes on a run
that never opened a connection. `pg_dsn` raises; the pure tests never ask for
it and cost nothing.

Everything Postgres-backed lives in a schema of its own (`uratori_selftest`),
dropped and rebuilt once per session, so this suite can share a database with
other suites -- the origin project's tables carry some of the same names, and
`search_path` is what keeps the two from ever meeting.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest

from uratori.server.db import ensure_schema, open_server_pool

_HINT = (
    "TEST_DATABASE_URL is not set, so this test has no database to run against. "
    "It fails rather than skips: a suite that skips its database half looks "
    "exactly like one that passes."
)

PG_SCHEMA = "uratori_selftest"

_prepared = False


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise RuntimeError(_HINT)
    return dsn


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool[Any]]:
    """A pool pinned to this suite's own Postgres schema, tables applied."""
    global _prepared
    if not _prepared:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f"drop schema if exists {PG_SCHEMA} cascade")
            await connection.execute(f"create schema {PG_SCHEMA}")
        finally:
            await connection.close()
    pool = await open_server_pool(pg_dsn, pg_schema=PG_SCHEMA, timeout=10.0)
    if not _prepared:
        await ensure_schema(pool)
        _prepared = True
    try:
        yield pool
    finally:
        await pool.close()
