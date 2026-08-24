"""What the engine says changed.

**This is the keystone of v2.** v1's engine reported movements and deliberately
under-reported them: deletions produced nothing at all, and a caught exception
returned an empty list. That is why v1 could not push engine output over its
socket -- the stream was lossy, so the only safe thing to do was re-read the
whole population every sync, which is why a hand-written republishing step
existed, which is where duplicate arithmetic crept back in.

So the rules here are the ones everything else rests on:

  - **A deletion is a change.** A departed subject that vanishes silently leaves
    a screen counting somebody who is gone.
  - **A failed run raises.** It never reports "nothing changed", because
    "nothing changed" is itself information -- a run that moved nothing must stay
    distinguishable from a run that did not finish.
  - **An unchanged recompute reports nothing.** A sync in which nothing happened
    filling the log is how the log stops being read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..lang.plan import Value


@dataclass(frozen=True)
class Change:
    figure: str
    subject: str
    """The storage key: a bare id, or `<subject>@<tail>` for a day-keyed or
    dimensioned figure."""

    kind: Literal["moved", "removed"]
    before: Value
    after: Value
    label: str
    """The subject's human-facing name, **rendered when the change happened and
    never re-derived**. A person renamed next week must not rewrite the history
    of what they moved."""

    display: str
    """The figure's own sentence, rendered the same way and for the same reason."""


@dataclass(frozen=True)
class Outcome:
    """What one engine run did.

    `covered` names the fact kinds this run actually read. A webhook covers
    almost nothing and a reconcile covers everything, and the difference decides
    what may be re-dated: a value whose inputs nobody looked at has not been
    confirmed unchanged, it has merely not been checked.
    """

    changes: tuple[Change, ...]
    covered: frozenset[str]
    rebuilt: tuple[str, ...]
    """Which figures were rebuilt from scratch, and why it is worth reporting:
    the observable difference between a narrow settings save and a full rebuild
    is *work*, and a figure recomputed to the value it already held writes
    nothing and reports nothing. Without this the two are indistinguishable from
    outside, and a test cannot tell them apart either."""
