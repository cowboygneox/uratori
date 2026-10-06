"""The provider protocol: one call, blind, per page.

Closed to exactly what D6's blind reading allows in: the page's own word
layer and (optionally) its rendered image, and which fields to read -- by
name, type, unit and the `#` prose the model is told, never the extract's
values or alternatives. Selected at the server boundary
(`URATORI_AUDIT_PROVIDER`); nothing in `uratori.audit` or `uratori.server`
calls a provider directly except the worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..server.words import Word
from .judge import FieldReading


@dataclass(frozen=True)
class FieldToRead:
    """What the model is told about one verified field -- D6's own list:
    name, type, unit, the `#` prose. Never the extract's alternatives or
    its current value."""

    extract: str
    field: str
    type: str | None
    units: tuple[str, ...]
    prose: str


class AuditProviderError(Exception):
    """A provider call failed in a way the worker records against the one
    page it happened on, rather than an unhandled exception that would
    abort every other page's turn in the sweep (review finding D/F4). A
    provider's own `read_page` should raise this (wrapping whatever its
    SDK or transport raised) instead of letting that raw exception
    propagate; `uratori.server.audit_worker.run_worker_sweep` catches any
    exception per page regardless, but a provider that raises this gives
    the worker a reason worth recording, not just a bare traceback."""


@dataclass(frozen=True)
class AuditProviderReading:
    """What a provider call produced: the raw response (stored verbatim,
    D6: "a reviewer asking what the model actually saw and said"), and the
    per-field answers parsed out of it."""

    response: str
    fields: tuple[FieldReading, ...]


class AuditProvider(Protocol):
    async def read_page(
        self,
        *,
        prompt: str,
        model: str,
        page_png: bytes | None,
        words: Sequence[Word],
        fields: Sequence[FieldToRead],
        page_key: str,
    ) -> AuditProviderReading: ...
