"""`carried forward`: a step function over a figure's own bucket sequence.

Sparse facts, dense buckets. Somebody sets a goal in February and nobody
touches it until June; a month-keyed figure over those records has two
buckets and a screen wants twelve. What fills the other ten is not an
interpolation and not a default -- it is the same value, still in force,
carried across the buckets in which nobody changed it.

**Why this may be stored at all.** The engine's standing rule is that a
stored value may never read a clock, because the clock is not an event and
nothing would ever recompute what it produced. A carried row does not read
one: its contents are decided entirely by the anchors at or before its own
bucket, so September's answer is the same answer for ever once September
exists. What *is* clock-dependent is only how far the sequence has been
extended -- an existence question, answered by a pass or by a read, both of
which are events. Splitting those two halves is what makes materialising
legal here where it is refused for `now - requested_at`.

**One function, three triggers.** A fact landing, a pass extending to the
present bucket, and a read filling a window it found unmaterialised all
produce their rows here. Three code paths writing the same rows would be
three chances to disagree, which is rule 1 -- one calculation system --
applied to plumbing rather than to arithmetic. Everything the triggers do
differently is *which labels they ask for*; what a label answers is decided
in one place, below.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from ..lang.plan import Value


@dataclass(frozen=True)
class Anchor:
    """A bucket somebody actually changed the value in.

    `value` is the figure's ordinary answer for that bucket -- computed by
    `evaluate` like any other, which is what keeps the carry from being a
    second way to work out what a bucket says. `members` is that bucket's
    evidence: the records the change is, which every bucket carrying it
    cites in turn.
    """

    label: str
    value: Value
    members: tuple[str, ...]


@dataclass(frozen=True)
class CarriedRow:
    """One materialised bucket, and the change it is still reporting.

    `anchor` is the label of the bucket the value was set in -- equal to
    `label` on a bucket that saw a change of its own. It travels with the
    row because a number a reader cannot walk back is the thing this engine
    exists to end: "25m, set in June" is auditable and "25m" is not.
    """

    label: str
    value: Value
    members: tuple[str, ...]
    anchor: str


def carried_rows(
    anchors: Sequence[Anchor], sequence: Sequence[str]
) -> list[CarriedRow]:
    """The value in force at each label of `sequence`, oldest first.

    `anchors` is every bucket the subject was changed in -- **all** of them,
    not only the ones inside `sequence`: a window over the second half of a
    year is governed by a change made in June, and starting the carry at the
    window's own first label would report an absence for exactly the months
    a value has been in force longest.

    `sequence` is the labels to answer for, in the figure's own declared
    grain. The caller decides where it ends, and that is deliberately the
    only place "never past the present" is enforced -- the alternative, a
    clock guard inside each of the three triggers, is three guards that can
    drift.

    A label with no anchor at or before it produces **no row**. Nothing has
    ever been set, so there is nothing to report, and an absence is what the
    engine says when it has not been told -- never a nought, which a band
    would happily colour comfortable.
    """
    ordered = sorted(anchors, key=lambda a: a.label)
    labels = [a.label for a in ordered]

    out: list[CarriedRow] = []
    for label in sequence:
        # The anchor in force is the latest one at or *before* this bucket.
        # `bisect_right` on the label puts an anchor sharing this bucket's
        # label on the left, so a bucket that saw a change reports its own
        # change rather than the previous one -- the difference between
        # June reading 25m and June reading 30m for a month it was changed
        # in.
        at = bisect_right(labels, label) - 1
        if at < 0:
            continue
        governing = ordered[at]
        out.append(
            CarriedRow(
                label=label,
                value=governing.value,
                members=governing.members,
                anchor=governing.label,
            )
        )
    return out
