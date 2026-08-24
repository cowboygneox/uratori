"""Reading dials out of a settings document.

Which dials exist, what they default to, and which of the four positions a
definition may name each one from are all the host's declarations -- they live
on the `Schema`, not here. This module is only the mechanics of a document:
walking a dotted path, reading a band, fingerprinting the dials a definition
names, and flattening a document to leaves and back.

**Every function here takes a complete document.** The engine completes a
tenant's sparse document over the schema's defaults exactly once, at its
boundary (`Schema.settings_for`), so a missing value below that boundary is a
real absence and **raises**. A fallback would produce values, numbers and
evidence, all about the wrong thing -- and the definition already said which
dial it wanted, so there is nothing to guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def get_path(document: dict[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def setting_value(document: dict[str, Any], path: str) -> Any:
    """The dial's value, or a refusal.

    A missing value **raises** rather than falling back to something plausible.
    The document arriving here is already complete over the host's defaults, so
    an absence means the host never declared the dial at all.
    """
    found = get_path(document, path)
    if found is None:
        raise KeyError(
            f'no value for setting "{path}". A definition named it, so the engine cannot '
            "answer without it; falling back would produce a number about the wrong dial."
        )
    return found


def band_value(document: dict[str, Any], path: str) -> tuple[float, float]:
    """A band's two edges. `{good, poor}` in the settings document."""
    node = setting_value(document, path)
    if not isinstance(node, dict) or "good" not in node or "poor" not in node:
        raise KeyError(f'setting "{path}" is not a band: it needs "good" and "poor".')
    return float(node["good"]), float(node["poor"])


def seconds_per(unit: str, document: dict[str, Any]) -> float:
    """How many seconds a threshold unit is worth.

    A constant per unit, so `document` is unused today. It is still a parameter
    because the unit this signature was designed for is not here: `work_hours`
    was declared, resolved to exactly 3,600 seconds, and was therefore a synonym
    for `hours` -- two spellings of one thing, with a docstring claiming a
    working day mattered and nothing that made it. It has been removed rather
    than left as a lie, because a construct no definition uses is a construct
    nobody has checked.

    Reinstating it means real working-hours arithmetic -- a wait that spans a
    night is not the same number of working hours as one that does not -- which
    is a calendar, not a scale factor.
    """
    if unit == "minutes":
        return 60.0
    if unit == "hours":
        return 3_600.0
    if unit == "days":
        return 86_400.0
    raise ValueError(f"{unit} is not a threshold unit")


def fingerprint(document: dict[str, Any], named: list[str]) -> str:
    """A stable string over exactly the settings a definition names.

    Stored beside the pointer rather than mixed into the version, because the
    version is the hash of the *definition* and the definition is shared by every
    tenant. Salting it per tenant would fork the plan lookup for a change that
    moves no definition. A changed fingerprint makes the figure pending, which is
    the cold path that already exists.
    """
    from .hash import canonical

    # Read off the complete document, path by path. `get_path` answers None for
    # a dial the host never declared, and None-valued keys drop out of
    # `canonical` -- so an undeclared dial hashes as though it were never named,
    # while a dial set to nought, `false` or `""` is a value somebody chose and
    # moves the fingerprint. That distinction is load-bearing: an invalidation
    # that treated a chosen nought as unset would leave the board banding
    # against the old number for ever while the settings page shows the new one.
    pairs: dict[str, Any] = {}
    for path in sorted(named):
        pairs[path] = get_path(document, path)
    return canonical(pairs)


# ------------------------------------------------------ documents and leaves --


def flatten(document: dict[str, Any]) -> dict[str, Any]:
    """Every leaf of a settings document, keyed by its dotted path.

    A band (`{good, poor}`) is a leaf, not two: it is one dial with two edges,
    `band_value` reads it whole, and splitting it here would put two records on
    the page for a thing a definition names once.
    """
    out: dict[str, Any] = {}

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict) and not is_band(node):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else str(key))
            return
        out[prefix] = node

    walk(document, "")
    out.pop("", None)
    return out


def is_band(node: dict[str, Any]) -> bool:
    return set(node) == {"good", "poor"}


def document_from(paths: Mapping[str, Any]) -> dict[str, Any]:
    """The nested document a flat set of paths describes.

    The inverse of `flatten`. The evaluator takes a document rather than a flat
    map because `setting_value` walks a path and `band_value` reads a whole node,
    and rewriting both to take a flat map would be changing the reader to suit
    the store.
    """
    document: dict[str, Any] = {}
    for path, value in paths.items():
        parts = path.split(".")
        node = document
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = value
    return document


def merge_settings(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """A complete document: the overrides laid over the defaults, leaf by leaf.

    A whole band (`{good, poor}`) lands as one value, matching `flatten`'s view
    that a band is one dial with two edges. An override carrying only one edge
    is a document no writer in the origin project produces -- `flatten` always
    emits bands whole -- and it merges leaf-wise like any other node, so the
    missing edge inherits the default. A host writing sparse documents by hand
    should write bands whole, or it is choosing one edge of a threshold and
    letting a release choose the other.
    """
    merged: dict[str, Any] = {}
    _deep_merge(merged, defaults)
    _deep_merge(merged, overrides)
    return merged


def _deep_merge(into: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Merge `source` into `into` without ever aliasing `source`'s nodes.

    The copy on the assignment arm is load-bearing. Assigning `source`'s own
    nested dicts by reference and then merging the next layer *into* them is
    how the shipped defaults -- shared by every tenant of a deployment -- came
    to be silently rewritten by the first tenant whose overrides touched a
    nested dial. One tenant's threshold must never become everybody's default.
    """
    import copy

    for key, value in source.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict) and not is_band(value):
            _deep_merge(into[key], value)
        elif isinstance(value, (dict, list)):
            into[key] = copy.deepcopy(value)
        else:
            into[key] = value
