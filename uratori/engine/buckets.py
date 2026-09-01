"""Which buckets of an index a record belongs to, and what a measure reads off it.

This is the only place record *contents* are looked at. Everything above it
works in ids, which is what makes a figure's declared dependencies true.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, assert_never

from ..lang.ast import ByAge, ByComposite, ByField, ByPredicate, ByPresence, IndexField
from ..lang.plan import CompiledIndex, CompiledMeasure
from ..windows import WindowSpec, _zone

SEPARATOR = "@"
"""What joins the parts of a composite key.

Printable on purpose: a subject id appears in URLs and on the Data screen, and a
control character makes the whole file it lives in unreadable to `grep` -- v1
used a NUL byte for exactly this join and its 1,500-line engine was classified
as binary and silently skipped by every search for as long as it existed.

A value containing the separator is refused rather than escaped, because an
escape scheme is a second thing to keep in step and no provider id has ever
contained one.
"""

OwnerReader = Callable[[str, str, str], list[Mapping[str, Any]]]
"""(fact kind, path, value) -> the *records* that own this value.

The same join `ThroughResolver` walks, answering the bodies rather than the
keys. An age filter reads its threshold off the owner -- `older than
stale_days from repo_id through code_repo.id` -- and a key cannot be read a
field off. Kept as a second callback rather than folded into the resolver so
that a pass which needs no field never pays to hold the bodies.
"""

ThroughResolver = Callable[[str, str, str], list[str]]
"""(fact kind, path, value) -> the ids of the records that own this value.

Resolves to *every* owner deliberately. An account claimed by two people is a
data problem an index should reflect rather than silently pick a winner for, and
an index can legitimately fan one record across both buckets. A projected field
cannot -- see `joined_value`.
"""


def part_of(value: str) -> str:
    if SEPARATOR in value:
        raise ValueError(
            f'a key part may not contain "{SEPARATOR}": it is what joins the parts of a '
            f"composite subject, and {value!r} would produce a subject that decomposes wrongly"
        )
    return value


def compose(parts: Sequence[str]) -> str:
    return SEPARATOR.join(part_of(p) for p in parts)


def subject_of(key: str) -> str:
    return key.split(SEPARATOR, 1)[0]


def tail_of(key: str) -> str | None:
    _, sep, rest = key.partition(SEPARATOR)
    return rest if sep else None


# ------------------------------------------------------------- reading --


def read_path(record: Mapping[str, Any], path: str) -> list[str]:
    """Every value at a path, flattened, as keys.

    Flattened because an index fans a record out across all of them:
    `accounts.accountId` means "any accountId of any account".

    **Finite numbers are keys.** They were not once, and the asymmetry was
    dangerous rather than narrow: an absent value satisfies `!=` by design, so a
    predicate over a numeric field matched *every record in the tenant* rather
    than none of them. An over-count, silently. Infinities and NaN are not keys,
    because they are not values anybody wrote down.
    """
    nodes: list[Any] = [record]
    for segment in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if isinstance(node, Mapping) and segment in node:
                found = node[segment]
                if isinstance(found, list):
                    nxt.extend(found)
                else:
                    nxt.append(found)
        nodes = nxt

    out: list[str] = []
    for node in nodes:
        if node is None:
            continue
        if isinstance(node, bool):
            out.append("true" if node else "false")
        elif isinstance(node, (int, float)) and node == node and node not in (
            float("inf"),
            float("-inf"),
        ):
            out.append(_number_key(float(node)))
        elif isinstance(node, str) and node != "":
            out.append(node)
    return out


def _number_key(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def read_number(record: Mapping[str, Any], path: str) -> float | None:
    """A *quantity* at a path.

    Deliberately a different function from `read_path`, which produces keys. A
    quantity that arrived as three numbers is not a quantity -- it is a shape the
    definition did not expect -- so absent, non-numeric and multiple all answer
    nothing rather than summing or first-winning.

    A numeric *string* is refused on purpose. Every provider writes a timestamp
    as an ISO string, and accepting strings here is how a date becomes a
    quantity.
    """
    nodes: list[Any] = [record]
    for segment in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if isinstance(node, Mapping) and segment in node:
                found = node[segment]
                nxt.extend(found) if isinstance(found, list) else nxt.append(found)
        nodes = nxt
    numbers = [n for n in nodes if isinstance(n, (int, float)) and not isinstance(n, bool)]
    if len(numbers) != 1:
        return None
    return float(numbers[0])


def read_instant(record: Mapping[str, Any], path: str) -> float | None:
    """An ISO instant at a path, as epoch milliseconds."""
    nodes: list[Any] = [record]
    for segment in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if isinstance(node, Mapping) and segment in node:
                found = node[segment]
                nxt.extend(found) if isinstance(found, list) else nxt.append(found)
        nodes = nxt
    for node in nodes:
        if isinstance(node, str) and node:
            parsed = parse_instant(node)
            if parsed is not None:
                return parsed
    return None


def parse_instant(text: str) -> float | None:
    try:
        cleaned = text.replace("Z", "+00:00")
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() * 1000.0


def day_in(epoch_ms: float, zone: str | None) -> str:
    """Which calendar day an instant falls on, in a named zone.

    A bare `by day` means UTC, and that is a choice a definition makes rather
    than a default it falls into: two figures on one card, one cut by UTC days
    and one by the tenant's, would be two rows headed "30d" measuring two
    different months.
    """
    moment = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
    if zone is not None:
        moment = moment.astimezone(_zone(zone))
    return moment.date().isoformat()


_GRAIN_MINUTES: dict[str, int] = {"minute": 1, "15 minutes": 15, "hour": 60}


def label_in(epoch_ms: float, zone: str | None, grain: str) -> str:
    """Which bucket an instant falls in, at a grain, in a named zone.

    A sub-day label is local time truncated to the grain -- `2026-08-25T14:30`
    -- exactly as a day key is the local date, so grouping a label into its
    hour or its day is prefix truncation and no zone arithmetic happens at
    read time. Fixed width per grain, so labels sort lexicographically and the
    store's range scan works unchanged.

    When the clocks go back the repeated hour's two passes share their labels
    and their records share a bucket -- the honest answer about a quarter-hour
    that occurred twice. Keying by UTC instead would put every local midnight
    mid-bucket, a constant error to avoid a twice-a-year merge.
    """
    if grain == "day":
        return day_in(epoch_ms, zone)
    if grain in ("week", "month", "quarter"):
        # A coarse label is the *local day's* week, month or quarter: the
        # zone is applied once, to find the day, and the rest is calendar
        # arithmetic -- so a month figure and a day figure over one event
        # can never disagree about which month the event's day was in.
        return label_of_day(date.fromisoformat(day_in(epoch_ms, zone)), grain)
    moment = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
    if zone is not None:
        moment = moment.astimezone(_zone(zone))
    step = _GRAIN_MINUTES[grain]
    if step == 60:
        return f"{moment.date().isoformat()}T{moment.hour:02d}:00"
    return f"{moment.date().isoformat()}T{moment.hour:02d}:{moment.minute - moment.minute % step:02d}"


def selected_day(epoch_ms: float, zone: str | None, rule: str) -> str | None:
    """The day an instant buckets under for a selective rule, or None.

    `first monday of month` files an instant on the first Monday of its
    (zoned) month under that day's date -- and every other instant under
    nothing at all. The partiality *is* the filter: it falls out of the
    function being undefined off-rule, the same doctrine as `is set`, so
    there is no separate narrowing step a cheap path could skip.
    """
    ordinal = ordinal_rule_of(rule)
    assert ordinal is not None, f'"{rule}" is not a selective rule'
    local_day = day_in(epoch_ms, zone)
    which, weekday = ordinal
    match = ordinal_weekday_day(int(local_day[:4]), int(local_day[5:7]), which, weekday)
    return local_day if match == local_day else None


def end_of_day_ms(day: str, zone: str | None) -> float:
    """The last millisecond of a calendar day in the tenant's zone.

    This is what an anchor date resolves to: a caller asking for "the window
    ending 2026-06-30" means the whole of that local day, so the anchoring
    instant is its final moment. The day's *start* would file under the right
    day too, but it would travel on the response as an `at` claiming the
    answer predates almost all of the data it covers -- provenance that reads
    backwards.

    Built from the next local midnight rather than 23:59:59.999 directly:
    even when a transition swallows that midnight, `fold=0` maps the missing
    wall time to the transition instant itself -- the first real instant of
    the next day -- so one millisecond earlier is the anchor day's true last
    moment whatever the clocks did.
    """
    anchor = date.fromisoformat(day)
    tz = _zone(zone) if zone is not None else UTC
    if anchor == date.max:
        # The one day with no next midnight inside `date`'s range, so it is
        # built from its own midnight instead: every zone's transition rules
        # end centuries earlier, making this day exactly 24 hours long. Not
        # `datetime.max.timestamp()`, because a float second at year 9999
        # cannot carry the last microsecond -- it rounds up to the midnight
        # beyond `datetime`'s range and the very overflow this branch exists
        # to answer comes back during rendering.
        midnight = datetime.combine(anchor, dt_time(), tzinfo=tz)
        # Clamped at the calendar's own last representable instant: west of
        # UTC, 9999-12-31's final local millisecond lands in the year 10000
        # by UTC's clock, which `datetime.fromtimestamp` cannot carry -- so
        # the unclamped value blew up later, in `day_in` and `_iso`, as a
        # traceback worn by a well-formed question. The clamp stays inside
        # the anchor's local day (UTC's 23:59:59.999 is still 9999-12-31
        # everywhere west of UTC), so day windows end on the day asked
        # about. What moves with the clamped instant, west of UTC on this
        # one day, is the provenance `at` and the anchor *bucket* of a
        # sub-day span -- the day's true final hour is literally
        # unaddressable, and a span counted from the last representable
        # bucket (the wire stating exactly which buckets it covered) is the
        # honest trade against having no answer at all.
        last_utc = (
            datetime.combine(anchor, dt_time(), tzinfo=UTC).timestamp() * 1000.0
            + 86_400_000.0
            - 1.0
        )
        return min(midnight.timestamp() * 1000.0 + 86_400_000.0 - 1.0, last_utc)
    next_midnight = datetime.combine(anchor + timedelta(days=1), dt_time(), tzinfo=tz)
    return next_midnight.timestamp() * 1000.0 - 1.0


def day_range(at_ms: float, zone: str | None, days: int) -> tuple[str, str]:
    """The trailing window ending today, in the tenant's calendar.

    Anchored on the tenant's *local* day rather than on a UTC one: for a tenant
    ahead of UTC the current local day sorts after a UTC anchor, so the window
    would drop it and the board would report a team that has merged nothing all
    morning. One function computes both ends, so they cannot disagree.

    The start is calendar arithmetic on the end day, not millisecond
    subtraction from the instant: a fall-back inside the window makes one of
    its days 25 hours long, so stepping back exact days from an instant
    within an hour of midnight crosses one midnight too few and the window
    quietly opens a day late -- and an anchored request sits at a day's last
    millisecond, which is exactly the exposed hour. (The spring-forward
    direction only pushes the wall clock further from midnight, which is why
    the bug hid for as long as every request was anchored on now.)

    The start clamps at the calendar's own edge rather than raising: enough
    trailing days from an early enough anchor walk out of `date`'s range, and
    "the window opens where the calendar begins" is an honest answer where an
    OverflowError is a 500.
    """
    end = day_in(at_ms, zone)
    end_date = date.fromisoformat(end)
    back = min(days - 1, (end_date - date.min).days)
    return (end_date - timedelta(days=back)).isoformat(), end


# ------------------------------------------------------ bucket sequences --
#
# A bucket rule names an ordered sequence of buckets; a window spec is a span
# of positions in it, bucket 1 being the bucket the anchor instant falls in.
# Every rule comes from one place -- the group clause of the figure being
# read, hashed into its version. The total grains (`minute`, `15 minutes`,
# `hour`, `day`, `week`, `month`, `quarter`) file every instant somewhere;
# the ordinal weekday-of-month family (`first monday of month`) is
# *selective*, sparse day buckets one per month at most. There is no
# read-time rule and no reading-level clause to declare one: a coarser view
# is its own figure under its own name, so nothing here re-slices what
# another rule stored.
#
# Resolving a span is calendar arithmetic on the anchor's *local day*: the
# zone is applied once, to find that day, and the rest is the calendar's --
# so the sequence a window walks is spelled exactly as `label_in` spelled
# the buckets when it wrote them, and a span can never name a label the
# store could not hold.

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ORDINALS = ("first", "second", "third", "fourth", "fifth")


def ordinal_rule_of(rule: str) -> tuple[int, int] | None:
    """`"first monday of month"` -> (1, 0) -- ordinal 1..5, weekday 0=Monday.

    None for anything that is not an ordinal weekday-of-month rule. The rule
    travels as its own declaration text so every surface -- hash, wire, UI --
    spells it exactly one way.
    """
    words = rule.split(" ")
    if len(words) != 4 or words[2] != "of" or words[3] != "month":
        return None
    if words[0] not in _ORDINALS or words[1] not in _WEEKDAYS:
        return None
    return _ORDINALS.index(words[0]) + 1, _WEEKDAYS.index(words[1])


def ordinal_weekday_day(year: int, month: int, ordinal: int, weekday: int) -> str | None:
    """The date of e.g. the first Monday of a month, or None when the month
    has no such day (a fifth Monday exists in some months only).

    Counted as a day *of the month*, never by adding days to the first: the
    fifth weekday of December 9999 lands in January 10000, and building that
    date to then reject it raises `OverflowError` -- an `ArithmeticError`,
    which the routes do not catch, so it reaches the client as a 500 rather
    than as the "no such day" this returns. `?at=9999-12-31` is an accepted
    anchor and `end_of_day_ms` has a branch for exactly it, so the far edge
    of the calendar is a place this function is genuinely asked about; a
    record stamped in that month reaches it through `selected_day` on the
    write path too.
    """
    day_of_month = 1 + (weekday - date(year, month, 1).weekday()) % 7 + (ordinal - 1) * 7
    if day_of_month > monthrange(year, month)[1]:
        return None
    return date(year, month, day_of_month).isoformat()


def _month_of(label: str) -> tuple[int, int]:
    return int(label[:4]), int(label[5:7])


def _month_label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _quarter_label(year: int, month: int) -> str:
    return f"{year:04d}-Q{(month - 1) // 3 + 1}"


def _week_label(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def label_of_day(local_day: date, grain: str) -> str:
    """Which bucket a *local calendar day* belongs to, at a grain.

    The half of `label_in` that happens after the zone has been applied,
    lifted out so a span can enumerate labels by walking days without
    round-tripping each one back through an epoch and a timezone database.
    Called by `label_in` itself, so the two cannot disagree about what a
    given day's week is.
    """
    if grain == "day":
        return local_day.isoformat()
    if grain == "week":
        return _week_label(local_day)
    if grain == "month":
        return _month_label(local_day.year, local_day.month)
    return _quarter_label(local_day.year, local_day.month)


def _next_period(local_day: date, grain: str) -> date:
    """The first day of the next bucket after the one `local_day` is in.

    Stepping by period rather than by day, so enumerating a two-year span at
    week grain walks 104 times instead of 730. Landing on the *first* day of
    the next bucket (rather than adding a period to an arbitrary day) is what
    makes the walk exact at month ends: 31 January plus a month is a question
    with no good answer, and 1 February is not.
    """
    if grain == "day":
        return local_day + timedelta(days=1)
    if grain == "week":
        # ISO weeks start on Monday. `weekday()` is 0 for Monday, so this
        # lands on the next Monday from any day of the week.
        return local_day + timedelta(days=7 - local_day.weekday())
    step = 1 if grain == "month" else 3
    month = local_day.month - 1 + step
    if grain == "quarter":
        # Snap to the quarter's own boundary first, or a span starting in
        # February would walk February, May, August -- periods that are three
        # months apart and not quarters.
        month = ((local_day.month - 1) // 3 + 1) * 3
    year = local_day.year + month // 12
    return date(year, month % 12 + 1, 1)


def labels_between(
    start_ms: float, end_ms: float, zone: str | None, grain: str
) -> list[str]:
    """Every bucket label the stretch from `start_ms` to `end_ms` touches.

    **Both ends inclusive.** A campaign that starts and finishes inside one
    week is in that week rather than in none -- the half-open reading loses the
    shortest spans entirely, which is the arithmetic mistake worth naming here
    because it looks so much like the right one.

    Backwards ends produce nothing rather than being reversed: a far end before
    a near end is somebody's typo, and quietly swapping them reports a run
    across a stretch nobody booked.

    Both ends are truncated in the same calendar, which is the other thing that
    has to be true and is invisible when a fixture is UTC-only: an instant is
    the Sunday of one week in London and the Monday of the next in Auckland, so
    a span with one end cut in the subject's zone and the other in UTC is
    right for exactly one of them.
    """
    first = date.fromisoformat(day_in(start_ms, zone))
    last = date.fromisoformat(day_in(end_ms, zone))
    if last < first:
        return []
    out: list[str] = []
    cursor = first
    while cursor <= last:
        out.append(label_of_day(cursor, grain))
        if cursor >= _LAST_PERIOD[grain]:
            # **The calendar's far edge, guarded rather than stepped past.**
            # `9999-12-31` is how a provider spells "runs for ever", so it
            # arrives through the facts door -- and stepping a cursor past it
            # raises, which `Engine.run` turns into a dead pass for the whole
            # tenant, on every pass, until somebody edits the record. The
            # sibling walker `resolve_span` learned this the same way; the
            # comment above it records that the traceback moved once already.
            break
        cursor = _next_period(cursor, grain)
    return out


_LAST_PERIOD: dict[str, date] = {
    "day": date.max,
    # The Monday of the last ISO week that fits, and the first day of the last
    # month and quarter: stepping from any earlier day inside those periods is
    # safe, and stepping from these is what overflows.
    "week": date.max - timedelta(days=date.max.weekday()),
    "month": date(date.max.year, date.max.month, 1),
    "quarter": date(date.max.year, ((date.max.month - 1) // 3) * 3 + 1, 1),
}
"""The last period at each grain, as the day a walk must stop on."""


def resolve_span(at_ms: float, zone: str | None, spec: WindowSpec, rule: str) -> list[str]:
    """A span of positions resolved to the concrete bucket labels it covers,
    oldest first, counted back from the anchor in the tenant's calendar.

    Bucket 1 is the bucket the anchor instant falls in -- the anchor day,
    the anchor month, the most recent first-Monday at or before the anchor
    -- so `1-N` over days covers exactly what the trailing window always
    has. The list is the answer's evidence: it travels (as edges, or whole
    for a sparse rule) so "buckets 31-60" is never the client's guess.

    Sub-day buckets step back through **label space**: local wall-clock
    arithmetic on the bucket label, ignoring transitions, because that is
    the calendar the store's labels live in. The fall-back hour's two
    passes merged into one label at write time, and the spring-forward
    hour's labels name buckets that hold nothing; counting them as
    positions keeps "48 hour-buckets" meaning the same wall-clock stretch
    on every day of the year.

    Every edge clamps at the calendar's own beginning rather than raising:
    "the span opens where the calendar begins" is an honest answer where an
    OverflowError is a 500. A clamped span simply covers fewer buckets.
    """
    anchor_day = date.fromisoformat(day_in(at_ms, zone))

    if rule == "day":
        newest = anchor_day - timedelta(days=min(spec.first - 1, (anchor_day - date.min).days))
        oldest = newest - timedelta(days=min(spec.last - spec.first, (newest - date.min).days))
        out: list[str] = []
        day = oldest
        while day <= newest:
            out.append(day.isoformat())
            if day == date.max:  # pragma: no cover - newest is <= anchor day
                break
            day += timedelta(days=1)
        return out

    if rule == "week":
        anchor_monday = anchor_day - timedelta(days=anchor_day.weekday())
        weeks: list[str] = []
        for k in range(spec.last, spec.first - 1, -1):
            back = timedelta(days=7 * (k - 1))
            if anchor_monday - date.min < back:
                continue
            weeks.append(_week_label(anchor_monday - back))
        return weeks

    if rule in ("month", "quarter"):
        step = 1 if rule == "month" else 3
        year, month = anchor_day.year, anchor_day.month
        if rule == "quarter":
            # Normalise the anchor to its quarter's first month, so the loop
            # below steps between quarters rather than between arbitrary
            # months inside them. A no-op for the label as written -- stepping
            # three months from any month of a quarter lands in the
            # corresponding month of the target quarter, which carries the
            # same label -- and kept because it makes the stepping mean what
            # it says: the sequence is quarters, not months counted by three.
            month = ((month - 1) // 3) * 3 + 1
        labels: list[str] = []
        for k in range(spec.last, spec.first - 1, -1):
            months_back = (k - 1) * step
            y, m = year, month - months_back
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            if y < 1:
                continue
            labels.append(_month_label(y, m) if rule == "month" else _quarter_label(y, m))
        return labels

    ordinal = ordinal_rule_of(rule)
    if ordinal is not None:
        # The sequence is the calendar's own: every month's matching day, in
        # order, months without one (a fifth Monday) contributing no bucket.
        # Enumerated from the calendar rather than from the data, because a
        # month whose bucket holds nothing is a hole in the window, never a
        # skipped position -- skipping it would quietly narrow the window.
        which, weekday = ordinal
        found: list[str] = []
        year, month = anchor_day.year, anchor_day.month
        while len(found) < spec.last and year >= 1:
            candidate = ordinal_weekday_day(year, month, which, weekday)
            if candidate is not None and candidate <= anchor_day.isoformat():
                found.append(candidate)
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        covered = found[spec.first - 1 : spec.last]
        return list(reversed(covered))

    if rule in ("hour", "15 minutes", "minute"):
        moment = datetime.fromtimestamp(at_ms / 1000.0, tz=UTC)
        if zone is not None:
            moment = moment.astimezone(_zone(zone))
        step = {"hour": 60, "15 minutes": 15, "minute": 1}[rule]
        anchor_bucket = moment.replace(
            minute=moment.minute - moment.minute % step if step != 60 else 0,
            second=0,
            microsecond=0,
            tzinfo=None,
        )
        floor = datetime.min
        newest_dt = anchor_bucket - min(
            timedelta(minutes=step * (spec.first - 1)), anchor_bucket - floor
        )
        oldest_dt = newest_dt - min(
            timedelta(minutes=step * (spec.last - spec.first)), newest_dt - floor
        )
        labels_out: list[str] = []
        cursor = oldest_dt
        while cursor <= newest_dt:
            labels_out.append(f"{cursor.date().isoformat()}T{cursor.hour:02d}:{cursor.minute:02d}")
            if cursor > datetime.max - timedelta(minutes=step):  # pragma: no cover
                break
            cursor += timedelta(minutes=step)
        return labels_out

    raise ValueError(f'"{rule}" is not a bucket rule')


# ------------------------------------------------------------ bucketing --


def buckets_of(
    index: CompiledIndex,
    record: Mapping[str, Any],
    resolve: ThroughResolver,
    now_ms: float,
    owners: OwnerReader | None = None,
    zones: Mapping[str, str] | None = None,
) -> list[str]:
    """Which buckets this record belongs to. Empty means it is in none."""
    spec = index.spec

    if isinstance(spec, ByPredicate):
        values = read_path(record, spec.field)
        if spec.op == "==":
            return [""] if spec.value in values else []
        # **An absent value satisfies `!=` by design**: a record with no `state`
        # is not in state "merged". That is right, and it is exactly why a
        # presence test needs its own arm rather than being spelled
        # `!= "0"` -- see `ByPresence`.
        return [""] if spec.value not in values else []

    if isinstance(spec, ByPresence):
        present = _is_set(record, spec.field)
        return [""] if present != spec.negated else []

    if isinstance(spec, ByAge):
        moment = read_instant(record, spec.field)
        if moment is None:
            return []
        days = _age_threshold(spec, record, owners)
        if days is None:
            # No threshold, so no membership. Never a default and never the
            # whole population: an owner nobody has collected yet is a
            # staleness rule nobody has stated, and guessing one files records
            # under a line the definition never drew.
            return []
        age_days = (now_ms - moment) / 86_400_000
        inside = age_days >= days if spec.direction == "older" else age_days < days
        return [""] if inside else []

    if isinstance(spec, ByField):
        return _keys_for(spec.part, record, resolve, None, zones, now_ms)

    if isinstance(spec, ByComposite):
        # **Not a plain cross product any more.** A time part's calendar is a
        # field on the *subject*, so which day an instant falls in depends on
        # which subject's column it is being filed in: one 08:00 UTC merge is
        # the 25th for a courier in Tokyo and the 24th for one in London.
        # Computing each part's keys once and crossing them would have to pick
        # one of those days for both, silently.
        #
        # So the keys of each part are computed against the prefix already
        # accumulated, whose first element is the subject. For every part that
        # reads no calendar this is the same cross product it always was.
        combos: list[list[str]] = [[]]
        for part in spec.parts:
            grown: list[list[str]] = []
            for prefix in combos:
                keys = _keys_for(
                    part, record, resolve, prefix[0] if prefix else None, zones, now_ms
                )
                grown.extend([*prefix, key] for key in keys)
            if not grown:
                # A composite with a missing part collapses to no bucket, which
                # means the record counts for nobody. That is the honest answer:
                # a pair key with a hole in it is not a pair.
                return []
            combos = grown
        return [compose(c) for c in combos]

    assert_never(spec)


def _is_set(record: Mapping[str, Any], path: str) -> bool:
    """Whether somebody has *said* something.

    Nought counts as absent, and that is a decision rather than an oversight:
    Jira writes `0` and `null` into the same field for the same state -- nobody
    sized this -- so honouring the difference would move a coverage figure when
    an operator cleared a box rather than when anybody estimated anything.

    A boolean `false` counts as **present**, because somebody answered.
    """
    nodes: list[Any] = [record]
    for segment in path.split("."):
        nxt: list[Any] = []
        for node in nodes:
            if isinstance(node, Mapping) and segment in node:
                found = node[segment]
                nxt.extend(found) if isinstance(found, list) else nxt.append(found)
        nodes = nxt
    for node in nodes:
        if isinstance(node, bool):
            return True
        if isinstance(node, (int, float)) and node != 0:
            return True
        if isinstance(node, str) and node != "":
            return True
    return False


def _age_threshold(
    spec: ByAge, record: Mapping[str, Any], owners: OwnerReader | None
) -> float | None:
    """How many days this filter's line is drawn at, for this record.

    A number in the definition, or a field on the record's owner. The owner
    path answers None wherever the join does not land on exactly one readable
    number -- no owner, several owners disagreeing, or a value that is not a
    number -- because each of those is a threshold nobody stated, and the
    filter is what a stated threshold produces.
    """
    if spec.days is not None:
        return spec.days
    if spec.read is None:
        return None
    if spec.through is None:
        # The line on the record's own column. Same rule as the join below:
        # a record holding no number, or two, has no line -- and a record
        # with no line is not a record whose line is nought.
        held = read_path(record, spec.read)
        if len(held) != 1:
            return None
        try:
            return float(held[0])
        except (TypeError, ValueError):
            return None
    if owners is None or spec.local is None:
        return None
    keys = read_path(record, spec.local)
    if len(keys) != 1:
        # A record naming two owners has two staleness rules and no way to
        # choose, which is the projection join's rule one construct along.
        return None
    found = owners(spec.through.kind, spec.through.path, keys[0])
    if len(found) != 1:
        return None
    values = read_path(found[0], spec.read)
    if len(values) != 1:
        return None
    try:
        return float(values[0])
    except (TypeError, ValueError):
        return None


def _keys_for(
    part: IndexField,
    record: Mapping[str, Any],
    resolve: ThroughResolver,
    subject: str | None,
    zones: Mapping[str, str] | None,
    now_ms: float,
) -> list[str]:
    raw = read_path(record, part.field)
    if not raw:
        return []

    if part.through is not None:
        owners: list[str] = []
        for value in raw:
            owners.extend(resolve(part.through.kind, part.through.path, value))
        raw = sorted(set(owners))
        if not raw:
            return []

    if part.truncate is not None or part.select is not None:
        zone_name: str | None = None
        if part.zone is not None and part.zone.named is not None:
            # One calendar for the board, written in the definition. No lookup
            # and no subject to miss on, which is the whole point of it.
            zone_name = part.zone.named
        elif part.zone is not None:
            assert part.zone.field is not None
            if subject is not None:
                # The subject's own calendar. Absent means the subject has no
                # calendar recorded, and a record cannot be filed under a day
                # nobody has said how to cut -- so it is in no bucket, never
                # in UTC's.
                zone_name = (zones or {}).get(subject)
            else:
                # No subject part, so the record is the thing: read the
                # calendar off it directly. The checker has already required
                # the kind named to be this record's own.
                held = read_path(record, part.zone.field)
                zone_name = str(held[0]) if len(held) == 1 else None
            if zone_name is None:
                return []
        if part.until is not None:
            # A span: every bucket between two instants, rather than the one
            # bucket an instant fell in.
            #
            # `raw` may hold several near ends if the path crossed a list; the
            # far end may not (the checker refuses it), so the span is read
            # from the earliest near end to the one far end. Either end
            # missing is no membership at all -- never a half-open span
            # running to now or to for ever, because a booking with no end is
            # one nobody has scheduled.
            far_values = read_path(record, part.until)
            far = parse_instant(far_values[0]) if len(far_values) == 1 else None
            nears = [m for m in (parse_instant(v) for v in raw) if m is not None]
            if far is None or not nears:
                return []
            labels = labels_between(min(nears), far, zone_name, part.truncate or "day")
            if part.ahead_only:
                # Drop the buckets whose period has already gone, in the same
                # calendar the labels were cut in. Comparing labels rather than
                # instants is what makes that exact: the pass instant's own
                # label is the period in progress, and a label sorts against
                # its siblings, so ">= this one" is "this period or later"
                # without any boundary arithmetic to get wrong.
                #
                # The period in progress is **kept**. Dropping it would make
                # the near future -- the only part anybody can still act on --
                # the one part missing from the answer.
                current = label_in(now_ms, zone_name, part.truncate or "day")
                labels = [label for label in labels if label >= current]
            return labels

        out: list[str] = []
        for value in raw:
            moment = parse_instant(value)
            if moment is None:
                continue
            if part.select is not None:
                # A selective rule is partial: an instant off the rule's day
                # is in no bucket, and that absence is the declaration's own
                # filter rather than a narrowing step.
                day = selected_day(moment, zone_name, part.select)
                if day is not None:
                    out.append(day)
            else:
                assert part.truncate is not None  # exactly one of the two is set
                out.append(label_in(moment, zone_name, part.truncate))
        return sorted(set(out))

    return raw


# ------------------------------------------------------------- measures --


def measure_of(
    measure: CompiledMeasure, record: Mapping[str, Any], at_ms: float | None
) -> float | None:
    """A measure's number for one record.

    `at_ms` is the instant a live reading was asked at, and a clock measure
    **raises** rather than falling back to the current time. A per-record clock
    never produces a visibly wrong number -- it produces a queue whose oldest
    wait disagrees with itself by milliseconds and an `at` on the response that
    describes one row of it.
    """
    if measure.shape == "field":
        assert measure.field_path is not None
        return read_number(record, measure.field_path)

    if measure.shape == "moment":
        assert measure.moment is not None
        return read_instant(record, measure.moment)

    assert measure.earlier is not None
    earlier = read_instant(record, measure.earlier)
    if earlier is None:
        return None
    if measure.clock:
        if at_ms is None:
            raise ValueError(
                f"{measure.name} is measured to now, and no instant was supplied. One instant "
                "must reach every record of a response, or the oldest wait in a queue "
                "disagrees with itself."
            )
        later: float | None = at_ms
    else:
        assert measure.later is not None
        later = read_instant(record, measure.later)
    if later is None:
        return None
    return (later - earlier) / 1000.0


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_day(text: str) -> bool:
    return bool(_ISO_DAY.match(text))


def days_between(start_ms: float | None, end_ms: float | None) -> float | None:
    """Signed calendar days, so "overdue by three" and "three days left" are one
    expression. Nothing when either end is missing: an epic with no due date has
    no days remaining, which is not nought -- nought is today."""
    if start_ms is None or end_ms is None:
        return None
    return (end_ms - start_ms) / 86_400_000


def add_days(epoch_ms: float, days: float) -> float:
    return (
        datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC) + timedelta(days=days)
    ).timestamp() * 1000.0
