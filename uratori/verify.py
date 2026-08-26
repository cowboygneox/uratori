"""Verification at the fact boundary: a record matches the world, or it lands
nowhere.

The engine never fetches or writes facts, so this is the one gate their
arrival can be checked at. Against a fact-taught world every written record is
held to the declaration -- an undeclared field, a wrong type or a wrong shape
refuses the **whole batch**, by kind, key and field. Not per-record quarantine:
silently dropping a record narrows a population by a cheap path (rule 4 at the
write boundary), and the host authored the mapping, so the fix belongs there.

Against a schema-taught world there are no fields to check, and only the kind
is verified -- a write against a kind nobody declared has never been readable
by any definition, so it is a typo worth stopping at the door rather than a
row stored for nothing.

An **absent** field is never an error, including as an explicit null: absence
means "nobody said", and the schema's claim is known/unknown, not
required/optional. What must not happen is a *present* value the declaration
cannot account for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .engine.buckets import parse_instant
from .lang.plan import CompiledFactField, Library


class FactError(Exception):
    """A batch that does not match the declared world."""


def verify_writes(
    library: Library,
    kinds: frozenset[str],
    writes: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    deletes: Mapping[str, Sequence[str]] | None = None,
) -> None:
    for kind in sorted({*(writes or {}), *(deletes or {})}):
        if kind not in kinds:
            raise FactError(
                f'"{kind}" is not a fact kind. Those are: '
                f'{", ".join(sorted(kinds)) or "none"}.'
            )
    if not library.facts:
        return
    for kind, records in (writes or {}).items():
        fields = library.facts[kind].fields
        for key, record in records.items():
            _record(kind, key, fields, record, at="")


def _record(
    kind: str,
    key: str,
    fields: tuple[CompiledFactField, ...],
    value: Any,
    at: str,
) -> None:
    if not isinstance(value, Mapping):
        raise FactError(
            f'fact {kind}, record "{key}": {at or "the record"} is not an object.'
        )
    declared = {f.name: f for f in fields}
    for name, held in value.items():
        if held is None:
            continue
        path = f"{at}{name}"
        f = declared.get(name)
        if f is None:
            raise FactError(
                f'fact {kind}, record "{key}": "{path}" is not a declared field. '
                f'Declared {"under " + at.rstrip(".") if at else "on " + kind}: '
                f'{", ".join(sorted(declared))}.'
            )
        if f.type is None:
            if f.many:
                if not isinstance(held, (list, tuple)):
                    raise FactError(
                        f'fact {kind}, record "{key}": "{path}" is declared `many`, '
                        "so it holds a list of records, and this is not a list."
                    )
                for element in held:
                    _record(kind, key, f.children, element, at=f"{path}.")
            else:
                if isinstance(held, (list, tuple)):
                    raise FactError(
                        f'fact {kind}, record "{key}": "{path}" is declared `one` '
                        "record, and this is a list."
                    )
                _record(kind, key, f.children, held, at=f"{path}.")
            continue
        _scalar(kind, key, path, f.type, held)


def _scalar(kind: str, key: str, path: str, wanted: str, held: Any) -> None:
    prefix = f'fact {kind}, record "{key}": "{path}"'
    if isinstance(held, (Mapping, list, tuple)):
        raise FactError(f"{prefix} is declared as {wanted} and arrived as a structure.")
    if wanted == "text":
        if not isinstance(held, str):
            raise FactError(f"{prefix} is declared as text and {held!r} is not text.")
    elif wanted == "number":
        # A bool IS an int in Python; without the explicit exclusion `true`
        # files as 1 and a mistyped flag becomes a plausible quantity.
        if isinstance(held, bool) or not isinstance(held, (int, float)):
            raise FactError(f"{prefix} is declared as number and {held!r} is not a number.")
    elif wanted == "flag":
        if not isinstance(held, bool):
            raise FactError(f"{prefix} is declared as flag and {held!r} is not true or false.")
    elif wanted == "moment":
        if not isinstance(held, str) or parse_instant(held) is None:
            raise FactError(
                f"{prefix} is declared as moment and {held!r} is not an ISO instant."
            )
    else:  # pragma: no cover - the type vocabulary is closed at parse time
        raise FactError(f"{prefix} has an unknown declared type {wanted}.")


__all__ = ["FactError", "verify_writes"]
