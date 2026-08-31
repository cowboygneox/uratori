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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..results import BundleResult, Result
from ..schema import Schema

AnyResult = Annotated[Result | BundleResult, Field(discriminator="kind")]
"""What an answering surface carries: a definition's `Result`, or a bundle's
wrapper of them. Discriminated on `kind` -- the field both shapes already
declare -- so a typed client branches on data rather than sniffing shapes."""


class SchemaIn(BaseModel):
    """A host's world, as JSON. Mirrors `uratori.Schema` field for field.

    `kinds` is optional since facts became declarable in the language: a
    fact-taught host PUTs a schema of settings and defaults alone, and the
    compile refuses a document that declares kinds beside a source that
    declares facts -- one world, one door.
    """

    kinds: list[str] = Field(default_factory=list)
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
    declaration: Literal[
        "group", "filter", "measure", "figure", "reading", "projection", "summary", "bundle"
    ]
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
    grain: str | None = None
    """The bucket rule of a time-keyed figure's sequence -- a stored grain
    (`minute`, `15 minutes`, `hour`, `day`, `week`, `month`, `quarter`) or
    a selective rule's canonical text (`first monday of month`). A string
    rather than a closed union because the ordinal weekday family is a
    rule with two parameters, and the value is the declaration's own
    spelling either way."""

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
    band_reads: list[str] = Field(default_factory=list)
    """The figures a band compares against, apart from `reads` deliberately.

    `reads` are what a stored value is computed from, so moving one rebuilds
    this definition; a goal a band judges against moves no stored value and
    must not force a rebuild -- but it does re-word the served level, so a
    host asking "what re-serves when this figure moves?" has to be told these,
    and one asking "what must rebuild?" must not be."""

    statistics: list[str] = Field(default_factory=list)
    """A reading's calculated statistics (mean, worst, series, ...). The
    others are absent from its answers, not zero -- a host binding a column
    to one the definition never calculates renders a dash for ever, and this
    list is what lets it refuse that at build time."""

    fields: list[str] = Field(default_factory=list)
    """The record field paths this declaration reads off `kind` -- a group's
    or filter's bucketing fields, a measure's record operands (never the
    clock: `now` is not a field), a projection's row fields (for a joined
    field, the LOCAL path that identifies the other record -- the remote path
    lives in `through`). Served so a host can hold its own drift guard
    ("every path a definition reads exists on the records I collect")
    without compiling anything."""

    through: list[str] = Field(default_factory=list)
    """Identity hops, as `kind.path`: the relations an index resolves through
    or a projection joins through."""

    members: list[str] = Field(default_factory=list)
    """A bundle's members, by name, in declaration order -- the order the
    response preserves, so a host binding tiles to positions can check them
    at build time. Window arguments live in `source` (the formula as
    written); the hash already covers them."""


class FactFieldOut(BaseModel):
    """One leaf of a fact's body, flattened for a reader.

    `path` is dotted through the nesting (`events.at`); `repeats` says a
    `many` sits somewhere on the way, because that is the property every
    downstream rule branches on. `prose` is the `#` run above the field --
    the customer-facing description of what the provider writes there.
    """

    path: str
    type: Literal["text", "number", "flag", "moment"]
    repeats: bool = False
    prose: str = ""


class FactOut(BaseModel):
    """One fact kind, described whole -- the schema a traced number bottoms
    out on. The version is the hash of the fields and types alone; prose and
    the name/url pointers are rendering and move nothing."""

    name: str
    version: str
    prose: str = ""
    source: str = ""
    name_field: str | None = None
    url_field: str | None = None
    fields: list[FactFieldOut] = Field(default_factory=list)


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
    facts: list[FactOut] = Field(default_factory=list)
    """The declared world, when the source declares one. Empty for a
    schema-taught deployment -- the kinds live on `GET /schema` there."""

    bundles: list[DeclarationOut] = Field(default_factory=list)
    """The composition stratum: each bundle's members in declaration order,
    and the hash a committed artifact reviews tiles by."""


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

    defer: bool = False
    """Write the batch and run no pass. For bulk imports: a pass per batch
    reads buckets every earlier batch already filled, so an import's cost
    grows with the square of its size. Verification still gates the batch
    whole; stored answers simply do not include it until the caller closes
    the import with `POST /tenants/{t}/runs {"full": true}`. The engine
    remembers the debt -- the tenant's next pass runs full whatever shape
    its caller asked for -- so a forgotten close costs one expensive pass,
    never stale answers served as current. Refused (422) together with
    `full`: defer skips the pass, full forces the biggest one, and either
    honoured would silently ignore the other."""

    serve: bool = True
    """Whether the response carries the re-served answers. `false` is for the
    caller that owns its own delivery -- a host holding per-client
    subscriptions -- which reads `moved` off the response and fetches exactly
    the watched names, by name, at each watcher's own arguments. The server's
    own socket subscribers are unaffected: their delivery is the server's
    job, not this caller's."""


class RunIn(BaseModel):
    full: bool = False
    serve: bool = True
    """See `FactsIn.serve`; the same lever on the fact-less pass."""


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
    carried: list[str] = []
    """Carried figures this pass extended to the present bucket.

    Named rather than counted so "the pass ran and moved nothing" stays a
    different finding from "the pass never reached this figure" -- which,
    for a construct whose whole job is to fill buckets nobody wrote records
    into, is otherwise impossible to tell apart from outside. A lazy fill on
    a read is the other trigger and never appears here.

    Defaulted, so a client written before carried figures existed reads a
    run report unchanged."""

    covered: list[str]
    shown: list[ShownChange]
    results: list[AnyResult]
    """The re-served answers for everything the pass moved (and every
    projection, always -- the clock is one of their inputs), impacted bundles
    included. The same objects `GET /tenants/{t}/results` returns and the
    websocket pushes; there is no run-only shape to drift. Empty when the
    request said `serve: false` -- the caller asked for `moved` instead."""

    moved: list[str] = Field(default_factory=list)
    """Every definition whose served answer this pass may have changed --
    bundles included, computed without evaluating anything. What a
    subscribing host intersects with its clients' interest before fetching
    by name; carried on every response, not only `serve: false` ones, so a
    caller can cross-check the results against the claim."""


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


class SubscribeEntry(BaseModel):
    """One named calculation a client wants to fetch and follow: a standing
    GET, with the arguments the HTTP API takes. `trailing` is the window list
    exactly as `GET /tenants/{t}/results/{name}` accepts it, meaningful for a
    windowed reading and refused for a bundle (whose windows are declared in
    its definition). Entry identity -- for unsubscribe, and for delivering one
    evaluation to every client that asked the same question -- is the name
    plus the canonical spelling of the windows."""

    name: str
    trailing: list[str] | None = None


class Subscribe(BaseModel):
    """What a websocket client sends. Declared so the socket is a contract in
    both directions -- an inbound shape that exists only in the server's parser
    is a shape every client guesses at.

    `subscribe` with no `entries` keeps its original meaning -- everything,
    at the serving defaults -- so a client written before subscriptions
    existed still paints and follows. With `entries`, each is answered
    immediately (the fetch) and re-answered whenever a pass impacts it (the
    follow), at the entry's own arguments; entries accumulate across frames,
    and `unsubscribe` removes by the same identity -- or everything, when it
    names none."""

    type: Literal["subscribe", "unsubscribe", "ping"]
    tenant: str | None = None
    entries: list[SubscribeEntry] | None = None


class Envelope(BaseModel):
    """What the websocket sends. The payload is a `Result` or a bundle's
    `BundleResult`, unchanged -- the same object the routes serve. `name` is
    set on an error about one subscription entry, so a client can tell "your
    entry was refused" apart from a frame-level complaint."""

    type: Literal["result", "error", "pong"]
    tenant: str | None = None
    result: AnyResult | None = None
    name: str | None = None
    message: str | None = None
