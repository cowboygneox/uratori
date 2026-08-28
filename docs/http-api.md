# The HTTP & websocket API

Every route the service exposes, every frame the socket carries, and the one
envelope every answer travels in. The service is a thin wrapper over the
engine, so nothing here computes anything of its own: HTTP is how a host teaches the
engine and reads it, and the socket is how a screen hears about movement
without polling.

One deployment holds one world. The schema and the definitions are global;
tenants are data partitions under them, named freely in the URL -- there is no
route to create a tenant, because a tenant exists the moment something is
stored under its name. Two products with two worlds get two containers.

All request and response bodies are JSON (`Content-Type: application/json`).
The examples below run against `http://localhost:8080`, the container's
default ([Setup](setup.md) covers `HOST`, `PORT` and the rest of the
environment), and they use a small courier world so every one of them is
runnable end to end.

## The world the examples use

Two fact kinds -- couriers and the orders they carry -- one dial, and two
figures, the second built on the first:

```bash
BASE=http://localhost:8080
AUTH='Authorization: Bearer s3cret'   # harmless if the server runs tokenless

cat > couriers.fig <<'EOF'
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"

# How many orders this courier is carrying right now.
figure shop_courier.carrying:
    display "{value} orders in hand"

    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open

    calculate:
        count(mine)

# Whether a courier is over the carrying limit.
figure shop_courier.load_band:
    display "{value}"

    combine:
        carrying = shop_courier.carrying

    calculate:
        when carrying >= limits.carrying.over then "over"
        otherwise "ok"
EOF
```

(`load_band` computes its word in `calculate:`, so the word *is* the value.
A figure can also carry a `band:` block beside a numeric value, which is
what populates a `Result`'s `level` and `banded` -- see
[the language guide](language.md).)

The [definition language](language.md) is its own document; here it is only
cargo.

## Authentication

Set `URATORI_TOKEN` on the server and every route except `GET /health`
requires

```
Authorization: Bearer <token>
```

A missing or wrong token is a `401` with
`{"detail": "Bad or missing bearer token"}`. The comparison is constant-time.
`/health` stays open deliberately: a liveness probe that has to carry a
credential is a credential in every probe log.

The websocket is gated by the same header, checked **before** the handshake
is authenticated -- a bad token closes the socket with code `4401` and no frames.
The token travels in the header **only**, on HTTP and on the socket alike; a
`?token=` query parameter is ignored and refused, because a query string lands
in every access and proxy log between the server and the client, and a logged
credential is a stored one.

With `URATORI_TOKEN` unset, no route checks anything. That mode is for a
network that is itself the boundary; do not expose it further.

One carve-out lives beside these routes: the [built-in UI](ui.md) at `/ui/`,
which is unauthenticated by design and therefore off by default the moment a
token is set. Its JSON under `/ui/api/*` serves that page and is not part of
this API's contract -- integrate against the routes documented here.

## Errors, uniformly

Every refusal is JSON with a `detail` field:

- **`401`** -- bad or missing bearer token.
- **`404`** -- the resource does not exist (`GET /schema` before a schema is
  declared; `GET .../results/{name}` for a name no definition has).
- **`409`** -- the server is not yet taught. The detail names the missing
  step -- `"No schema has been declared yet"`, `"No definitions have been
  loaded yet"`, or (after an upgrade whose compiler refuses the stored
  source) `"The stored definitions do not compile under this build: ..."`
  quoting the refusal -- because an unconfigured server is
  a state the client can fix, and it must be told which fix. `409` rather than
  `500`, deliberately.
- **`422`** -- the body was understood and refused. Two shapes: a malformed
  body gets FastAPI's standard validation list, while a refusal by the engine
  (a schema that breaks its own rules, a definition the checker rejects) gets
  a plain string in the checker's or the schema's own words. The words are the
  point -- an API that flattened them to "invalid" would send an author
  hunting for a typo the checker had already named.
- **`501`** -- asked for something declared but not yet servable (a live
  reading, today).

## Routes

### `GET /health`

No auth. Always `200`:

```json
{"ok": true, "version": "1.4.0", "ready": true, "figures": 2, "readings": 0}
```

| Field | Meaning |
|---|---|
| `ok` | The process is up and answering. |
| `version` | The server's `APP_VERSION` (or `"dev"`). |
| `ready` | Whether a schema **and** definitions are loaded. A server that is up but untaught answers `409` to facts and runs; `ready` is how a boot script tells "down" from "unconfigured" without parsing errors. |
| `figures` | Compiled figure definitions. |
| `readings` | Compiled reading definitions. |

### `PUT /schema`

Declare (or replace) the world. The body mirrors the library's `Schema`,
field for field:

```bash
curl -s -X PUT "$BASE/schema" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "kinds": ["shop_courier", "shop_order"],
  "name_fields": {"shop_courier": "name", "shop_order": "ref"},
  "url_fields": {"shop_order": "url"},
  "figure_settings": ["limits.carrying.over"],
  "defaults": {"tenant": {"hoursPerDay": 8}, "limits": {"carrying": {"over": 3}}}
}'
```

| Field | Type | Meaning |
|---|---|---|
| `kinds` | `[string]` | The closed set of fact kinds definitions may name -- **omit it entirely in a fact-taught world** (see below). Each must lex as one identifier (`[A-Za-z_][A-Za-z0-9_]*`): `-` is the language's set-difference operator and `.` would collide with figure names, so a kind containing either is refused with an explanation. |
| `name_fields` | `{kind: field}` | Which field of a record carries its human name, per kind. The engine freezes this name when a value is written. A name field for a kind not in `kinds` is refused as a typo rather than ignored -- ignoring it would leave the intended kind rendering raw ids for ever while everything looked configured. |
| `url_fields` | `{kind: field}` | The same decision for a record's link: which field holds the address of the record in the source system, per kind. Evidence members carry it so a reader can walk from a cited record to the source. A kind with no url field serves bare titles -- declared rather than guessed, because a field that happens to be called `url` is a host convention the engine was never taught. Stray kinds are refused, like `name_fields`. |
| `bucket_settings` | `[string]` | Dial paths a definition may read, split by what turning the dial costs: a bucket setting re-buckets a tenant's whole history... |
| `figure_settings` | `[string]` | ...a figure setting recomputes one value per subject... |
| `reading_settings` | `[string]` | ...and a reading or |
| `project_settings` | `[string]` | projection setting is free, because nothing is stored. |
| `defaults` | object | The shipped settings document, as a nested object. A tenant's stored settings are sparse; the engine completes them over these at every use. A dial a definition names that resolves to nothing under the completed document **raises** rather than guessing. |

Every field defaults to empty, `kinds` included: a host that declares its
facts **in the definition language** (`fact shop_order:` -- see
[the language guide](language.md)) PUTs a schema of settings and defaults
alone, and the kinds, name fields and url fields derive from the source at
`PUT /definitions`. Declaring both is refused at compile time -- one world,
one door. One dial path is reserved:
`tenant.hoursPerDay`, which the renderer divides by to print an `effort`
(seconds of working time) as days -- a world that renders efforts must carry
it in `defaults`.

Responses:

- `200` `{"ok": true}`.
- `422` with the schema's own refusal (kind naming, stray name fields), or --
  when definitions are already loaded -- with
  `"the loaded definitions do not compile under this schema: ..."`.

That last refusal is whole-or-nothing: when definitions are loaded, they are
recompiled against the replacement **before** anything is persisted. A schema
change that breaks them is rejected entirely and the old world stands intact,
because persisting it would leave a server unable to rebuild its own library
at the next boot. Shrink the world only after the definitions have stopped
naming the part you are removing.

The schema document and the definitions source are persisted in the server's
own Postgres; a restarted container comes back taught, recompiling from
source at boot. (A build whose compiler refuses the stored source -- an
upgrade across a language change -- boots unready instead, answering `409`
with the refusal until corrected definitions are `PUT`.)

### `GET /schema`

The stored document, in exactly the `PUT` shape -- so in a fact-taught
world it answers `kinds: []`, honestly: the world lives in the source, and
`GET /definitions` is where its kinds, fields and versions are served.
`404` when no schema has ever been declared -- the one route where "not taught yet" is a missing
resource rather than a `409`, because here the resource being asked for *is*
the missing configuration.

### `PUT /definitions`

Compile and load definition source:

```bash
jq -Rs '{source: .}' couriers.fig |
  curl -s -X PUT "$BASE/definitions" -H "$AUTH" -H 'Content-Type: application/json' -d @-
```

(`jq` is doing the one fiddly part -- turning a file into a JSON string with
the newlines escaped; any JSON encoder does the same.)

The body is `{"source": "<the .fig text>"}` -- your definitions as written,
concatenated, the same text you commit. The server compiles it; source is the
truth and the compiled plans are its consequence, never something a client
uploads directly.

**Adopting facts on a live deployment happens through this door.** A source
that brings its own `fact` declarations refuses a schema that also declares
kinds -- but rather than deadlocking a running schema-taught world, this
route retires the stored schema's kinds, name fields and url fields in the
same save: the new world is compiled whole before anything is persisted, and
the settings and defaults survive. (Any other refusal is served verbatim; the
retirement happens only for the one conflict.) Fact versions are downstream
of nothing, so the adoption rebuilds no stored value.

Responses:

- `409` when no schema is declared yet (definitions compile against it).
- `422` with the compiler's message, verbatim, whichever layer refused --
  the parser's (`line 1: expected "fact", "group", "filter", …` for text
  that does not parse) or the checker's (`"not a fact kind"` when a
  definition names a kind the world does not declare). A refused load
  changes nothing: the previously loaded definitions (or the untaught state)
  stand whole, and `/health` still says so.
- `200` with the library, **described**: every declaration, each carrying
  its prose, its formula, and the names it rests on. A fact-taught world
  additionally carries `facts` -- per kind, the version, the prose, the
  name/url pointers, and every leaf field as a dotted path with its type,
  whether it `repeats` (a `many` on the way), and its own prose. That array
  is the schema a UI walks a number back to. One entry of each shape,
  abridged:

```json
{
  "facts": [
    {
      "name": "shop_order",
      "version": "af4ffe488502",
      "prose": "An order in the shop, as the provider last showed it.",
      "name_field": "ref",
      "url_field": "url",
      "fields": [
        {"path": "ref", "type": "text", "repeats": false, "prose": ""},
        {"path": "courier_id", "type": "text", "repeats": false, "prose": "Which courier holds it; absent until assigned."}
      ]
    }
  ],
  "figures": [
    {
      "name": "shop_courier.carrying",
      "declaration": "figure",
      "version": "7a65feeb434b",
      "prose": "How many orders this courier is carrying right now.",
      "source": "figure shop_courier.carrying:\n    depends:\n        mine = shop_order.carried_by:{shop_courier} & shop_order.open\n\n    calculate:\n        count(mine)",
      "display": "{value} orders in hand",
      "unit": "count",
      "kind": "shop_courier",
      "grain": null,
      "across": null,
      "banded": false,
      "indexes": ["shop_order.carried_by", "shop_order.open"],
      "measures": [],
      "reads": [],
      "settings": []
    }
  ],
  "readings": [],
  "projections": [],
  "summaries": [],
  "bundles": [
    {
      "name": "shop_courier.card",
      "declaration": "bundle",
      "version": "3f6f37a01c22",
      "prose": "The courier tile.",
      "source": "bundle shop_courier.card:\n    typical = reading shop_courier.typical_ride over 9, 2\n    carrying = figure shop_courier.carrying",
      "members": ["shop_courier.typical_ride", "shop_courier.carrying"]
    }
  ],
  "indexes": [
    {
      "name": "shop_order.carried_by",
      "declaration": "group",
      "version": null,
      "prose": "",
      "source": "group shop_order.carried_by from courier_id",
      "kind": "shop_order",
      "id_space": "shop_order",
      "fields": ["courier_id"],
      "through": []
    }
  ],
  "measures": []
}
```

(Fields a declaration's kind has no use for are `null`/empty rather than
invented; the wire carries them all.) `version` is the content hash of the
declaration's semantics -- prose edits do not fork it (see
[Concepts](concepts.md)); a group, filter or measure has none of its own
because it is hashed into every reader's. `prose` is the `#` explanation
above the declaration and `source` is the formula as written with the
display template stripped -- the split a Data screen renders, served here so
a host never needs the engine's code to describe the engine's library.
`indexes` (the collective key groups and filters travel under), `measures`
and `reads` are what a declaration rests on, one hop -- enough to draw a
derivation pane by following names -- and `fields`/`through` are the record
paths it reads, enough for a host to hold its own drift guard ("every path
my definitions read exists on the records I collect") without compiling
anything. A bundle's `members` are its members' names in declaration order --
the order its response preserves -- and its `version` is the review-only
hash of that list: it appears in no storage key and no number's citation.

The versions are the review surface, and the check is pure API: start the
same image your deploy pins against a scratch database, `PUT /schema` and
`PUT /definitions` with the text you reviewed, and assert the version map it
answers matches the one your production server reports. A mismatch means the
server compiled different text, or a different engine release changed a
definition's meaning, and either is worth stopping a deploy for. Every
`Result` cites one of these versions.

### `GET /definitions`

The same `200` body for whatever is currently loaded; `409` while the server
is untaught.

### `PUT /tenants/{tenant}/settings`

Store one tenant's dial document:

```bash
curl -s -X PUT "$BASE/tenants/t1/settings" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"document": {"limits": {"carrying": {"over": 10}}}}'
```

Answers `200` `{"ok": true}`.

The document is **sparse** -- only the dials an operator actually moved. The
engine completes it over the schema's `defaults` at every use; storing it
sparse keeps "an operator chose this" distinguishable from "the default
reached through". A non-band node merges leaf by leaf (setting one dial does
not unset its neighbours); a band node -- a thresholds object -- replaces
whole.

Saving settings deliberately does **not** run a pass. Values computed under
the old dial keep serving (and a moved dial a figure names will report as
`setting-moved` until recomputed); whether to pay for the rebuild now or let
the next fact push carry it is the host's call, and
`POST /tenants/{t}/runs` is how the host says *now*.

### `POST /tenants/{tenant}/facts`

Apply one batch of fact movement and run the pass it implies. This is the
route a webhook handler and a reconcile loop both call.

```bash
curl -s -X POST "$BASE/tenants/t1/facts" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "writes": {
    "shop_courier": {"c1": {"name": "Aki"}},
    "shop_order": {
      "o1": {"ref": "A-1", "courier_id": "c1", "status": "riding"},
      "o2": {"ref": "A-2", "courier_id": "c1", "status": "riding"},
      "o3": {"ref": "A-3", "courier_id": "c1", "status": "riding"}
    }
  },
  "stamps": {"shop_order": {"o1": "2026-08-24T12:00:00Z"}}
}'
```

Request fields, all optional:

| Field | Type | Meaning |
|---|---|---|
| `writes` | `{kind: {key: record}}` | Records as the provider shows them now. Arbitrary JSON in a schema-taught world; verified against the declaration in a fact-taught one. |
| `stamps` | `{kind: {key: instant}}` | The provider's own updated-at instant per record, ISO 8601, where one exists. Sparse. |
| `deletes` | `{kind: [key]}` | Keys gone from the world. |
| `full` | bool | Force a full pass: reindex and recompute everything rather than only what moved. The right call after a destructive change whose scope the warm path cannot see. Default `false`. |
| `defer` | bool | Write the batch and run **no pass**. For bulk imports: a pass per batch reads buckets every earlier batch already filled, so an import's cost grows with the square of its size. Verification still gates the batch whole. The caller owes the close -- `POST /tenants/{t}/runs {"full": true}` -- because until it runs, the tenant's stored answers describe the world as it stood before the deferred batches. The engine remembers the debt: the tenant's **next pass, whatever shape its caller asked for** -- a warm run, an ordinary push -- runs full and settles it, so a forgotten close costs one expensive pass rather than stale values served as current for ever. Contradicts `full`, and the pair is refused (`422`) rather than one silently winning. Default `false`. |

**Send everything you saw; the server decides what moved.** `writes` may --
should -- include records whose value has not changed. The server's copy is
the population calculations run over and the only baseline that matters; a
client that filters to its own idea of "changed" makes that copy unrepairable
after any missed push. An identical re-push writes nothing, moves nothing,
and the response says so honestly (`written: 0, changed: 0`).

**Stamps are the stale-write guard.** A batch built from a snapshot read
*before* another batch's event must not put the pre-event record back -- which
is exactly what a reconcile racing a webhook produces. A write lands only when
its stamp is `>=` the stored one; `>=` rather than `>`, so a rewrite at the
same version still lands (that is how a parser change reaches records nobody
touched). A record with no stamp on either side has nothing to compare and
**always lands** -- a guard that cannot see the versions must never be the
thing that drops data. So: pass the provider's stamp whenever the provider
gives one, and omit it when it does not.

Ordering inside the batch: deletes are applied first, then writes, then the
pass -- the engine reads the fact table to work out whose numbers a departure
moves, so the table must already say what the batch said. A batch that carries
any deletion escalates to a full pass (visible as a populated `rebuilt` in
the response); correctness over cheapness, and rare enough to be a fair price.
A **deferred** batch's deletes are applied with it and swept out of every
figure by the closing run, not before.

Passes are serialised per tenant: two concurrent posts for one tenant queue,
posts for different tenants overlap.

**A batch is verified before anything lands.** A *write* against a kind the
world does not declare is a `422` in either mode -- new behaviour from the
release that added fact declarations; such writes previously stored rows
nothing could ever read. A *delete* of an unknown kind is deliberately
allowed: it is the cleanup path for a retired kind's stored rows. In a
fact-taught world every written body is additionally checked against its
declaration -- an undeclared field, a wrong type or a wrong shape (`one`
receiving a list, a scalar receiving an object) refuses the **whole batch**,
with the kind, key and field (down to the list element: `events[1]`) in the
detail. Not per-record quarantine: silently landing a record's batch-mates
would narrow a population by a cheap path, and the fix is in the pushing
host's mapping. The batch's deletes and writes are also applied in one
transaction, so anything verification could not foresee still fails whole
rather than half-landing. An *absent* field -- omitted, an explicit null, or
the empty string -- is never an error: the declaration's claim is
known/unknown, not required/optional.

`409` while the server is untaught; otherwise `200` with a **run report**:

```json
{
  "written": 4,
  "deleted": 0,
  "changed": 2,
  "rebuilt": ["shop_courier.carrying", "shop_courier.load_band"],
  "covered": ["shop_courier", "shop_order"],
  "shown": [
    {"figure": "shop_courier.load_band", "subject_id": "c1", "kind": "moved",
     "label": "Aki", "before_display": "—", "after_display": "over",
     "unit": "level", "weight": 2.0},
    {"figure": "shop_courier.carrying", "subject_id": "c1", "kind": "moved",
     "label": "Aki", "before_display": "—", "after_display": "3",
     "unit": "count", "weight": 1.0}
  ],
  "results": [ …two Result objects… ]
}
```

| Field | Meaning |
|---|---|
| `written` | How many records **actually landed** -- new, changed, and admitted by the stamp guard. Not how many you sent: identical values and stale snapshots do not count, which is what makes this number worth logging. |
| `deleted` | How many keys the batch asked to delete. |
| `changed` | The **true** count of figure movements the pass produced, however large. |
| `rebuilt` | Figure names rebuilt from scratch this pass (a moved dial, a redeploy, a deletion, `full`). A figure recomputed to the value it already held writes nothing and appears nowhere; `rebuilt` is how a rebuild and a no-op stay distinguishable from outside. |
| `covered` | The fact kinds this pass actually read, sorted. A webhook covers almost nothing and a reconcile covers everything, and the difference says which values were *confirmed* unchanged rather than merely not checked. |
| `shown` | A **ranked sample** of the movements, capped at 40, for an activity log. See below. |
| `results` | The re-served answers for everything the pass moved -- plus, on any pass through the facts door or one that ran `full` (an empty batch counts: the door is the sync moment, not the batch's contents, and a standing import debt upgrades a run to `full`), every projection, because the clock is one of a projection's inputs and the sync is when that contract pays out. A definition-only pass (`POST /tenants/{t}/runs` after a deploy or a settings save) re-serves exactly what the change reached: the moved figures, the figures and readings a moved dial re-words or re-renders (a band threshold, the effort dial), and the projections whose answer can differ -- a rebuilt grouping they filter through, a moved figure they read, their own text (or a summary's over them) having changed, or a dial they render under having turned. Every **bundle** any of those members sits in re-serves whole, at its declared windows, as a `BundleResult` in the same list -- branch on `kind`. A change that reaches none of that serves nothing. The same objects `GET /tenants/{t}/results` returns and the websocket pushes; there is no run-only shape to drift. (One spelling difference: HTTP responses write absent fields as `null`, the socket omits them -- treat both as absent.) Empty when the request said `"serve": false`. |
| `moved` | Every definition name whose served answer this pass may have changed -- bundles included, computed without evaluating anything. A **superset** of what `results` re-serves, in exactly two ways: a summary's name appears here while its numbers travel inside its projection's `Result`, and a time-keyed or dimension-split figure appears here while the bulk surface leaves it to the by-name route -- both answer `GET /tenants/{t}/results/{name}` under the names listed. For the host that owns its own delivery (per-client subscriptions): send `"serve": false`, intersect `moved` with what your clients actually watch, and fetch exactly those by name at each watcher's own arguments. Carried on every response, not only lean ones. |

`"serve": false` in the request body (facts and runs alike) skips *shipping*
`results`, and skips evaluating them unless the server's own websocket has a
firehose subscriber on the tenant -- those subscribers' delivery is the
server's job, not the HTTP caller's, and their paint must not depend on
which client triggered the pass. `moved` still reports either way.

A **deferred** post answers the same shape with only `written` and `deleted`
populated -- `changed` 0, everything else empty -- because nothing recomputed,
and re-serving the stored answers would present pre-import values as the
batch's outcome.

Each entry in `shown` is one movement, rendered at the instant it happened
against the tenant's dials as they stood, and never re-derived -- a log line
is history, and a formatter improved next week must not rewrite it:

| Field | Meaning |
|---|---|
| `figure` | The figure that moved. |
| `subject_id` | Whose value. |
| `kind` | `"moved"` or `"removed"` -- a subject leaving the board is reported, never silent. |
| `label` | The subject's name as frozen at write time. |
| `before_display`, `after_display` | The value on each side, rendered. An absence renders as a dash, never a nought: 0 is a measured answer the engine did not give. |
| `unit` | The figure's unit (see the envelope, below). |
| `weight` | Why this row made the sample. |

`changed` is the true total and `shown` is the sample, and the pairing is the
honesty: a full rebuild moves every value on the board, and a capped list
under an honest total is checkable where a capped list alone reads as
complete at every size. The ranking normalises across units so importance,
not unit choice, decides the cut -- a count ranks by how far it moved, efforts
and durations by *hours* moved, shares by points, moments by days; a band
change weighs 2 (above a count ticking by one, below a real jump), and a
movement the arithmetic cannot measure -- a first reading, a value arriving at
or leaving null -- weighs 1, one thing having happened rather than nothing.
Removals rank ahead of every non-removal, because a departure buried under
forty routine edits is a roster change reported by nothing.

### `POST /tenants/{tenant}/runs`

A pass with no new facts:

```bash
curl -s -X POST "$BASE/tenants/t1/runs" -H "$AUTH" -H 'Content-Type: application/json' -d '{}'
```

The body is `{"full": bool, "serve": bool}`, both defaulted (`false`,
`true`). Use it to pick up a settings change (the response's `rebuilt` will
name the figures that read the moved dial, and `results` carries everything
the dial re-worded -- a band threshold re-serves the banded figure, its
readings and its tiles even though no stored value moved), to recompute
after loading a changed definition, or -- with `{"full": true}` -- to
rebuild everything a tenant has from the facts stored.

Returns the same run report as the facts route, with `written` and `deleted`
zero -- deliberately the same shape, because a host's activity log should not
need to know which door the work came through. `409` while untaught.

### `GET /tenants/{tenant}/results`

Every current answer, as a JSON array -- what a screen's first paint reads:

```bash
curl -s "$BASE/tenants/t1/results" -H "$AUTH"
```

The list carries, in this order: every stored figure (except time-keyed and
dimension-split ones, which serve by name only -- they are evidence panes, not
cards, and shipping every stored person-day on the bulk surface would spend
every pass on history nobody is watching), then every window reading, then
every projection, evaluated live at this instant -- then every **bundle**, as
a `BundleResult` (branch on `kind`), each at its declared windows, so a
screen bound to a tile paints with everything else. Two stated exceptions:
an anchored request (`at`, below) serves no bundles -- the by-name route
refuses an anchor on a bundle because its non-reading members can only be
served as they stand, and this route honours the same refusal rather than
serving anchored readings beside the same readings unanchored inside a tile
-- and a bundle naming a **live reading** is left off entirely, because such
a tile is not servable yet anywhere (its by-name route answers `501`) and
one member's gap must not fail the whole first paint.

`trailing` is a repeatable query parameter selecting the windows readings
are served over. Each value is a **span of buckets counted back from the
anchor**, bucket 1 being the bucket the request is anchored in:

- `?trailing=30` -- the last 30 days, exactly as when this parameter was a
  bare integer. The default is 30, 14 and 7.
- `?trailing=1-30&trailing=31-60` -- this month beside last month: exact,
  non-overlapping offset buckets, each independently getting the reading's
  statistics, sample floor, band and resolved dates.
- `?trailing=1-48h` / `?trailing=1-90m` -- a sub-day span, in hours or
  minutes (`d` is the explicit-days suffix, an alias of the bare form, the
  way a bundle member may write `in days`). Bare numbers are **days,
  always** -- never the source figure's storage grain, which would let a
  regrade silently re-scale every bookmarked URL -- so anything finer says
  so with the suffix. The same span twice in one request is a `422`: one
  request serves each window once, exactly as a bundle's window list
  refuses its duplicates. A sub-day
  span must slice whole stored buckets: hours over a figure stored by day
  (or minutes over quarter-hour storage) are a `422` naming the figure's
  grain, on this route and the by-name one alike -- refused whole rather
  than served with that reading quietly absent. A span whose series grain
  does not fit inside it (a `by day` sparkline in an hour window) is the
  same `422`.

A malformed span (`0-30`, `60-31`, `7.5`, junk) is a `422`, never coerced --
and a bound of `0` is refused with the convention spelled out, because
`0-30, 31-60` reads natural and would silently make the first bucket 31
days wide. Spans may reach at most 3660 days back (ten years of daily
buckets): every bucket is a stored point the server may walk per subject,
and a board wanting a decade of history is a definition conversation, not a
request parameter.

`at` anchors those windows on a chosen day instead of today: an ISO date
(`?at=2026-06-30&trailing=30` is "the 30 days ending June 30"), resolved by
the server to that day's end in each reading's own zone -- offset buckets
compose with it (`?at=2026-06-30&trailing=31-60` counts days-ago from June
30), and a sub-day span anchors on the day's final bucket. Absent, the
anchor is now, exactly as before. Anything that is not a `YYYY-MM-DD` calendar day
is a `422` -- including a bare epoch number, which is refused rather than
guessed at as a timestamp. Any absolute range at day granularity is
reachable this way: `at` is the end date and `trailing` the span. An anchor
before a tenant's data is not an error: `subjects` is empty and the `empty`
prototype's windows carry `days_covered: 0` with their requirements unmet,
the same absence answer an empty board serves. On this bulk route the
anchor reaches the window readings only -- a figure or projection is a
point-in-time answer with no window to move, and each result's own `at`
says when it was computed.

Windows and their anchor are the things a client may choose, because both are
presentation, not calculation -- a reading's statistics, minimums and band are
hashed into its version, and `trailing` and `at` only move which stored
buckets take part. The anchor travels back on the answer as provenance: the result's
`at` is the instant the anchor resolved to, and each window's `frm`/`to` are
the days it actually covered.

There is no `404` for an unheard-of tenant, and that is not an oversight: a
tenant is a data partition, not a resource, and the honest answer about one
with nothing stored is a full list of `Result`s whose `state` says
`never-computed`. An absence with a reason, not an error to special-case.

### `GET /tenants/{tenant}/results/{name}`

One definition's current answer, by name, with the same `trailing` and `at`
parameters:

```bash
curl -s "$BASE/tenants/t1/results/shop_courier.carrying" -H "$AUTH"
```

The name may be any figure (including the day-keyed and dimension-split ones
the bulk route excludes -- their rows carry the day or the dimension in each
subject's `dimension` field), any window reading, any projection, or any
summary -- a summary is answered by evaluating the projection it is declared
over, because its counts are *defined* as being over those rows and a cheaper
second route would be duplicate arithmetic wearing a shortcut's name.

The name may also be a **bundle**, and the answer is then the other shape
this route serves -- a wrapper, discriminated from a plain `Result` by
`kind`:

```json
{
  "kind": "bundle",
  "name": "shop_courier.card",
  "version": "3f6f37a01c22",
  "at": "2026-06-30T23:59:59+00:00",
  "label": "Card",
  "doc": "The courier tile.",
  "results": [
    { "slot": "typical",  "result": { …an ordinary reading Result… } },
    { "slot": "carrying", "result": { …an ordinary figure Result… } }
  ]
}
```

Each entry in `results` carries the member's **slot** -- the address the
bundle's definition binds it to (`typical = reading …`), so a client reads
`card.typical` and the tile's layout is decoupled from definition names --
beside the member's ordinary `Result`, which is exactly what requesting it
by name would return, own `version`, `state`, `label` and provenance
included. The slot is an address, never a display label: nothing lets a
bundle rename what a number is called. There is no bundle-level `state`:
"one number is behind a deploy" is a per-member fact the wrapper must not
flatten. The
members are evaluated at one instant; a reading member's windows come from
the bundle's definition (`trailing` deliberately does not reach inside a
bundle -- a tile whose windows the caller could move would be a different
tile under the same hash), and `at` is refused outright (`400`): an anchor
moves only a reading's windows, the other members can only be served as they
stand, and an anchored tile would put June's reading beside today's page
under a wrapper claiming one clock -- anchor the reading by its own name
instead. A summarise member arrives with `subjects` empty
and the population row in `summary` -- computed over ALL the projection's
rows, never the page; only the row payload stays home. The wrapper's
`version` is the bundle's content hash -- the review token for the committed
artifact -- and appears in no storage key and no number's citation.

- `200` with a single `Result` -- or, for a bundle, a single wrapper as
  above.
- `404` `"No definition called {name}"`.
- `501` for a reading declared `live` -- declared but not yet servable, and
  "no such definition" and "not built yet" send a caller to different fixes.
  A bundle naming a live reading answers the same `501`, rather than
  silently serving a tile one member short.
- `400` when the engine refuses the request, in its own words.

### `GET /tenants/{tenant}/evidence/{name}?subject={id}`

The records behind one stored value. The engine stores every value with the
record ids it was computed from; this joins that citation back to the records,
so a row reading "1.0h, 2.0h" can be traced to the two records that produced
it. Fetched on request rather than carried on `Result`, because every row of a
served figure dragging its members along would make the common read pay for
the rare check.

```bash
curl -s "$BASE/tenants/t1/evidence/shop_courier.carrying?subject=c1" -H "$AUTH"
```

```json
{
  "figure": "shop_courier.carrying",
  "version": "7a65feeb434b",
  "subject": "c1",
  "state": {"ok": true},
  "display": "2",
  "note": null,
  "members": [
    {"key": "o1", "title": "A-1", "url": "https://shop/o1", "held": true, "display": null, "figure": null, "dimension": null},
    {"key": "o2", "title": "A-2", "url": "https://shop/o2", "held": true, "display": null, "figure": null, "dimension": null}
  ],
  "parts": false,
  "source": null,
  "kind": "shop_order",
  "measure": null
}
```

Each member is one thing the value cites. `title` and `url` are resolved
through the schema's `name_fields` and `url_fields`; `held: false` means the
store was asked for this record and does not have it -- deleted at the source,
or never collected -- and the member is listed anyway, because a list quietly
shorter than the value beside it breaks the one check this payload exists to
enable. (When a figure's members span more than one fact kind there is no one
table to ask, so no lookup is made: the keys are served bare and `held` stays
true, because "not held" is a claim only a lookup can earn.)

`display` is the member's own measurement, rendered. A `list` figure serves
its stored values, positionally paired with the members. A `sum` or an
extreme (`latest`/`earliest`) serves each held record **as its measure reads
it now** -- live, so a record corrected after the pass visibly disagrees with
the stored value above it, which is true; the alternative is a panel that
agrees with a number that has stopped being right. A count deliberately
serves none -- a "1" beside each record would be a number nothing computed.
The top-level `measure` names the measure those displays were read through
(null for a count and for parts, and withheld whenever `note` withholds the
measurements).

For a rollup, `parts` is true and the members are the stored cells it read --
each naming its source `figure`, carrying that cell's own value as its
`display`, and its day or dimension cell as `dimension` (split off the
storage key server-side; a record's key is never split, because a raw fact
key may contain `@` without it meaning anything) -- because a total's
evidence is its parts and re-listing the records underneath would re-derive
the number a second way.

When the figure is unavailable the response carries its `state` and no
members -- an empty list under an `ok` state would read as "this value cites
nothing", a confident claim about a figure the tenant has never run.

- `200` with the `Evidence` object above.
- `404` for anything that is not a figure, each naming where the evidence
  actually lives: a windowed reading forwards to the figure whose stored days
  it summarises, a live reading stores nothing, a projection's or summary's
  *rows* are the evidence. Also `404` for a subject with no stored row, with
  the reason.

### `DELETE /tenants/{tenant}`

Every row the tenant owns -- facts, computed values, index state, stored
settings -- gone. The schema and definitions are untouched; they are the
deployment's, not the tenant's.

```bash
curl -s -X DELETE "$BASE/tenants/t1" -H "$AUTH"
```

```json
{"facts_removed": 4, "values_removed": 2}
```

The counts are the response because "ok" is the least useful true thing a
destructive route can say: a caller expecting thousands and told 4 has just
learned it deleted the wrong tenant, while an `{"ok": true}` would have let it
find out later. Deletion is not soft; the tenant's next `GET .../results`
serves `never-computed` absences, exactly like a tenant that never existed.

## The `Result` envelope

Every answer, over every transport -- the results routes, the run reports, the
websocket -- is the same object. There is no route-specific shape,
deliberately: a second shape is where republishing steps, and with them
duplicate arithmetic, come back.

A served figure from the courier world:

```json
{
  "kind": "figure",
  "name": "shop_courier.carrying",
  "version": "7a65feeb434b",
  "at": "2026-08-24T12:00:03.214000+00:00",
  "zone": null,
  "unit": "count",
  "label": "Carrying",
  "doc": "How many orders this courier is carrying right now.",
  "state": {"ok": true},
  "banded": false,
  "subjects": [
    {"id": "c1", "name": "Aki", "value": 3.0, "display": "3",
     "windows": null, "row": null, "level": "unknown", "dimension": null}
  ],
  "empty": null,
  "summary": null
}
```

Top level:

| Field | Meaning |
|---|---|
| `kind` | `"figure"`, `"reading"`, `"projection"` or `"summary"`. What varies between them is what sits inside `subjects`, not the envelope around it. |
| `name` | The definition's name. |
| `version` | The content hash of the definition that produced this answer. **The citation**: it is one of the hashes `PUT /definitions` returned, so any number on any screen traces to reviewed text. |
| `at` | The instant the answer is *about*, ISO 8601: evaluation time, or -- for a reading served with `?at=` -- the requested anchor day's last moment in the reading's zone. One instant for the whole result, by construction -- a per-row clock produces a list whose oldest entry disagrees with itself. |
| `zone` | The tenant's calendar timezone, when the definition is anchored to one, so a screen can say *when* rather than printing an instant verbatim. A client cannot work this out: it knows its own timezone, and the board belongs to a team that may not share it. |
| `unit` | What the values *are* -- see below. |
| `label` | A heading, rendered by the server. |
| `doc` | The definition's explanation -- the `#` comment lines written directly above its declaration, served wherever the number is cited. |
| `state` | `{"ok": true}` or an explained absence -- see below. |
| `banded` | Whether the definition declares thresholds at all. A property of the definition, never of the data, so it holds its value while `state` is unavailable. Without it, "no thresholds declared" and "banded, but nothing to band yet" are the same word on the wire (`level: "unknown"`), and a screen would print a Band column of stated absences under definitions that never claimed to band. A projection is never banded: any band words it carries travel as row values, cited to the figure whose thresholds produced them. |
| `subjects` | The rows, **in the server's order**. A client does not sort: sorting is a calculation and the sort key is a definition's answer. |
| `empty` | What the definition says about a subject it has nothing for -- "nobody merged anything" is a finding, and which requirement that fails is the definition's decision, not the caller's assumption. |
| `summary` | For a projection: the summary row declared over it, computed at the same instant over the same population -- which is why it travels here rather than on a route of its own. |

`unit` is one of `count`, `duration`, `effort`, `share`, `days`, `level`,
`moment`. The distinction that matters: `duration` is wall-clock and `effort`
is working time -- the same 28,800 seconds is "8h" under one and, against the
tenant's `tenant.hoursPerDay`, "1d" under the other, and both are right about
their own quantity. The unit travels so a renderer never guesses.

### `state`, and the four ways an answer can be missing

`state` is a tagged union on `ok`:

```json
{"ok": true}
{"ok": false, "because": "never-computed", "detail": null}
```

When `ok` is `false`, `subjects` is empty and `because` is exactly one of
four words. This replaces the trio of booleans (`live`, `stale`, `populated`)
that the predecessor design made every caller combine slightly differently:
one value, and it says *why*, so a screen that ignores it renders nothing
rather than a fabricated zero.

| `because` | Meaning |
|---|---|
| `never-computed` | This tenant has never run this definition. A new board, or the window between a deploy and the next pass. |
| `behind-deploy` | Values exist, at an **older version** of the definition. They are not shown, because a number computed by a definition that no longer exists is worse than a dash. Run a pass to recompute. |
| `setting-moved` | A dial the definition names has changed and the rebuild has not finished; the stored values describe the old dial. |
| `nothing-collected` | The definition ran, and nothing it reads holds anything. A board with no source connected -- as opposed to a team whose queue is genuinely empty, which is a *measured* answer with real subjects and real zeroes. |

The last distinction matters more than it looks: a fully-backfilled tenant
with no data source stores a complete, confident table of zeroes,
indistinguishable from a quiet week unless something says which it is.
`detail`, when present, is a human sentence with more context.

**An absence is never a zero.** A client that maps any of these to `0` has
reintroduced the exact lie this envelope exists to end.

### `Subject`

One row of an answer: identity and value together, never two parallel maps to
join.

| Field | Meaning |
|---|---|
| `id` | The record's key. For a time-keyed figure the id is `<subject>@<label>` -- the label an ISO day, or local ISO time truncated to the grain (`2026-08-23T14:30`) for a sub-day figure. |
| `name` | Rendered by the server. For a stored figure this is the name **frozen when the value was written** -- a person renamed next week must not rewrite the history of what they moved. For a live answer (a projection) it is the current one, since there is no history to contradict. |
| `value` | The magnitude: number, band word, or `null`. It exists for anything *positional* -- a bar's width, a marker's offset. `null` where the underlying value is a collection: a day's measurements arrive as rendered text in `display`, never as a numeric list, because a numeric list is something a client could reduce over and this API's claim is that there is nothing to reduce. |
| `display` | `value`, rendered by the server. Both travel because they answer different questions -- the number is positional, the text is what a reader sees. Rendering is not the client's job because rendering a duration or an effort is a division against a dial the client does not have, and a client that had the dial would be one step from banding against a threshold. |
| `windows` | For a reading: the served windows (bucket spans), below. Otherwise `null`. |
| `row` | For a projection: the assembled row, below. Otherwise `null`. |
| `level` | The band word, from the definition's own thresholds evaluated against the tenant's live dials. See "band words", below. |
| `dimension` | The other half of a pair when the figure is split across something (or time-keyed) -- present so two rows about one subject are told apart by a field rather than by a reader noticing. |

### `Window` (readings)

One window: a span of stored buckets, resolved by the server. `span` and
`bucket` are the question and `frm`/`to` are the answer, and both travel:
"buckets 31-60" depends on when the tenant's midnight was, which a client
cannot know. Note the spelling -- the field is `frm`.

| Field | Meaning |
|---|---|
| `span` | The bucket span asked for, in the canonical spelling's span half: `"30"` (the last 30 buckets, bucket 1 being the anchor bucket), `"31-60"` (the 30 before them). The unit deliberately lives in `bucket`, not here -- read the pair; a client keying on `span` alone would conflate `48` days with `48` hours. |
| `bucket` | What one bucket of the span is: `"day"`, `"hour"` or `"minute"`. Days unless the request said otherwise, always. |
| `trailing` | The span as a plain trailing-days count, kept meaning what it always has: present exactly when the span *is* the last N days, `null` for an offset or sub-day span -- an offset bucket wearing a trailing-looking number is the lie this field refuses to tell. |
| `frm`, `to` | The first (oldest) and last (newest) bucket labels the span resolved to -- ISO days for day buckets, local bucket labels (`2026-08-25T14:00`) for sub-day ones. |
| `zone` | The calendar it resolved in. |
| `mean`, `median`, `worst`, `total`, `count` | The statistics the reading declares; each is a number or `null`. A definition's `sum` arrives on the wire as `total` -- the rename happens here, nowhere else. |
| `series` | Per-point values, when the definition asked for them -- the one non-scalar statistic, and it exists so a sparkline is a definition's answer rather than the client slicing a range and computing ten means. |
| `series_by` | What one series point spans -- `"15 minutes"`, `"hour"` or `"day"` -- when the definition grouped a sub-day figure. Absent (`null`) for a day-keyed source, where a point has always been a day. |
| `display` | Each statistic above, rendered, keyed by statistic name. Rendered on the server because rendering a duration is a division, and a division is a calculation. |
| `sample` | How many values took part. For a count figure this is *days that contributed*, not records -- a different number of similar magnitude, which is why it is named rather than left to be inferred. |
| `days_covered`, `days_requested` | How many calendar days of the window hold data, against how many it touches -- a claim about days whatever the bucket unit. |
| `level` | The window's band word. |
| `unmet` | Which declared requirement fell short, in words -- so a suppressed mean is a dash with a stated reason rather than an undifferentiated one. |

### `Row` and `Flag` (projections)

A projection's subject carries a `row`: named, finished values and the
sentences the row earned.

| Field | Meaning |
|---|---|
| `values` | `{name: number \| string \| null}` -- finished values, never the records they came from. A screen wanting a count of rows asks for the summary that counts them. |
| `display` | Every value above, rendered. Required, not defaulted: every reader indexes into it. |
| `units` | `{name: unit}`, so a renderer never guesses what a column is. |
| `flags` | Sentences the row earned, each with `name`, `label`, `detail`, an optional `action`, and `severity` (`"info"` or `"attention"`). Rendered by the server. |

### Band words (`level`)

`level` anywhere is a word from the definition: the author's own word on a
`when` ladder (`over`, `warn`, `at-risk`, ...), the engine's word (`good`,
`watch`, `poor`) from a `band` clause's thresholds, `"unknown"` where nothing
banded. A renderer maps words to colours, and an unrecognised word must render
as **neutral** -- never as good, which is what a missing case silently does
when green is the default. A renderer never compares a number to a threshold:
banding in two places is how a card reads Watch while a sort weighs the same
subject as Good, with list order the only symptom.

## The websocket: `/stream`

Subscribe to a tenant; receive its current answers, then every answer that
moves -- the same `Result` objects the routes return, delivered one per moved
definition. What the socket adds is only *when*. One spelling difference: the
socket omits absent fields where HTTP writes `null`; a client must treat a
missing key and a `null` alike, as *absent*, never as zero.

```
ws://localhost:8080/stream
```

Auth first: when `URATORI_TOKEN` is set, the `Authorization: Bearer`
**header** must be right or the socket closes with code `4401` and no
frames. The handshake is accepted and then immediately closed, deliberately:
a rejected handshake surfaces in a browser's WebSocket API as the same
opaque failure a network fault does, and a client retrying network faults
would retry an auth problem for ever -- `4401` as a close code says which it
is, and nothing travels between accept and close. A token in the query
string does not work, on purpose (see Authentication).

### Frames the client sends

Three, all JSON:

```json
{"type": "subscribe", "tenant": "t1"}
{"type": "subscribe", "tenant": "t1", "entries": [
  {"name": "shop_courier.card"},
  {"name": "shop_courier.typical_ride", "trailing": ["9", "31-60"]}
]}
{"type": "unsubscribe", "entries": [{"name": "shop_courier.card"}]}
{"type": "ping"}
```

A `subscribe` **without** `entries` is the firehose, unchanged from before
subscriptions existed: everything, at the serving defaults. With `entries`
it is a set of **standing GETs**: each entry names a calculation with the
same arguments `GET /tenants/{t}/results/{name}` takes -- `trailing` for a
windowed reading, nothing for everything else. `trailing` anywhere it means
nothing is refused rather than ignored: on a bundle because its windows are
declared in its definition, and on a figure, projection or summary because
an ignored argument would still become part of the entry's identity, and
one question would fork into two subscriptions serving identical frames
(the HTTP route can afford to ignore it; a standing entry cannot). Each
entry is answered immediately with its current answer at those arguments,
and re-answered whenever a pass impacts it. Entries accumulate across
frames; `unsubscribe` removes by the same identity (the name plus the
canonical window spelling), and an `unsubscribe` naming nothing clears
everything, firehose included. An empty `entries` list is an empty set of
standing GETs -- it subscribes to nothing and is not the firehose.

An entry the server cannot honour -- an unknown name, a window that does not
parse, windows where they mean nothing, a span whose unit cannot slice the
reading's storage, a live reading -- is refused with an `error` frame
carrying the entry's `name` and the same sentence the HTTP route would
answer, and is **not** followed; the valid entries beside it in the same
frame proceed. Nothing is ever dropped silently -- including at a redeploy:
a teach that removes a definition ends every standing entry on it with one
`error` frame naming the entry, because a subscription that can never be
impacted again going quiet would be the freeze this protocol exists to end.

Anything that does not parse as one of these frames is **ignored** rather
than fatal: a client that sends nonsense is a bug in that client, and taking
the connection down makes the bug look like an outage. The one parseable
mistake gets an answer: a `subscribe` with no `tenant` (and none remembered)
receives an error frame.

A connection watches one tenant at a time. Subscribing to a different tenant
switches it and clears every prior interest -- entries made watching one
board must not silently follow another.

### Frames the server sends

All are one envelope, with absent fields omitted. `result` carries a
`Result`, or a `BundleResult` for a bundle -- branch on its `kind`. `name`
is the subscription entry a frame answers or refuses: set on every
entry-addressed `result` (a summary entry is answered with its projection's
`Result`, and without the address the asking tile could never attribute the
frame) and on every entry-scoped `error`; firehose results and frame-level
complaints carry none.

```json
{"type": "result", "tenant": "t1", "result": { …a Result or BundleResult… }}
{"type": "result", "tenant": "t1", "name": "shop_order.book", "result": { …the projection's Result… }}
{"type": "error", "tenant": "t1", "name": "no.such", "message": "No definition called no.such"}
{"type": "error", "message": "subscribe names a tenant"}
{"type": "pong"}
```

### The lifecycle

1. **Connect** (with the header when the server has a token).
2. **Subscribe.** The server immediately answers: for the firehose, the
   **first paint** -- one `result` frame per servable definition, bundles
   included: the same list, in the same order, as `GET /tenants/{t}/results`
   (default windows) -- so a client never renders from a partial world while
   waiting for a pass. For entries, one frame per entry: its current answer
   at the subscribed arguments. A tenant with nothing stored paints too:
   every frame an explained absence, because a blank board and a board with
   nothing to say are different things.
3. **Movement.** Whenever a pass for the subscribed tenant changes something
   (a facts post, a manual run, a settings save picked up by a run), the
   firehose receives one `result` frame per definition that **moved** -- and
   only those; an unchanged recompute pushes nothing. An entry receives a
   frame exactly when the pass impacted it, re-evaluated at ITS OWN
   arguments -- never the serving defaults -- and evaluated once however many
   clients hold the same entry. "Impacted" means: a figure that moved or
   whose served rendering a dial turned (a band threshold, the effort dial);
   a reading whose source figure moved or whose own dial turned; a
   projection being re-served (every sync pass -- the clock is one of its
   inputs -- or a definition-only pass whose change reaches it); a bundle
   any member of which is impacted, evaluated at its declared windows. A
   subscription narrows which answers travel, never the population inside
   any answer: every result is served whole.
4. **Ping** whenever you like; the `pong` doubles as an ordering fence, since
   frames are delivered in order.

There is no replay: a reconnecting client subscribes again and receives a
fresh first paint, which *is* the catch-up. A socket that cannot be written
to is dropped from delivery; the client's job is to reconnect.

The hub lives in process memory, which is one reason the container runs a
single worker -- see [Setup](setup.md) before putting a load balancer in
front of this.

### An example

With [websocat](https://github.com/vi/websocat):

```bash
websocat -H='Authorization: Bearer s3cret' ws://localhost:8080/stream
{"type": "subscribe", "tenant": "t1"}
```

The first paint arrives at once (two frames, in the courier world). Leave it
open and push a fact from another shell --

```bash
curl -s -X POST "$BASE/tenants/t1/facts" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"writes": {"shop_order": {"o4": {"ref": "A-4", "courier_id": "c1", "status": "riding"}}}}'
```

-- and the socket prints exactly the answers that moved:
`shop_courier.carrying` goes to 4, and `shop_courier.load_band` only if the
new count crossed the tenant's dial. Nothing else arrives, because nothing
else changed.
