// The whole client. Hash-routed, framework-free, and honest about its job:
// layout and navigation. Every number, display string, band and verdict on
// screen arrived rendered from the server -- the only text this file composes
// is chrome ("3 records", "showing 40 of 217"), never a value.
//
// DOM is built with el()/text() and never innerHTML: fact records are
// arbitrary JSON from providers, and a page that interpolates them into
// markup is a page they can script.

const API = 'api';

// ------------------------------------------------------------- utilities --

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null) continue; // {disabled: undefined} means "not disabled"
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  // flat(Infinity), not flat(): views build children with nested maps
  // (a projection's dt/dd pairs, a reading's stat runs), and a depth-1
  // flatten leaves inner arrays to stringify as "[object HTMLElement]".
  for (const child of children.flat(Infinity)) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function get(path) {
  const response = await fetch(`${API}/${path}`);
  let body = null;
  try { body = await response.json(); } catch { /* a non-JSON error page */ }
  return { ok: response.ok, status: response.status, body };
}

async function send(method, path, payload) {
  const response = await fetch(`${API}/${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  let body = null;
  try { body = await response.json(); } catch { /* a non-JSON error page */ }
  return { ok: response.ok, status: response.status, body };
}

function safeDecode(text) {
  // A hand-typed '%zz' in the hash must land on the index, not a stuck page.
  try { return decodeURIComponent(text); } catch { return null; }
}

function safeUrl(candidate) {
  // Evidence URLs come out of stored provider records; only a fetchable
  // scheme may become a clickable link on this (unauthenticated) origin.
  try {
    const parsed = new URL(candidate, location.href);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
  } catch { /* not a URL at all */ }
  return null;
}

function problem(answer, sentence) {
  const detail = answer.body && answer.body.detail ? answer.body.detail : `HTTP ${answer.status}`;
  return el('div', { class: 'notice problem' }, sentence, ' ', el('span', { class: 'mono' }, detail));
}

// --------------------------------------------------------------- state --

const view = document.getElementById('view');
const tabs = document.getElementById('tabs');
const tenantSelect = document.getElementById('tenant');

let world = null;        // the /ui/api/world payload, fetched once per page load
let byName = new Map();  // declaration name -> declaration
let usedBy = new Map();  // declaration name -> [names that rest on it]

function tenant() { return localStorage.getItem('uratori.tenant') || ''; }

tenantSelect.addEventListener('change', () => {
  localStorage.setItem('uratori.tenant', tenantSelect.value);
  render();
});

async function loadTenants() {
  const answer = await get('tenants');
  if (!answer.ok) return;
  const names = answer.body.tenants.map((t) => t.tenant);
  tenantSelect.replaceChildren(
    ...names.map((name) => el('option', { value: name }, name)),
  );
  if (!names.length) {
    tenantSelect.append(el('option', { value: '' }, '(none yet)'));
    localStorage.removeItem('uratori.tenant'); // a stale choice must not outlive its tenant
  } else if (names.includes(tenant())) {
    tenantSelect.value = tenant();
  } else {
    tenantSelect.value = names[0];
    localStorage.setItem('uratori.tenant', names[0]);
  }
}

async function loadWorld() {
  const answer = await get('world');
  if (!answer.ok) return answer;
  world = answer.body;
  byName = new Map(world.declarations.map((d) => [d.name, d]));
  usedBy = new Map();
  for (const declaration of world.declarations) {
    for (const edge of declaration.rests_on) {
      if (edge.type === 'setting') continue;
      // A fact edge counts as usage when the kind is DECLARED (0.4.0 fact
      // declarations): its page must say who reads it, or the leaves every
      // trace bottoms out on would all claim nobody does.
      if (edge.type === 'fact' && !byName.has(edge.name)) continue;
      if (!usedBy.has(edge.name)) usedBy.set(edge.name, []);
      usedBy.get(edge.name).push(declaration.name);
    }
  }
  return answer;
}

// -------------------------------------------------------------- routing --

function routes() {
  const held = [
    ['#/definitions', 'Definitions'],
    ['#/facts', 'Facts'],
    ['#/activity', 'Activity'],
  ];
  // The tab exists only where the deployment grants editing -- a door drawn
  // on a wall is worse than no door, and the world payload already says.
  if (world && world.editable) held.push(['#/edit', 'Editor']);
  return held;
}

function drawTabs() {
  const here = location.hash || '#/definitions';
  tabs.replaceChildren(
    ...routes().map(([hash, label]) =>
      el('a', { href: hash, class: here.startsWith(hash) ? 'active' : '' }, label)),
  );
}

window.addEventListener('hashchange', render);

async function render() {
  drawTabs();
  const hash = location.hash || '#/definitions';
  const [, route, ...rest] = hash.split('/');
  // The query is split off BEFORE decoding. Decoding the whole tail first
  // would turn an encoded '&' or '=' inside a search term or a fact key back
  // into query syntax -- a cursor that skips records, a search answering a
  // different question.
  const raw = rest.join('/');
  const cut = raw.indexOf('?');
  const path = cut === -1 ? raw : raw.slice(0, cut);
  const params = new URLSearchParams(cut === -1 ? '' : raw.slice(cut + 1));
  // Segments are decoded one by one AFTER splitting: a record key carrying
  // an encoded '/' must stay one segment, and decoding the joined path first
  // would split it in two -- a record page for half a key.
  const segments = path ? path.split('/').map(safeDecode) : [];
  const argument = segments.length ? segments[0] : null;
  view.replaceChildren(el('p', { class: 'faint' }, 'loading…'));

  if (world === null) {
    const answer = await loadWorld();
    if (!answer.ok) {
      view.replaceChildren(problem(answer, 'This server is not ready to be investigated:'));
      return;
    }
    drawTabs(); // the Editor tab is known only once the world payload is
  }

  // flat(Infinity): a view may return nested arrays of nodes, and a nested
  // array handed to replaceChildren renders as "[object HTMLDivElement]".
  // The null filter is for the same reason el() skips nulls: a view may say
  // "nothing here" with a null, and replaceChildren would print the word.
  const draw = (nodes) => view.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null));
  if (route === 'facts') draw(await factsView(segments, params));
  else if (route === 'activity') draw(await activityView(params));
  else if (route === 'edit') draw(await editorView(params));
  else draw(await definitionsView(argument, params));
}

// -------------------------------------------------------- definitions --

const KIND_ORDER = ['bundle', 'figure', 'reading', 'projection', 'summary', 'group', 'filter', 'measure', 'fact'];

function namespaceOf(name) {
  const dot = name.indexOf('.');
  return dot === -1 ? name : name.slice(0, dot);
}

function roster(selected) {
  const list = el('div', {});
  const fill = (filter) => {
    list.replaceChildren();
    const groups = new Map();
    for (const declaration of world.declarations) {
      if (filter && !declaration.name.includes(filter)) continue;
      const group = namespaceOf(declaration.name);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(declaration);
    }
    for (const [group, members] of [...groups.entries()].sort()) {
      list.append(el('div', { class: 'kind-head' }, group));
      members.sort((a, b) =>
        KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.name.localeCompare(b.name));
      for (const declaration of members) {
        // The name is its own span so it may ellipsise; the stamp keeps the
        // row's right edge whatever the name's length.
        list.append(el('a', {
          href: `#/definitions/${encodeURIComponent(declaration.name)}`,
          class: declaration.name === selected ? 'here' : '',
          title: declaration.name, // the ellipsis needs a recovery path
        }, el('span', { class: 'name mono' },
            declaration.name.slice(group.length + 1) || declaration.name),
          el('span', { class: `badge ${declaration.kind}` }, declaration.kind)));
      }
    }
  };
  fill('');
  // The list rebuilds under a stable input, so typing never loses focus.
  const search = el('input', {
    type: 'search', placeholder: 'filter…',
    oninput: (event) => fill(event.target.value),
  });
  const holder = el('div', { class: 'roster' }, search, list);
  // The roster scrolls on its own now, and it is rebuilt on every
  // navigation, so the fresh element starts at the top. Bring the selected
  // row back into view, or clicking the fortieth entry snaps the list away
  // from it.
  queueMicrotask(() => {
    const here = holder.querySelector('.here');
    if (here && holder.isConnected) here.scrollIntoView({ block: 'nearest' });
  });
  return holder;
}

async function definitionsView(name, params) {
  const pane = name ? await declarationPane(name, params) : [libraryPlate()];
  // Pane before roster: a keyboard should reach the content in a few tabs,
  // not after all 75 roster links. The stylesheet places the roster left.
  return [el('div', { class: 'split' }, el('div', { class: 'pane' }, pane), roster(name))];
}

// The landing pane's title block: what this deployment holds, at a glance.
// The per-kind counts are chrome -- entries counted off the list the server
// sent, like the evidence view's "cites N records" and the activity view's
// "showing the newest N of M"; no value is ever derived here.
function libraryPlate() {
  const counts = new Map(KIND_ORDER.map((kind) => [kind, 0]));
  for (const declaration of world.declarations) {
    counts.set(declaration.kind, (counts.get(declaration.kind) || 0) + 1);
  }
  return el('div', {},
    el('div', { class: 'title-block' },
      el('div', { class: 'tb-head' }, el('h1', {}, 'The library')),
      el('div', { class: 'plate-counts' },
        [...counts.entries()].filter(([, n]) => n > 0).map(([kind, n]) =>
          el('span', { class: 'cell' },
            el('span', { class: 'n' }, String(n)),
            el('span', { class: `badge ${kind}` }, kind)))),
      el('div', { class: 'tb-doc' },
        el('p', { class: 'prose' },
          'Every declaration this deployment computes from, including the ',
          'groups, filters and measures that have no version of their own. ',
          'Pick one to read it as written, see what can move it, and drill ',
          'through the records it filed down to the facts themselves.'))),
    world.refusal
      ? el('div', { class: 'notice problem' },
          'Definitions are stored but refused by this build’s compiler: ',
          el('span', { class: 'mono' }, world.refusal))
      : null);
}

function defHash(name, extra) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(extra || {})) {
    if (value) params.set(key, value);
  }
  const tail = params.toString();
  return `#/definitions/${encodeURIComponent(name)}${tail ? '?' + tail : ''}`;
}

function recordHash(kind, key) {
  return `#/facts/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`;
}

// One leaf of the impact answer: where the records live, or which dial.
// A dial line carries the tenant's current value when the dials payload is
// in hand: the definition names the dial by NAME (the compile-once rule),
// and this is the value the engine reads through that name at serve time —
// both halves on one line, because "which dial" without "holding what" sends
// the reader to a settings document to finish the sentence.
function leafLine(edge, dials) {
  if (edge.type === 'fact') {
    return el('li', {},
      el('span', { class: 'badge fact' }, 'fact'), ' ',
      el('a', { class: 'mono', href: `#/facts/${encodeURIComponent(edge.name)}` }, edge.name),
      el('span', { class: 'leaf' }, ' — the records themselves'));
  }
  const held = dials instanceof Map ? dials.get(edge.name) : null;
  return el('li', {},
    el('span', { class: 'badge setting' }, 'setting'), ' ',
    el('span', { class: 'mono' }, edge.name),
    el('span', { class: 'leaf' }, ' — a tenant dial'),
    held
      ? (held.source === 'unset'
          // Declarable, defaulted nowhere, set by nobody: a stated absence,
          // in the finding voice — a definition reading a dial that holds
          // nothing is exactly what an investigator came to find.
          ? el('span', { class: 'finding' }, ' — holding no value anywhere')
          : el('span', { class: 'leaf' }, ', currently ',
              el('span', { class: 'mono' }, held.display),
              held.source === 'tenant' ? ' (set by this tenant)' : ' (the schema default)'))
      : dials === 'failed'
        ? el('span', { class: 'faint' }, ' — its current value could not be read')
        : null);
}

// One direct dependency, one hop, never a subtree. The old recursive tree
// answered "what can move this?" only after the reader walked all of it, and
// two levels down it held more entries than the library has declarations.
// That walk is the server's job now (moved_by); structure reads one hop at a
// time, by navigation.
function edgeLine(edge) {
  if (edge.type === 'fact' || edge.type === 'setting') return leafLine(edge);
  return el('li', {},
    el('span', { class: `badge ${edge.type}` }, edge.type), ' ',
    el('a', { class: 'mono', href: `#/definitions/${encodeURIComponent(edge.name)}` }, edge.name),
    byName.has(edge.name) ? null : el('span', { class: 'leaf' }, ' — not in the library?'));
}

// The keyset pager every paged section shares: `trail` carries the stack of
// prior cursors, so ← back re-asks the exact previous page rather than
// guessing at one. Returns the two buttons bare, so each caller can seat
// them in its own controls row beside the count — a pager fifty rows away
// from the total it pages is how a reader loses their place.
function pager(params, more, lastKey, go, names) {
  const afterKey = (names && names.after) || 'after';
  const trailKey = (names && names.trail) || 'trail';
  const after = params.get(afterKey);
  const crumbs = (params.get(trailKey) || '').split('|').filter((s) => s !== '');
  return [
    el('button', {
      disabled: crumbs.length === 0 && !after ? 'disabled' : undefined,
      onclick: () => {
        const previous = crumbs.pop();
        go(previous ? decodeURIComponent(previous) : null, crumbs.join('|'));
      },
    }, '← back'),
    el('button', {
      disabled: more ? undefined : 'disabled',
      onclick: () => {
        // Each crumb is encoded before joining, so a '|' inside a key can
        // never split the trail.
        crumbs.push(encodeURIComponent(after || ''));
        go(lastKey, crumbs.join('|'));
      },
    }, 'next →'),
  ];
}

function unavailable(state) {
  return el('div', { class: 'notice' },
    'Not available: ', el('span', { class: 'mono' }, state.because), ' — ',
    state.detail || 'the server gave no further sentence.');
}

async function declarationPane(name, params) {
  const declaration = byName.get(name);
  if (!declaration) {
    return [el('div', { class: 'notice problem' },
      'No declaration called ', el('span', { class: 'mono' }, name),
      ' — the library may have been redeployed since this link was made.')];
  }
  const parts = [
    // The declaration's title block: name and stamps, the authored prose,
    // and the citation line beneath -- the drawing's legend, not a heading.
    el('div', { class: 'title-block' },
      el('div', { class: 'tb-head' },
        el('h1', {}, declaration.name), ' ',
        el('span', { class: `badge ${declaration.kind}` }, declaration.kind),
        declaration.unit ? el('span', { class: 'badge' }, declaration.unit) : null,
        declaration.mode ? el('span', { class: 'badge' }, declaration.mode) : null,
        world.editable
          ? el('a', { class: 'tb-edit', href: `#/edit/?at=${encodeURIComponent(declaration.name)}` },
              'edit source')
          : null),
      declaration.doc
        ? el('div', { class: 'tb-doc' }, el('p', { class: 'prose' }, declaration.doc))
        : null,
      el('div', { class: 'tb-cite' },
        declaration.kind === 'bundle'
          // A bundle's hash is review-only: it names the composition in the
          // committed artifact and appears in no number's citation, and a
          // line calling it "the citation every value carries" would send a
          // verifier hunting for values that cite it.
          ? ['review hash ', el('span', { class: 'mono' }, declaration.version),
             ' — names the composition only; every number inside cites its ',
             'own member’s version']
          : declaration.version
            ? ['version ', el('span', { class: 'mono' }, declaration.version),
               ' — the citation every value computed by this text carries']
            : ['no version of its own — this text is hashed into every ',
               'definition that reads it, so editing it moves their versions'])),
    el('h2', {}, 'As written'),
    declaration.source
      ? el('pre', {}, declaration.source)
      : el('p', { class: 'faint' }, 'The source of this declaration could not be located.'),
  ];

  // The impact answer, precomputed by the server: leaves only, so a reader
  // learns in one glance whether a change to some data can reach this number.
  // A fact declaration IS a leaf -- saying "nothing moves it" would be the
  // wrong sentence about the records everything else moves with.
  const movedBy = declaration.moved_by || [];
  if (declaration.kind !== 'fact') {
    // The dial values, for the setting leaves below: fetched only when this
    // declaration reads a dial, and joined here by name — the joining is
    // structure (which value sits on which line), the values themselves are
    // the server's, rendered.
    let dials = null;
    if (tenant() && movedBy.some((edge) => edge.type === 'setting')) {
      const answer = await get(`tenants/${encodeURIComponent(tenant())}/dials`);
      dials = answer.ok
        ? new Map(answer.body.dials.map((dial) => [dial.name, dial]))
        : 'failed';
    }
    parts.push(el('h2', {}, 'Moved by'));
    if (movedBy.length) {
      parts.push(
        el('p', { class: 'prose' },
          'A change to any of these records or dials can move this ',
          declaration.kind, ' — nothing else can.',
          dials
            ? [' Dial values are tenant ',
               el('span', { class: 'verbatim' }, tenant()), '’s, as served right now.']
            : null),
        el('ul', { class: 'tree' }, movedBy.map((edge) => leafLine(edge, dials))));
    } else {
      parts.push(el('p', { class: 'faint' }, 'Nothing — it reads no records and no dials.'));
    }
  }

  // Structure, only when there is structure: the declarations this one
  // composes, one hop each. Leaves are left out — Moved by just stated every
  // fact and dial, and repeating them under a second heading would make the
  // two lists read as different answers to one question. For a group or a
  // measure every direct edge is a leaf, so the section vanishes entirely.
  // A bundle's structure is its slot table instead: the edges are exactly
  // the members, and the slot names — the addresses a screen binds to, the
  // thing the review hash covers — live only on `slots`.
  if (declaration.kind === 'bundle' && (declaration.slots || []).length) {
    parts.push(el('h2', {}, 'Slots'),
      el('table', { class: 'ledger' },
        el('tr', {}, el('th', {}, 'slot'), el('th', {}, 'member'), el('th', {}, 'windows')),
        declaration.slots.map((slot) => el('tr', {},
          el('td', { class: 'mono' }, slot.slot),
          el('td', {},
            el('span', { class: `badge ${slot.kind}` }, slot.kind), ' ',
            el('a', { class: 'mono', href: `#/definitions/${encodeURIComponent(slot.name)}` },
              slot.name)),
          // null windows on a windowed reading mean the serving default
          // decides — an absence to state, not an empty cell that reads as
          // "no windows". A LIVE reading is the other case: the compiler
          // refuses windows on one, so "serving default" there would promise
          // a dial that does not exist.
          el('td', { class: 'mono' },
            slot.windows
              ? slot.windows.join(', ')
              : el('span', { class: 'faint' },
                  slot.kind === 'reading'
                    ? ((byName.get(slot.name) || {}).mode === 'live'
                        ? 'live — takes none'
                        : 'serving default')
                    : '—'))))));
  } else {
    const structural = declaration.rests_on.filter(
      (edge) => edge.type !== 'fact' && edge.type !== 'setting');
    if (structural.length) {
      parts.push(el('h2', {}, 'Built from'),
        el('ul', { class: 'tree' }, structural.map((edge) => edgeLine(edge))));
    }
  }

  const dependants = usedBy.get(name) || [];
  parts.push(el('h2', {}, 'Used by'),
    dependants.length
      ? el('p', {}, dependants.map((other, i) => [
          i ? ', ' : null,
          el('a', { class: 'mono', href: `#/definitions/${encodeURIComponent(other)}` }, other),
        ]))
      : el('p', { class: 'faint' }, 'Nothing in the library reads this.'));

  if (declaration.kind === 'fact') {
    parts.push(el('h2', {}, 'Records — tenant ',
      el('span', { class: 'verbatim' }, tenant() || '?')));
    parts.push(await factCountLine(declaration));
  } else if (declaration.kind === 'group' || declaration.kind === 'filter') {
    parts.push(el('h2', {},
      declaration.kind === 'filter' ? 'Matching records — tenant ' : 'Buckets — tenant ',
      el('span', { class: 'verbatim' }, tenant() || '?')));
    parts.push(await membershipSection(declaration, params));
  } else if (declaration.kind === 'measure') {
    parts.push(el('h2', {}, 'Measurements — tenant ',
      el('span', { class: 'verbatim' }, tenant() || '?')));
    parts.push(await measuredSection(declaration, params));
  } else {
    // The tenant id rides in a verbatim span: the label style uppercases,
    // and a case-mangled identifier on this page would be a small lie.
    parts.push(el('h2', {}, 'Current answer — tenant ',
      el('span', { class: 'verbatim' }, tenant() || '?')));
    parts.push(await answerSection(declaration));
  }
  return parts;
}

// ------------------------------------------------- membership & measures --

// A fact declaration's data section: the count from the same endpoint the
// Facts tab reads, and the door to the records themselves.
async function factCountLine(declaration) {
  if (!tenant()) return el('p', { class: 'faint' }, 'No tenant to ask.');
  const kind = declaration.fact_kind || declaration.name;
  const answer = await get(`tenants/${encodeURIComponent(tenant())}/facts`);
  if (!answer.ok) return problem(answer, 'Could not count the records:');
  const entry = answer.body.kinds.find((k) => k.kind === kind);
  const held = entry ? entry.records : 0;
  if (!held) {
    return el('p', { class: 'finding' },
      'Nothing collected — declared, and no record of this kind is stored.');
  }
  return el('p', { class: 'dim' },
    `${held} records stored. `,
    el('a', { href: `#/facts/${encodeURIComponent(kind)}` }, 'Browse them →'));
}

async function membershipSection(declaration, params) {
  if (!tenant()) return el('p', { class: 'faint' }, 'No tenant to ask.');
  const bq = new URLSearchParams();
  const bafter = params.get('bafter');
  if (bafter) bq.set('buckets_after', bafter);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/membership/`
    + `${encodeURIComponent(declaration.name)}${bq.toString() ? '?' + bq : ''}`);
  if (!answer.ok) return problem(answer, 'The engine declined to answer:');
  const m = answer.body;
  if (!m.state.ok) return unavailable(m.state);
  if (m.population === 0 && m.members === 0) {
    // The pass ran and found nothing to read: the same finding sentence the
    // measured section and the facts list use, not a "0 of 0" that dresses
    // an empty collection up as a filter verdict.
    return el('p', { class: 'faint' },
      'Nothing collected for ', el('span', { class: 'mono' }, m.id_space),
      ', so there is nothing to file.');
  }

  const blocks = [];
  // Chrome, not a computation: every number in these sentences arrived from
  // the server; this only places them side by side. Under `keyed as` the
  // members are another kind's ids, so "N of M records" would compare two
  // different populations — the sentence says what the ids are instead.
  const keyed = m.fact_kind !== m.id_space;
  blocks.push(el('p', { class: 'dim' },
    keyed
      ? [`${m.members} `, el('span', { class: 'mono' }, m.id_space),
         ' ids are filed, keyed from ', el('span', { class: 'mono' }, m.fact_kind),
         ' records',
         m.kind === 'group' ? ` across ${m.buckets_total} buckets.` : '.']
      : [`${m.members} of ${m.population} `, el('span', { class: 'mono' }, m.id_space),
         m.kind === 'filter'
           ? ' records match.'
           : ` records are filed, across ${m.buckets_total} buckets.`]));
  if (m.note) blocks.push(el('p', { class: 'faint' }, m.note));

  const chosen = m.kind === 'filter' ? '' : params.get('bucket');
  if (m.kind === 'group') {
    const lastBucket = m.buckets.length ? m.buckets[m.buckets.length - 1].bucket : null;
    blocks.push(el('table', { class: 'ledger' },
      el('tr', {}, el('th', {}, 'bucket'), el('th', { class: 'num' }, 'records')),
      m.buckets.map((b) => el('tr', { class: b.bucket === chosen ? 'here' : '' },
        el('td', {}, el('a', {
          class: 'mono',
          href: defHash(declaration.name, { bucket: b.bucket, bafter, btrail: params.get('btrail') }),
          ...(b.bucket === chosen ? { 'aria-current': 'true' } : {}),
        }, b.bucket)),
        el('td', { class: 'mono num' }, String(b.members))))));
    if (m.buckets_total > m.buckets.length || m.buckets_more || params.get('btrail')) {
      blocks.push(el('div', { class: 'controls' },
        el('span', { class: 'faint' },
          `${m.buckets_total} buckets`),
        el('span', { class: 'spacer' }),
        pager(params, m.buckets_more, lastBucket, (nextAfter, trail) => {
          location.hash = defHash(declaration.name, { bafter: nextAfter, btrail: trail });
        }, { after: 'bafter', trail: 'btrail' })));
    }
  }
  if (chosen !== null) {
    blocks.push(await memberList(declaration, m, chosen, params));
  } else if (m.kind === 'group') {
    blocks.push(el('p', { class: 'faint' }, 'Pick a bucket to see the records it holds.'));
  }
  return el('div', {}, blocks);
}

async function memberList(declaration, m, bucket, params) {
  const query = new URLSearchParams();
  query.set('bucket', bucket);
  const after = params.get('after');
  if (after) query.set('after', after);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/membership/`
    + `${encodeURIComponent(declaration.name)}/members?${query}`);
  if (!answer.ok) return problem(answer, 'Could not read the members:');
  const page = answer.body;
  const lastKey = page.records.length ? page.records[page.records.length - 1].key : null;
  const go = (nextAfter, trail) => {
    location.hash = defHash(declaration.name, {
      bucket: m.kind === 'group' ? bucket : null,
      bafter: params.get('bafter'), btrail: params.get('btrail'),
      after: nextAfter, trail,
    });
  };
  return el('div', {},
    // Count and pager in one bar, above the rows: a pager fifty rows below
    // the total is a pager the reader has already lost their place by.
    el('div', { class: 'controls' },
      el('span', { class: 'dim' },
        m.kind === 'group'
          ? ['bucket ', el('span', { class: 'mono' }, bucket), ` — ${page.total} records`]
          : `${page.total} records`),
      el('span', { class: 'spacer' }),
      pager(params, page.more, lastKey, go)),
    page.records.length
      ? el('table', { class: 'ledger' },
          el('tr', {}, el('th', {}, 'key'), el('th', {}, 'name')),
          page.records.map((record) => el('tr', {},
            el('td', {}, record.held
              // The drill's next rung: every member links to the record
              // itself, so "which records?" ends at the fact, not at a key.
              ? el('a', { class: 'mono', href: recordHash(m.id_space, record.key) }, record.key)
              : [el('span', { class: 'mono' }, record.key),
                 el('span', { class: 'faint' }, ' (no record stored)')]),
            el('td', {}, record.name ?? el('span', { class: 'faint' }, '—')))))
      : el('p', { class: 'faint' },
          // Reachable by a hand-typed cursor: the population is above, so
          // "nothing" here means past-the-cursor, and ← back is the way out.
          page.total ? 'No records past this cursor.' : 'Nothing here.'));
}

async function measuredSection(declaration, params) {
  if (!tenant()) return el('p', { class: 'faint' }, 'No tenant to ask.');
  const after = params.get('after');
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/measured/${encodeURIComponent(declaration.name)}`
    + (after ? `?after=${encodeURIComponent(after)}` : ''));
  if (!answer.ok) return problem(answer, 'The engine declined to answer:');
  const page = answer.body;
  if (!page.total) {
    // total, not this page's rows: an empty page past a cursor is not an
    // empty population, and claiming "nothing collected" over 17,000 records
    // strands the reader on a lie.
    return el('p', { class: 'faint' },
      'Nothing collected for ', el('span', { class: 'mono' }, page.fact_kind),
      ', so there is nothing to measure.');
  }
  const lastKey = page.records.length ? page.records[page.records.length - 1].key : null;
  const go = (nextAfter, trail) => {
    location.hash = defHash(declaration.name, { after: nextAfter, trail });
  };
  return el('div', {},
    el('div', { class: 'controls' },
      el('span', { class: 'dim' },
        `${page.total} `, el('span', { class: 'mono' }, page.fact_kind), ' records'),
      el('span', { class: 'spacer' }),
      pager(params, page.more, lastKey, go)),
    page.records.length
      ? el('table', { class: 'ledger' },
          el('tr', {}, el('th', {}, 'key'), el('th', {}, 'name'),
            el('th', { class: 'num' }, 'measures as')),
          page.records.map((record) => el('tr', {},
            el('td', {}, el('a', {
              class: 'mono', href: recordHash(page.fact_kind, record.key),
            }, record.key)),
            el('td', {}, record.name ?? el('span', { class: 'faint' }, '—')),
            // null is the server saying "no measurement for this record" — an
            // absence to show as one, never a rendered nought.
            el('td', { class: 'mono num' },
              record.display ?? el('span', { class: 'faint' }, '— no measurement')))))
      : el('p', { class: 'faint' }, 'No records past this cursor.'));
}

async function answerSection(declaration) {
  if (!tenant()) return el('p', { class: 'faint' }, 'No tenant to ask.');
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/results/${encodeURIComponent(declaration.name)}`);
  if (!answer.ok) return problem(answer, 'The engine declined to answer:');
  const result = answer.body;
  if (result.kind === 'bundle') return el('div', {}, bundleBlocks(result));
  // The trailer only under an answer: "answered … under version …" is the
  // citation sentence reserved for a computed number, and printing it under
  // a not-available notice would stamp an absence as answered.
  return el('div', {}, resultBlocks(result),
    result.state.ok
      ? el('p', { class: 'faint' },
          'answered ', el('span', { class: 'mono' }, result.at),
          ' under version ', el('span', { class: 'mono' }, result.version))
      : null);
}

// A bundle's answer: each member's ordinary Result under its slot name,
// rendered by the same code the member gets standalone — composition, never
// a second renderer that could disagree with the first. Provenance is per
// member (name @ version), because the tile's hash cites nothing.
function bundleBlocks(result) {
  const blocks = [];
  for (const member of result.results) {
    const r = member.result;
    blocks.push(el('h2', { class: 'subject' }, member.slot));
    blocks.push(el('p', { class: 'faint' },
      el('span', { class: `badge ${r.kind}` }, r.kind), ' ',
      el('a', { class: 'mono', href: defHash(r.name) }, r.name),
      el('span', { class: 'mono' }, ` @ ${r.version}`)));
    blocks.push(resultBlocks(r));
  }
  blocks.push(el('p', { class: 'faint' },
    'answered ', el('span', { class: 'mono' }, result.at),
    ' — tile hash ', el('span', { class: 'mono' }, result.version),
    ', review-only; every number above cites its own member’s version'));
  return blocks;
}

// One ordinary Result's content, shared by the standalone answer section and
// every bundle member. Availability first, always: an unavailable member on a
// tile states its reason exactly as it would alone. Evidence is fetched by
// the RESULT's own name — inside a bundle the declaration on screen is the
// tile, and asking the evidence route for the tile's name would 404.
function resultBlocks(result) {
  if (!result.state.ok) {
    return [el('div', { class: 'notice' },
      'Not available: ', el('span', { class: 'mono' }, result.state.because), ' — ',
      result.state.detail || 'the server gave no further sentence.')];
  }

  const blocks = [];
  if (result.kind === 'projection' || result.kind === 'summary') {
    // Rows first, then the population row. A summarise member arrives with
    // NO subject rows and its one row in `summary` — that emptiness is the
    // shape working as declared, not "computed for nobody".
    for (const subject of result.subjects) {
      if (!subject.row) continue;
      // Each dt/dd pair rides in a div (the dl content model allows it), so
      // a wrap happens between pairs, never between a label and its value.
      blocks.push(el('div', { class: 'kv' },
        el('dl', { class: 'kv' },
          el('div', { class: 'pair' }, el('dt', {}, 'row'), el('dd', {}, subject.name)),
          Object.entries(subject.row.display).map(([column, text]) =>
            el('div', { class: 'pair' }, el('dt', {}, column), el('dd', {}, text)))),
        subject.row.flags.map((flag) =>
          el('div', { class: flag.severity === 'attention' ? 'dim' : 'faint' },
            `⚑ ${flag.label} — ${flag.detail}`))));
    }
    if (result.summary) {
      blocks.push(
        result.kind === 'summary' ? null : el('h2', {}, 'Summary'),
        el('dl', { class: 'kv' },
          Object.entries(result.summary.display).map(([column, text]) =>
            el('div', { class: 'pair' }, el('dt', {}, column), el('dd', {}, text)))),
        // The sentences the population row earned. The subject rows above
        // render theirs; dropping these made the flagged finding — often the
        // only thing distinguishing a healthy summary from a broken one —
        // silently invisible.
        result.summary.flags.map((flag) =>
          el('div', { class: flag.severity === 'attention' ? 'dim' : 'faint' },
            `⚑ ${flag.label} — ${flag.detail}`)),
        // On a projection, this row is computed by the summarise DECLARED
        // over it — a different definition with a version of its own, which
        // this payload does not carry. Said out loud, because the citation
        // beside these rows is the projection's and must not be read as
        // covering a number it did not compute.
        result.kind === 'summary'
          ? null
          : el('p', { class: 'faint' },
              'Computed by the summarise declared over this page — its number ',
              'cites that definition, not this projection.'));
    }
    if (!result.subjects.length && !result.summary) {
      blocks.push(el('p', { class: 'faint' }, 'Computed, and there are no rows.'));
    }
    return blocks;
  }

  if (result.subjects.length === 0) {
    blocks.push(result.empty
      ? el('p', { class: 'faint' },
          'Computed for nobody in particular: ',
          el('span', { class: 'mono' }, result.empty.display ?? '—'))
      : el('p', { class: 'faint' }, 'Computed, and there are no subjects.'));
  } else if (result.kind === 'figure') {
    // The band column exists only when the definition claims to band --
    // a column of "unknown" under a bandless figure is a stated absence
    // the definition never made.
    blocks.push(el('table', {},
      el('tr', {}, el('th', {}, 'subject'), el('th', {}, 'value'),
        result.banded ? el('th', {}, 'band') : null, el('th', {})),
      result.subjects.map((subject) => {
        const row = el('tr', {},
          el('td', {}, subject.name, ' ', el('span', { class: 'faint mono' }, subject.id),
            subject.dimension
              ? el('span', { class: 'dim' }, ` × ${subject.dimension}`) : null),
          // The dash, never the raw number: formatting is the server's job,
          // and a value with no display has no client-side rescue.
          el('td', { class: 'mono' }, subject.display ?? '—'),
          result.banded ? el('td', { class: 'mono dim' }, subject.level) : null,
          el('td', {}, el('button', {
            onclick: () => evidenceRow(row, result.name, subject.id),
          }, 'evidence')));
        return row;
      })));
  } else if (result.kind === 'reading') {
    // One column per DECLARED statistic (result.statistics -- the wire's
    // own spelling, `sum` travelling as `total`), never a union of whatever
    // display keys happen to be present: a withheld window carries no
    // values, and a table shaped by presence would silently narrow itself
    // the day every window fell short. The band column sits immediately
    // after the one statistic the band judges (result.banded_on) -- the
    // word is that column's verdict, never a tint on the statistics beside
    // it -- and trails the row only if the banded statistic is somehow not
    // declared, so the verdict always has a home.
    const stats = result.statistics || [];
    const bandOn = result.banded ? result.banded_on : null;
    const bandCell = (window) => el('td', { class: 'mono dim' }, window.level);
    // A factory, not a shared array: DOM nodes appended twice MOVE, so a
    // header built once would vanish from every table but the last one.
    const statHeads = () => {
      const heads = stats.map((stat) => [
        el('th', { class: stat === bandOn ? 'banded-stat' : '' }, stat),
        stat === bandOn ? el('th', { title: `the band judges ${bandOn}` }, 'band') : null,
      ]);
      if (result.banded && !stats.includes(bandOn)) heads.push(el('th', {}, 'band'));
      return heads;
    };
    const bandCols = result.banded ? 1 : 0;
    for (const subject of result.subjects) {
      // class 'subject': the drawing-label h2 uppercases, and this text is
      // a server-rendered name that must appear verbatim.
      blocks.push(el('h2', { class: 'subject' }, subject.name));
      blocks.push(el('table', {},
        el('tr', {}, el('th', {}, 'window'), statHeads(),
          el('th', {}, 'sample'), el('th', {}, 'coverage')),
        (subject.windows || []).map((window) => el('tr', {},
          // The span with its bucket unit -- `30d`, `31-60d`, `1-48h` --
          // never `trailing`, which is null for any span that is not a
          // plain trailing-days count.
          el('td', { class: 'mono' }, `${window.span}${{ day: 'd', hour: 'h', minute: 'm' }[window.bucket] || ''}`, ' ',
            el('span', { class: 'faint' }, `${window.frm} → ${window.to}`)),
          window.unmet.length
            // Every statistic is withheld together, so one cell spans the
            // columns with the reason -- the columns still exist (the
            // header holds their names); this is an absence stated where
            // the values would sit, never a blank that reads as computed.
            ? el('td', { class: 'faint', colspan: String(stats.length + bandCols) },
                window.unmet.join('; '))
            : [
                stats.map((stat) => [
                  stat === 'series'
                    ? el('td', {}, sparkline(window))
                    : el('td', { class: 'mono' }, window.display[stat] ?? '—'),
                  stat === bandOn ? bandCell(window) : null,
                ]),
                result.banded && !stats.includes(bandOn) ? bandCell(window) : null,
              ],
          el('td', { class: 'mono' }, String(window.sample)),
          el('td', { class: 'mono dim' }, `${window.days_covered}/${window.days_requested}d`)))));
    }
  }
  return blocks;
}

// A series, drawn: one bar per point, heights scaled to the window's own
// tallest. Positional only -- the wire sends the numbers exactly so a bar
// can have a width or a height (Subject.value's documented purpose), and no
// numeral is ever composed from them here. A null point is a gap with its
// own look: an absence drawn as an absence, never a zero-height bar.
function sparkline(window) {
  const points = window.series;
  if (!points || !points.length) return el('span', { class: 'faint' }, '—');
  const held = points.filter((value) => value != null);
  const peak = held.length ? Math.max(...held) : 0;
  return el('span', {
    class: 'spark',
    title: `${points.length} points, one per ${window.series_by || 'day'}`,
  }, points.map((value) => {
    if (value == null) return el('span', { class: 'spark-gap' });
    const bar = el('span', { class: 'spark-bar' });
    bar.style.height = `${peak > 0 ? Math.max(8, (value / peak) * 100) : 8}%`;
    return bar;
  }));
}

async function evidenceRow(row, figure, subject) {
  if (row.nextSibling && row.nextSibling.classList.contains('expansion')) {
    row.nextSibling.remove(); // second click folds it back up
    return;
  }
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/evidence/${encodeURIComponent(figure)}`
    + `?subject=${encodeURIComponent(subject)}`);
  const holder = el('td', { colspan: String(row.children.length) });
  const expansion = el('tr', { class: 'expansion' }, holder);
  if (!answer.ok) {
    holder.append(problem(answer, 'No evidence:'));
  } else {
    holder.append(evidencePanel(answer.body));
  }
  row.after(expansion);
}

function evidencePanel(evidence) {
  // The part drill fetches evidence for figures no surrounding table has
  // availability-gated, so the gate lives here: without it a never-computed
  // or dial-moved part renders "This value cites nothing." -- the confident
  // claim the state field exists to prevent.
  if (!evidence.state.ok) return unavailable(evidence.state);
  // Through el(), not bare append(): append() stringifies a null child
  // into the visible word "null", el() skips it.
  return el('div', {}, el('p', { class: 'dim' },
    evidence.members.length
      ? [`This value cites ${evidence.members.length} ${evidence.parts ? 'parts' : 'records'}`,
         // The definition the numbers travel through, named on the panel:
         // "these records, measured as this definition says" is what makes
         // the rows lead to the amount rather than merely sit under it.
         // "measured as", tense-neutral on purpose: a list row's numbers are
         // the stored addends, a sum's or an extreme's are read live.
         evidence.measure
           ? [', each measured as ', el('a', {
               class: 'mono', href: `#/definitions/${encodeURIComponent(evidence.measure)}`,
             }, evidence.measure), ':']
           : ':']
      : 'This value cites nothing.'),
    evidence.note ? el('p', { class: 'faint' }, evidence.note) : null,
    el('ul', {}, evidence.members.map((member) => {
      const link = member.url ? safeUrl(member.url) : null;
      const item = el('li', { class: 'mono' },
        member.figure
          ? [el('a', { class: 'faint', href: `#/definitions/${encodeURIComponent(member.figure)}` },
              member.figure), el('span', { class: 'faint' }, ' · ')]
          : null,
        link
          ? el('a', { href: link, target: '_blank', rel: 'noreferrer' },
              member.title || member.key)
          : (member.title || member.key),
        // The cell a part is for, or twenty-seven season rows of one team
        // all read as the same frozen label.
        member.dimension ? el('span', { class: 'dim' }, ` × ${member.dimension}`) : null,
        member.display ? el('span', { class: 'dim' }, ` — ${member.display}`) : null,
        member.held ? null : el('span', { class: 'faint' }, ' (no longer held)'),
        // The citation's last rung: when the members are records of one
        // kind, each held one links to the record itself, so a value can
        // be walked to the stored fact without leaving the trace.
        evidence.kind && member.held
          ? [' ', el('a', { class: 'trace', href: recordHash(evidence.kind, member.key) },
              'record →')]
          : null,
        // A part is a stored value of its own, so the walk continues: its
        // citation opens in place, and the trace runs figure by figure down
        // to the records without leaving the page.
        member.figure && member.held
          ? [' ', el('button', { onclick: () => partDrill(item, member) }, 'evidence')]
          : null);
      return item;
    })));
}

async function partDrill(item, member) {
  const open = item.querySelector(':scope > .expansion');
  if (open) { open.remove(); return; } // second click folds it back up
  // Two fast clicks would both pass the check above before either fetch
  // lands, stacking two panels the toggle then removes one at a time.
  if (item.dataset.drilling) return;
  item.dataset.drilling = '1';
  try {
    const answer = await get(
      `tenants/${encodeURIComponent(tenant())}/evidence/${encodeURIComponent(member.figure)}`
      + `?subject=${encodeURIComponent(member.key)}`);
    const nested = el('div', { class: 'expansion' },
      answer.ok ? evidencePanel(answer.body) : problem(answer, 'No evidence:'));
    item.append(nested);
  } finally {
    delete item.dataset.drilling;
  }
}

// A self-contained paged browser in an expansion row: the door behind a
// capped entry. The pages come from the server already ordered and totalled;
// this walks them with a local cursor trail, so ← back re-asks the exact
// previous page. Second click on the opener folds it away.
async function pagedExpansion(anchorRow, span, fetchPage) {
  if (anchorRow.nextSibling && anchorRow.nextSibling.classList.contains('expansion')) {
    anchorRow.nextSibling.remove();
    return;
  }
  const holder = el('td', { colspan: span });
  const expansion = el('tr', { class: 'expansion' }, holder);
  anchorRow.after(expansion);
  const trail = [];
  let cursor = null;
  const draw = async () => {
    holder.replaceChildren(el('p', { class: 'faint' }, 'loading…'));
    const page = await fetchPage(cursor);
    if (!expansion.isConnected) return;
    if (!page.ok) { holder.replaceChildren(page.problem); return; }
    holder.replaceChildren(
      el('div', { class: 'controls' },
        el('span', { class: 'dim' }, page.summary),
        el('span', { class: 'spacer' }),
        el('button', {
          disabled: trail.length ? undefined : 'disabled',
          onclick: () => { cursor = trail.pop() ?? null; draw(); },
        }, '← back'),
        el('button', {
          disabled: page.more ? undefined : 'disabled',
          onclick: () => { trail.push(cursor); cursor = page.lastKey; draw(); },
        }, 'next →')),
      el('table', { class: 'ledger' }, page.header, page.rows));
  };
  draw();
}

// One page of a figure's rows for one record, drawn like the overview's own
// rows — value, band, evidence — from the same narrowed serving the
// overview entry is cut from. The order sentence is part of the page: a
// pager over an unstated order is an arbitrary subset with buttons.
async function computedPage(kind, key, figure, banded, after) {
  const query = new URLSearchParams();
  if (after) query.set('after', after);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/computed/${encodeURIComponent(figure.name)}`
    + `/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`
    + (query.toString() ? `?${query}` : ''));
  if (!answer.ok) return { ok: false, problem: problem(answer, 'Could not page the rows:') };
  const body = answer.body;
  const served = body.result;
  const rows = served.subjects.map((subject) => {
    const row = el('tr', {},
      el('td', {}, el('span', { class: 'faint mono' }, subject.id),
        subject.dimension ? el('span', { class: 'dim' }, ` × ${subject.dimension}`) : null),
      el('td', { class: 'mono num' }, subject.display ?? '—'),
      banded ? el('td', { class: 'mono dim' }, served.banded ? subject.level : '') : null,
      el('td', {}, el('button', {
        onclick: () => evidenceRow(row, served.name, subject.id),
      }, 'evidence')));
    return row;
  });
  return {
    ok: true,
    summary: `${rows.length} of ${body.total} rows — the figure’s own order, oldest first`,
    header: el('tr', {}, el('th', {}, 'row'), el('th', { class: 'num' }, 'value'),
      banded ? el('th', {}, 'band') : null, el('th', {})),
    rows,
    lastKey: served.subjects.length ? served.subjects[served.subjects.length - 1].id : null,
    more: body.more,
  };
}

// One page of the stored rows of one figure that counted this record — the
// continuation of the overview's sample, in the same subject order.
async function citedPage(kind, key, entry, after) {
  const query = new URLSearchParams();
  if (after) query.set('after', after);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/cited/${encodeURIComponent(entry.figure)}`
    + `/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`
    + (query.toString() ? `?${query}` : ''));
  if (!answer.ok) return { ok: false, problem: problem(answer, 'Could not page the citations:') };
  const body = answer.body;
  const rows = body.rows.map((row) => el('tr', {},
    el('td', {}, el('a', { href: recordHash(body.scope, row.subject) }, row.name),
      ' ', el('span', { class: 'faint mono' }, row.subject),
      row.dimension ? el('span', { class: 'dim' }, ` × ${row.dimension}`) : null),
    el('td', { class: 'mono num' }, row.display ?? '—')));
  return {
    ok: true,
    summary: `${rows.length} of ${body.total} citations — subject order`,
    header: el('tr', {}, el('th', {}, 'whose row'), el('th', { class: 'num' }, 'value')),
    rows,
    lastKey: body.rows.length ? body.rows[body.rows.length - 1].id : null,
    more: body.more,
  };
}

// --------------------------------------------------------------- facts --

async function factsView(segments, params) {
  if (!tenant()) {
    return [el('h1', {}, 'Facts'),
      el('p', { class: 'faint' }, 'No tenant holds any facts yet.')];
  }
  // A segment that failed to decode is null; asking the server for a record
  // literally called "null" would be a page about a key nobody wrote.
  // A hand-typed '%zz' lands on the kind list, as safeDecode promises.
  if (segments.some((segment) => segment === null)) return kindListView();
  const kind = segments.length ? segments[0] : null;
  if (!kind) return kindListView();
  if (segments.length > 1) {
    // A hand-typed literal '/' in a key splits into extra segments; joining
    // them back is the only honest recovery.
    return recordView(kind, segments.slice(1).join('/'));
  }
  return kindView(kind, params.get('q') || '', params.get('after'), params.get('trail') || '');
}

// Which declarations a change to records of this kind can reach — the
// inverse of the server's moved_by, inverted here because it is structure
// (the same composition usedBy already does), never a value.
function moversOf(kind) {
  return world.declarations.filter((declaration) =>
    (declaration.moved_by || []).some((edge) => edge.type === 'fact' && edge.name === kind));
}

function moverLinks(kind) {
  const movers = moversOf(kind);
  if (!movers.length) return null;
  // A chip row, not a comma-run: twenty-odd names in serif prose read as a
  // paragraph to skim past; a wrapped list of mono chips reads as the index
  // it is.
  return el('div', {},
    el('p', { class: 'faint' }, 'A change to these records can move:'),
    // Each chip wears its kind, like the roster: a bundle and a figure in
    // one unlabelled list read as the same sort of thing, and they are not.
    el('ul', { class: 'movers' }, movers.map((declaration) =>
      el('li', {}, el('a', {
        class: 'mono', href: `#/definitions/${encodeURIComponent(declaration.name)}`,
      }, declaration.name), ' ',
        el('span', { class: `badge ${declaration.kind}` }, declaration.kind)))));
}

async function recordView(kind, key) {
  // Two requests, together: the stored half (the document, the filings, the
  // measurements) and the derived half (figures, citations, pages). The
  // second evaluates projections, so it prices differently — but the page
  // is one story and waits for both rather than reflowing under the reader.
  const path = `${encodeURIComponent(kind)}/${encodeURIComponent(key)}`;
  const [answer, aboutAnswer] = await Promise.all([
    get(`tenants/${encodeURIComponent(tenant())}/facts/${path}`),
    get(`tenants/${encodeURIComponent(tenant())}/about/${path}`),
  ]);
  if (!answer.ok) return [problem(answer, 'Could not read this record:')];
  const record = answer.body;
  const link = record.url ? safeUrl(record.url) : null;

  // One h1 per page — the record's name in the title block. The way back up
  // is a quiet crumb line, not a second heading louder than the name itself.
  const parts = [
    el('nav', { class: 'crumbs' },
      el('a', { href: '#/facts' }, 'Facts'), ' / ',
      el('a', { class: 'mono', href: `#/facts/${encodeURIComponent(kind)}` }, kind), ' / ',
      el('span', { class: 'mono' }, key)),
    el('div', { class: 'title-block' },
      el('div', { class: 'tb-head' },
        el('h1', {}, record.name ?? key), ' ',
        el('span', { class: 'badge fact' }, 'fact'),
        el('span', { class: 'badge' }, kind)),
      el('div', { class: 'tb-cite' },
        record.source_stamp
          ? ['source stamp ', el('span', { class: 'mono' }, record.source_stamp),
             ' — the provider’s own version of this record']
          : 'no source stamp — the provider did not date this record',
        link ? [' · ', el('a', { href: link, target: '_blank', rel: 'noreferrer' },
            'open at the source')] : null)),
  ];

  // Both verdict sections are tenant-scoped, and must say so the way every
  // definition page does — switching the tenant switches every verdict here.
  const bare = !world.declarations.length;
  parts.push(el('h2', {}, 'Filed under — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (bare) {
    parts.push(el('p', { class: 'faint' },
      'No definitions are loaded, so there is no classification to report.'));
  } else if (!record.filed_state.ok) {
    parts.push(unavailable(record.filed_state));
  } else if (!record.filed.length) {
    parts.push(el('p', { class: 'faint' },
      'No group or filter is keyed by ', el('span', { class: 'mono' }, kind), ' ids.'));
  } else {
    // Every grouping over this kind reports, the rejections included: "this
    // filter did not take it" is exactly the verdict a verifier came to check.
    parts.push(el('table', { class: 'ledger' },
      el('tr', {}, el('th', {}, 'declaration'), el('th', {}, 'kind'),
        el('th', {}, 'verdict')),
      record.filed.map((filed) => el('tr', {},
        el('td', {}, el('a', {
          class: 'mono', href: `#/definitions/${encodeURIComponent(filed.index)}`,
        }, filed.index)),
        el('td', {}, el('span', { class: `badge ${filed.kind}` }, filed.kind)),
        el('td', { class: 'mono' },
          filed.kind === 'filter'
            ? (filed.member ? 'matches'
               : el('span', { class: 'dim' }, 'does not match'))
            : (filed.member
               ? ['in ', filed.buckets.map((bucket, i) => [
                   i ? ', ' : null,
                   el('a', { class: 'mono', href: defHash(filed.index, { bucket }) }, bucket),
                 ])]
               : el('span', { class: 'dim' }, 'in no bucket')))))));
  }

  parts.push(el('h2', {}, 'Measured as — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (bare) {
    parts.push(el('p', { class: 'faint' },
      'No definitions are loaded, so there is nothing to measure with.'));
  } else if (!record.measured.length) {
    parts.push(el('p', { class: 'faint' },
      'No measure reads ', el('span', { class: 'mono' }, kind), ' records.'));
  } else {
    parts.push(el('table', { class: 'ledger' },
      el('tr', {}, el('th', {}, 'measure'), el('th', { class: 'num' }, 'value')),
      record.measured.map((entry) => el('tr', {},
        el('td', {}, el('a', {
          class: 'mono', href: `#/definitions/${encodeURIComponent(entry.measure)}`,
        }, entry.measure)),
        el('td', { class: 'mono num' },
          entry.display ?? el('span', { class: 'faint' }, '— no measurement'))))));
  }

  // The upward half: what the library made of this record. Three verdicts,
  // each stated when empty — a section silently missing would read as
  // "nothing derives from this", which is a claim only the server may make.
  parts.push(el('h2', {}, 'Computed for this record — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (!aboutAnswer.ok) {
    parts.push(problem(aboutAnswer, 'Could not read the derived values:'));
  } else if (!aboutAnswer.body.state.ok) {
    // No library at all: saying "no figure is scoped to this kind" here
    // would be a verdict about definitions that do not exist.
    parts.push(unavailable(aboutAnswer.body.state));
  } else if (!aboutAnswer.body.figures.length) {
    parts.push(el('p', { class: 'faint' },
      'No figure is scoped to ', el('span', { class: 'mono' }, kind),
      ', so nothing is computed per record of this kind.'));
  } else {
    const banded = aboutAnswer.body.figures.some((f) => f.result.banded);
    parts.push(el('table', { class: 'ledger' },
      el('tr', {}, el('th', {}, 'figure'), el('th', { class: 'num' }, 'value'),
        banded ? el('th', {}, 'band') : null, el('th', {})),
      aboutAnswer.body.figures.map((entry) => {
        const figure = entry.result;
        const cite = el('a', { class: 'mono', href: defHash(figure.name) }, figure.name);
        if (!figure.state.ok) {
          return el('tr', {}, el('td', {}, cite),
            el('td', { colspan: banded ? '3' : '2', class: 'dim' },
              `not available: ${figure.state.because} — ${figure.state.detail || ''}`));
        }
        if (!figure.subjects.length) {
          // Available and no row: the pass's roster did not include this
          // record — computed for others, not (yet) for this one.
          return el('tr', {}, el('td', {}, cite),
            el('td', { colspan: banded ? '3' : '2', class: 'faint' },
              'no stored value for this record'));
        }
        const rows = figure.subjects.map((subject, i) => {
          const row = el('tr', {},
            el('td', {}, i ? el('span', { class: 'faint mono' }, '〃') : cite,
              subject.dimension
                ? el('span', { class: 'dim' }, ` × ${subject.dimension}`) : null),
            el('td', { class: 'mono num' }, subject.display ?? '—'),
            banded ? el('td', { class: 'mono dim' },
              figure.banded ? subject.level : '') : null,
            el('td', {}, el('button', {
              onclick: () => evidenceRow(row, figure.name, subject.id),
            }, 'evidence')));
          return row;
        });
        // A by-day figure holds dozens of cells per subject; folded, so the
        // single-value figures below it stay on screen. Folded, not paged:
        // the rows are already here, and the count on the button is the
        // honest size of what a click reveals.
        if (rows.length > 7) {
          const folded = rows.splice(6);
          folded.forEach((row) => row.classList.add('folded'));
          const toggle = el('tr', {}, el('td', { colspan: banded ? '4' : '3' },
            el('button', {
              onclick: () => { folded.forEach((row) => row.classList.remove('folded'));
                               toggle.remove(); },
            }, `show all ${figure.subjects.length} rows`)));
          rows.push(toggle, ...folded);
        }
        // After the fold, so the cap is stated on the collapsed view too —
        // a capped set whose disclosure hides behind its own fold reads as
        // complete, which is the lie this row exists to prevent. The true
        // total is the server's, and the browse button opens the paged walk
        // over every row — the cap keeps this overview light, it no longer
        // seals the rest away.
        if (entry.more) {
          const span = banded ? '4' : '3';
          const capRow = el('tr', {}, el('td', { colspan: span, class: 'faint' },
            `… the latest ${figure.subjects.length} of ${entry.total} rows — `,
            el('button', {
              onclick: () => pagedExpansion(capRow, span, (after) =>
                computedPage(kind, key, figure, banded, after)),
            }, `browse all ${entry.total}, paged`)));
          rows.push(capRow);
        }
        return rows;
      })));
  }

  // The readings scoped to this kind, narrowed to this record — the same
  // evaluation each reading's own page runs, this subject's rows picked
  // out, rendered by the same blocks. A reading the engine cannot serve
  // (live, today) states the route's own sentence instead of vanishing.
  parts.push(el('h2', {}, 'Readings — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (!aboutAnswer.ok) {
    parts.push(el('p', { class: 'faint' }, 'Unavailable (see above).'));
  } else if (!aboutAnswer.body.state.ok) {
    parts.push(unavailable(aboutAnswer.body.state));
  } else if (!aboutAnswer.body.readings.length) {
    parts.push(el('p', { class: 'faint' },
      'No reading is scoped to ', el('span', { class: 'mono' }, kind), '.'));
  } else {
    for (const entry of aboutAnswer.body.readings) {
      if (!entry.result) {
        parts.push(el('p', { class: 'faint' },
          entry.note ?? 'This reading cannot be served.'));
        continue;
      }
      const r = entry.result;
      parts.push(el('p', { class: 'faint' },
        el('span', { class: 'badge reading' }, 'reading'), ' ',
        el('a', { class: 'mono', href: defHash(r.name) }, r.name),
        el('span', { class: 'mono' }, ` @ ${r.version}`)));
      if (r.state.ok && !r.subjects.length) {
        // Computed, and this record earned no windows: its source figure
        // holds no days for it — a per-record absence the reading's own
        // page would show by this subject simply not appearing.
        parts.push(el('p', { class: 'faint' },
          'Computed, and there are no rows for this record.'));
      } else {
        parts.push(resultBlocks(r));
      }
    }
  }

  parts.push(el('h2', {}, 'Counted into — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (!aboutAnswer.ok) {
    parts.push(el('p', { class: 'faint' }, 'Unavailable (see above).'));
  } else if (!aboutAnswer.body.state.ok) {
    parts.push(unavailable(aboutAnswer.body.state));
  } else if (!aboutAnswer.body.cited.length) {
    parts.push(el('p', { class: 'faint' },
      'No figure counts ', el('span', { class: 'mono' }, kind), ' records.'));
  } else {
    const counted = aboutAnswer.body.cited.filter((c) => c.rows.length || !c.state.ok);
    const idle = aboutAnswer.body.cited.filter((c) => !c.rows.length && c.state.ok);
    if (counted.length) {
      parts.push(el('table', { class: 'ledger' },
        el('tr', {}, el('th', {}, 'figure'), el('th', {}, 'whose row'),
          el('th', { class: 'num' }, 'value')),
        counted.map((entry) => {
          const cite = el('a', { class: 'mono', href: defHash(entry.figure) }, entry.figure);
          if (!entry.state.ok) {
            return el('tr', {}, el('td', {}, cite),
              el('td', { colspan: '2', class: 'dim' },
                `not available: ${entry.state.because} — ${entry.state.detail || ''}`));
          }
          const rows = entry.rows.map((row, i) => el('tr', {},
            el('td', {}, i ? el('span', { class: 'faint mono' }, '〃') : cite),
            // The subject links to its own record page: the trace keeps
            // walking up — a play to its team, the team to its figures.
            el('td', {}, el('a', { href: recordHash(entry.scope, row.subject) }, row.name),
              ' ', el('span', { class: 'faint mono' }, row.subject),
              row.dimension
                ? el('span', { class: 'dim' }, ` × ${row.dimension}`) : null),
            el('td', { class: 'mono num' }, row.display ?? '—')));
          if (entry.more) {
            // The first rows in subject order ARE this walk's first page,
            // so the browse below continues rather than repeats.
            const capRow = el('tr', {}, el('td', { colspan: '3', class: 'faint' },
              `… the first ${entry.rows.length} of ${entry.total} citations — `,
              el('button', {
                onclick: () => pagedExpansion(capRow, '3', (after) =>
                  citedPage(kind, key, entry, after)),
              }, `browse all ${entry.total}, paged`)));
            rows.push(capRow);
          }
          return rows;
        })));
    }
    if (idle.length) {
      // The stated rejections, folded to a line: "this figure did not count
      // it" is a verdict, and per-figure empty tables would bury the ones
      // that did.
      parts.push(el('p', { class: 'faint' }, 'Did not count it: ',
        idle.map((entry, i) => [i ? ', ' : null,
          el('a', { class: 'mono', href: defHash(entry.figure) }, entry.figure)])));
    }
  }

  parts.push(el('h2', {}, 'On the pages — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (!aboutAnswer.ok) {
    parts.push(el('p', { class: 'faint' }, 'Unavailable (see above).'));
  } else if (!aboutAnswer.body.state.ok) {
    parts.push(unavailable(aboutAnswer.body.state));
  } else if (!aboutAnswer.body.pages.length) {
    // Stated, like every other empty section: a heading that vanished
    // would read as "nothing derives from this", a server-only claim.
    parts.push(el('p', { class: 'faint' },
      'No projection is of ', el('span', { class: 'mono' }, kind), ' records.'));
  } else {
    for (const page of aboutAnswer.body.pages) {
      parts.push(el('h2', { class: 'subject' },
        el('a', { class: 'mono', href: defHash(page.projection) }, page.projection)));
      if (!page.state.ok) {
        parts.push(unavailable(page.state));
      } else if (!page.present) {
        parts.push(el('p', { class: 'faint' }, page.note ?? 'Not on this page.'));
      } else {
        parts.push(el('div', { class: 'kv' },
          el('dl', { class: 'kv' },
            Object.entries(page.row.row.display).map(([column, text]) =>
              el('div', { class: 'pair' }, el('dt', {}, column), el('dd', {}, text)))),
          page.row.row.flags.map((flag) =>
            el('div', { class: flag.severity === 'attention' ? 'dim' : 'faint' },
              `⚑ ${flag.label} — ${flag.detail}`))));
      }
    }
  }

  // The tiles: every bundle with a member about this record's kind, served
  // whole by the server and narrowed to this record there — each member
  // rendered by the same blocks its kind gets standalone, under its slot,
  // with its own name @ version, exactly like the tile's own page. Members
  // whose rows are another kind's, and the page-level summarise, arrive as
  // sentences instead of rows; both are stated, never skipped.
  parts.push(el('h2', {}, 'On the tiles — tenant ',
    el('span', { class: 'verbatim' }, tenant() || '?')));
  if (!aboutAnswer.ok) {
    parts.push(el('p', { class: 'faint' }, 'Unavailable (see above).'));
  } else if (!aboutAnswer.body.state.ok) {
    parts.push(unavailable(aboutAnswer.body.state));
  } else if (!aboutAnswer.body.tiles.length) {
    parts.push(el('p', { class: 'faint' },
      'No bundle has a member about ', el('span', { class: 'mono' }, kind), ' records.'));
  } else {
    for (const tile of aboutAnswer.body.tiles) {
      parts.push(el('h2', { class: 'subject' },
        el('a', { class: 'mono', href: defHash(tile.bundle) }, tile.bundle), ' ',
        el('span', { class: 'badge bundle' }, 'bundle')));
      if (tile.note) {
        parts.push(el('div', { class: 'notice' },
          'This tile cannot be served: ', el('span', { class: 'mono' }, tile.note)));
      }
      for (const member of tile.members) {
        parts.push(el('p', { class: 'faint' },
          el('span', { class: 'mono' }, member.slot), ' — ',
          el('span', { class: `badge ${member.kind}` }, member.kind), ' ',
          el('a', { class: 'mono', href: defHash(member.name) }, member.name),
          el('span', { class: 'mono' }, ` @ ${member.version}`)));
        if (member.note) {
          parts.push(el('p', { class: 'faint' }, member.note));
        }
        if (member.result) {
          if (member.result.state.ok && !member.result.subjects.length && !member.note) {
            parts.push(el('p', { class: 'faint' },
              'Computed, and there are no rows for this record.'));
          } else if (member.result.subjects.length || !member.result.state.ok) {
            parts.push(resultBlocks(member.result));
          }
          if (member.more) {
            // The figure member keeps the latest rows, like every capped
            // entry here; the full walk lives under Computed for this
            // record, which pages the same figure's rows for this record.
            parts.push(el('p', { class: 'faint' },
              `… the latest ${member.result.subjects.length} of ${member.total} rows — `,
              'the same figure is paged in full under Computed for this record.'));
          }
        }
      }
      parts.push(el('p', { class: 'faint' },
        'tile hash ', el('span', { class: 'mono' }, tile.version),
        ', review-only; every number above cites its own member’s version'));
    }
  }

  parts.push(el('h2', {}, 'Can move'),
    moverLinks(kind)
      ?? el('p', { class: 'faint' }, 'Nothing in the library reads these records.'));

  parts.push(el('h2', {}, 'As stored'),
    el('pre', {}, JSON.stringify(record.value, null, 2)));
  return parts;
}

async function kindListView() {
  const answer = await get(`tenants/${encodeURIComponent(tenant())}/facts`);
  if (!answer.ok) return [problem(answer, 'Could not list fact kinds:')];
  return [
    el('h1', {}, 'Facts'),
    el('p', { class: 'prose' },
      'What the server actually holds, per kind — the bottom of every trace. ',
      'A kind at zero is a finding: declared in the schema, never collected.'),
    el('table', {},
      el('tr', {}, el('th', {}, 'kind'), el('th', {}, 'records')),
      answer.body.kinds.map((entry) => el('tr', {},
        el('td', {}, el('a', {
          class: 'mono', href: `#/facts/${encodeURIComponent(entry.kind)}`,
        }, entry.kind)),
        // A kind at zero is a finding, so it must not be the dimmest thing
        // on the page -- the warm ink is the same voice the unversioned
        // stamps use: look here, something structural.
        el('td', { class: entry.records ? 'mono' : 'mono finding' },
          String(entry.records),
          entry.records ? '' : el('span', { class: 'faint' }, ' — nothing collected'))))),
  ];
}

function factsHash(kind, q, after, trail) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (after) params.set('after', after);
  if (trail) params.set('trail', trail);
  const tail = params.toString();
  return `#/facts/${encodeURIComponent(kind)}${tail ? '?' + tail : ''}`;
}

async function kindView(kind, q, after, trail) {
  const params = new URLSearchParams();
  if (after) params.set('after', after);
  if (q) params.set('q', q);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/facts/${encodeURIComponent(kind)}`
    + (params.toString() ? `?${params}` : ''));
  if (!answer.ok) return [problem(answer, 'Could not read these records:')];
  const page = answer.body;
  const crumbs = trail ? trail.split('|').filter((s) => s !== '') : [];

  const search = el('input', {
    type: 'search', placeholder: 'search key or record text…', value: q,
    onkeydown: (event) => {
      if (event.key === 'Enter') location.hash = factsHash(kind, event.target.value, null, '');
    },
  });

  const rows = page.records.map((record) => {
    // tabindex + keydown: the expansion is the only way to read a record,
    // so it cannot be mouse-only.
    const row = el('tr', { class: 'record-row', tabindex: '0', 'aria-expanded': 'false' },
      // The key is the door to the record's own page (classification,
      // measurements, the full JSON); the row click stays the quick peek.
      // stopPropagation so following the link does not also toggle the row.
      el('td', {}, el('a', {
        class: 'mono', href: recordHash(kind, record.key),
        onclick: (event) => event.stopPropagation(),
      }, record.key)),
      el('td', {}, record.name ?? el('span', { class: 'faint' }, '—')),
      el('td', { class: 'mono faint' }, record.source_stamp ?? '—'));
    const toggle = () => {
      if (row.nextSibling && row.nextSibling.classList.contains('expansion')) {
        row.nextSibling.remove();
        row.setAttribute('aria-expanded', 'false');
        return;
      }
      row.after(el('tr', { class: 'expansion' },
        el('td', { colspan: '3' },
          el('pre', {}, JSON.stringify(record.value, null, 2)))));
      row.setAttribute('aria-expanded', 'true');
    };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (event) => {
      // Only when the ROW itself holds focus: a keydown bubbling up from the
      // key's link would otherwise be cancelled here, and Enter on that link
      // would toggle the peek instead of navigating -- the whole drill made
      // mouse-only.
      if (event.target !== row) return;
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
    });
    return row;
  });

  const lastKey = page.records.length ? page.records[page.records.length - 1].key : null;
  return [
    el('h1', {}, el('a', { href: '#/facts' }, 'Facts'), ' / ', el('span', { class: 'mono' }, kind)),
    moverLinks(kind),
    el('div', { class: 'controls' },
      search,
      el('span', { class: 'faint' },
        q ? `${page.total} matching records` : `${page.total} records`),
      el('span', { class: 'spacer' }),
      el('button', {
        disabled: crumbs.length === 0 && !after ? 'disabled' : undefined,
        onclick: () => {
          const previous = crumbs.pop();
          location.hash = factsHash(
            kind, q, previous ? decodeURIComponent(previous) : null, crumbs.join('|'));
        },
      }, '← back'),
      el('button', {
        disabled: page.more ? undefined : 'disabled',
        onclick: () => {
          // Each crumb is encoded before joining, so a '|' inside a key can
          // never split the trail.
          crumbs.push(encodeURIComponent(after || ''));
          location.hash = factsHash(kind, q, lastKey, crumbs.join('|'));
        },
      }, 'next →')),
    page.records.length
      ? el('table', {},
          el('tr', {}, el('th', {}, 'key'), el('th', {}, 'name'), el('th', {}, 'source stamp')),
          rows)
      : el('p', { class: 'faint' },
          q ? 'Nothing matches.' : 'Nothing collected for this kind.'),
    el('p', { class: 'faint' }, 'Click a record to see everything the server stores for it.'),
  ];
}

// ------------------------------------------------------------ activity --

async function activityView(params) {
  if (!tenant()) {
    return [el('h1', {}, 'Activity'), el('p', { class: 'faint' }, 'No tenant has run yet.')];
  }
  const quiet = sessionStorage.getItem('uratori.quiet') === '1';
  const query = new URLSearchParams();
  if (quiet) query.set('quiet', '1');
  const after = params.get('after');
  if (after) query.set('after', after);
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/activity${query.toString() ? '?' + query : ''}`);
  if (!answer.ok) return [problem(answer, 'Could not read the run log:')];
  const page = answer.body;

  const toggle = el('label', { class: 'faint' },
    el('input', {
      type: 'checkbox', ...(quiet ? { checked: '' } : {}),
      onchange: (event) => {
        sessionStorage.setItem('uratori.quiet', event.target.checked ? '1' : '0');
        render();
      },
    }),
    ' show runs that did nothing',
    !quiet && page.quiet_hidden
      ? ` (${page.quiet_hidden} hidden)` : '');

  // An enum-to-English map with a verbatim fallback: a future cause must
  // surface as itself, never be mislabelled as one of today's two.
  const TRIGGERS = {
    facts: 'facts arrived',
    'facts-deferred': 'batch landed, pass deferred',
    run: 'manual run',
  };

    // A table, not stacked flex rows: the whole point of the log is
    // comparing movements, and comparison needs the befores under each
    // other and the afters under each other.
  const runs = page.runs.map((run) => {
    const moves = run.shown.length ? el('table', { class: 'moves' },
      run.shown.map((change) => el('tr', {
        class: change.kind === 'removed' ? 'move removed' : 'move',
      },
        el('td', {}, el('a', {
          class: 'mono',
          href: `#/definitions/${encodeURIComponent(change.figure)}`,
        }, change.figure)),
        el('td', { class: 'dim' }, change.label,
          change.kind === 'removed' ? [' ', el('span', { class: 'badge' }, 'removed')] : null),
        el('td', { class: 'mono faint' }, change.before_display),
        el('td', { class: 'arrow' }, '→'),
        el('td', { class: 'after' }, change.after_display)))) : null;

    return el('div', { class: 'run' },
      el('div', { class: 'head' },
        el('span', { class: 'when' }, run.at),
        el('span', { class: 'badge' }, TRIGGERS[run.trigger] ?? run.trigger),
        run.full ? el('span', { class: 'badge' }, 'full rebuild') : null,
        el('span', { class: 'dim' },
          `${run.written} written · ${run.deleted} deleted · ${run.changed} moved`),
        run.covered.length
          ? el('span', { class: 'faint mono' }, run.covered.join(', '))
          : null),
      run.rebuilt.length
        ? el('p', { class: 'faint' }, 'rebuilt: ',
            el('span', { class: 'mono' }, run.rebuilt.join(', ')))
        : null,
      moves ?? el('p', { class: 'faint' }, 'No figure moved.'),
      run.not_shown > 0
        ? el('p', { class: 'faint' },
            `${run.not_shown} more moved and are not listed — the log stores `
            + 'a capped sample per pass, removals first, then the heaviest movements.')
        : null);
  });

  // Keyset over run ids, newest first — the log's own order. The pager and
  // the count share the controls row, and "the newest 50 of 200" is now a
  // door: the kept log pages back to its first retained run.
  const lastId = page.runs.length ? String(page.runs[page.runs.length - 1].id) : null;
  return [
    el('h1', {}, 'Activity'),
    el('p', { class: 'prose' },
      'Cause before effect: each pass with the movements it caused, frozen ',
      'when they happened. Push a fact and the newest run says what it cascaded to.'),
    el('div', { class: 'controls' }, toggle,
      page.total > page.runs.length
        ? el('span', { class: 'faint' },
            after
              ? `${page.runs.length} of ${page.total} runs, newest first`
              : `showing the newest ${page.runs.length} of ${page.total} runs`)
        : null,
      el('span', { class: 'spacer' }),
      page.more || after || params.get('trail')
        ? pager(params, page.more, lastId, (nextAfter, trail) => {
            const next = new URLSearchParams();
            if (nextAfter) next.set('after', nextAfter);
            if (trail) next.set('trail', trail);
            location.hash = `#/activity${next.toString() ? '?' + next : ''}`;
          })
        : null),
    runs.length
      ? runs
      : el('p', { class: 'faint' },
          page.total ? 'No runs past this cursor.' : 'No runs recorded for this tenant.'),
  ];
}

// --------------------------------------------------------------- editor --
//
// The drafting table. The stored source, editable in place: highlighted as
// the lexer reads it, completed from what the world already knows, checked
// against the real compiler as it is typed, and saved through the same teach
// the API performs. The one calculation this section still never does is a
// value's: the compile, the diff and every verdict come from the server; the
// browser's contributions are colours, caret positions and word lists.

// The language's closed vocabularies, mirrored from the parser. Mirrored
// rather than served because they are compile-time constants of the engine
// build this page shipped inside -- the world-dependent lists (kinds, fields,
// dials, declared names) DO arrive from the server, on /ui/api/source.
const FIG_DECLS = ['fact', 'group', 'filter', 'measure', 'figure', 'reading', 'projection', 'summarise', 'bundle'];
const FIG_SECTIONS = {
  fact: ['name', 'url', 'one', 'many'],
  figure: ['display', 'unit', 'depends', 'combine', 'calculate', 'band'],
  reading: ['display', 'band', 'depends', 'requires', 'calculate'],
  projection: ['from', 'field', 'read', 'value', 'flag', 'omit', 'sort', 'limit'],
  summarise: ['count', 'total', 'value', 'flag'],
};
const FIG_UNITS = ['share', 'days', 'effort', 'count', 'duration'];
const FIG_FACT_TYPES = ['text', 'number', 'flag', 'moment'];
const FIG_FIELD_TYPES = ['text', 'date', 'number', 'flag'];
const FIG_WORDS = new Set([
  ...FIG_DECLS.filter((word) => word !== 'fact'),
  ...Object.values(FIG_SECTIONS).flat(),
  'from', 'where', 'keyed', 'as', 'through', 'by', 'in', 'label', 'is', 'set', 'not',
  'older', 'younger', 'than', 'against', 'on', 'at', 'least', 'values', 'over',
  'when', 'then', 'otherwise', 'now', 'days', 'moment', 'ascending', 'descending',
  'detail', 'action', 'severity', 'info', 'attention', 'true', 'false',
  'mean', 'median', 'worst', 'sum', 'series', 'list', 'latest', 'earliest', 'max', 'min',
  ...FIG_UNITS, ...FIG_FACT_TYPES, ...FIG_FIELD_TYPES,
  'hour', 'hours', 'minute', 'minutes', 'day',
]);

// The draft outlives the view: a tenant switch or a wander to the Facts tab
// re-renders everything, and an hour of drafting must not ride on nobody
// touching the chrome. Cleared by a save, superseded by somebody else's.
let draft = null; // { base: fingerprint the draft edits, text }

window.addEventListener('beforeunload', (event) => {
  if (draft) event.preventDefault(); // the browser words the warning itself
});

// One line, tokenised for colour only. Correctness lives in the server's
// compiler; this must merely never mislead -- so it mirrors the lexer's
// simple truths (strings, `#` outside a string, dots inside one name) and
// classifies words by position, since the language reserves no keywords.
function tokenizeLine(line, first) {
  const out = [];
  let i = 0;
  const flush = (to, cls) => { if (to > i) { out.push([line.slice(i, to), cls]); i = to; } };
  const indent = /^\s*/.exec(line)[0].length;
  flush(indent, null);
  let atWord = 0; // which word of the line we are on, for position classes
  while (i < line.length) {
    const ch = line[i];
    if (ch === '#') { flush(line.length, 'cmt'); break; }
    if (ch === '"') {
      let j = i + 1;
      while (j < line.length && line[j] !== '"') j += line[j] === '\\' ? 2 : 1;
      flush(Math.min(j + 1, line.length), 'str');
      continue;
    }
    if (/[0-9]/.test(ch)) {
      let j = i;
      while (j < line.length && /[0-9.]/.test(line[j])) j += 1;
      flush(j, 'num');
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let j = i;
      while (j < line.length && /[A-Za-z0-9_.]/.test(line[j])) j += 1;
      const word = line.slice(i, j);
      atWord += 1;
      let cls = null;
      if (first && indent === 0 && atWord === 1 && FIG_DECLS.includes(word)) cls = 'kw';
      else if (word.includes('.')) cls = 'ref';
      else if (FIG_WORDS.has(word)) cls = 'sec';
      flush(j, cls);
      continue;
    }
    if (/[&|:={}()<>!,+*/-]/.test(ch)) { flush(i + 1, 'op'); continue; }
    flush(i + 1, null);
  }
  return out;
}

function highlightInto(hl, text, errorLine) {
  const lines = text.split('\n');
  hl.replaceChildren(...lines.map((line, index) =>
    el('div', { class: `ed-line${index + 1 === errorLine ? ' err' : ''}` },
      // Fixed-height rows in the stylesheet keep an empty line from
      // collapsing and drifting every line below it off the textarea's grid.
      tokenizeLine(line, true).map(([piece, cls]) =>
        cls ? el('span', { class: `tok-${cls}` }, piece) : piece))));
}

async function editorView(params) {
  const answer = await get('source');
  if (!answer.ok) return [problem(answer, 'Nothing to edit yet:')];
  const page = answer.body;
  if (!page.editable) {
    return [el('h1', {}, 'Editor'),
      el('div', { class: 'notice' },
        'This deployment does not grant editing from the UI — the source is ',
        'readable under Definitions, and the operator can grant editing with ',
        el('span', { class: 'mono' }, 'URATORI_UI_EDIT=on'), '.')];
  }

  // What the completion knows. `names` learns from every green check, so a
  // figure drafted a minute ago completes inside the next one.
  const vocab = {
    kinds: page.kinds,
    dials: page.dials,
    names: new Map(page.declarations.map((d) => [d.name, d.kind])),
  };

  let base = page.fingerprint;
  let restored = false;
  let orphan = null;
  let text = page.source;
  if (draft && draft.base === base && draft.text !== page.source) {
    text = draft.text;
    restored = true;
  } else if (draft && draft.base !== base) {
    // Drafted against text that has since been replaced. Dropping it
    // silently would eat work the beforeunload guard promised to protect;
    // restoring it silently would hide that the ground moved. So it is
    // held aside and the choice is stated.
    orphan = draft;
    draft = null;
  }

  // ---- the surface -------------------------------------------------------
  const gutterInner = el('div', { class: 'ed-lines mono' });
  const gutter = el('div', { class: 'ed-gutter', 'aria-hidden': 'true' }, gutterInner);
  const hl = el('pre', { class: 'ed-hl mono', 'aria-hidden': 'true' });
  const input = el('textarea', {
    class: 'ed-input mono', spellcheck: 'false', autocapitalize: 'off',
    autocomplete: 'off', wrap: 'off', 'aria-label': 'definitions source',
  });
  input.value = text;
  const popup = el('ul', { class: 'ed-popup', role: 'listbox', hidden: '' });
  const scroll = el('div', { class: 'ed-scroll' }, hl, input, popup);
  const shell = el('div', { class: 'ed-shell' }, gutter, scroll);

  const status = el('span', { class: 'ed-status faint', 'aria-live': 'polite' }, 'checking…');
  const saveButton = el('button', { class: 'ed-save', onclick: () => save() }, 'Save');
  const discard = el('button', {
    hidden: restored ? undefined : '',
    onclick: () => {
      draft = null;
      input.value = page.source;
      discard.hidden = true;
      onEdit();
    },
  }, 'discard draft');
  const bar = el('div', { class: 'controls ed-bar' },
    status, el('span', { class: 'spacer' }), discard, saveButton);
  const report = el('div', { class: 'ed-report' });
  const outcome = el('div', {});

  let lineHeight = 21;
  let charWidth = 8.4;

  function renumber() {
    const count = input.value.split('\n').length;
    if (gutterInner.childElementCount === count) return;
    gutterInner.replaceChildren(...Array.from({ length: count }, (_, index) =>
      el('div', { class: 'ed-no' }, String(index + 1))));
  }

  function markError(line) {
    for (const [index, node] of [...gutterInner.children].entries()) {
      node.classList.toggle('err', index + 1 === line);
    }
  }

  function sync() {
    hl.scrollTop = input.scrollTop;
    hl.scrollLeft = input.scrollLeft;
    gutterInner.style.transform = `translateY(${-input.scrollTop}px)`;
  }

  function caretTo(offset, line) {
    input.focus();
    input.setSelectionRange(offset, offset);
    input.scrollTop = Math.max(0, (line - 1) * lineHeight - input.clientHeight / 3);
    sync();
  }

  function lineOffset(line) {
    const lines = input.value.split('\n');
    let offset = 0;
    for (let i = 0; i < Math.min(line - 1, lines.length - 1); i += 1) offset += lines[i].length + 1;
    return offset;
  }

  // ---- the check loop ----------------------------------------------------
  // Debounced against typing, sequenced against itself: a slow answer about
  // an old draft must never repaint over a fresh one.
  let checkTimer = null;
  let checkSeq = 0;
  let errorAt = null; // the checked refusal's line, kept on the highlight

  function paint() {
    highlightInto(hl, input.value, errorAt);
    renumber();
    markError(errorAt);
    sync();
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    status.textContent = 'checking…';
    status.className = 'ed-status faint';
    checkTimer = setTimeout(runCheck, 500);
  }

  async function runCheck() {
    const seq = ++checkSeq;
    const source = input.value;
    const out = await send('POST', 'check', { source });
    if (seq !== checkSeq || !shell.isConnected) return;
    if (!out.ok) {
      status.textContent = 'the check could not run';
      status.className = 'ed-status finding';
      report.replaceChildren(problem(out, 'The check itself failed:'));
      return;
    }
    applyCheck(out.body, source);
  }

  function applyCheck(checked, source) {
    if (!checked.ok) {
      const refusal = checked.refusal;
      errorAt = refusal.line;
      paint();
      status.textContent = refusal.line == null
        ? 'does not compile'
        : `does not compile — line ${refusal.line}`;
      status.className = 'ed-status finding';
      report.replaceChildren(el('div', { class: 'notice problem ed-refusal' },
        el('p', { class: 'mono ed-message' },
          refusal.line == null ? null : [el('a', {
            class: 'mono', href: '#',
            onclick: (event) => {
              event.preventDefault();
              const offset = lineOffset(refusal.line) + (refusal.column ?? 0);
              caretTo(offset, refusal.line);
            },
          }, `line ${refusal.line}`), ' — '],
          refusal.message)));
      return;
    }
    errorAt = null;
    paint();
    const moves = checked.declarations.filter((d) => d.change !== 'unchanged');
    const untouched = checked.declarations.length - moves.length;
    status.textContent = `compiles — ${checked.declarations.length} declarations`;
    status.className = 'ed-status good';
    // The compile just proved these names; let the completion learn them,
    // and forget the ones this draft removed.
    for (const declaration of checked.declarations) {
      if (declaration.change === 'removed') vocab.names.delete(declaration.name);
      else vocab.names.set(declaration.name, declaration.kind);
    }
    if (!moves.length) {
      // Through el(), never bare replaceChildren with a possible null:
      // replaceChildren stringifies a null into the visible word.
      report.replaceChildren(el('div', {},
        checked.adoption ? el('p', { class: 'finding' }, checked.adoption) : null,
        el('p', { class: 'faint' },
          !checked.declarations.length && source.trim() === ''
            ? 'Nothing is taught yet — write the first definitions and save.'
            : source === page.source && base === page.fingerprint
              ? 'Unchanged from what is taught.'
              : 'No calculation moves — the edit is prose or display only.')));
      return;
    }
    report.replaceChildren(el('div', { class: 'ed-diff' },
      checked.adoption ? el('p', { class: 'finding' }, checked.adoption) : null,
      el('p', { class: 'dim' }, 'What a save would change:'),
      el('ul', {}, moves.map((d) => el('li', {},
        el('span', { class: `badge change-${d.change}` }, d.change), ' ',
        el('span', { class: `badge ${d.kind}` }, d.kind), ' ',
        el('span', { class: 'mono' }, d.name)))),
      untouched ? el('p', { class: 'faint' }, `${untouched} declarations untouched.`) : null));
  }

  // ---- saving ------------------------------------------------------------
  async function save() {
    if (saveButton.disabled) return; // Cmd+S while a save is in flight
    completionClose();
    saveButton.disabled = true;
    saveButton.textContent = 'saving…';
    const source = input.value;
    const out = await send('PUT', 'source', { source, expected: base });
    saveButton.disabled = false;
    saveButton.textContent = 'Save';
    if (!shell.isConnected) return;
    if (out.status === 409 && out.body && typeof out.body.detail === 'string') {
      // Two explicit ways forward, neither silent: keep the draft and aim it
      // at the new base (a knowing overwrite -- the other save's text is
      // readable under Definitions), or yield to what was saved.
      outcome.replaceChildren(el('div', { class: 'notice problem' },
        el('p', {}, out.body.detail),
        el('div', { class: 'ed-runs' },
          el('button', {
            onclick: async () => {
              const fresh = await get('source');
              if (!fresh.ok) { outcome.replaceChildren(problem(fresh, 'Could not reload:')); return; }
              base = fresh.body.fingerprint;
              page.source = fresh.body.source;
              page.fingerprint = base;
              draft = { base, text: input.value };
              outcome.replaceChildren(el('p', { class: 'dim' },
                'Retargeted at the latest save. Saving now knowingly replaces ',
                'it — its text is readable under Definitions first.'));
            },
          }, 'Keep my draft, retarget it'),
          el('button', {
            onclick: async () => {
              const fresh = await get('source');
              if (!fresh.ok) { outcome.replaceChildren(problem(fresh, 'Could not reload:')); return; }
              base = fresh.body.fingerprint;
              page.source = fresh.body.source;
              page.fingerprint = base;
              input.value = fresh.body.source;
              draft = null;
              outcome.replaceChildren();
              onEdit();
            },
          }, 'Discard my draft, load theirs'))));
      return;
    }
    if (out.status === 422 && out.body && out.body.detail) {
      applyCheck({ ok: false, refusal: out.body.detail, declarations: [] }, source);
      outcome.replaceChildren();
      return;
    }
    if (!out.ok) {
      outcome.replaceChildren(problem(out, 'The save was refused:'));
      return;
    }
    base = out.body.fingerprint;
    page.source = source;
    page.fingerprint = base;
    // Keystrokes that landed while the save was in flight are NOT in what
    // was saved; forgetting them here would disarm the unsaved-draft guard
    // while the screen still shows text the server never received.
    if (input.value === source) {
      draft = null;
      discard.hidden = true;
    } else {
      draft = { base, text: input.value };
      discard.hidden = false;
    }
    // The library moved; the other tabs must not keep describing the old
    // one, and the stale "what a save would change" panel must re-check.
    await loadWorld();
    drawTabs();
    scheduleCheck();
    outcome.replaceChildren(await savedPanel(out.body));
  }

  async function savedPanel(saved) {
    const moves = saved.declarations.filter((d) => d.change !== 'unchanged');
    const tenants = await get('tenants');
    if (!tenants.ok) {
      // A failed read is not an empty world: saying "no tenants" here would
      // be a fabricated absence over a fetch that never answered.
      return el('div', { class: 'notice ed-saved' },
        el('p', {}, 'Saved.'),
        problem(tenants, 'Could not list the tenants to offer a pass:'));
    }
    const names = tenants.body.tenants.map((t) => t.tenant);
    // `stale` is the server's verdict, not a client inference from the diff:
    // a changed label serves immediately and owes no pass, and offering one
    // would send every tenant through a rebuild that moves nothing.
    return el('div', { class: 'notice ed-saved' },
      saved.adoption ? el('p', { class: 'finding' }, saved.adoption) : null,
      el('p', {},
        saved.stale
          ? `Saved. ${moves.length} declaration${moves.length === 1 ? '' : 's'} moved — `
            + 'the changed groupings and figures answer behind-deploy for every '
            + 'tenant until its next pass (the rest keep serving). '
            + 'Run one now, or let the next facts push do it:'
          : moves.length
            ? `Saved. ${moves.length} declaration${moves.length === 1 ? '' : 's'} moved and `
              + `${moves.length === 1 ? 'serves its' : 'serve their'} new text immediately `
              + '— nothing stored needs recomputing.'
            : 'Saved. No calculation moved, so nothing needs recomputing.'),
      saved.stale && names.length
        ? el('div', { class: 'ed-runs' }, names.map((name) => {
            const line = el('span', {});
            const button = el('button', {
              onclick: async () => {
                button.disabled = true;
                button.textContent = `running — ${name}…`;
                const ran = await send('POST', `tenants/${encodeURIComponent(name)}/runs`, {});
                if (!ran.ok) {
                  button.remove();
                  line.append(problem(ran, `The pass for ${name} failed:`));
                  return;
                }
                button.remove();
                // "values moved", not "moved": the saved panel above counts
                // declarations, and one word doing both jobs reads as the
                // two numbers disagreeing.
                line.append(el('span', { class: 'dim' },
                  `${name}: ${ran.body.changed} values moved`,
                  ran.body.rebuilt.length ? `, rebuilt ${ran.body.rebuilt.length} groupings` : '',
                  ' — ', el('a', { href: '#/activity' }, 'see the pass')));
              },
            }, `Run a pass — ${name}`);
            return el('span', { class: 'ed-run' }, button, line);
          }))
        : null,
      saved.stale && !names.length
        ? el('p', { class: 'faint' }, 'No tenants yet, so there is nothing to recompute.')
        : null);
  }

  // ---- completion --------------------------------------------------------
  let items = [];
  let active = 0;
  let wordStart = 0;

  function completionClose() {
    popup.hidden = true;
    items = [];
  }

  function enclosingDeclaration(uptoLine) {
    const lines = input.value.split('\n');
    for (let i = uptoLine; i >= 0; i -= 1) {
      const line = lines[i];
      if (!line || /^\s/.test(line) || line.startsWith('#')) continue;
      const m = /^([a-z]+)\s+([A-Za-z0-9_.]+)/.exec(line);
      if (m && FIG_DECLS.includes(m[1])) return { kw: m[1], name: m[2], at: i };
      return null;
    }
    return null;
  }

  function sectionAbove(uptoLine, declLine) {
    const lines = input.value.split('\n');
    for (let i = uptoLine; i > declLine; i -= 1) {
      const m = /^\s+([a-z]+)\s*:\s*(#.*)?$/.exec(lines[i]);
      if (m) return m[1];
    }
    return null;
  }

  function fieldsOf(kind) {
    return (vocab.kinds[kind] || []).map((field) => [field, 'field']);
  }

  function namesOf(...kinds) {
    const out = [];
    for (const [name, kind] of vocab.names) {
      if (kinds.includes(kind)) out.push([name, kind]);
    }
    return out;
  }

  function kindPaths() {
    const out = [];
    for (const [kind, fields] of Object.entries(vocab.kinds)) {
      for (const field of fields) out.push([`${kind}.${field}`, 'field']);
    }
    return out;
  }

  // What may be written here. Deliberately modest: it reads one line and one
  // enclosing block, offers the closed lists the parser will accept, and
  // leaves being *right* to the compiler running underneath.
  function candidates() {
    const caret = input.selectionStart;
    const before = input.value.slice(0, caret);
    const lineStart = before.lastIndexOf('\n') + 1;
    const lineNo = (before.match(/\n/g) || []).length;
    let start = caret;
    while (start > lineStart && /[A-Za-z0-9_.]/.test(input.value[start - 1])) start -= 1;
    const prefix = input.value.slice(start, caret);
    const head = input.value.slice(lineStart, start);
    const decl = enclosingDeclaration(lineNo);
    const ownLine = decl && decl.at === lineNo;
    const kindOf = decl ? decl.name.split('.')[0] : null;
    const prev = (/([A-Za-z0-9_.]+)\s+$/.exec(head)
      || /([&|(=])\s*$/.exec(head) || [])[1] || null;

    let list = null;
    if (head.trim() === '' && !/^\s/.test(head + prefix) ) {
      list = FIG_DECLS.map((word) => [word, 'kw']);
    } else if (head.trim() === '' && decl && !ownLine) {
      list = (FIG_SECTIONS[decl.kw] || []).map((word) => [word, 'kw']);
      if (decl.kw === 'fact') list.push(...FIG_FACT_TYPES.map((w) => [w, 'kw']));
    } else if (prev === 'through') {
      list = kindPaths();
    } else if (prev === 'where') {
      list = kindOf ? fieldsOf(kindOf) : [];
    } else if (prev === 'when') {
      // Band ladders compare `value`; projection ladders and omits compare
      // fields and bound names. The compiler is the judge of which.
      list = [...(kindOf ? fieldsOf(kindOf) : []), ['value', 'kw']];
    } else if (prev === 'is') {
      list = [['set', 'kw'], ['not set', 'kw'], ['nothing', 'kw'], ['something', 'kw']];
    } else if (prev === 'unit') {
      list = FIG_UNITS.map((word) => [word, 'kw']);
    } else if (prev === 'as') {
      list = (decl && decl.kw === 'projection' ? FIG_FIELD_TYPES : FIG_FACT_TYPES)
        .map((word) => [word, 'kw']);
    } else if (prev === 'in') {
      list = [...vocab.dials.map((dial) => [dial, 'setting']),
              ...FIG_UNITS.map((word) => [word, 'kw'])];
    } else if (prev === 'from' || prev === 'over') {
      list = ownLine && (decl.kw === 'group' || decl.kw === 'filter')
        ? fieldsOf(kindOf)
        : namesOf('group', 'filter');
      if (prev === 'over') list.push(...namesOf('figure', 'projection'));
    } else if (prev === '&' || prev === '|' || prev === '(') {
      list = namesOf('group', 'filter');
    } else if (prev === '=') {
      const section = decl && !ownLine ? sectionAbove(lineNo, decl.at) : null;
      if (decl && decl.kw === 'measure' && ownLine) {
        list = [...fieldsOf(kindOf), ['moment', 'kw'], ['now', 'kw']];
      } else if (section === 'combine') {
        list = namesOf('figure');
      } else if (section === 'depends') {
        list = namesOf('group', 'filter');
      } else {
        list = [...(kindOf ? fieldsOf(kindOf) : []), ...namesOf('figure', 'measure')];
      }
    } else if (prefix.includes('.')) {
      list = [...vocab.dials.map((dial) => [dial, 'setting']),
              ...[...vocab.names].map(([name, kind]) => [name, kind]),
              ...kindPaths()];
    } else if (prefix.length >= 2) {
      list = [...(kindOf ? fieldsOf(kindOf) : []),
              ...[...vocab.names].map(([name, kind]) => [name, kind]),
              ...[...FIG_WORDS].map((word) => [word, 'kw'])];
    }
    if (!list) return { start, prefix, found: [] };

    const seen = new Set();
    const found = [];
    for (const rank of [0, 1]) {
      for (const [label, type] of list) {
        if (seen.has(label) || label === prefix) continue;
        const hit = rank === 0
          ? label.startsWith(prefix)
          : prefix.length >= 2 && label.includes(prefix);
        if (!hit) continue;
        seen.add(label);
        found.push({ label, type });
        if (found.length >= 12) break;
      }
      if (found.length >= 12) break;
    }
    return { start, prefix, found };
  }

  function completionOpen(force) {
    const { start, prefix, found } = candidates();
    if (!found.length || (!force && prefix.length === 0)) { completionClose(); return; }
    items = found;
    active = 0;
    wordStart = start;
    popup.replaceChildren(...items.map((item, index) =>
      el('li', {
        role: 'option', class: index === active ? 'active' : '',
        'aria-selected': index === active ? 'true' : 'false',
        // mousedown, not click: click fires after the textarea loses focus
        // and the popup has already been dismissed by the blur.
        onmousedown: (event) => { event.preventDefault(); active = index; completionAccept(); },
      },
      el('span', { class: 'mono' }, item.label), ' ',
      el('span', { class: `badge ${item.type}` }, item.type))));
    // Monospace makes the caret's place arithmetic: column times one glyph.
    // Clamped to the visible pane, flipped above the caret when the room
    // below has run out -- an off-screen popup would still be swallowing
    // Enter and the arrows for completions nobody can see.
    const before = input.value.slice(0, input.selectionStart);
    const lineNo = (before.match(/\n/g) || []).length;
    const column = wordStart - (before.lastIndexOf('\n') + 1);
    const pad = 12; // .ed-hl padding, kept in step with the stylesheet
    popup.hidden = false;
    const caretTop = pad + lineNo * lineHeight - input.scrollTop;
    let top = caretTop + lineHeight;
    if (top + popup.offsetHeight > input.clientHeight && caretTop - popup.offsetHeight >= 0) {
      top = caretTop - popup.offsetHeight;
    }
    if (top < 0 || top > input.clientHeight) { completionClose(); return; }
    const left = Math.min(
      Math.max(0, pad + column * charWidth - input.scrollLeft),
      Math.max(0, input.clientWidth - popup.offsetWidth - 4));
    popup.style.left = `${left}px`;
    popup.style.top = `${top}px`;
  }

  function completionMove(delta) {
    active = (active + delta + items.length) % items.length;
    [...popup.children].forEach((node, index) => {
      node.classList.toggle('active', index === active);
      node.setAttribute('aria-selected', index === active ? 'true' : 'false');
      if (index === active) node.scrollIntoView({ block: 'nearest' });
    });
  }

  function insertText(from, to, piece) {
    // execCommand keeps the native undo stack; the fallback loses undo
    // granularity but never the text.
    input.setSelectionRange(from, to);
    if (!document.execCommand('insertText', false, piece)) {
      // The fallback fires no input event, so the bookkeeping is manual.
      input.setRangeText(piece, from, to, 'end');
      onEdit();
    }
  }

  function completionAccept() {
    const chosen = items[active];
    if (!chosen) return;
    insertText(wordStart, input.selectionStart, chosen.label);
    completionClose();
  }

  // ---- wiring ------------------------------------------------------------
  function onEdit() {
    draft = input.value === page.source && base === page.fingerprint
      ? null
      : { base, text: input.value };
    errorAt = null; // the old refusal points at lines that may have moved
    paint();
    scheduleCheck();
  }

  let tabLeaves = false; // armed by Escape, spent by the next key or click
  input.addEventListener('input', () => { tabLeaves = false; onEdit(); completionOpen(false); });
  input.addEventListener('mousedown', () => { tabLeaves = false; });
  input.addEventListener('scroll', () => { sync(); completionClose(); });
  input.addEventListener('blur', () => completionClose());
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' && event.key !== 'Escape') tabLeaves = false;
    if (!popup.hidden) {
      if (event.key === 'ArrowDown') { event.preventDefault(); completionMove(1); return; }
      if (event.key === 'ArrowUp') { event.preventDefault(); completionMove(-1); return; }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault(); completionAccept(); return;
      }
      if (event.key === 'Escape') { event.preventDefault(); completionClose(); return; }
    }
    if (event.key === ' ' && event.ctrlKey) {
      event.preventDefault(); completionOpen(true); return;
    }
    if (event.key === 's' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault(); save(); return;
    }
    if (event.key === 'Escape') {
      // The way out of the trap: Escape arms one focus-moving Tab, so a
      // keyboard user is never stuck inside the textarea (Tab otherwise
      // indents, as every code editor's does).
      tabLeaves = true;
      return;
    }
    if (event.key === 'Tab' && tabLeaves) return;
    if (event.key === 'Tab') {
      event.preventDefault();
      const from = input.selectionStart;
      const to = input.selectionEnd;
      if (from !== to || event.shiftKey) {
        // Block indent/dedent: a Tab that replaced the selection with four
        // spaces would be the one editing gesture that deletes work.
        const blockStart = input.value.lastIndexOf('\n', from - 1) + 1;
        const block = input.value.slice(blockStart, to);
        const shifted = block.split('\n')
          .map((line) => event.shiftKey ? line.replace(/^ {1,4}/, '') : `    ${line}`)
          .join('\n');
        if (shifted !== block) {
          insertText(blockStart, to, shifted);
          input.setSelectionRange(blockStart, blockStart + shifted.length);
        }
      } else {
        insertText(from, to, '    ');
      }
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      const caret = input.selectionStart;
      const lineStart = input.value.lastIndexOf('\n', caret - 1) + 1;
      const line = input.value.slice(lineStart, caret);
      const indent = /^\s*/.exec(line)[0];
      const deeper = line.trimEnd().endsWith(':') ? '    ' : '';
      insertText(caret, input.selectionEnd, `\n${indent}${deeper}`);
    }
  });

  // Metrics are read from the mounted element, not assumed: the stylesheet
  // owns the font, and a hardcoded width drifts the popup off the caret the
  // day the font changes.
  setTimeout(() => {
    if (!shell.isConnected) return;
    const style = getComputedStyle(input);
    lineHeight = parseFloat(style.lineHeight) || lineHeight;
    const probe = el('span', { class: 'mono' }, '0'.repeat(100));
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    probe.style.whiteSpace = 'pre';
    probe.style.font = style.font;
    document.body.append(probe);
    charWidth = probe.getBoundingClientRect().width / 100 || charWidth;
    probe.remove();

    const at = params.get('at');
    if (at) {
      const pattern = new RegExp(
        `^(?:${FIG_DECLS.join('|')})\\s+${at.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`);
      const found = input.value.split('\n').findIndex((line) => pattern.test(line));
      if (found !== -1) caretTo(lineOffset(found + 1), found + 1);
    }
  }, 0);

  paint();
  scheduleCheck();

  return [
    el('div', { class: 'title-block ed-title' },
      el('div', { class: 'tb-head' }, el('h1', {}, 'Editor')),
      el('div', { class: 'tb-doc' }, el('p', { class: 'prose' },
        'The definitions as stored, editable. Every keystroke is checked by ',
        'the same compiler a save runs; a save is the same teach the API ',
        'performs, and what it changes is stated before you commit to it.'))),
    page.refusal
      ? el('div', { class: 'notice problem' },
          'The stored source does not compile under this build — this editor ',
          'is the repair path: ', el('span', { class: 'mono' }, page.refusal))
      : null,
    restored
      ? el('p', { class: 'faint' },
          'An unsaved draft from this tab was restored; “discard draft” returns ',
          'to the text as stored.')
      : null,
    orphan
      ? (() => {
          const banner = el('div', { class: 'notice' },
            el('p', {},
              'An unsaved draft from this tab edited text that has since been ',
              'replaced by another save.'),
            el('div', { class: 'ed-runs' },
              el('button', {
                onclick: () => {
                  input.value = orphan.text;
                  banner.remove();
                  onEdit(); // retargets the draft at the current base
                },
              }, 'Restore the draft over the current text'),
              el('button', {
                onclick: () => { orphan = null; banner.remove(); },
              }, 'Discard it')));
          return banner;
        })()
      : null,
    bar, shell, report, outcome,
  ];
}

// ---------------------------------------------------------------- boot --

await loadTenants();
render();
