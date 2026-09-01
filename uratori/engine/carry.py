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
present bucket, and a read filling a window it found unmaterialised are all
produce their rows here. Three code paths writing the same rows
would be three chances to disagree, which is rule 1 -- one calculation
system -- applied to plumbing rather than to arithmetic. Everything the
triggers do differently is *which labels they ask for*; what a label answers
is decided in one place, below.

**One assumption, stated because it is load-bearing.** Bucket labels are
compared as *text* here -- `bisect_right` below, and the `>=` in
`sequence_to_present`. That is only sound because every label format this
engine writes is fixed-width and chronologically ordered under a plain
string sort: `2026-08-25`, `2026-08-25T14:30`, and (as the calendar grains
land) `2026-08`, `2026-Q3`, and ISO weeks as `2026-W35` -- the *ISO* year,
so the week straddling New Year still sorts with the year it belongs to. A
grain whose labels broke that would not fail loudly; it would carry the
wrong anchor. A new label format has to be checked against this paragraph.
The module deliberately does no *arithmetic* on labels; ordering is the one
property it reads.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..lang.plan import Value

log = logging.getLogger("uratori.engine")

MAX_CARRY_BUCKETS = 3660
"""How far back a carry may reach, in buckets of the figure's own sequence.

The same number the window's reach ceiling uses, and for the same reason:
the reach *is* the cost, since every bucket between the first change and now
is a stored row per subject. Ten years of days, or three centuries of
months -- far beyond any board's horizon, and a first change older than that
is a definition question rather than a materialisation."""

LabelsBack = Callable[[int], list[str]]
"""`n` -> the n most recent buckets of a figure's own sequence, oldest first,
ending at the bucket containing the anchor instant.

Exactly the resolver a window uses to turn `over 1-6` into six concrete
labels. It is a *parameter* rather than something this module works out,
because "which buckets does this figure have" must have one answer: a carry
that counted months itself would agree with the window's resolver until the
first ISO week that belongs to the previous year, and then quietly
materialise a label no window ever asks for.

**It must return exactly `n` labels unless the calendar itself is shorter.**
`sequence_to_present` reads a short answer as "there are no older buckets"
and stops widening, so a resolver with a cap of its own -- a page size, a
`LIMIT`, its own reach bound -- would end the search early and report an
absence for exactly the buckets a value had been in force longest. It is a
hard precondition rather than something checkable, which is the price of
delegating the calendar; the engine's own span resolver clamps only at the
beginning of the calendar, the one short answer this may safely mean.

It is also asked for `cap + 1` exactly once, at the ceiling, to tell a
calendar that happens to be `cap` long from one that runs on. A resolver
with a reach bound of its own may refuse that question; a `ValueError` --
which the engine's own `WindowError` is -- is read as "there is more
calendar than the cap" and becomes a `CarryReachExceeded`.
"""


class CarryReachExceeded(ValueError):
    """A carried figure whose first change is further back than the reach
    allows.

    The reach *is* the cost -- every bucket between the anchor and now is a
    row written per subject -- so an anchor dated decades ago is a refusal
    rather than six hundred rows materialised on the first read that touches
    it. The same reasoning the window's own reach bound records, at the other
    end of the same sequence.
    """


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
    if len(set(labels)) != len(labels):
        # **One bucket, one anchor.** A bucket's value is the figure's ordinary
        # answer for it, computed once, so two anchors under one label cannot
        # arise from anything but a caller assembling them wrongly. Left to
        # fall through, `bisect_right` takes whichever the caller happened to
        # list second -- a stable, deterministic, invisible coin toss whose
        # loser is then carried over every later bucket. Aborting is the
        # choice `evaluate._scalar` makes when stored values disagree with the
        # plan: a wrong number nobody can see is worse than a failed pass
        # somebody can.
        clash = sorted({label for label in labels if labels.count(label) > 1})
        raise ValueError(
            f"two anchors share the bucket {', '.join(clash)}. A bucket has one value, "
            "so a second anchor for it means the caller built the list wrongly -- and "
            "picking one silently would carry it forward over every later bucket."
        )

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


def sequence_to_present(
    earliest: str, labels_back: LabelsBack, *, cap: int
) -> list[str]:
    """The figure's own buckets from `earliest` through the present one.

    Found by asking the resolver for a widening span until it reaches back
    past the anchor, then keeping the tail from the anchor onward. Written
    as a search rather than as arithmetic on the label deliberately: working
    out "how many months since February" here would be a second calendar
    implementation living beside the window's, and two calendars agree right
    up until the day they do not.

    **Never past the present** falls out of the resolver, which counts back
    from the anchor instant and so cannot name a future bucket. A change
    dated next month therefore materialises nothing at all until a pass runs
    next month -- which is right: the bucket does not exist yet, and writing
    it would put a value on a month that has not happened.

    **`earliest` must be a bucket label of this same sequence** -- an
    anchor's own label, which is what the grouping wrote. It is compared as
    text, so a label at a *different* grain is silently wrong rather than
    refused: `"2026-02-15"` against a monthly sequence sorts after
    `"2026-02"` and would drop the very bucket the change was made in. The
    tempting robustness fix -- taking the bucket that *contains* `earliest`
    rather than the ones at or after it -- cannot be had, because by string
    comparison alone `"2026-08-15"` (mid-month, inside the last bucket) and
    `"2026-09"` (next month, outside it) are the same case, and conflating
    them would materialise a bucket for a change dated in the future. So the
    grain agreement is a precondition the caller owes, stated here because
    breaking it produces a wrong answer rather than an error.
    """
    span = 1
    labels: list[str] = []
    while True:
        got = labels_back(span)
        if got and got[0] <= earliest:
            labels = got
            break
        if len(got) < span:
            # The resolver clamped at the beginning of its own calendar: it
            # has no more buckets to give, so this is the whole sequence and
            # the anchor simply predates it.
            labels = got
            break
        if span >= cap:
            # One more question before refusing. The search never asks for
            # more than `cap`, so a calendar that is *exactly* `cap` long
            # looks identical to one that runs for ever -- and refusing to
            # carry a sequence that would have fitted whole is the wrong
            # error. Asking for one past the ceiling settles it, at the cost
            # of a single call, only at the boundary.
            try:
                beyond = labels_back(cap + 1)
            except ValueError:
                # The resolver has a ceiling of its own and this probe went
                # over it -- the engine's own span resolver refuses a reach
                # past its bound rather than clamping. That refusal answers
                # the question the probe was asking ("is there more calendar
                # than the cap?") with a yes, so it becomes our refusal
                # rather than escaping as somebody else's exception type.
                beyond = []
                raise CarryReachExceeded(
                    f"carrying forward from {earliest} reaches past what the bucket "
                    f"resolver will answer for, and past this carry's own ceiling of "
                    f"{cap} buckets."
                ) from None
            if len(beyond) <= cap:
                labels = beyond
                break
            raise CarryReachExceeded(
                f"carrying forward from {earliest} would reach back more than {cap} "
                "buckets. The reach is the cost -- every bucket between the change "
                "and now is a stored row per subject -- so a first change this far "
                "back is a definition question rather than a materialisation."
            )
        span = min(span * 2, cap)

    return [label for label in labels if label >= earliest]


# --------------------------------------------------------- materialising --


async def materialise(
    store: Any,
    plan: Any,
    tenant: str,
    bases: Iterable[str],
    *,
    at_ms: float,
    zones: Mapping[str, str],
    written: str | None = None,
    trigger: str,
    cap: int = MAX_CARRY_BUCKETS,
) -> list[tuple[str, Value, Value]]:
    """Bring a carried figure's buckets up to the present, for these subjects.

    **The one implementation all three triggers use.** A fact landing, a pass
    reaching a new bucket and a read filling a window it found unmaterialised
    all arrive here; what they do differently is *when* they call and which
    subjects they name, never what a bucket ends up saying. Three code paths
    writing these rows would be three chances to disagree, and the
    disagreement would be invisible -- every row plausible, only some of them
    the same.

    The anchors are read from the **index**, not from the stored values: the
    buckets holding records are exactly the buckets somebody changed
    something in, which is what an anchor is. Reading them back out of the
    figure's own rows instead would mean carried rows and anchor rows became
    indistinguishable after the first pass, and the second pass would carry
    a carried value forward from a bucket nobody ever touched.

    Returns `(subject, before, after)` per row that actually moved, so the
    caller can report exactly what changed and nothing else. A pass in which
    nothing moved writes nothing and says nothing.
    """
    from .buckets import SEPARATOR, compose

    rule = plan.grain or "day"
    moved: list[tuple[str, Value, Value]] = []

    held = {
        row.subject: row
        for row in await store.values(tenant, plan.name, plan.version)
    }

    # Read once, not once per subject. Inside the loop this was a query per
    # subject on Postgres and a walk of the whole index each time -- paid on
    # every pass *and* every read of a carried figure, which is the shape
    # test_pass_cost.py exists to keep out.
    anchors_by_base: dict[str, list[str]] = {}
    for key in await store.bucket_keys(tenant, plan.scope_index):
        base, sep, label = key.partition(SEPARATOR)
        if sep:
            anchors_by_base.setdefault(base, []).append(label)

    for base in sorted(set(bases)):
        anchor_labels = sorted(anchors_by_base.get(base, ()))
        if not anchor_labels:
            # Nothing has ever been set for this subject, so there is nothing
            # to carry. An absence, and the pass leaves it that way.
            continue

        anchors: list[Anchor] = []
        # The anchor's own stored row, kept beside it: a carried bucket is
        # headed by the same rendered subject name as the change it reports,
        # and re-resolving the name here would be a second lookup that could
        # answer differently after a rename.
        anchor_rows: dict[str, Any] = {}
        for label in anchor_labels:
            row = held.get(compose([base, label]))
            if row is None:
                # The anchor's own value has not been computed yet. Skipping
                # rather than inventing one: the ordinary recompute owns
                # anchor buckets, and carrying from a bucket whose value we
                # do not know would fabricate the very number this figure is
                # supposed to be evidence for.
                continue
            anchors.append(Anchor(label=label, value=row.value, members=row.members))
            anchor_rows[label] = row
        if not anchors:
            continue

        # This subject's own calendar. A carried sequence is a run of
        # consecutive buckets, and which buckets are consecutive is a question
        # only a calendar answers -- so the one that cut the anchors has to be
        # the one that fills between them, or the fill lands on days the
        # subject's own rows never used.
        # One calendar for every subject, written in the definition, beats
        # the per-subject map -- which is empty in that case, because there
        # is no record to read one off.
        zone = written or zones.get(base)

        def labels_back(n: int, _at: float = at_ms, _zone: str | None = zone) -> list[str]:
            from ..windows import WindowSpec
            from .buckets import resolve_span

            return resolve_span(_at, _zone, WindowSpec(first=1, last=n), rule)

        try:
            sequence = sequence_to_present(anchors[0].label, labels_back, cap=cap)
        except CarryReachExceeded as refused:
            # **Contained to the subject that caused it.** The ceiling is a
            # fact about one subject's data -- one site whose first change is
            # dated 1998 -- and the loop has already written rows for the
            # subjects before it. Letting it out of `materialise` threw those
            # away: they stayed on the board, reported in no change stream,
            # and the next pass found them equal and said nothing either, so
            # they were never announced at all.
            log.warning("%s, subject %s: %s", plan.name, base, refused)
            continue

        wanted = carried_rows(anchors, sequence)

        # **Rows before the first anchor are retired.** Nothing else
        # revisits a bucket once written, so re-dating the first change from
        # February to May would leave March and April reporting a target
        # that was never in force -- indistinguishable from real rows,
        # because they *were* real until somebody corrected the date.
        #
        # Only *before* the earliest anchor, deliberately. Retiring
        # everything outside this call's sequence looked tidier and was
        # wrong: the sequence ends at the caller's own present, so a read
        # filling at today's date would delete the buckets a pass anchored
        # later had legitimately written, and the two triggers would spend
        # for ever undoing each other. Nothing before the first anchor can
        # be justified by any clock, which is what makes that edge safe to
        # act on and the other edge not.
        earliest = anchors[0].label
        for subject, row in list(held.items()):
            head, sep, label = subject.partition(SEPARATOR)
            if not sep or head != base or label >= earliest:
                continue
            await store.remove(tenant, plan.name, plan.version, subject)
            held.pop(subject, None)
            moved.append((subject, row.value, None))

        for row in wanted:
            if row.label in anchor_labels:
                # The ordinary recompute owns a bucket somebody changed
                # something in. Writing it again here would be a second
                # author for one row, and the two could drift.
                continue
            subject = compose([base, row.label])
            before = held.get(subject)
            if before is None:
                # **Insert-or-nothing.** Two readers racing on the same
                # unmaterialised bucket both compute it, benignly -- the rows
                # are identical because this function is the only thing that
                # makes them -- but only the one that actually inserted may
                # report a movement.
                inserted = await store.save_if_absent(
                    tenant,
                    plan.name,
                    plan.version,
                    subject,
                    row.value,
                    row.members,
                    anchor_rows[row.anchor].label,
                )
                if inserted:
                    moved.append((subject, None, row.value))
                continue
            if before.value == row.value and before.members == row.members:
                # An unchanged bucket writes nothing and reports nothing: a
                # pass in which nothing happened filling the log is how the
                # log stops being read.
                continue
            # The value in force changed -- a retroactive edit, reaching
            # forward from its own bucket. Buckets *before* it compute to
            # what they already hold and are skipped by the line above, so
            # history is left alone without a rule saying so.
            await store.save(
                tenant,
                plan.name,
                plan.version,
                subject,
                row.value,
                row.members,
                anchor_rows[row.anchor].label,
            )
            moved.append((subject, before.value, row.value))

    if moved:
        log.info(
            "carried %s to the present for %d bucket(s), triggered by %s",
            plan.name,
            len(moved),
            trigger,
        )
    return moved
