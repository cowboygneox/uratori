"""The in-memory stores: complete implementations, not test doubles.

These hold the same data the Postgres pair holds, in dictionaries. They are
shipped rather than tucked into a test directory because they are the honest
smallest deployment -- a host that recomputes from facts on boot, a notebook, a
test suite -- and because a second full implementation is what keeps the
`EngineStore` protocol honest: a method only Postgres can express is a method
the protocol should not have.

The parity suite in this package's tests runs the same scenarios over this pair
and the Postgres pair, so the two cannot quietly diverge in ordering, range
semantics or change reporting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..engine.buckets import SEPARATOR
from ..lang.plan import Value
from .base import BucketChange, FactRow, Pointer, StoredValue


class MemoryFactStore:
    """Facts in a dictionary, written by whoever owns them.

    `put` and `drop` are the whole write surface, and the *caller* keeps track
    of what moved: change detection belongs to real persistence (see
    `PostgresFactStore.upsert`), where the old value is only knowable by the
    store. Here the caller just wrote the old value, so reporting it back would
    be an answer to a question the caller can already answer.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], Mapping[str, Any]] = {}

    def put(self, tenant: str, kind: str, key: str, value: Mapping[str, Any]) -> None:
        self._rows[(tenant, kind, key)] = value

    def drop(self, tenant: str, kind: str, key: str) -> None:
        self._rows.pop((tenant, kind, key), None)

    async def of_kind(self, tenant: str, kind: str) -> list[FactRow]:
        return [
            FactRow(kind=k, key=key, value=value)
            for (t, k, key), value in sorted(self._rows.items())
            if t == tenant and k == kind
        ]

    async def some(self, tenant: str, kind: str, keys: Sequence[str]) -> list[FactRow]:
        out: list[FactRow] = []
        for key in keys:
            value = self._rows.get((tenant, kind, key))
            if value is not None:
                out.append(FactRow(kind=kind, key=key, value=value))
        return out


class MemoryEngineStore:
    """The engine's persistence over dictionaries. Mirrors the Postgres store
    method for method; the parity suite is what enforces "mirrors"."""

    def __init__(self) -> None:
        self._definitions: dict[str, tuple[str, str, str]] = {}
        self._pointers: dict[tuple[str, str], Pointer] = {}
        self._index: dict[tuple[str, str], dict[str, set[str]]] = {}
        self._index_versions: dict[tuple[str, str], str] = {}
        self._index_sets: dict[str, str] = {}  # pre-0.7 whole-set stamps; legacy only
        self._values: dict[tuple[str, str, str, str], StoredValue] = {}

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
        self._definitions[version] = (name, doc, display)

    async def pointer(self, tenant: str, name: str) -> Pointer | None:
        return self._pointers.get((tenant, name))

    async def pointers(self, tenant: str) -> dict[str, Pointer]:
        return {n: p for (t, n), p in self._pointers.items() if t == tenant}

    async def set_pointer(self, tenant: str, name: str, pointer: Pointer) -> bool:
        held = self._pointers.get((tenant, name))
        if held == pointer:
            return False
        self._pointers[(tenant, name)] = pointer
        return True

    async def index_versions(self, tenant: str) -> dict[str, str]:
        return {
            index: version
            for (held, index), version in self._index_versions.items()
            if held == tenant
        }

    async def set_index_version(self, tenant: str, index: str, version: str) -> None:
        self._index_versions[(tenant, index)] = version

    async def legacy_index_set(self, tenant: str) -> str | None:
        # `_index_sets` survives as the legacy holder so the migration seed
        # can be modelled in memory; nothing writes it any more.
        return self._index_sets.get(tenant)

    async def drop_legacy_index_set(self, tenant: str) -> None:
        self._index_sets.pop(tenant, None)

    # ------------------------------------------------------------- indexes --

    def _buckets(self, tenant: str, index: str) -> dict[str, set[str]]:
        return self._index.setdefault((tenant, index), {})

    async def set_buckets(
        self, tenant: str, index: str, member: str, buckets: Sequence[str]
    ) -> BucketChange:
        table = self._buckets(tenant, index)
        held = {b for b, members in table.items() if member in members}
        want = set(buckets)
        for bucket in held - want:
            table[bucket].discard(member)
        for bucket in want - held:
            table.setdefault(bucket, set()).add(member)
        return BucketChange(
            index=index,
            member=member,
            added=tuple(sorted(want - held)),
            removed=tuple(sorted(held - want)),
        )

    async def set_buckets_many(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> list[BucketChange]:
        out: list[BucketChange] = []
        for member, buckets in wanted.items():
            change = await self.set_buckets(tenant, index, member, buckets)
            if change.added or change.removed:
                out.append(change)
        return out

    async def buckets_holding(self, tenant: str, index: str, member: str) -> list[str]:
        return sorted(
            b for b, members in self._buckets(tenant, index).items() if member in members
        )

    async def members(self, tenant: str, index: str, bucket: str) -> frozenset[str]:
        return frozenset(self._buckets(tenant, index).get(bucket, set()))

    async def all_buckets(self, tenant: str, index: str) -> dict[str, frozenset[str]]:
        return {b: frozenset(m) for b, m in self._buckets(tenant, index).items() if m}

    async def bucket_keys(self, tenant: str, index: str) -> list[str]:
        return sorted(b for b, m in self._buckets(tenant, index).items() if m)

    async def index_has_rows(self, tenant: str, index: str) -> bool:
        return any(self._buckets(tenant, index).values())

    async def drop_index(self, tenant: str, index: str) -> None:
        self._index.pop((tenant, index), None)
        # The stamp goes with the rows: a version without buckets would read
        # as built-and-empty, which is a lie about a grouping that is gone.
        self._index_versions.pop((tenant, index), None)

    async def remove_member(self, tenant: str, index: str, member: str) -> None:
        for members in self._buckets(tenant, index).values():
            members.discard(member)

    async def replace_index(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> None:
        table: dict[str, set[str]] = {}
        for member, buckets in wanted.items():
            for bucket in buckets:
                table.setdefault(bucket, set()).add(member)
        self._index[(tenant, index)] = table

    # -------------------------------------------------------------- values --

    async def value(
        self, tenant: str, name: str, version: str, subject: str
    ) -> StoredValue | None:
        return self._values.get((tenant, name, version, subject))

    async def values(self, tenant: str, name: str, version: str) -> list[StoredValue]:
        return [
            v
            for (t, n, ver, _), v in sorted(self._values.items())
            if t == tenant and n == name and ver == version
        ]

    async def values_under(
        self, tenant: str, name: str, version: str, prefix: str
    ) -> list[StoredValue]:
        return [
            v
            for v in await self.values(tenant, name, version)
            if v.subject.startswith(prefix)
        ]

    async def values_in_range(
        self, tenant: str, name: str, version: str, frm: str, to: str
    ) -> list[StoredValue]:
        out: list[StoredValue] = []
        for stored in await self.values(tenant, name, version):
            _, sep, day = stored.subject.partition(SEPARATOR)
            if sep and frm <= day <= to:
                out.append(stored)
        return out

    async def subjects(self, tenant: str, name: str, version: str) -> list[str]:
        return [v.subject for v in await self.values(tenant, name, version)]

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
        self._values[(tenant, name, version, subject)] = StoredValue(
            subject=subject, value=value, members=tuple(members), label=label
        )

    async def remove(self, tenant: str, name: str, version: str, subject: str) -> None:
        self._values.pop((tenant, name, version, subject), None)
