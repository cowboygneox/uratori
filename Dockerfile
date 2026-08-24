# uratori: the definition engine as one deployable service.
#
# State lives in Postgres, never in the container -- the schema a host
# declared, the definitions source, every fact and every computed value. The
# container is disposable, and the only thing it carries that matters is which
# build it is.
#
# Built from the repository root: `docker build .`

FROM python:3.12-slim AS build
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies resolved from the manifest alone, before the source is copied,
# so editing the engine does not reinstall pydantic and uvicorn.
COPY pyproject.toml ./
COPY uratori/__init__.py ./uratori/__init__.py
RUN pip install --prefix=/install ".[server]"

COPY uratori/ ./uratori/

# -------------------------------------------------------------- runtime ----
FROM python:3.12-slim AS runtime
WORKDIR /app

# The build that produced this image, reported at /health so a deploy can be
# checked rather than assumed. Declared in the runtime stage only: in a build
# stage it would change a layer on every pipeline and throw away the
# dependency cache for a string nothing compiles against.
ARG APP_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APP_VERSION=${APP_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1001 uratori \
 && useradd -u 1001 -g 1001 -M -s /usr/sbin/nologin uratori

COPY --from=build /install /usr/local
COPY --from=build /app/uratori ./uratori

EXPOSE 8080

# Numeric, not a name: Kubernetes cannot verify `runAsNonRoot` against a
# username it has no way to resolve.
USER 1001

HEALTHCHECK --interval=60s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# tini reaps zombies and forwards signals, so a rollout stops promptly instead
# of waiting out the grace period on every deploy.
ENTRYPOINT ["/usr/bin/tini", "--"]

# One worker, deliberately: the websocket hub and the per-tenant pass locks
# live in process memory, so a second worker is a second hub half the
# subscribers land on and a second lock nobody else can see.
CMD ["python", "-m", "uratori.server"]
