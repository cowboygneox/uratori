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

const ROUTES = [
  ['#/definitions', 'Definitions'],
  ['#/facts', 'Facts'],
  ['#/activity', 'Activity'],
];

function drawTabs() {
  const here = location.hash || '#/definitions';
  tabs.replaceChildren(
    ...ROUTES.map(([hash, label]) =>
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
  }

  // flat(Infinity): a view may return nested arrays of nodes, and a nested
  // array handed to replaceChildren renders as "[object HTMLDivElement]".
  // The null filter is for the same reason el() skips nulls: a view may say
  // "nothing here" with a null, and replaceChildren would print the word.
  const draw = (nodes) => view.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null));
  if (route === 'facts') draw(await factsView(segments, params));
  else if (route === 'activity') draw(await activityView());
  else draw(await definitionsView(argument, params));
}

// -------------------------------------------------------- definitions --

const KIND_ORDER = ['figure', 'reading', 'projection', 'summary', 'group', 'filter', 'measure', 'fact'];

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
function leafLine(edge) {
  if (edge.type === 'fact') {
    return el('li', {},
      el('span', { class: 'badge fact' }, 'fact'), ' ',
      el('a', { class: 'mono', href: `#/facts/${encodeURIComponent(edge.name)}` }, edge.name),
      el('span', { class: 'leaf' }, ' — the records themselves'));
  }
  return el('li', {},
    el('span', { class: 'badge setting' }, 'setting'), ' ',
    el('span', { class: 'mono' }, edge.name),
    el('span', { class: 'leaf' }, ' — a tenant dial'));
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
        declaration.mode ? el('span', { class: 'badge' }, declaration.mode) : null),
      declaration.doc
        ? el('div', { class: 'tb-doc' }, el('p', { class: 'prose' }, declaration.doc))
        : null,
      el('div', { class: 'tb-cite' },
        declaration.version
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
    parts.push(el('h2', {}, 'Moved by'));
    if (movedBy.length) {
      parts.push(
        el('p', { class: 'prose' },
          'A change to any of these records or dials can move this ',
          declaration.kind, ' — nothing else can.'),
        el('ul', { class: 'tree' }, movedBy.map(leafLine)));
    } else {
      parts.push(el('p', { class: 'faint' }, 'Nothing — it reads no records and no dials.'));
    }
  }

  // Structure, only when there is structure: the declarations this one
  // composes, one hop each. Leaves are left out — Moved by just stated every
  // fact and dial, and repeating them under a second heading would make the
  // two lists read as different answers to one question. For a group or a
  // measure every direct edge is a leaf, so the section vanishes entirely.
  const structural = declaration.rests_on.filter(
    (edge) => edge.type !== 'fact' && edge.type !== 'setting');
  if (structural.length) {
    parts.push(el('h2', {}, 'Built from'),
      el('ul', { class: 'tree' }, structural.map((edge) => edgeLine(edge))));
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

  if (!result.state.ok) {
    return el('div', { class: 'notice' },
      'Not available: ', el('span', { class: 'mono' }, result.state.because), ' — ',
      result.state.detail || 'the server gave no further sentence.');
  }

  const blocks = [];
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
            onclick: () => evidenceRow(row, declaration.name, subject.id),
          }, 'evidence')));
        return row;
      })));
  } else if (result.kind === 'reading') {
    for (const subject of result.subjects) {
      // class 'subject': the drawing-label h2 uppercases, and this text is
      // a server-rendered name that must appear verbatim.
      blocks.push(el('h2', { class: 'subject' }, subject.name));
      blocks.push(el('table', {},
        el('tr', {}, el('th', {}, 'window'), el('th', {}, 'statistics'),
          el('th', {}, 'sample'), el('th', {}, 'coverage')),
        (subject.windows || []).map((window) => el('tr', {},
          el('td', { class: 'mono' }, `${window.trailing}d`, ' ',
            el('span', { class: 'faint' }, `${window.frm} → ${window.to}`)),
          el('td', { class: 'mono' },
            Object.entries(window.display).map(([stat, text], i) =>
              [i ? ' · ' : null, el('span', { class: 'faint' }, `${stat} `), text]),
            window.unmet.length
              ? el('div', { class: 'faint' }, window.unmet.join('; '))
              : null),
          el('td', { class: 'mono' }, String(window.sample)),
          el('td', { class: 'mono dim' }, `${window.days_covered}/${window.days_requested}d`)))));
    }
  } else {
    // Projection and summary rows: named, server-rendered cells.
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
      blocks.push(el('h2', {}, 'Summary'),
        el('dl', { class: 'kv' },
          Object.entries(result.summary.display).map(([column, text]) =>
            el('div', { class: 'pair' }, el('dt', {}, column), el('dd', {}, text)))));
    }
  }
  blocks.push(el('p', { class: 'faint' },
    'answered ', el('span', { class: 'mono' }, result.at),
    ' under version ', el('span', { class: 'mono' }, result.version)));
  return el('div', {}, blocks);
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
    const evidence = answer.body;
    // Through el(), not bare append(): append() stringifies a null child
    // into the visible word "null", el() skips it.
    holder.append(el('div', {}, el('p', { class: 'dim' },
      evidence.members.length
        ? `This value cites ${evidence.members.length} ${evidence.parts ? 'parts' : 'records'}:`
        : 'This value cites nothing.'),
      evidence.note ? el('p', { class: 'faint' }, evidence.note) : null,
      el('ul', {}, evidence.members.map((member) => {
        const link = member.url ? safeUrl(member.url) : null;
        return el('li', { class: 'mono' },
          member.figure ? el('span', { class: 'faint' }, `${member.figure} · `) : null,
          link
            ? el('a', { href: link, target: '_blank', rel: 'noreferrer' },
                member.title || member.key)
            : (member.title || member.key),
          member.display ? el('span', { class: 'dim' }, ` — ${member.display}`) : null,
          member.held ? null : el('span', { class: 'faint' }, ' (no longer held)'),
          // The citation's last rung: when the members are records of one
          // kind, each held one links to the record itself, so a value can
          // be walked to the stored fact without leaving the trace.
          evidence.kind && member.held
            ? [' ', el('a', { class: 'trace', href: recordHash(evidence.kind, member.key) },
                'record →')]
            : null);
      }))));
  }
  row.after(expansion);
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
    el('ul', { class: 'movers' }, movers.map((declaration) =>
      el('li', {}, el('a', {
        class: 'mono', href: `#/definitions/${encodeURIComponent(declaration.name)}`,
      }, declaration.name)))));
}

async function recordView(kind, key) {
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/facts/${encodeURIComponent(kind)}`
    + `/${encodeURIComponent(key)}`);
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

async function activityView() {
  if (!tenant()) {
    return [el('h1', {}, 'Activity'), el('p', { class: 'faint' }, 'No tenant has run yet.')];
  }
  const quiet = sessionStorage.getItem('uratori.quiet') === '1';
  const answer = await get(
    `tenants/${encodeURIComponent(tenant())}/activity${quiet ? '?quiet=1' : ''}`);
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
  const TRIGGERS = { facts: 'facts arrived', run: 'manual run' };

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

  return [
    el('h1', {}, 'Activity'),
    el('p', { class: 'prose' },
      'Cause before effect: each pass with the movements it caused, frozen ',
      'when they happened. Push a fact and the newest run says what it cascaded to.'),
    el('div', { class: 'controls' }, toggle,
      page.total > page.runs.length
        ? el('span', { class: 'faint' },
            `showing the newest ${page.runs.length} of ${page.total} runs`)
        : null),
    runs.length ? runs : el('p', { class: 'faint' }, 'No runs recorded for this tenant.'),
  ];
}

// ---------------------------------------------------------------- boot --

await loadTenants();
render();
