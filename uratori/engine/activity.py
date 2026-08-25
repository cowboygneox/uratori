"""A figure movement, as a row in the activity log.

Pure, deliberately: the recorder freezes what these produce into Postgres, so
everything here is decided *once*, at write time, against the unit and the
tenant's settings as they were then. Nothing is re-derived on read -- a figure
redefined next week, or a formatter improved, must not rewrite the history of
what moved.

**The weights are judgements, not arithmetic**, and each one is the answer to a
specific eviction. `SHOWN_KEEP` caps how many rows one run stores, so the
weight decides what a reader sees on a busy sync -- and a weight taken from the
raw value ranks by unit choice rather than by importance. v1 wrote each of
these down after getting it wrong; they are kept, not rediscovered:

  - a **count** ranks by how far it moved, which is the baseline everything
    else is normalised against: a merge request opening is 1;
  - an **effort** or a **duration** moves in *hours* -- raw, a half-day
    estimate edit is 14,400 seconds and pins the top of every sample
    containing one, evicting the movements a reader came for;
  - a **share** moves in *points* -- raw, eight points of a quarter's plan is
    0.08 and loses to every count that ticked, which is backwards;
  - a **moment** moves in *days* -- raw, a day of slippage is 86,400,000
    milliseconds and holds all forty slots by itself;
  - a **band** change is 2: there is no arithmetic between "ok" and "over",
    so the number is picked -- above a count ticking by one, below a real jump;
  - a **removal** is 2 for the same reason: a subject leaving the board is a
    claim about the roster, not a distance, and an absence is not a nought to
    subtract from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..lang.ast import FigureUnit
from ..lang.plan import Library
from .change import Change
from .project import format_value

SHOWN_KEEP = 40
"""Change rows stored per run, beside the true count.

A full rebuild moves every value on the board; storing all of them would make
the log's biggest rows the ones nobody can read, and storing none would make
the rebuild unexplainable. Forty ranked rows under an honest total is v1's
number, kept because nothing has changed about how much a person reads.
"""


@dataclass(frozen=True)
class RenderedChange:
    """One movement, rendered for the log and never touched again."""

    figure: str
    subject_id: str
    kind: Literal["moved", "removed"]
    label: str
    before_display: str
    after_display: str
    unit: str
    weight: float


def weight_of(change: Change, unit: FigureUnit) -> float:
    """How much of the log's limited room this movement deserves.

    The header carries the table; what is worth stating here is the shape of
    the fallthroughs. A movement the arithmetic cannot measure -- a first
    reading, a value arriving at or leaving null, a list corrected in place --
    ranks as **one thing having happened** rather than as nothing, because a
    weight of nought is an eviction and every one of those is a real event.
    """
    if change.kind == "removed":
        return 2.0
    if unit == "level":
        return 2.0

    before, after = change.before, change.after
    if isinstance(before, list) or isinstance(after, list):
        if isinstance(before, list) and isinstance(after, list):
            # Length is the arrival count; a list revised under a stable
            # length still moved, and nought would rank the one event that
            # changed the number below everything.
            return float(max(abs(len(after) - len(before)), 1))
        return 1.0
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        # A first reading, a movement into or out of "cannot tell", or a word
        # under a unit that should not carry one. There is no distance to
        # measure, and something happened.
        return 1.0

    delta = abs(float(after) - float(before))
    if unit in ("effort", "duration"):
        return delta / 3_600.0
    if unit == "share":
        return delta * 100.0
    if unit == "moment":
        return delta / 86_400_000.0
    return delta  # count, days: already on the order of things that happened


def render_change(
    change: Change, unit: FigureUnit, settings: Mapping[str, Any]
) -> RenderedChange:
    """Freeze one movement as text.

    `format_value` is the one place a value becomes text everywhere else, so it
    is the one place here too -- a second renderer would be a second opinion
    about what "4.0h" means, showing up as the log disagreeing with the card.

    A `None` on either side renders as a dash, never as a nought: an absence
    means *not computed*, and 0 is a measured answer the engine did not give.
    The dash is deliberately not labelled "first reading" -- the engine's
    change stream cannot tell a value that never existed from a stored null,
    and claiming the distinction it cannot make would be the confident wrong
    answer this log exists to avoid.
    """
    return RenderedChange(
        figure=change.figure,
        subject_id=change.subject,
        kind=change.kind,
        label=change.label,
        before_display=format_value(change.before, unit, settings),
        after_display=format_value(change.after, unit, settings),
        unit=unit,
        weight=weight_of(change, unit),
    )


def shown_changes(
    changes: list[Change] | tuple[Change, ...],
    library: Library,
    settings: Mapping[str, Any],
) -> list[RenderedChange]:
    """Rank every movement, cap, and render only the survivors.

    **A removal is never evicted by routine movements.** Weighted at 2 alone it
    loses the cap to ordinary work -- forty estimate edits above two hours is
    an unremarkable Jira reconcile, and a departure buried inside "N more
    moved and are not listed" is v1's worst activity bug (deletions reported
    by nothing) coming back wearing a cap. So removals rank ahead of every
    non-removal and keep their weight order among themselves; when a run holds
    more than forty departures the cap is still the cap, and the true count
    beside the list is what says so.

    Ranked before it is cut for the same reason, and *rendered after* the cut:
    weighing needs no text, and on the pass that moves every value on the
    board -- any deploy that moves a version -- rendering first would format
    thousands of rows to store forty. Python's sort is stable, so equal
    weights keep the engine's order, which is at least a consistent one.
    """
    units: dict[str, FigureUnit] = {plan.name: plan.unit for plan in library.figures}
    weighed: list[tuple[Change, FigureUnit, float]] = []
    for change in changes:
        unit = units.get(change.figure, "count")
        weighed.append((change, unit, weight_of(change, unit)))
    weighed.sort(key=lambda entry: (entry[0].kind != "removed", -entry[2]))
    return [
        render_change(change, unit, settings)
        for change, unit, _ in weighed[:SHOWN_KEEP]
    ]
