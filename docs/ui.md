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
  bottoms out on the schema rather than on raw records. Each page shows the prose above the declaration, the
  version hash (the citation every value carries), the source exactly as
  written, and two walks: **rests on**, a tree following the declaration's
  dependencies down through figures, groups, filters and measures to the fact
  kinds and tenant dials at the leaves; and **used by**, the reverse. For servable kinds the
  page also asks for the current answer, and a figure's rows carry an
  *evidence* button -- the stored citation, joined back to the records.
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
themselves. Be clear-eyed about what the split grants, though: the UI serves
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
