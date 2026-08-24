"""`python -m uratori.server` -- the container's entry point.

One uvicorn worker, deliberately. The websocket hub and the per-tenant pass
locks live in process memory, so a second worker would be a second hub that
half the subscribers landed on and a second lock nobody else can see. Scaling
this service is a design decision (shared locks, a broadcast channel), not a
CLI flag.
"""

from __future__ import annotations

import os

import uvicorn

from .app import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
