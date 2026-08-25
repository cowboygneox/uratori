# Setup

One container, one Postgres database. Everything uratori knows lives in that
database -- the schema you declared, the definitions source, every fact and
every computed value -- so the container is disposable, and the only thing it
carries that matters is which build it is. This page takes you from nothing to
a production-shaped deployment: image, environment, database, token, probes,
proxy, upgrades.

For what to do with the server
once it is running -- declaring a schema, loading definitions, pushing facts
-- see the [HTTP & websocket API](http-api.md), and [Concepts](concepts.md)
for the model behind it.

## Quickstart

With a Postgres database of uratori's own to point at:

```bash
docker run -p 8080:8080 \
  -e DATABASE_URL=postgres://user:pass@your-postgres:5432/uratori \
  cowboygneox/uratori:latest
```

If that Postgres is itself a container on the same machine, the host in
`DATABASE_URL` is `host.docker.internal` (or the two containers share a
network) -- `localhost` inside the engine's container is the engine's
container.

Or clone the repository and bring up an engine with its database beside it:

```bash
docker compose up
```

Either way the server is on http://localhost:8080, and `curl
localhost:8080/health` confirms it. The compose file builds the image from the
checkout; to run the published image instead, replace `build: .` with
`image: cowboygneox/uratori:latest`. It deliberately sets no `URATORI_TOKEN`
-- it is the local playground. A deployment that anything else can reach sets
one (see [the token](#the-auth-token)).

At boot the server waits up to sixty seconds for the database before giving
up, because boot order is not something it gets to decide: under `docker
compose up` or any orchestrator, Postgres is routinely seconds behind it.
Waiting quietly and then dying loudly distinguishes "not ready yet" from
"misconfigured".

The container holds no state worth keeping. In the compose file the named
volume under Postgres is the deployment; the `uratori` service can be deleted
and recreated freely.

## Images and tags

Images live on [Docker Hub](https://hub.docker.com/r/cowboygneox/uratori) as
`cowboygneox/uratori` and on GHCR as `ghcr.io/cowboygneox/uratori`, built for
both `linux/amd64` and `linux/arm64`. CI publishes on every push to `main`
and on every release tag.

| Tag | Published when | Meaning |
|---|---|---|
| `X.Y.Z` | a `vX.Y.Z` git tag | a release, immutable |
| `X.Y` | a `vX.Y.Z` git tag | the latest patch of a minor line |
| `sha-<commit>` | every push to `main` | exactly one build, for ever |
| `latest` | pushes to `main` and releases | whatever shipped most recently |

Pin a production deployment to `X.Y.Z` or `sha-<commit>`. `latest` is fine for
a first look, but it changes underneath you, and an upgrade should be a
decision you made rather than a restart you happened to do. Whichever tag you
run, the build stamps itself into the image as `APP_VERSION` and reports it at
`/health`, so which build is actually serving is a check, not a hope.

## Environment

The server is configured entirely through the environment; there are no
config files.

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_URL` | *(required)* | The Postgres DSN, e.g. `postgres://user:pass@host:5432/uratori`. Without it the server refuses to start: uratori keeps facts, computed values and its own configuration in Postgres, and there is no file-based fallback. |
| `URATORI_TOKEN` | unset | When set, every route except `/health` -- the websocket included -- requires `Authorization: Bearer <token>`. Unset means no authentication at all. |
| `HOST` | `0.0.0.0` | The address the server binds. |
| `PORT` | `8080` | The port the server binds. The image sets it to `8080` and exposes the same. |
| `APP_VERSION` | `dev` | The build identifier reported at `/health`. Published images bake it in at build time (the release tag, or `sha-<commit>`); you set it yourself only when building your own image, via the `APP_VERSION` build argument. |
| `URATORI_UI` | follows the token | Mounts the [built-in investigation UI](ui.md) at `/ui/`. Unset, the UI is on exactly when `URATORI_TOKEN` is unset -- the UI is unauthenticated by design, so a token'd API does not silently carry an open window. `on`/`off` (also `true`/`false`, `1`/`0`, `yes`/`no`) overrides in either direction; anything else refuses to boot. |
| `URATORI_UI_FRAME_ANCESTORS` | `'self'` | Who may iframe the UI, pasted verbatim into its `Content-Security-Policy: frame-ancestors` header. Set it to the embedding application's origin to allow embedding; see [the UI's own page](ui.md) for the proxy alternative. |

## The database

`DATABASE_URL` names a Postgres database that belongs to uratori and nothing
else. This is not a suggestion the server merely documents -- it enforces it.

**The ownership guard.** Some of uratori's table names are generic enough to
exist in other products, so pointing the server at somebody else's database
must be a loud refusal at boot, not a wrong answer later. On startup, before
creating anything, the server checks who owns the database:

- If a `uratori_meta` table exists and its owner marker names something other
  than uratori, the server refuses to start and names the owner it found.
- If there is no marker but a `figure_definition` table already exists, the
  server refuses too: the table is probably another product's, and applying
  uratori's schema on top would interleave two schemas that reuse table names,
  leaving every later error pointing at data rather than at this moment.

In both cases nothing has been written; the refusal comes before any DDL runs.
The fix is the same either way: point `DATABASE_URL` at a database of
uratori's own.

**Self-migration at boot.** A fresh database is set up automatically: schema
management is one idempotent pass that creates anything missing, taken under a
Postgres advisory lock that every booting process requests. Two replicas
booting at once -- a rolling deploy, a restart race -- serialise on that lock
and each apply the same additive DDL, so boot-time migration needs no
migration job, no init container, and no coordination beyond the lock Postgres
already provides.

**What to back up.** The database is the deployment. `pg_dump` of the one
database captures the declared schema, the definitions source, every tenant's
settings, every fact and every computed value -- restore it, start a
container, and the server comes back knowing its world, because the
definitions source is stored and recompiled at boot (a build whose compiler
refuses the stored source boots unready rather than crashing; see
[Upgrading](#upgrading)). There is nothing in the
container worth backing up.

## The auth token

Set `URATORI_TOKEN` on anything reachable by more than the process pushing
facts at it. With the token set, every route except `/health` demands
`Authorization: Bearer <token>` -- `/health` stays open because probes and
load balancers do not carry credentials.

The token travels in the header and nowhere else. A token in a query string
lands in every access log and proxy log between the client and the server, and
a logged credential is a stored one -- so the server simply never reads one
from a URL, and a request that offers its token any other way gets a 401. The
websocket at `/stream` follows the same rule: the token goes in the
`Authorization` header of the upgrade request, and a bad or missing one
closes the socket with code 4401 and no frames, so an auth failure reads as
an auth failure rather than as a network fault a client would retry for ever.

## One worker, one replica

Run exactly one process. This is deliberate, not an oversight: the websocket
hub and the per-tenant pass locks live in process memory. A second worker or
replica is a second hub that half the subscribers land on -- each seeing only
half the movement -- and a second set of locks nobody else can see, so two
passes over one tenant could interleave their reads and writes into a state
neither pass computed. The container therefore starts a single uvicorn worker
with no flag to change it: scaling this service out is a design decision
(shared locks, a broadcast channel), not a CLI setting, and it has not been
made. One replica handles the workload this engine is shaped for; if it stops
being enough, that is a conversation, not a `replicas: 2`.

## Health and probes

`GET /health` is unauthenticated and answers as soon as the server is serving
-- which, given boot order, already means the database was reached, the
migration pass ran, and any stored world was restored and recompiled.

| Field | Meaning |
|---|---|
| `ok` | `true` whenever the process answers -- this is the liveness signal. |
| `version` | The `APP_VERSION` of the running build; compare it to what you deployed. |
| `ready` | `true` once definitions are compiled and loaded. `false` on a freshly booted server with no schema declared -- and after an upgrade whose stored definitions the new build's compiler refuses (see [Upgrading](#upgrading)). |
| `figures`, `readings` | How many of each the loaded library holds. |

`ready: false` is a state, not an error: a new server must accept traffic in
order to be taught, so `/health` returns 200 either way and requests that need
a world answer 409 with what is missing. Point both liveness and readiness
probes at `/health` and do not gate on the `ready` field -- it is there for
the operator checking a deploy, not for the orchestrator. The image also
carries its own Docker `HEALTHCHECK` against the same endpoint, so plain
`docker run` deployments get health for free.

## Kubernetes

A production-shaped deployment is one Deployment with one replica, its
configuration in a Secret, and a Service in front. The image runs as the
numeric non-root user `1001` -- numeric deliberately, because Kubernetes
cannot verify `runAsNonRoot` against a username it has no way to resolve --
and ships `tini` as PID 1, so signals are forwarded and a rollout stops
promptly instead of waiting out the grace period.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: uratori
stringData:
  DATABASE_URL: postgres://uratori:change-me@postgres:5432/uratori
  URATORI_TOKEN: change-me
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: uratori
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: uratori
  template:
    metadata:
      labels:
        app: uratori
    spec:
      containers:
        - name: uratori
          image: cowboygneox/uratori:0.1.0   # pin a release or a sha-<commit>
          ports:
            - containerPort: 8080
          envFrom:
            - secretRef:
                name: uratori
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 30
          securityContext:
            runAsNonRoot: true
            runAsUser: 1001
            allowPrivilegeEscalation: false
---
apiVersion: v1
kind: Service
metadata:
  name: uratori
spec:
  selector:
    app: uratori
  ports:
    - port: 8080
      targetPort: 8080
```

`Recreate` rather than a rolling update, for the reason in
[one worker, one replica](#one-worker-one-replica): a rolling update briefly
runs old and new side by side, and while the boot-time migration is safe under
that overlap (the advisory lock serialises it), the per-tenant locks are not
shared between the two processes. The few seconds of downtime buys the
guarantee that there is never a moment with two hubs and two lock sets alive.

## Behind a reverse proxy

Two things matter. First, `/stream` is a websocket, and a proxy must be told
to upgrade it -- and told to be patient, since the connection is long-lived by
design. Second, authentication rides in the `Authorization` header, so the
proxy must pass it through untouched (most do by default; some setups that do
their own auth strip it). For nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}

location /stream {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 1h;
}
```

TLS terminates wherever your proxies terminate it; the server itself speaks
plain HTTP and expects the network in front of it to be handled.

## Upgrading

Pull the new image and restart the container; that is the whole procedure.
At boot the new build re-runs the migration pass under the advisory lock
(additive and idempotent, so there is nothing to run by hand) and recompiles
the stored definitions source, because the source is the truth and a compiled
artifact read back would let a stale copy decide what the server computes.

If the new build carries new definitions, load them with `PUT /definitions`
as usual. Changed definitions get new versions and are computed fresh, and
the previous versions' stored values are kept, not discarded -- any history
that cites an old version can still be explained by it. That retention is the
point of the versioning (see [Concepts](concepts.md)), and it means an
upgrade never silently rewrites what a number used to be.

Then verify rather than assume:

```bash
curl -s localhost:8080/health
```

and check that `version` names the build you just deployed.

**If the language itself changed** between the build you ran and the build
you pulled, the stored source may no longer compile. The server still boots
-- crash-looping would lock the fix out behind the crash -- and comes up with
`ready: false`, logging the compiler's refusal; every route that needs
definitions answers 409 quoting it (*"The stored definitions do not compile
under this build: ..."*). The repair is the ordinary teach: `PUT /schema` is
accepted as usual, and `PUT /definitions` with corrected source makes the
server ready. Release notes name any change that requires this.
