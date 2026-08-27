"""The two shipped store pairs must be one behaviour.

Every scenario here runs over the in-memory store and the Postgres store and
asserts identical answers. This is what keeps the protocol honest: a method
only one implementation can express, an ordering only one guarantees, or a
range boundary only one honours would surface as the two disagreeing -- and
the engine's tests, which run over the in-memory pair, would otherwise be
holding properties production never has.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from uratori.store import EngineStore, MemoryEngineStore, Pointer
from uratori.store.postgres import PostgresEngineStore, PostgresFactStore


@pytest.fixture(params=["memory", "postgres"])
def store(
    request: pytest.FixtureRequest, pg_pool: asyncpg.Pool[Any]
) -> tuple[EngineStore, str]:
    """Each scenario runs twice, once per implementation, on a fresh tenant.

    The in-memory half asks for the database too, deliberately: this file *is*
    the comparison, so a run that could green the memory half while the
    Postgres half silently skipped would be the parity claim without the
    parity."""
    tenant = str(uuid.uuid4())
    if request.param == "memory":
        return MemoryEngineStore(), tenant
    return PostgresEngineStore(pg_pool), tenant


async def test_pointers_report_movement_and_only_movement(
    store: tuple[EngineStore, str],
) -> None:
    s, tenant = store
    # The definition first, as the engine's cold pass always writes it: a
    # pointer names a version, and the Postgres store enforces that the version
    # exists to be named.
    await s.ensure_definition("v1", "f", "figure", "doc", "display", "source", {})
    first = Pointer(version="v1", settings_fingerprint="fp1")
    assert await s.set_pointer(tenant, "f", first) is True
    assert await s.set_pointer(tenant, "f", first) is False, (
        "re-setting an identical pointer must not read as a release event"
    )
    assert await s.set_pointer(tenant, "f", Pointer("v1", "fp2")) is True
    assert (await s.pointers(tenant)) == {"f": Pointer("v1", "fp2")}


async def test_index_stamps_round_trip_per_grouping_per_tenant(
    store: tuple[EngineStore, str],
) -> None:
    """The membership pointers that are not a figure's: what each grouping's
    stored buckets were built under -- spec version AND dial fingerprint,
    because a spec hash excludes settings and an age filter re-bucketed only
    on hash moves would describe the old dial for ever. Per grouping,
    because staleness is the unit of work; per tenant, because one tenant's
    rebuild proves nothing about another's."""
    s, tenant = store
    assert await s.index_stamps(tenant) == {}, (
        "a tenant no pass has bucketed holds no stamps -- an invented row "
        "here would read as built"
    )
    one = Pointer(version="v1", settings_fingerprint="")
    dialled = Pointer(version="v1", settings_fingerprint="fp1")
    await s.set_index_stamp(tenant, "k.open", one)
    await s.set_index_stamp(tenant, "k.fresh", dialled)
    assert await s.index_stamps(tenant) == {"k.open": one, "k.fresh": dialled}
    # Re-setting the identical stamp is a no-op, a moved fingerprint is not:
    # the dial half must survive the round trip or dial staleness is blind.
    await s.set_index_stamp(tenant, "k.fresh", dialled)
    turned = Pointer(version="v1", settings_fingerprint="fp2")
    await s.set_index_stamp(tenant, "k.fresh", turned)
    assert (await s.index_stamps(tenant))["k.fresh"] == turned
    await s.set_index_stamp(tenant, "k.open", Pointer(version="v2", settings_fingerprint=""))
    assert (await s.index_stamps(tenant))["k.open"].version == "v2"
    assert await s.index_stamps("some-other-tenant") == {}


async def test_drop_index_retires_the_rows_and_the_stamp_together(
    store: tuple[EngineStore, str],
) -> None:
    """Rows without a stamp read as never-built, which is honest; a stamp
    without rows would read as built-and-empty, which is a lie about a
    grouping that is gone. So the two leave together."""
    s, tenant = store
    stamp = Pointer(version="v1", settings_fingerprint="")
    await s.set_buckets(tenant, "k.open", "m1", [""])
    await s.set_index_stamp(tenant, "k.open", stamp)
    await s.set_buckets(tenant, "k.other", "m1", [""])
    await s.set_index_stamp(tenant, "k.other", stamp)

    await s.drop_index(tenant, "k.open")
    assert await s.members(tenant, "k.open", "") == frozenset()
    assert await s.index_stamps(tenant) == {"k.other": stamp}
    assert await s.members(tenant, "k.other", "") == frozenset({"m1"}), (
        "retiring one grouping must not touch its neighbour"
    )


async def test_the_legacy_stamp_reads_once_and_retires(
    store: tuple[EngineStore, str],
) -> None:
    """The pre-0.7 whole-set stamp: the memory store models it for the
    engine's seed tests, the Postgres store reads whatever an older release
    left in `index_state`. Neither may resurrect it once dropped."""
    s, tenant = store
    assert await s.legacy_index_set(tenant) is None
    if isinstance(s, MemoryEngineStore):
        s._index_sets[tenant] = "setv1"
    else:
        await s._pool.execute(  # type: ignore[attr-defined]
            "insert into index_state (tenant_id, version) values ($1, $2)",
            tenant,
            "setv1",
        )
    assert await s.legacy_index_set(tenant) == "setv1"
    await s.drop_legacy_index_set(tenant)
    assert await s.legacy_index_set(tenant) is None


async def test_bucket_diffs_are_the_invalidation_signal(
    store: tuple[EngineStore, str],
) -> None:
    s, tenant = store
    change = await s.set_buckets(tenant, "idx", "m1", ["a", "b"])
    assert (change.added, change.removed) == (("a", "b"), ())

    change = await s.set_buckets(tenant, "idx", "m1", ["b", "c"])
    assert (change.added, change.removed) == (("c",), ("a",))

    assert await s.buckets_holding(tenant, "idx", "m1") == ["b", "c"]
    assert await s.members(tenant, "idx", "b") == frozenset({"m1"})
    assert await s.bucket_keys(tenant, "idx") == ["b", "c"]
    assert await s.index_has_rows(tenant, "idx") is True

    batched = await s.set_buckets_many(tenant, "idx", {"m1": ["b", "c"], "m2": ["c"]})
    # The unchanged member reports nothing; reporting it would recompute a
    # subject nobody moved on every webhook.
    assert [(c.member, c.added, c.removed) for c in batched] == [("m2", ("c",), ())]

    await s.remove_member(tenant, "idx", "m1")
    assert await s.buckets_holding(tenant, "idx", "m1") == []

    await s.drop_index(tenant, "idx")
    assert await s.index_has_rows(tenant, "idx") is False


async def test_replace_index_is_wholesale(store: tuple[EngineStore, str]) -> None:
    s, tenant = store
    await s.set_buckets(tenant, "idx", "old", ["stale"])
    await s.replace_index(tenant, "idx", {"m1": ["a"], "m2": ["a", "b"]})
    assert await s.all_buckets(tenant, "idx") == {
        "a": frozenset({"m1", "m2"}),
        "b": frozenset({"m2"}),
    }
    assert await s.buckets_holding(tenant, "idx", "old") == [], (
        "a rebuild that keeps departed members is a figure counting ghosts"
    )


async def test_values_round_trip_with_every_stored_shape(
    store: tuple[EngineStore, str],
) -> None:
    """A number, a word, a list and a null are all real stored answers, and a
    store that coerced any of them (a null to a nought, a list to text) would
    be the one calculation-system rule failing at the persistence layer."""
    s, tenant = store
    cases: list[tuple[str, Any]] = [
        ("num", 3.5),
        ("word", "over"),
        ("list", [1.0, None, 2.5]),
        ("nothing", None),
    ]
    for subject, value in cases:
        # Deliberately NOT in lexical order: members are a positional citation
        # (values[i] measures members[i]), so a store that sorted or otherwise
        # reordered them would pair the right numbers with the wrong records.
        await s.save(tenant, "fig", "v1", subject, value, ["e2", "e1"], f"label-{subject}")
    for subject, value in cases:
        held = await s.value(tenant, "fig", "v1", subject)
        assert held is not None
        assert held.value == value
        assert held.members == ("e2", "e1")
        assert held.label == f"label-{subject}"

    assert [v.subject for v in await s.values(tenant, "fig", "v1")] == [
        "list",
        "nothing",
        "num",
        "word",
    ], "values read back in subject order, because every consumer sorts by it"

    assert await s.value(tenant, "fig", "v2", "num") is None, (
        "the version is in the key: a moved definition is a cache miss, not a hit"
    )

    await s.remove(tenant, "fig", "v1", "num")
    assert await s.value(tenant, "fig", "v1", "num") is None
    assert sorted(await s.subjects(tenant, "fig", "v1")) == ["list", "nothing", "word"]


async def test_day_ranges_are_inclusive_at_both_ends(
    store: tuple[EngineStore, str],
) -> None:
    s, tenant = store
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        await s.save(tenant, "fig", "v1", f"p1@{day}", 1.0, [], "Aki")
    await s.save(tenant, "fig", "v1", "p1", 9.0, [], "Aki")  # no day part: never in a range

    inside = await s.values_in_range(tenant, "fig", "v1", "2026-01-02", "2026-01-03")
    assert [v.subject for v in inside] == ["p1@2026-01-02", "p1@2026-01-03"], (
        "a window off by one day at either end is a mean over a different month "
        "than the heading claims"
    )

    under = await s.values_under(tenant, "fig", "v1", "p1@")
    assert len(under) == 4


async def test_sub_day_labels_scan_between_sub_day_bounds(
    store: tuple[EngineStore, str],
) -> None:
    """Every label on a window's final day sorts *after* the bare day string,
    so the serve path passes `T00:00`/`T23:59` bounds for a sub-day figure.
    Postgres compares under the database's own collation and the memory store
    under Python byte order -- this is the one place the two could disagree
    about a `T`-suffixed bound, so both edges get a value and a control just
    outside each proves the scan is bounded."""
    s, tenant = store
    for label in (
        "2026-08-17T23:45",  # before the range -- must not leak in
        "2026-08-18T00:00",  # the first label of the first day
        "2026-08-20T10:15",  # mid-range
        "2026-08-24T23:45",  # the last quarter of the final day
        "2026-08-25T00:00",  # after the range -- must not leak in
    ):
        await s.save(tenant, "fig", "v1", f"p1@{label}", 1.0, [], "Aki")

    inside = await s.values_in_range(tenant, "fig", "v1", "2026-08-18T00:00", "2026-08-24T23:59")
    assert [v.subject for v in inside] == [
        "p1@2026-08-18T00:00",
        "p1@2026-08-20T10:15",
        "p1@2026-08-24T23:45",
    ]


async def test_postgres_fact_upsert_reports_only_what_moved(pg_pool: Any) -> None:
    """The service's change detection: what this returns is what the engine
    recomputes, so an unchanged record reported as moved turns every push into
    a full recompute, and a changed one unreported is a stale figure for ever."""
    tenant = str(uuid.uuid4())
    facts = PostgresFactStore(pg_pool)

    moved = await facts.upsert(tenant, "shop_order", {"o1": {"status": "riding"}})
    assert moved == ["o1"]

    moved = await facts.upsert(tenant, "shop_order", {"o1": {"status": "riding"}})
    assert moved == [], "an identical rewrite is not a change"

    moved = await facts.upsert(tenant, "shop_order", {"o1": {"status": "delivered"}})
    assert moved == ["o1"]

    rows = await facts.of_kind(tenant, "shop_order")
    assert [(r.key, r.value) for r in rows] == [("o1", {"status": "delivered"})]

    some = await facts.some(tenant, "shop_order", ["o1", "missing"])
    assert [r.key for r in some] == ["o1"]

    await facts.delete(tenant, "shop_order", ["o1"])
    assert await facts.of_kind(tenant, "shop_order") == []


async def test_the_server_refuses_a_database_it_did_not_build(pg_dsn: str) -> None:
    """Pointing the service at another product's database must fail at boot,
    not corrupt at runtime -- the origin project has a `figure_definition`
    table of its own with different column types."""
    import asyncpg

    from uratori.server.db import (
        DatabaseBelongsToSomethingElse,
        ensure_schema,
        open_server_pool,
    )

    schema = "uratori_refusal_test"
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(f"drop schema if exists {schema} cascade")
        await connection.execute(f"create schema {schema}")
        await connection.execute(
            f"create table {schema}.figure_definition (version text primary key)"
        )
    finally:
        await connection.close()

    pool = await open_server_pool(pg_dsn, pg_schema=schema, timeout=10.0)
    try:
        with pytest.raises(DatabaseBelongsToSomethingElse):
            await ensure_schema(pool)
    finally:
        await pool.close()


async def test_a_stale_write_loses_to_a_newer_stored_record(pg_pool: Any) -> None:
    """The webhook-vs-reconcile race, at the service's door.

    A full reconcile reads the provider for a minute and applies what it read,
    so a push built from that snapshot can arrive carrying a record from
    *before* an event a webhook already delivered -- or carry a version the
    provider's own lagging search index served up stale. Last-write-wins puts
    the pre-event state back and the board carries the wrong figure until the
    next reconcile "discovers" the change all over again. The stamp compared
    is the provider's own, never a clock of ours: the question is which
    version of the record this is, and only the provider can answer it.

    `>=` rather than `>`, so a rewrite at the same version still lands -- that
    is how a parser change reaches records nobody has touched. Either side
    missing a stamp means there is nothing to compare, so the write goes
    through: a guard that cannot see the versions must never be the thing
    that drops data.
    """
    tenant = str(uuid.uuid4())
    facts = PostgresFactStore(pg_pool)

    moved = await facts.upsert(
        tenant,
        "shop_order",
        {"o1": {"status": "delivered"}},
        stamps={"o1": "2026-08-24T12:00:00Z"},
    )
    assert moved == ["o1"]

    # The reconcile's stale snapshot arrives late: refused, and not reported
    # as moved -- a refusal that still said "changed" would recompute figures
    # from a value that was never written.
    moved = await facts.upsert(
        tenant,
        "shop_order",
        {"o1": {"status": "riding"}},
        stamps={"o1": "2026-08-24T11:00:00Z"},
    )
    assert moved == []
    rows = await facts.of_kind(tenant, "shop_order")
    assert rows[0].value == {"status": "delivered"}, "the pre-event record came back"

    # Same stamp, different body: a parser change rewrites in place.
    moved = await facts.upsert(
        tenant,
        "shop_order",
        {"o1": {"status": "delivered", "ref": "A-1"}},
        stamps={"o1": "2026-08-24T12:00:00Z"},
    )
    assert moved == ["o1"]

    # No stamp on the write: nothing to compare, so it lands.
    moved = await facts.upsert(tenant, "shop_order", {"o1": {"status": "returned"}})
    assert moved == ["o1"]


async def test_values_citing_finds_exact_members_and_only_them(
    store: tuple[EngineStore, str],
) -> None:
    """The reverse citation: which stored values counted this record. The
    match is exact membership -- a key that is a prefix or substring of
    another record's key must not match, because the record page lists what
    this claims and a loose match would print citations that cite somebody
    else's record."""
    s, tenant = store
    await s.save(tenant, "fig", "v1", "c1", 2.0, ["o1", "o12"], "Aki")
    await s.save(tenant, "fig", "v1", "c2", 1.0, ["o12"], "Ren")
    await s.save(tenant, "fig", "v1", "c3", 0.0, [], "Mo")
    await s.save(tenant, "other", "v9", "c8", 4.0, ["o1"], "Kim")
    # Another era's citation is not this era's: the version is in the key.
    await s.save(tenant, "fig", "v2", "c9", 1.0, ["o1"], "old")

    wanted = {"fig": "v1", "other": "v9", "quiet": "v1"}
    found = await s.values_citing(tenant, "o1", wanted, limit=10)
    assert set(found) == {"fig", "other"}, (
        "grouped by figure; a figure with no citing rows is absent, and the "
        "v2 row is invisible -- an old era's citation cites nothing current"
    )
    assert [v.subject for v in found["fig"]] == ["c1"], (
        "o1 is a prefix of o12; matching by containment-as-text would add c2"
    )
    both = await s.values_citing(tenant, "o12", wanted, limit=10)
    assert [v.subject for v in both["fig"]] == ["c1", "c2"], (
        "subject order, like every other values read -- consumers render in it"
    )
    assert both["fig"][0].members == ("o1", "o12"), (
        "the whole stored row comes back: the page shows the value and its label"
    )
    capped = await s.values_citing(tenant, "o12", wanted, limit=1)
    assert [v.subject for v in capped["fig"]] == ["c1"], (
        "the cap is per figure; the first rows in subject order are the page"
    )


async def test_values_under_treats_the_prefix_as_text_not_a_pattern(
    store: tuple[EngineStore, str],
) -> None:
    """Record keys carry underscores routinely -- the NFL's own game ids do --
    and `_` is a LIKE wildcard. An unescaped prefix would put another
    subject's day rows onto the first subject's record page, which is a wrong
    number on the one surface whose claim is traceability."""
    s, tenant = store
    await s.save(tenant, "fig", "v1", "a_b@2026-01-01", 1.0, [], "one")
    await s.save(tenant, "fig", "v1", "axb@2026-01-01", 2.0, [], "two")
    await s.save(tenant, "fig", "v1", "p%q@2026-01-01", 3.0, [], "three")
    await s.save(tenant, "fig", "v1", "pXq@2026-01-01", 4.0, [], "four")

    assert [v.subject for v in await s.values_under(tenant, "fig", "v1", "a_b@")] == [
        "a_b@2026-01-01"
    ]
    assert [v.subject for v in await s.values_under(tenant, "fig", "v1", "p%q@")] == [
        "p%q@2026-01-01"
    ]
