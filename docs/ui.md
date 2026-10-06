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
  the server-computed closure to its leaves -- the fact kinds a change to
  which can move this number, and nothing else can;
  **built from**, the declarations it composes, one hop at a time; and
  **used by**, the reverse. Every page then shows its data for the chosen
  tenant -- a filter its matching records, a group a chosen bucket's members
  first and its other buckets below, a measure each record's rendered
  measurement -- and drills to the
  record pages themselves. Every leaf under **moved by** is a fact kind, and
  that is the whole of it: a definition's numbers come from records, so the
  page can name the records and stop. (A leaf used to be able to be a tenant
  dial, carried with its current value beside the name, because "which dial"
  without "holding what" sent the reader elsewhere to finish the sentence.
  There are no dials.) For servable kinds the page also asks for the
  current answer. A reading's windows draw one column per **declared**
  statistic -- the column set is the definition's, stable even when a
  window is withheld, the band column sits beside exactly the statistic the
  band judges, and a declared `series` draws as bars in a column of its own.
  A window's date bounds carry **whose calendar** they are in wherever the
  answer has no single one: a calendar is a field on the subject's record,
  so two rows reading the same dates can be two different weeks -- and a
  figure's rows carry a *show work* button that expands, inline, that
  subject's worksheet (see below). A **bundle**'s page adds the slot
  table -- each address beside its member and any declared window spans --
  and its current answer is the tile itself: every member rendered under its
  slot name by the same code that kind gets standalone, each with its own
  `name @ version` provenance, because the bundle's hash is review-only and
  cites nothing.
- **An `extract`'s page** (documents-plan-v3, D4) is named bare, after the
  fact kind it targets -- by design the one case two declarations share a
  name, so the roster carries both rows and a link from either names which
  one it means (`?kind=extract`); open either and the page still answers
  **moved by**/**built from**/**used by** the same way every other kind's
  does -- built from its source page kind, the groups and filters it is
  gated `over`, and the extracts it copies fields from; used by whatever
  reads the fact kind it writes, which is the same list the fact's own page
  shows. Beside the source text, a table lists each target field with the
  matcher that reads it -- `number after`, `date after`, `text after`,
  `if page contains`, or `copy of <extract>.<field>` -- and its declared
  alternatives and units, so a reader comparing a dozen fields does not
  re-parse the declaration's prose for each one. The tenant data is three
  numbers and a list, never a re-derivation of the engine's own pass:
  records currently produced (linking on to the target kind's own Facts
  page), source pages done, and pages failed, each failure naming the
  record, the field it failed on (absent for "no row matched this page at
  all"), the reason, and a link to the page itself.
- **Facts.** Per kind, what the server holds -- a kind the schema declares
  but nobody has pushed appears at zero, because "nothing collected" is a
  finding. Records page by key, search over key and record text, and each row
  expands to the whole stored JSON. A document kind (`as document`,
  [Documents](documents.md)) carries a `document` badge and a "browse
  pages" link beside its name -- its own records are the raw metadata
  (title, mime, sha256, pages, uploaded at), which a bare JSON dump states
  but does not usefully *read*.
- **The document viewer.** `#/documents/<kind>` lists one document kind's
  uploads; `#/document/<kind>/<id>/<page>` reads one page -- the rendered
  image beside its word layer, with a substring search over the words
  already fetched for that page. A link from a record's page, a worksheet
  line or the evidence panel carries `?boxes=` -- every box of one
  `Source` (documents-plan-v3, D2/D3), page-normalised `[0,1]` -- drawn as
  highlights over the image at any render scale, percentage-positioned so
  no pixel geometry is needed at all. Gated by `URATORI_UI_DOCUMENTS` (see
  the posture below) -- unlike every other UI screen, this one serves
  bytes an `<img src>` cannot carry a bearer token alongside, so it needs
  its own grant.
- **A record's page walks both directions.** Downward: the stored document,
  where every grouping filed it, what every measure reads off it. Upward,
  which is where a verification usually starts: every figure scoped to the
  record's kind answers with this record's rows (day and dimension cells
  included, each with its evidence one click away); every reading scoped to
  the kind answers with this record's windows, the same evaluation its own
  page runs narrowed to one subject (a live reading states the sentence its
  route answers instead of vanishing); every leaf figure that
  counts records of this kind says whether a stored value cites this one --
  "did not count it" is stated, not inferred -- with each citing row linking
  on to *its* record's page; every projection of the kind shows this
  record's row exactly as the page serves it, or says why it is not on it;
  and every **bundle** with a member about this kind lists as a tile, each
  such member rendered under its slot by the same blocks its kind gets
  standalone, narrowed to this record, with its own `name @ version` --
  members about other kinds, and the page-level summarise, arrive as stated
  sentences rather than another kind's rows.
  Long row sets cap, say the true total, and open a paged walk over every
  row -- the figure's own order for computed rows, subject order for
  citations, keyset-paged so a boundary neither drops nor doubles a row; an
  unavailable figure answers with its state rather than an empty table.
  A **"Where it came from"** section (documents-plan-v3, D2/D3) lists every
  field a write's `provenance` map ever cited: the page it was read from,
  the words' own printed text, and a link to the viewer with that citation's
  boxes highlighted. Absent when nothing was ever cited, which is most
  records on a server with no documents feature.
- **A value's worksheet.** `#/work/<figure>/<subject>` shows one stored
  value's own page -- a school-child's "show your work" rather than a flat
  roster of records. The title block prints the sentence, the stored value
  large beside its version, and, when the live re-derivation disagrees, the
  sentence saying so (a record moved since the pass that wrote the row).
  Below it, the working: a tree of the calculation as declared, each step
  its expression on the left and its value on the right, nested exactly as
  deep as the calculation is. Sets show what a narrowing removed; a sum or
  extreme shows every record it read, including the ones that carried no
  measurement and so contributed nothing -- present and stated, never
  dropped; a ladder shows every rung's verdict in order, matched, failed,
  unknown or not reached; an operand that is itself a stored value opens its
  own worksheet in place with a *work* toggle, or links straight to its own
  page. Every value on the page that cites another figure -- a "computed for
  this record" row, a "counted into" row, an arithmetic operand -- links
  here instead of to the figure's general definition page, because the
  reader followed one number, not the figure's whole population. A leaf
  step that read a field directly (`field-pick`, `field-total`,
  `subject-field`) shows, beside each record it actually read, where that
  field's own value came from: a link to the page, the printed words, and a
  small crop of the page image clipped client-side to the union of the
  cited boxes (documents-plan-v3, D3) -- the box a reader checks the number
  against without leaving the worksheet. Nothing on this page is computed
  by the browser: the tree, the notes and every display are rendered by the
  one evaluator the engine itself runs; the crop is a clip of an image the
  server already rendered, not a calculation.
- **Activity.** One entry per engine pass, newest first, cause before
  effect: what arrived (written/deleted counts, the kinds covered, whether it
  was a full rebuild) and then the movements it caused, each one
  `before → after` in text frozen at the moment it happened. The true
  `changed` count travels beside the capped sample, and runs that did nothing
  are hidden behind a toggle that says how many it is hiding. The listing
  pages: "the newest 50 of 200" is a door, keyset over run ids back to the
  first kept run.

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
  and their fields, declared names -- plus the language's
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

**The document viewer is a third, separate grant.** `URATORI_UI_DOCUMENTS`
follows the same spellings and the same refuse-garbage rule, and gates page
images and word layers specifically (`/ui/api/.../documents/...`), never
the authenticated API's equivalent routes, which serve hosts regardless of
this setting. It exists apart from `URATORI_UI_EDIT` because the reason is
different: an `<img src>` cannot carry a bearer token, so the moment a
token protects the API, serving page images on the unauthenticated UI would
be a second, wide-open door into exactly the data the token exists to
guard -- a scanned medical record's page is a posture change on its own,
not a detail of "the UI is on". Default follows the same rule as
`URATORI_UI_EDIT` (on only where the UI is on AND the API itself is open);
setup.md's table has it beside `URATORI_BLOB_DIR`. Where the document
viewer is wanted beside a token, grant it explicitly and treat it the way
you would treat the editor: for a deployment whose UI sits behind its own
authenticating proxy, not for one relying on the API's own token.

The same gate decides `Source.page_url` (documents-plan-v3, D2/D3) on
every `/ui/api` route that decorates one -- the record page, the
worksheet, the UI's own evidence mirror. Without the grant every other
field of a source still renders (the field name, the printed words,
whether it agrees with the record now); there is simply nothing to click
through to a page image with. The authenticated API's `GET /evidence`
always links the authenticated page-image route regardless of this
setting, the same split as everywhere else in this document.

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
