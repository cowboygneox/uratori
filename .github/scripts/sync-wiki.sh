#!/usr/bin/env bash
# Renders docs/ into a GitHub-wiki working tree (the path given as $1).
#
# The wiki is derived, never edited: this script is the only writer, run by
# .github/workflows/wiki.yml on every docs change (and runnable by hand).
# Inter-doc links are rewritten from files (`setup.md`) to wiki pages
# (`Setup`), because the wiki serves pages, not files; anchors survive the
# rewrite untouched.
set -euo pipefail

wiki="${1:?usage: sync-wiki.sh <wiki-checkout>}"
root="$(cd "$(dirname "$0")/../.." && pwd)"

render() {
  sed -e 's|](concepts\.md|](Concepts|g' \
      -e 's|](setup\.md|](Setup|g' \
      -e 's|](http-api\.md|](HTTP-API|g' \
      -e 's|](language\.md|](Language|g' \
      -e 's|](ui\.md|](UI|g' \
      "$root/docs/$1"
}

render concepts.md > "$wiki/Concepts.md"
render setup.md    > "$wiki/Setup.md"
render http-api.md > "$wiki/HTTP-API.md"
render language.md > "$wiki/Language.md"
render ui.md       > "$wiki/UI.md"

cat > "$wiki/Home.md" <<'EOF'
**uratori** (裏取り) makes every number in your product mean something both
sides can read: what a number means is written down in a small definition
language a product owner can review, the engine computes exactly what is
written, and every answer carries the versioned definition that produced it
-- over HTTP and over a websocket.

```bash
docker run -p 8080:8080 \
  -e DATABASE_URL=postgres://user:pass@your-postgres:5432/uratori \
  cowboygneox/uratori:latest
```

| | |
|---|---|
| [Concepts](Concepts) | Facts, schemas, definitions, versions, tenants -- the model in full. Start here. |
| [Setup](Setup) | Deploying the container: environment, database, token, health, upgrades. |
| [HTTP & websocket API](HTTP-API) | Every route and frame, with request and response shapes. |
| [The definition language](Language) | Writing `.fig`: groups, filters, measures, figures, readings, projections. |
| [The built-in UI](UI) | The investigation surface at `/ui/`: definitions as written, dependency traces, facts, and the activity log. |

These pages are rendered from
[`docs/`](https://github.com/cowboygneox/uratori/tree/main/docs) on every
change to them. Edits belong there, not here -- anything written directly in
the wiki is overwritten by the next sync.
EOF

cat > "$wiki/_Sidebar.md" <<'EOF'
**[uratori](Home)**

- [Concepts](Concepts)
- [Setup](Setup)
- [HTTP & websocket API](HTTP-API)
- [The definition language](Language)
- [The built-in UI](UI)

[Repository](https://github.com/cowboygneox/uratori)
EOF
