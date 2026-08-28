# The built-in UI

Every deployment carries a small investigation surface at `/ui/`: the whole
library readable as written, the facts the server actually holds, and a
persisted activity log that answers "I sent a fact -- what did it cascade
to?". It is for the developer standing behind the firewall, not for a
product's end users; a host builds its own screens against the
[API](http-api.md) and treats this one as the engine's own gauge panel.

## What it shows

- **Definitions.** Every declaration of every kind -- figures, readings,
  projections, summaries, the groups, filters and measures that have no
  version of their own, *and* the facts of a fact-taught world, so a trace
  bottoms out on the schema rather than on raw records. Each page shows the
  prose above the declaration, the version hash (the citation every value
  carries), the source exactly as written, and three answers: **moved by**,
  the server-computed closure to its leaves -- the fact kinds and tenant
  dials a change to which can move this number, and nothing else can;
  **built from**, the declarations it composes, one hop at a time; and
  **used by**, the reverse. Every page then shows its data for the chosen
  tenant -- a filter its matching records, a group its buckets and their
  members, a measure each record's rendered measurement -- and drills to the
  record pages themselves. For servable kinds the page also asks for the
  current answer, and a figure's rows carry an *evidence* button -- the
  stored citation, joined back to the records, and made to *lead to the
  amount*: the panel names the measure the value reads its members through,
  a sum's or an extreme's rows each show that record as the measure reads it
  now (live, like a rollup's parts -- a record corrected since the pass
  visibly disagrees with the stored total, which is true), and a part row
  opens its own citation in place, so the walk runs figure by figure down to
  the records without leaving the page. A **bundle**'s page adds the slot
  table -- each address beside its member and any declared window spans --
  and its current answer is the tile itself: every member rendered under its
  slot name by the same code that kind gets standalone, each with its own
  `name @ version` provenance, because the bundle's hash is review-only and
  cites nothing.
- **Facts.** Per kind, what the server holds -- a kind the schema declares
  but nobody has pushed appears at zero, because "nothing collected" is a
  finding. Records page by key, search over key and record text, and each row
  expands to the whole stored JSON.
- **A record's page walks both directions.** Downward: the stored document,
  where every grouping filed it, what every measure reads off it. Upward,
  which is where a verification usually starts: every figure scoped to the
  record's kind answers with this record's rows (day and dimension cells
  included, each with its evidence one click away); every leaf figure that
  counts records of this kind says whether a stored value cites this one --
  "did not count it" is stated, not inferred -- with each citing row linking
  on to *its* record's page; and every projection of the kind shows this
  record's row exactly as the page serves it, or says why it is not on it.
  Long row sets cap and say so; an unavailable figure answers with its state
  rather than an empty table.
- **Activity.** One entry per engine pass, newest first, cause before
  effect: what arrived (written/deleted counts, the kinds covered, whether it
  was a full rebuild) and then the movements it caused, each one
  `before → after` in text frozen at the moment it happened. The true
  `changed` count travels beside the capped sample, and runs that did nothing
  are hidden behind a toggle that says how many it is hiding.

The run log behind the activity view is persisted server-side (`run_log`,
capped at 1000 rows per tenant, pruned on insert) and is recorded whether or
not the UI is mounted -- the question it answers is asked after the fact by
definition.

## Editing definitions

Where the deployment grants it (see the posture below), the UI carries an
**Editor** tab: the stored `.fig` source, editable in place.

- **The compiler is the assistant.** Every pause in typing runs the same
  compile a save would (`POST /ui/api/check`, a dry run); the page shows
  either the checker's refusal verbatim, pointed at its line, or what a save
  would change -- each declaration classified `new`, `changed` or `removed`,
  where `changed` tells the cascade's truth: editing a filter marks every
  figure whose plan hashes its text in, even though their own lines are
  untouched. Completion is served from what the world knows -- fact kinds
  and their fields, declarable dials, declared names -- plus the language's
  own closed word lists.
- **A save is a teach.** `PUT /ui/api/source` compiles and persists exactly
  the way [`PUT /definitions`](http-api.md) does, fact declarations and
  world adoption included. Every save names the text it edited (a
  fingerprint), so two editors cannot silently overwrite each other -- the
  later save is refused with the state of play.
- **The loop closes with a pass.** A save that moves stored state -- a
  figure's version, a grouping's spec -- leaves exactly the changed
  groupings and figures `behind-deploy` until one runs (their untouched
  neighbours keep serving; staleness is tracked per declaration, so the
  pass rebuilds only what moved), and the saved panel says so and offers
  "run a pass" per tenant (the same pass `POST /tenants/{t}/runs` performs,
  recorded in the activity log like any other). A save that moves nothing
  stored (a label, a reading) says that instead, because offering a pass
  for it would recompute nothing.
- **It is the repair path.** A stored source this build's compiler refuses
  (an upgrade across a language change) boots the server unready; the editor
  serves the refused text with the reason and saves the correction.

Without the grant, the Editor tab is absent, `GET /ui/api/source` still
answers (read-only -- with a compiling world it serves nothing the
declaration pages don't, and with a boot-refused one it is the only place
the stored text is visible, which is exactly when the repair needs it), and
the check, save and run routes answer 403 naming `URATORI_UI_EDIT`.

Definitions edited here live in the engine's own Postgres, exactly as if the
API had taught them. A host that treats a git repository as the source of
truth and re-teaches on deploy will overwrite UI edits at its next teach --
which is why editing is a per-deployment grant, not a default.

## Security posture

The UI and its JSON (`/ui/api/*`) are **deliberately unauthenticated**. The
intended door is the network: a firewall, a private ingress, a VPN. That is
only a sound posture when it is chosen, so the default follows the token:

| `URATORI_TOKEN` | `URATORI_UI` | UI |
|---|---|---|
| unset | unset | **on** -- the API is open anyway |
| set | unset | **off** -- a token plus a silently open UI would leak everything the token guards |
| either | `on` / `off` | what you said |

`URATORI_UI` accepts `on/off/true/false/1/0/yes/no` (empty counts as unset);
anything else refuses to boot rather than guessing. Enabling the UI beside a
token is a deliberate split -- API callers authenticate, UI readers are gated
by network reach -- and turning it on does not loosen the API routes
themselves.

**Editing is a second, stricter grant.** `URATORI_UI_EDIT` follows the same
spellings and the same refuse-garbage rule, and defaults to on only where
the API itself is open -- an open server already accepts an unauthenticated
`PUT /definitions`, so its UI editing grants nothing new. Beside a token the
default is off: the API's writes are gated there, and a UI that could still
save would hand "redefine every figure" to anyone who can reach the port.
Granting it (`URATORI_UI_EDIT=on`) beside a token is for deployments whose
UI sits behind an authenticating proxy. The grant with the UI itself off is
refused at boot as the contradiction it is.

Be clear-eyed about what the read split already grants: the UI serves
*more* data than the token'd API does -- full definition source, the tenant
list, and the stored records themselves have no API equivalent at all. Anyone
who can reach the port can read everything, so "behind the firewall" has to
be true, not aspirational.

`/ui/api/*` is the UI's own contract, versioned with the page it serves, and
may change between releases without notice. Integrate against the
[documented API](http-api.md).

## Embedding it in another application

The page sends `Content-Security-Policy: frame-ancestors 'self'` by default:
nobody may iframe it. To embed it in the application hosting uratori, either

- **proxy it** -- serve `/ui/` under the host application's own origin
  through its reverse proxy, which makes the frame same-origin and keeps
  uratori itself off the public network entirely (the recommended shape); or
- **grant the origin** -- set `URATORI_UI_FRAME_ANCESTORS` to the embedding
  application's origin (e.g. `https://app.example.com`, or
  `'self' https://app.example.com`) and iframe `/ui/` directly. The value is
  pasted verbatim into the CSP directive.

There is no CORS configuration, deliberately: the page and its JSON share an
origin, so none is needed -- and its absence means no other site's scripts
can read these endpoints from a visitor's browser even while the UI itself is
unauthenticated.
