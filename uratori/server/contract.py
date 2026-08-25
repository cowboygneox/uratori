"""The server's wire contract.

Requests and responses only -- the `Result` a host actually renders from is
`uratori.results.Result`, unchanged, because the whole point of the service is
that the answer shape is the engine's and not a transport's. What this module
adds is the administrative surface around it: declaring a schema, loading
definitions, pushing facts, and hearing what a pass did.

`RunOut` is deliberately the same report whether a pass was triggered by facts
or by hand: a host's activity log should not need to know which door the work
came through.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..results import Result
from ..schema import Schema


class SchemaIn(BaseModel):
    """A host's world, as JSON. Mirrors `uratori.Schema` field for field."""

    kinds: list[str]
    name_fields: dict[str, str] = Field(default_factory=dict)
    url_fields: dict[str, str] = Field(default_factory=dict)
    bucket_settings: list[str] = Field(default_factory=list)
    figure_settings: list[str] = Field(default_factory=list)
    reading_settings: list[str] = Field(default_factory=list)
    project_settings: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)

    def build(self) -> Schema:
        """The dataclass, with its own validation given the chance to refuse.
        Delegated to `Schema.from_document` so the HTTP shape and the embedded
        shape cannot drift -- this model exists only to give it an OpenAPI
        schema and a 422 on malformed JSON."""
        return Schema.from_document(self.model_dump())


def schema_out(schema: Schema) -> SchemaIn:
    return SchemaIn(**schema.to_document())


class DefinitionsIn(BaseModel):
    source: str
    """The definitions as written, concatenated -- the same text a host commits.
    The server compiles it; source is the truth and the compiled plans are its
    consequence, never something a client uploads directly."""


class DeclarationOut(BaseModel):
    """One declaration, described whole: prose, formula, and what it rests on.

    This is how an API-only host renders a catalogue, a derivation pane or a
    source view without importing the engine's code -- a host that had to
    embed the package to describe the library would be coupling to the engine
    twice to avoid coupling once. Sparse by design: a field that does not
    apply to a declaration's kind is null/empty rather than invented.

    `prose` is the `#` explanation above the declaration (required for the
    four rendered kinds, best-effort for groups, filters and measures) and `source`
    is the formula as written, display template stripped -- the same split
    `declaration_prose`/`declaration_source` make for embedding hosts, so the
    two doors describe one library identically.
    """

    name: str
    declaration: Literal["group", "filter", "measure", "figure", "reading", "projection", "summary"]
    version: str | None = None
    """The content hash, for the kinds that version (a group, filter or
    measure is hashed into its readers instead, so alone it has none)."""

    prose: str = ""
    source: str = ""
    display: str | None = None
    """A figure's or reading's display template; a group's or filter's label."""

    unit: str | None = None
    kind: str | None = None
    """The fact kind this is about: an index's or measure's record kind, a
    figure's or reading's subject kind, a projection's row kind."""

    id_space: str | None = None
    """Whose ids a group's or filter's members are -- its own kind unless
    `keyed as` says otherwise. A group fans out (one bucket per value); a
    filter is one bucket that narrows -- the declaration word carries that
    split, so no separate flag repeats it."""

    mode: Literal["window", "live"] | None = None
    grain: Literal["day", "minute", "15 minutes"] | None = None
    across: str | None = None
    banded: bool | None = None
    over: str | None = None
    """The projection a summary counts."""

    indexes: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    """The figures this rests on: a rollup's combine sources, a reading's
    stored-day source, a projection's read figures."""

    settings: list[str] = Field(default_factory=list)

    fields: list[str] = Field(default_factory=list)
    """The record field paths this declaration reads off `kind` -- an index's
    bucketing fields, a measure's operands, a projection's row fields. Served
    so a host can hold its own drift guard ("every path a definition reads
    exists on the records I collect") without compiling anything."""

    through: list[str] = Field(default_factory=list)
    """Identity hops, as `kind.path`: the relations an index resolves through
    or a projection joins through."""


class LibraryOut(BaseModel):
    """What compiled: every declaration, described.

    The versions are the review surface: a host that commits this document
    can assert these hashes match its reviewed copy, which is how "the server
    runs what I reviewed" becomes a check rather than a hope. The rest is the
    library described for readers -- see `DeclarationOut`.
    """

    figures: list[DeclarationOut]
    readings: list[DeclarationOut]
    projections: list[DeclarationOut]
    summaries: list[DeclarationOut]
    indexes: list[DeclarationOut]
    """Groups and filters together, under the engine's collective key."""

    measures: list[DeclarationOut]


class SettingsIn(BaseModel):
    document: dict[str, Any]
    """The tenant's sparse settings document. The engine completes it over the
    schema's defaults at every use; storing it sparse keeps "an operator chose
    this" distinguishable from "the default reached through"."""


class FactsIn(BaseModel):
    """One batch of fact movement, as the host saw it.

    `writes` may include records whose value has not changed -- the server's
    own change detection decides what moved, because the server's copy is the
    population calculations run over and the only baseline that matters. A
    host that filters to its own idea of "changed" makes the server's copy
    unrepairable after any missed push.
    """

    writes: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    """kind -> key -> record."""

    stamps: dict[str, dict[str, str]] = Field(default_factory=dict)
    """kind -> key -> the provider's own updated-at instant, ISO, where one
    exists. The stale-write guard: a batch built from a snapshot read before
    another batch's event must not put the pre-event record back, and only the
    provider's stamp can say which version a record is. Sparse -- a record
    without one has nothing to compare and always lands."""

    deletes: dict[str, list[str]] = Field(default_factory=dict)
    """kind -> keys gone from the world."""

    full: bool = False
    """Force a full pass: reindex and recompute everything rather than only
    what moved. The right call after a destructive change whose scope the
    warm path cannot see."""


class RunIn(BaseModel):
    full: bool = False


class ShownChange(BaseModel):
    """One movement, rendered at the instant it happened and never re-derived."""

    figure: str
    subject_id: str
    kind: Literal["moved", "removed"]
    label: str
    before_display: str
    after_display: str
    unit: str
    weight: float


class RunOut(BaseModel):
    """What one pass did: the true counts, a ranked sample, and the answers.

    `changed` is the TRUE count of movements and `shown` is a ranked sample --
    a full rebuild moves every value on the board, and a capped list under an
    honest total is checkable where a capped list alone reads as complete at
    every size.
    """

    written: int
    deleted: int
    changed: int
    rebuilt: list[str]
    covered: list[str]
    shown: list[ShownChange]
    results: list[Result]
    """The re-served answers for everything the pass moved (and every
    projection, always -- the clock is one of their inputs). The same objects
    `GET /tenants/{t}/results` returns and the websocket pushes; there is no
    run-only shape to drift."""


class Health(BaseModel):
    ok: bool
    version: str
    ready: bool
    """Whether a schema and definitions are loaded. A server that is up but
    unconfigured answers 409 to facts and runs; `ready` is how a boot script
    tells the difference without parsing errors."""

    figures: int
    readings: int


class Ack(BaseModel):
    ok: bool


class TenantRemoved(BaseModel):
    facts_removed: int
    values_removed: int


class Subscribe(BaseModel):
    """What a websocket client sends. Declared so the socket is a contract in
    both directions -- an inbound shape that exists only in the server's parser
    is a shape every client guesses at."""

    type: Literal["subscribe", "ping"]
    tenant: str | None = None


class Envelope(BaseModel):
    """What the websocket sends. The payload is a `Result`, unchanged."""

    type: Literal["result", "error", "pong"]
    tenant: str | None = None
    result: Result | None = None
    message: str | None = None
