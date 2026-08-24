"""The deployable service. `create_app` builds it; `python -m uratori.server`
runs it. Requires the `server` extra (`pip install uratori[server]`)."""

from .app import create_app

__all__ = ["create_app"]
