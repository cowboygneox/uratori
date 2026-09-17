#!/usr/bin/env python3
"""Stand the tutorial's support desk up against a running uratori.

Three calls and two reads, in the order the engine needs them:

    PUT  /schema                  the world, empty: this world is fact-taught
    PUT  /definitions             support.fig, as written
    POST /tenants/desk/facts      facts.json, one batch, which runs the pass
    GET  /tenants/desk/results/…  two answers, to prove it worked

Idempotent. Running it twice re-PUTs identical text (no version moves) and
re-pushes identical records, which the server reports as `written: 0,
changed: 0` -- the facts route exists to be given everything you saw and to
decide for itself what moved.

Running the engine locally, from a checkout (see docs/setup.md):

    docker compose up                     # from docs/tutorial -- engine + db + this
    docker compose -f ../../docker-compose.yml up    # engine + db, then run this

Or against any uratori you can reach:

    python seed.py --base http://localhost:8080 --tenant desk

Standard library only, so it runs in a bare python image.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def call(base: str, method: str, path: str, body: object | None = None,
         token: str | None = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as failure:
        detail = failure.read().decode(errors="replace")
        raise SystemExit(
            f"{method} {path} answered {failure.code}:\n{detail}"
        ) from None


def wait_for(base: str, token: str | None, seconds: int = 120) -> None:
    """Poll /health until the process answers. It needs no auth."""
    deadline = time.time() + seconds
    while True:
        try:
            health = call(base, "GET", "/health")
            print(f"  engine up: version {health['version']}")
            return
        except SystemExit:
            raise
        except Exception:
            if time.time() > deadline:
                raise SystemExit(f"nothing answering on {base} after {seconds}s")
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--tenant", default="desk")
    parser.add_argument("--token", default=None,
                        help="only if the server sets URATORI_TOKEN")
    args = parser.parse_args()
    base, tenant, token = args.base.rstrip("/"), args.tenant, args.token

    print(f"waiting for {base} ...")
    wait_for(base, token)

    # The world is declared in the source (`fact desk_ticket:` and friends),
    # so the schema document carries nothing at all. It still has to exist:
    # definitions compile against a schema, and PUT /definitions answers 409
    # until one is declared.
    print("PUT /schema (empty -- the world is declared in support.fig)")
    call(base, "PUT", "/schema", {}, token)

    source = (HERE / "support.fig").read_text()
    print(f"PUT /definitions ({len(source.splitlines())} lines)")
    library = call(base, "PUT", "/definitions", {"source": source}, token)
    print("  " + ", ".join(
        f"{len(library.get(group, []))} {group}"
        for group in ("facts", "indexes", "measures", "figures", "readings",
                      "projections", "summaries", "bundles")))

    facts = json.loads((HERE / "facts.json").read_text())
    total = sum(len(rows) for rows in facts.values())
    print(f"POST /tenants/{tenant}/facts ({total} records, "
          f"{len(facts)} kinds) -- this runs the pass")
    report = call(base, "POST", f"/tenants/{tenant}/facts",
                  {"writes": facts, "serve": False}, token)
    print(f"  written {report.get('written')}, "
          f"figure movements {report.get('changed')}, "
          f"kinds covered {len(report.get('covered') or [])}")

    print()
    print("two answers, to prove it:")
    for name in ("desk_agent.in_hand", "desk_team.reply_target_month"):
        answer = call(base, "GET", f"/tenants/{tenant}/results/{name}", None, token)
        print(f"\n  {name} @ {answer['version']}  ({answer['unit']})")
        for subject in answer.get("subjects", []):
            period = f" {subject['dimension']}" if subject.get("dimension") else ""
            level = f"  [{subject['level']}]" if answer.get("banded") else ""
            print(f"    {subject['name']}{period}: {subject['display']}{level}")

    print(f"""
Done. Open the engine's own screens at {base}/ui/ --

  the Definitions tab   every declaration, as written, with its version
  the Facts tab         the {total} records this script just pushed
  the Activity tab      the pass that computed everything, cause before effect

and one number taken apart, step by step:

  {base}/ui/#/work/desk_agent.in_hand/ag-mira

and start at docs/tutorial.md.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
