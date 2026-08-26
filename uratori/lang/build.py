"""Compiling a directory of definitions into a committable artifact.

The host commits the **plan**, not generated code: a definition that does not
compile fails the build rather than the process serving it, a client can read a
definition without asking the server for one, and a change to a `.fig` file
shows up in a diff as a moved version -- which is the part of a definition
change worth reviewing, because the version is what decides whether stored
values are reused.

The command-line entry point lives with the host, not here: a build step needs
the host's `Schema`, its definitions directory and its own idea of what a stale
artifact should tell the person who sees the message. What every host's ten
lines of `main()` share is below.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from .plan import Library


def read_definitions(directory: Path) -> str:
    """Every `.fig` file, in name order, concatenated.

    Order matters and alphabetical is the rule: a figure may only read one
    declared before it, so which file sorts first decides what can be built on
    what. That is a real constraint on how definitions are named and it is
    better stated than discovered -- one project depended on `connection.fig`
    sorting before `person.fig` and nothing said so until somebody nearly
    renamed one.
    """
    parts: list[str] = []
    for path in sorted(directory.glob("*.fig")):
        parts.append(f"# ---- {path.name} " + "-" * max(0, 60 - len(path.name)))
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def as_json(library: Library) -> dict[str, Any]:
    return {
        "facts": {k: _plain(v) for k, v in library.facts.items()},
        "indexes": {k: _plain(v) for k, v in library.indexes.items()},
        "measures": {k: _plain(v) for k, v in library.measures.items()},
        "figures": [_plain(p) for p in library.figures],
        "readings": [_plain(p) for p in library.readings],
        "projections": [_plain(p) for p in library.projections],
        "summaries": [_plain(p) for p in library.summaries],
        "source": library.source,
    }


def render(library: Library) -> str:
    """The artifact's exact text, so "stale" is a string comparison.

    Compared as strings rather than shelling out to git, because a build image
    may have no git -- and a missing binary once made this kind of check fail
    *open*, reporting success for a stale artifact.
    """
    return json.dumps(as_json(library), indent=2, sort_keys=True) + "\n"


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items() if v is not None}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
