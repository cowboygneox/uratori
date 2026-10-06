"""`audit` -- a second, model-backed reader over a page.

Split, like `uratori.documents.extract`, into a pure function (`judge`, this
package's `judge` module) and the server runtime that calls it. No code in
this package runs inside the engine: `audit` values enter only through
`Engine.accept`/`Uratori.accept` (`uratori/engine/engine.py`,
`uratori/facade.py`), called by the server's pass and worker
(`documents-plan-v3`, D6).
"""

from __future__ import annotations
