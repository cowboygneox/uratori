"""What a host teaches the engine about its world.

The language and the engine are deliberately ignorant of any particular
product: they know how to bucket records, evaluate definitions and cascade
recomputation, and nothing about *which* records exist. A `Schema` is the whole
of what a host declares, handed over once when the library is compiled and when
the engine is constructed -- never threaded through individual calls, because
two call sites disagreeing about the world is a class of bug this object exists
to make unwritable.

Three things live here, and each is a decision the host owns:

- **Fact kinds.** The closed set of record kinds a definition may name. Closed
  because kinds are compile-time -- they are written in definitions and hashed
  into versions -- while the number of *sources* is a runtime property of a
  tenant. A kind must be a valid identifier in the definition language: `-` is
  the set-difference operator, so a kind containing one cannot be written at
  all.
- **Name fields.** Which field of a record carries its human-facing name, per
  kind. The engine freezes a subject's rendered name when a value is written,
  and a kind with no name field renders as its raw id -- honest, and ugly
  enough that the checker refuses to split a figure across such a kind.
  **Url fields** are the same decision for a record's link: evidence members
  carry one so a reader can walk from a cited record to the source system,
  and a kind with no url field serves bare titles. Declared rather than
  guessed, because a field that happens to be called "url" is a host
  convention the engine was never taught.
There were two more, and they are gone. **Settings lists** said which dials a
definition could name in which position, and **defaults** was the shipped
document those dials were completed against. A definition's numbers come from
facts now -- a figure computed from the records that set them -- or are
written where a reader can see them, so there is no dial to declare, no
document to complete and no position to police. A host that still sends the
retired keys is not refused: they are dropped, so a deploy mid-migration is a
no-op rather than a failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # a type-only import; schema.py must stay importable first
    from .lang.plan import Library

_KIND = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Schema:
    kinds: frozenset[str]
    name_fields: Mapping[str, str] = field(default_factory=dict)
    url_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for kind in sorted(self.kinds):
            if not _KIND.match(kind):
                raise ValueError(
                    f'"{kind}" cannot be a fact kind: a kind is named directly in the '
                    "definition language, where it must lex as one identifier. "
                    '"code_review-request" parses as a set difference, and a dot would make '
                    "it indistinguishable from a figure name."
                )
        strays = set(self.name_fields) - set(self.kinds)
        if strays:
            # Refused rather than ignored: a name field for a kind that does not
            # exist is a typo, and ignoring it means the kind it was meant for
            # renders raw ids for ever while everything looks configured.
            raise ValueError(
                f"name fields declared for unknown kinds: {', '.join(sorted(strays))}"
            )
        stray_urls = set(self.url_fields) - set(self.kinds)
        if stray_urls:
            # The same typo, one field over: ignored, the kind it was meant for
            # serves linkless evidence for ever while everything looks configured.
            raise ValueError(
                f"url fields declared for unknown kinds: {', '.join(sorted(stray_urls))}"
            )

    def is_kind(self, name: str) -> bool:
        return name in self.kinds

    def taught_by(self, library: Library) -> Schema:
        """This schema, completed by a fact-taught library.

        When the source declares facts, the kinds, name fields and url fields
        derive from those declarations -- the compile has already refused a
        schema that declared kinds of its own. Everything that consumes a
        `Schema` at run time (the engine freezing labels, evidence resolving
        links) goes through this, so the two doors cannot disagree about the
        world. A schema-taught world passes through untouched.
        """
        if not library.facts:
            return self
        return replace(
            self,
            kinds=frozenset(library.facts),
            name_fields={
                k: f.name_field for k, f in library.facts.items() if f.name_field is not None
            },
            url_fields={
                k: f.url_field for k, f in library.facts.items() if f.url_field is not None
            },
        )

    def to_document(self) -> dict[str, Any]:
        """The schema as JSON, the shape the service's `PUT /schema` takes.

        Defined beside the dataclass rather than in the server so an embedding
        host and an HTTP client serialise the world identically -- and so the
        round trip (`from_document(to_document())`) is a property a test can
        hold.

        """
        return {
            "kinds": sorted(self.kinds),
            "name_fields": dict(self.name_fields),
            "url_fields": dict(self.url_fields),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Schema:
        # The retired settings keys are dropped rather than refused: a host
        # deploying against this build with a document it has not yet edited
        # should find the keys inert, not the door shut.
        return cls(
            kinds=frozenset(document.get("kinds", ())),
            name_fields=dict(document.get("name_fields", {})),
            url_fields=dict(document.get("url_fields", {})),
        )
