"""Which buckets of an index a record belongs to, and what a measure reads off it.

This is the only place record *contents* are looked at. Everything above it
works in ids, which is what makes a figure's declared dependencies true.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, assert_never
from zoneinfo import ZoneInfo

from ..lang.ast import ByAge, ByComposite, ByField, ByPredicate, ByPresence, IndexField
from ..lang.plan import CompiledIndex, CompiledMeasure

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


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


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


def day_range(at_ms: float, zone: str | None, days: int) -> tuple[str, str]:
    """The trailing window ending today, in the tenant's calendar.

    Anchored on the tenant's *local* day rather than on a UTC one: for a tenant
    ahead of UTC the current local day sorts after a UTC anchor, so the window
    would drop it and the board would report a team that has merged nothing all
    morning. One function computes both ends, so they cannot disagree.
    """
    end = day_in(at_ms, zone)
    start_ms = at_ms - (days - 1) * 86_400_000
    return day_in(start_ms, zone), end


# ------------------------------------------------------------ bucketing --


def buckets_of(
    index: CompiledIndex,
    record: Mapping[str, Any],
    settings: Mapping[str, Any],
    resolve: ThroughResolver,
    now_ms: float,
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
        days = float(_setting(settings, spec.setting))
        age_days = (now_ms - moment) / 86_400_000
        inside = age_days >= days if spec.direction == "older" else age_days < days
        return [""] if inside else []

    if isinstance(spec, ByField):
        return _keys_for(spec.part, record, settings, resolve)

    if isinstance(spec, ByComposite):
        combos: list[list[str]] = [[]]
        for part in spec.parts:
            keys = _keys_for(part, record, settings, resolve)
            if not keys:
                # A composite with a missing part collapses to no bucket, which
                # means the record counts for nobody. That is the honest answer:
                # a pair key with a hole in it is not a pair.
                return []
            combos = [[*prefix, key] for prefix in combos for key in keys]
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


def _keys_for(
    part: IndexField,
    record: Mapping[str, Any],
    settings: Mapping[str, Any],
    resolve: ThroughResolver,
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

    if part.truncate == "day":
        zone = _setting(settings, part.zone) if part.zone is not None else None
        out: list[str] = []
        for value in raw:
            moment = parse_instant(value)
            if moment is not None:
                out.append(day_in(moment, str(zone) if zone is not None else None))
        return sorted(set(out))

    return raw


def _setting(settings: Mapping[str, Any], path: str) -> Any:
    node: Any = settings
    for segment in path.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            raise KeyError(
                f'no value for setting "{path}". A definition named it, so bucketing cannot '
                "proceed without it -- and falling back would file every record under the "
                "wrong day or the wrong side of a threshold."
            )
        node = node[segment]
    return node


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
