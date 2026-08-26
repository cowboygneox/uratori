"""What the engine needs from storage, and nothing more.

Two protocols, and the narrowness of each is the point.

`FactSource` is the world as the engine may see it: every record of a kind, or
some records by key. Nothing else -- no filters, no projections, no ordering --
because **a cheap path may never narrow a population**, and every method a
store grows is a way the calculation could start depending on where the records
live. The engine never writes facts; how they arrive is the host's business.

`EngineStore` is what the engine keeps: definitions, per-tenant pointers, index
membership and computed values. Two things about its keys are load-bearing:

**A value is keyed by `(tenant, name, version, subject)`.** The version being
*in the key* is what makes a definition change a cache miss rather than an
invalidation: the new version has no rows, so recomputing is a fresh write, and
the old version's rows stay intact to explain any history that cites them.

**A definition row is not tenant-scoped.** Two tenants running the same
definition are running the same definition, and the hash says so. What is
per-tenant is the *pointer* -- which version this tenant has actually computed --
plus a fingerprint of the settings that definition names.

Both are `Protocol`s rather than base classes so that an implementation is
anything with the right shape: the shipped Postgres and in-memory stores, or a
host's own. The engine is exercised over the in-memory pair in this package's
own tests, which is not a testing convenience -- it is what keeps the
calculation independent of any database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..lang.plan import Value


@dataclass(frozen=True)
class FactRow:
    kind: str
    key: str
    value: Mapping[str, Any]


class FactSource(Protocol):
    async def of_kind(self, tenant: str, kind: str) -> list[FactRow]: ...

    async def some(self, tenant: str, kind: str, keys: Sequence[str]) -> list[FactRow]: ...


@dataclass(frozen=True)
class Pointer:
    version: str
    settings_fingerprint: str


@dataclass(frozen=True)
class StoredValue:
    subject: str
    value: Value
    members: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class BucketChange:
    """What moving a record did to one index. The diff *is* the invalidation
    signal, which is why `set_buckets` returns it rather than writing silently."""

    index: str
    member: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


class EngineStore(Protocol):
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
    ) -> None: ...

    async def pointer(self, tenant: str, name: str) -> Pointer | None: ...

    async def pointers(self, tenant: str) -> dict[str, Pointer]: ...

    async def set_pointer(self, tenant: str, name: str, pointer: Pointer) -> bool:
        """Returns whether it actually moved, which is the release event."""
        ...

    async def index_stamps(self, tenant: str) -> dict[str, Pointer]:
        """Per grouping, what its stored buckets were built under: the spec
        version, and a fingerprint of the dials the spec reads.

        The same two-part discipline a figure's pointer carries, for the same
        reason: a spec hash deliberately excludes settings, so an age filter
        or a zoned calendar grouping re-bucketed only when its hash moved
        would describe the old dial for ever. Its own rows rather than
        `figure_pointer` ones because these exist for every index --
        including the ones no figure reads (a projection's `from`), whose
        arrival or redefinition moves no pointer at all. Per index rather
        than one hash over the whole set, because staleness is the unit of
        *work*: one stamp meant one new filter re-bucketed every grouping
        over every record, and a million-fact tenant paid for the world to
        answer a one-line edit.
        """
        ...

    async def set_index_stamp(self, tenant: str, index: str, stamp: Pointer) -> None:
        """Recorded after that index's rebuild actually ran -- a pass that
        dies mid-way must leave exactly the unbuilt groupings stale."""
        ...

    async def drop_index(self, tenant: str, index: str) -> None:
        """A retired grouping's rows and its stamp, gone together. Nothing
        erased a dead grouping's rows before staleness went per-index (they
        simply accumulated); narrow rebuilds retire them deliberately, so a
        dropped definition's rows cannot outlive it."""
        ...

    async def legacy_index_set(self, tenant: str) -> str | None:
        """The pre-0.7 whole-set stamp, if one still stands.

        Read once, at the first pass after an upgrade: a stamp matching the
        current library proves every grouping current (seeded into per-index
        versions), a mismatched one proves nothing (everything rebuilds).
        Either way `drop_legacy_index_set` retires it. A store with no
        legacy state answers None for ever.
        """
        ...

    async def drop_legacy_index_set(self, tenant: str) -> None: ...

    # ------------------------------------------------------------- indexes --

    async def set_buckets(
        self, tenant: str, index: str, member: str, buckets: Sequence[str]
    ) -> BucketChange: ...

    async def set_buckets_many(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> list[BucketChange]: ...

    async def buckets_holding(self, tenant: str, index: str, member: str) -> list[str]: ...

    async def members(self, tenant: str, index: str, bucket: str) -> frozenset[str]: ...

    async def all_buckets(self, tenant: str, index: str) -> dict[str, frozenset[str]]: ...

    async def bucket_keys(self, tenant: str, index: str) -> list[str]: ...

    async def index_has_rows(self, tenant: str, index: str) -> bool: ...

    async def remove_member(self, tenant: str, index: str, member: str) -> None: ...

    async def replace_index(
        self, tenant: str, index: str, wanted: Mapping[str, Sequence[str]]
    ) -> None:
        """Prune and rebuild atomically.

        A rebuild that deleted first and then failed half way would leave an
        index empty, and an empty index is a figure reading zero for everybody
        rather than an error anybody sees.
        """
        ...

    # -------------------------------------------------------------- values --

    async def value(
        self, tenant: str, name: str, version: str, subject: str
    ) -> StoredValue | None: ...

    async def values(self, tenant: str, name: str, version: str) -> list[StoredValue]: ...

    async def values_under(
        self, tenant: str, name: str, version: str, prefix: str
    ) -> list[StoredValue]: ...

    async def values_in_range(
        self, tenant: str, name: str, version: str, frm: str, to: str
    ) -> list[StoredValue]:
        """Time-keyed values whose bucket label falls inside a range.

        The comparison is on the text after the separator, which works because
        every label is fixed-width local ISO time -- `2026-08-23`, or
        `2026-08-23T14:30` at a sub-day grain -- and those sort
        lexicographically. The caller supplies bounds in the stored grain's own
        shape: bare days for a day figure, `T00:00`/`T23:59` suffixes for a
        sub-day one, since every label on a day sorts after the bare day
        string.
        """
        ...

    async def subjects(self, tenant: str, name: str, version: str) -> list[str]: ...

    async def save(
        self,
        tenant: str,
        name: str,
        version: str,
        subject: str,
        value: Value,
        members: Iterable[str],
        label: str,
    ) -> None: ...

    async def remove(self, tenant: str, name: str, version: str, subject: str) -> None: ...
