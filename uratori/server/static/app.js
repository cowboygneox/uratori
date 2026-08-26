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
      if (edge.type === 'fact' || edge.type === 'setting') continue;
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
  const argument = path ? safeDecode(path) : null;
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
  if (route === 'facts') view.replaceChildren(...(await factsView(argument, params)).flat(Infinity));
  else if (route === 'activity') view.replaceChildren(...(await activityView()).flat(Infinity));
  else view.replaceChildren(...(await definitionsView(argument)).flat(Infinity));
}

// -------------------------------------------------------- definitions --

const KIND_ORDER = ['figure', 'reading', 'projection', 'summary', 'group', 'filter', 'measure'];

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

async function definitionsView(name) {
  const pane = name ? await declarationPane(name) : [libraryPlate()];
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
          'Pick one to read it as written and walk its dependencies down ',
          'to the facts.'))),
    world.refusal
      ? el('div', { class: 'notice problem' },
          'Definitions are stored but refused by this build’s compiler: ',
          el('span', { class: 'mono' }, world.refusal))
      : null);
}

function dependencyNode(edge, seen) {
  if (edge.type === 'fact') {
    return el('li', {},
      el('span', { class: 'badge fact' }, 'fact'), ' ',
      el('a', { class: 'mono', href: `#/facts/${encodeURIComponent(edge.name)}` }, edge.name),
      el('span', { class: 'leaf' }, ' — the records themselves'));
  }
  if (edge.type === 'setting') {
    return el('li', {},
      el('span', { class: 'badge setting' }, 'setting'), ' ',
      el('span', { class: 'mono' }, edge.name),
      el('span', { class: 'leaf' }, ' — a tenant dial'));
  }
  const declaration = byName.get(edge.name);
  const line = el('li', {},
    el('span', { class: `badge ${edge.type}` }, edge.type), ' ',
    el('a', { class: 'mono', href: `#/definitions/${encodeURIComponent(edge.name)}` }, edge.name));
  if (!declaration) {
    line.append(el('span', { class: 'leaf' }, ' — not in the library?'));
    return line;
  }
  if (seen.has(edge.name)) {
    line.append(el('span', { class: 'leaf' }, ' — shown above'));
    return line;
  }
  seen.add(edge.name);
  if (declaration.rests_on.length) {
    line.append(el('ul', { class: 'tree' },
      declaration.rests_on.map((below) => dependencyNode(below, seen))));
  }
  return line;
}

async function declarationPane(name) {
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

  if (declaration.rests_on.length) {
    parts.push(el('h2', {}, 'Rests on'),
      el('ul', { class: 'tree' },
        declaration.rests_on.map((edge) => dependencyNode(edge, new Set([name])))));
  } else {
    parts.push(el('h2', {}, 'Rests on'),
      el('p', { class: 'faint' }, 'Nothing — this declaration reads only its own records.'));
  }

  const dependants = usedBy.get(name) || [];
  parts.push(el('h2', {}, 'Used by'),
    dependants.length
      ? el('p', {}, dependants.map((other, i) => [
          i ? ', ' : null,
          el('a', { class: 'mono', href: `#/definitions/${encodeURIComponent(other)}` }, other),
        ]))
      : el('p', { class: 'faint' }, 'Nothing in the library reads this.'));

  if (['figure', 'reading', 'projection', 'summary'].includes(declaration.kind)) {
    // The tenant id rides in a verbatim span: the label style uppercases,
    // and a case-mangled identifier on this page would be a small lie.
    parts.push(el('h2', {}, 'Current answer — tenant ',
      el('span', { class: 'verbatim' }, tenant() || '?')));
    parts.push(await answerSection(declaration));
  }
  return parts;
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
          member.held ? null : el('span', { class: 'faint' }, ' (no longer held)'));
      }))));
  }
  row.after(expansion);
}

// --------------------------------------------------------------- facts --

async function factsView(kind, params) {
  if (!tenant()) {
    return [el('h1', {}, 'Facts'),
      el('p', { class: 'faint' }, 'No tenant holds any facts yet.')];
  }
  if (!kind) return kindListView();
  return kindView(kind, params.get('q') || '', params.get('after'), params.get('trail') || '');
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
    const row = el('tr', { class: 'record-row', tabindex: '0' },
      el('td', { class: 'mono' }, record.key),
      el('td', {}, record.name ?? el('span', { class: 'faint' }, '—')),
      el('td', { class: 'mono faint' }, record.source_stamp ?? '—'));
    const toggle = () => {
      if (row.nextSibling && row.nextSibling.classList.contains('expansion')) {
        row.nextSibling.remove();
        return;
      }
      row.after(el('tr', { class: 'expansion' },
        el('td', { colspan: '3' },
          el('pre', {}, JSON.stringify(record.value, null, 2)))));
    };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
    });
    return row;
  });

  const lastKey = page.records.length ? page.records[page.records.length - 1].key : null;
  return [
    el('h1', {}, el('a', { href: '#/facts' }, 'Facts'), ' / ', el('span', { class: 'mono' }, kind)),
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
