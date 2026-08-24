"""Content addressing for definitions.

A definition's version *is* the hash of its semantics, which is what makes a
change a create rather than an update: the new version has no stored values, so
recomputing is a cache miss instead of an invalidation, and the old version's
values stay intact to explain any history that references them.

Two properties this has to have, and both are tested:

  - **Prose does not change the hash.** Only the parts that decide the number
    reach it. Fixing a typo in a docstring must not fork a version and recompute
    three hundred values.
  - **Key order does not change the hash.** `canonical` sorts keys at every
    depth, so a refactor that happens to build a plan in a different order does
    not silently invalidate every tenant's board.

SHA-256 rather than something cheap: this value decides whether a stored result
is reused, so a collision is a wrong number nothing would throw on.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    """JSON with keys sorted at every depth, and `None`-valued keys dropped.

    Dropping them is what lets a new optional keyword be added without forking
    every version written before it: a field nobody set hashes as though it were
    never there. It is also why every optional part of a plan must default to
    `None` rather than to a falsy stand-in -- `False` and `0` are values, and
    they *do* change the hash.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted((k, v) for k, v in value.items() if v is not None)
        return "{" + ",".join(f"{json.dumps(k)}:{canonical(v)}" for k, v in items) + "}"
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def version_of(value: Any) -> str:
    """The first twelve hex characters of the SHA-256, which is what a version is."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:12]
