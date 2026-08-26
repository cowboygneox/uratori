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
  stored citation, joined back to the records.
- **Facts.** Per kind, what the server holds -- a kind the schema declares
  but nobody has pushed appears at zero, because "nothing collected" is a
  finding. Records page by key, search over key and record text, and each row
  expands to the whole stored JSON.
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
- **The loop closes with a pass.** A save leaves every tenant honestly
  `behind-deploy` until one runs; the saved panel offers "run a pass" per
  tenant (the same pass `POST /tenants/{t}/runs` performs, recorded in the
  activity log like any other).
- **It is the repair path.** A stored source this build's compiler refuses
  (an upgrade across a language change) boots the server unready; the editor
  serves the refused text with the reason and saves the correction.

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
refused at boot as the contradiction it is. Be clear-eyed about what the split grants, though: the UI serves
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
