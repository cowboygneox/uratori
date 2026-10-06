"""Bytes beside the facts, never in them.

A document's own bytes are large, mostly opaque to a definition, and arrive
once; keeping them in Postgres would mean every backup carries every PDF and
every page render pulls a file through the database. `BlobStore` is
deliberately narrow -- put, exists, open, delete, content-addressed -- so a
later object-storage backend is the same four methods against S3 instead of
a filesystem, and the engine itself never sees this protocol: `store/base.py`
is "what the engine needs from storage, and nothing more", and the engine
never needs bytes. This lives in `uratori/server/`, not `uratori/store/`,
for exactly that reason.

**Tenant-namespaced, deliberately.** A shared `<sha[:2]>/<sha>` path would
mean deleting one tenant's document -- or the tenant itself -- could unlink
bytes another tenant references, and an orphan sweep could never tell whose
an unreferenced file was. `<tenant>/<sha[:2]>/<sha>` keeps every tenant's
bytes answerable to that tenant alone, at the cost of storing the same PDF
twice if two tenants happen to upload the identical file -- an acceptable
price for a guarantee a shared path cannot make.

**Never overwritten.** A blob is content-addressed, so two uploads producing
the same sha256 are the same bytes by definition -- `put` is a no-op the
second time, never a second write. Every write to disk goes to a temp name
in the same directory and is renamed into place, so a reader can never
observe a partially written file: `os.replace` is atomic on the same
filesystem, and a crash mid-write leaves an orphaned temp file beside the
final name, never the final name itself half full.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    """Content-addressed bytes, namespaced by tenant. Every method is async
    because the filesystem implementation does real I/O; the memory twin
    just never awaits anything underneath."""

    async def put(self, tenant: str, data: bytes) -> str:
        """Writes `data` if its sha256 is not already stored for this
        tenant, and returns that sha256 (hex, lowercase) either way. The
        caller tells a fresh write from a dedupe by comparing against what
        it already knew the hash would be -- `put` itself never raises for
        "already there"."""
        ...

    async def exists(self, tenant: str, sha256: str) -> bool: ...

    async def open(self, tenant: str, sha256: str) -> bytes | None:
        """The stored bytes, or `None` if missing. Never raises for a
        missing blob -- a document row whose file is gone on disk is a
        state callers must render as `held: false` with a reason, not a
        500, and that starts with this method answering `None` rather than
        raising."""
        ...

    async def delete(self, tenant: str, sha256: str) -> None:
        """Removes the blob if present. Deleting an already-missing blob is
        not an error -- the delete route's job is "make sure it is gone",
        and a retry of a delete that already succeeded must not fail."""
        ...


class FilesystemBlobStore:
    """`BlobStore` over local disk, under `URATORI_BLOB_DIR`.

    Every blocking filesystem call runs through `asyncio.to_thread`: this
    class is used from request handlers, and a multi-megabyte write or read
    on the event loop thread would stall every other tenant's request for
    the duration.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, tenant: str, sha256: str) -> Path:
        return self._root / tenant / sha256[:2] / sha256

    def _put_sync(self, tenant: str, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self._path(tenant, sha)
        if dest.exists():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, dest)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise
        return sha

    async def put(self, tenant: str, data: bytes) -> str:
        return await asyncio.to_thread(self._put_sync, tenant, data)

    async def exists(self, tenant: str, sha256: str) -> bool:
        return await asyncio.to_thread(self._path(tenant, sha256).exists)

    def _open_sync(self, tenant: str, sha256: str) -> bytes | None:
        try:
            return self._path(tenant, sha256).read_bytes()
        except FileNotFoundError:
            return None

    async def open(self, tenant: str, sha256: str) -> bytes | None:
        return await asyncio.to_thread(self._open_sync, tenant, sha256)

    def _delete_sync(self, tenant: str, sha256: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(tenant, sha256).unlink()

    async def delete(self, tenant: str, sha256: str) -> None:
        await asyncio.to_thread(self._delete_sync, tenant, sha256)


class MemoryBlobStore:
    """`BlobStore` in a dictionary -- the honest smallest deployment, and
    what keeps the protocol honest: a method only the filesystem could
    express would be a method the protocol should not have."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def put(self, tenant: str, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self._data.setdefault((tenant, sha), data)
        return sha

    async def exists(self, tenant: str, sha256: str) -> bool:
        return (tenant, sha256) in self._data

    async def open(self, tenant: str, sha256: str) -> bytes | None:
        return self._data.get((tenant, sha256))

    async def delete(self, tenant: str, sha256: str) -> None:
        self._data.pop((tenant, sha256), None)
