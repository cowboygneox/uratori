"""The bundle: a named composition of definitions, served as one request.

A bundle defines no calculation -- members are names plus arguments, nothing
else -- so every test here is about the two things a bundle *does* claim: the
checker refusals that make a broken tile a build error rather than a
serve-time surprise, and the serving shape -- members' own `Result`s, in
declaration order, at one instant, with a summary that travels without its
projection's rows and is still computed over all of them.
"""

from __future__ import annotations

import pytest

from uratori import (
    MemoryEngineStore,
    MemoryFactStore,
    Ok,
    Result,
    Schema,
    Uratori,
)
from uratori import compile_source as compile_against
from uratori.lang.check import CheckError
from uratori.lang.lex import SyntaxError_
from uratori.results import BundleResult
from uratori.store.base import FactRow

from .test_lang import BASE
from .world import compile_source

# The declarations the bundles below compose: a plain figure, a day-keyed
# figure, a windowed reading over it, a live reading, a dimensioned figure,
# a projection and its summary. BASE already carries team_person.wip and the
# day-keyed team_person.time_to_merge.
MEMBERS = """
# Open changes per source.
figure team_person.open_by_source across data_connection:
    display "{team_person} open in {data_connection}"
    depends:
        mine = code_change.authored_in:{team_person} & code_change.open
    calculate:
        count(mine)

# How long this person's changes took to merge.
reading team_person.to_merge(range):
    display "{team_person} to merge"
    depends:
        merged = team_person.time_to_merge in range
    calculate:
        mean(merged)

# Review asks waiting on this person right now.
reading team_person.pending_reviews():
    display "{team_person} pending"
    depends:
        waiting = code_review_request.waiting_seconds over (code_review_request.asked_of:{team_person} & code_review_request.pending)
    calculate:
        count(waiting)

# One row per work item.
projection work_issue.item:
    field:
        title = title as text

# The backlog, in one row.
summarise work_issue.backlog over work_issue.item:
    count items
"""

CARD = """
# Everything the person card shows.
bundle team_person.card:
    reading team_person.to_merge over 7, 14, 30
    figure team_person.wip
    projection work_issue.item
    summarise work_issue.backlog
"""


def compile_ok(extra: str = ""):
    return compile_source(BASE + MEMBERS + extra)


def refuses(extra: str, *fragments: str) -> str:
    with pytest.raises(CheckError) as caught:
        compile_source(BASE + MEMBERS + extra)
    message = caught.value.message
    for fragment in fragments:
        assert fragment in message, f"expected {fragment!r} in {message!r}"
    return message


# ------------------------------------------------------------- compiles --


def test_a_bundle_compiles_and_keeps_its_members_in_declaration_order() -> None:
    """The control for every refusal below -- and the order is substantive:
    a tile's members travel in the order the definition wrote them, so the
    plan must hold that order rather than any sorted convenience."""
    lib = compile_ok(CARD)
    plan = lib.bundle("team_person.card")
    assert plan is not None
    assert plan.doc == "Everything the person card shows."
    assert [(m.kind, m.name) for m in plan.members] == [
        ("reading", "team_person.to_merge"),
        ("figure", "team_person.wip"),
        ("projection", "work_issue.item"),
        ("summary", "work_issue.backlog"),
    ]
    assert plan.members[0].windows == (7, 14, 30)
    assert plan.members[1].windows is None
    assert plan.version


def test_a_windowed_reading_member_may_leave_its_windows_unwritten() -> None:
    """The window list is optional: absent, the serving default decides, the
    same way the results surface serves an unqualified reading."""
    lib = compile_ok(
        "\n# A card with the default windows.\n"
        "bundle team_person.plain:\n"
        "    reading team_person.to_merge\n"
    )
    plan = lib.bundle("team_person.plain")
    assert plan is not None
    assert plan.members[0].windows is None


def test_a_live_reading_member_is_named_bare() -> None:
    """The control for the windows-on-live refusal below."""
    lib = compile_ok(
        "\n# The queue, right now.\n"
        "bundle team_person.queue:\n"
        "    reading team_person.pending_reviews\n"
    )
    assert lib.bundle("team_person.queue") is not None


# ------------------------------------------------------------- refusals --


def test_a_bundle_requires_an_explanation() -> None:
    """A bundle is served to a reader by name, so like every rendered kind it
    is refused without the `#` prose above it."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(BASE + MEMBERS + "\nbundle team_person.bare:\n    figure team_person.wip\n")
    assert "has no explanation" in caught.value.message


def test_a_member_that_resolves_to_nothing_is_refused() -> None:
    refuses(
        "\n# A card over a typo.\n"
        "bundle team_person.card:\n"
        "    figure team_person.wpi\n",
        'there is no figure called "team_person.wpi"',
    )


def test_a_member_written_under_the_wrong_keyword_is_refused_by_what_it_is() -> None:
    """`figure X` where X is a projection would compile to a member whose
    payload is nothing like what the keyword promised -- the reader of the
    bundle is told rows travel, and a figure's subjects arrive instead."""
    refuses(
        "\n# A card mislabelling a projection.\n"
        "bundle team_person.card:\n"
        "    figure work_issue.item\n",
        "names work_issue.item as a figure",
        "it is a projection",
    )


def test_a_bundle_may_not_name_another_bundle_by_keyword() -> None:
    """Composition stays flat: no nesting, no cycles to refuse."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + MEMBERS
            + CARD
            + "\n# A card of cards.\n"
            "bundle team_person.mega:\n"
            "    bundle team_person.card\n"
        )
    assert "may not name another bundle" in caught.value.message


def test_a_bundle_may_not_name_another_bundle_under_a_member_keyword_either() -> None:
    """The flatness rule holds against the sneaky spelling too: a bundle
    smuggled in as `figure <bundle>` is refused as a bundle, not as a typo."""
    refuses(
        CARD + "\n# A card of cards, wearing a figure keyword.\n"
        "bundle team_person.mega:\n"
        "    figure team_person.card\n",
        "which is a bundle",
        "flat",
    )


def test_a_duplicate_member_is_refused() -> None:
    refuses(
        "\n# A card that stutters.\n"
        "bundle team_person.card:\n"
        "    figure team_person.wip\n"
        "    figure team_person.wip\n",
        "names team_person.wip twice",
    )


def test_windows_on_a_live_reading_are_refused() -> None:
    """Written `over 7`, a live member would accept a window, ignore it, and
    return today's answer under a heading saying seven days -- the exact
    mistake the language's (range)/() redundancy exists to make loud."""
    refuses(
        "\n# A card windowing the un-windowed.\n"
        "bundle team_person.card:\n"
        "    reading team_person.pending_reviews over 7\n",
        "measures records as they stand",
        "nothing stored to window",
    )


def test_windows_on_a_figure_member_are_refused_at_parse() -> None:
    """A figure has one current value; a window list on it is a category
    error the parser can already see."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + MEMBERS
            + "\n# A card windowing a figure.\n"
            "bundle team_person.card:\n"
            "    figure team_person.wip over 7\n"
        )
    assert "only a reading member takes a window list" in caught.value.message


def test_a_window_must_be_a_whole_positive_number_of_days() -> None:
    for wrong in ("0", "7.5"):
        with pytest.raises(SyntaxError_) as caught:
            compile_source(
                BASE
                + MEMBERS
                + "\n# A card with a nonsense window.\n"
                "bundle team_person.card:\n"
                f"    reading team_person.to_merge over {wrong}\n"
            )
        assert "whole number of trailing days" in caught.value.message, wrong


def test_a_duplicate_window_is_refused_as_the_typo_it_is() -> None:
    """`over 7, 7` would serve the same window twice, and a screen binding
    to window positions would show a duplicated column from a typo -- the
    same shape of mistake a duplicate member is."""
    with pytest.raises(SyntaxError_) as caught:
        compile_source(
            BASE
            + MEMBERS
            + "\n# A card that stutters a window.\n"
            "bundle team_person.card:\n"
            "    reading team_person.to_merge over 7, 7\n"
        )
    assert "twice" in caught.value.message


def test_a_day_keyed_figure_member_is_refused_toward_a_reading() -> None:
    """The same refusal the bulk results surface applies: a tile subscribing
    to every stored person-day would carry the board's history on every
    request, and what a tile wants is the statistic -- a reading."""
    refuses(
        "\n# A card dragging history along.\n"
        "bundle team_person.card:\n"
        "    figure team_person.time_to_merge\n",
        "time-keyed",
        "reading",
    )


def test_a_dimensioned_figure_member_is_refused_toward_the_rollup() -> None:
    refuses(
        "\n# A card serving raw pairs.\n"
        "bundle team_person.card:\n"
        "    figure team_person.open_by_source\n",
        "split across data_connection",
        "rollup",
    )


def test_a_bundle_shares_the_one_namespace() -> None:
    refuses(
        "\n# A bundle stealing a figure's name.\n"
        "bundle team_person.wip:\n"
        "    figure team_person.wip\n",
        "team_person.wip is already a figure",
    )


def test_a_bundle_needs_a_fact_kind_prefix() -> None:
    refuses(
        "\n# A bundle about nothing the schema knows.\n"
        "bundle shop_thing.card:\n"
        "    figure team_person.wip\n",
        "not a fact kind",
    )


# ------------------------------------------------------------ versioning --


def test_prose_does_not_move_a_bundles_version_and_members_do() -> None:
    """The hash is the review surface: rewording the explanation must not
    show as a moved tile, and re-ordering, adding or re-windowing members
    must -- member order is what a screen binds to."""
    base = compile_ok(CARD).bundle("team_person.card")
    assert base is not None

    reworded = compile_ok(
        CARD.replace("Everything the person card shows.", "The person card, retold.")
    ).bundle("team_person.card")
    assert reworded is not None
    assert reworded.version == base.version

    reordered = compile_ok(
        "\n# Everything the person card shows.\n"
        "bundle team_person.card:\n"
        "    figure team_person.wip\n"
        "    reading team_person.to_merge over 7, 14, 30\n"
        "    projection work_issue.item\n"
        "    summarise work_issue.backlog\n"
    ).bundle("team_person.card")
    assert reordered is not None
    assert reordered.version != base.version

    rewindowed = compile_ok(CARD.replace("over 7, 14, 30", "over 7, 30")).bundle(
        "team_person.card"
    )
    assert rewindowed is not None
    assert rewindowed.version != base.version

    shorter = compile_ok(
        "\n# Everything the person card shows.\n"
        "bundle team_person.card:\n"
        "    reading team_person.to_merge over 7, 14, 30\n"
        "    figure team_person.wip\n"
        "    projection work_issue.item\n"
    ).bundle("team_person.card")
    assert shorter is not None
    assert shorter.version != base.version

    longer = compile_ok(
        CARD.replace(
            "    figure team_person.wip\n",
            "    figure team_person.wip\n    reading team_person.pending_reviews\n",
        )
    ).bundle("team_person.card")
    assert longer is not None
    assert longer.version != base.version

    renamed = compile_ok(CARD.replace("team_person.card", "team_person.deck")).bundle(
        "team_person.deck"
    )
    assert renamed is not None
    assert renamed.version != base.version, (
        "a renamed tile is a different tile; two identically-composed bundles "
        "must still be told apart by hash"
    )


def test_a_members_own_change_moves_the_member_not_the_bundle() -> None:
    """The bundle hashes names and arguments, never member versions: each
    member's `Result` carries its own version and provenance, so a moved
    figure shows on the member's citation and the tile's composition -- which
    did not change -- keeps its hash."""
    before = compile_source(BASE + MEMBERS + CARD)
    after = compile_source(
        BASE.replace(
            "filter work_issue.active where active == true",
            "filter work_issue.active where active != false",
        )
        + MEMBERS
        + CARD
    )
    wip_before = before.figure("team_person.wip")
    wip_after = after.figure("team_person.wip")
    assert wip_before is not None and wip_after is not None
    assert wip_before.version != wip_after.version, "the control moved nothing"
    card_before = before.bundle("team_person.card")
    card_after = after.bundle("team_person.card")
    assert card_before is not None and card_after is not None
    assert card_before.version == card_after.version


def test_the_committed_artifact_carries_the_bundles() -> None:
    """The library artifact is where a moved tile is reviewed, so the plan
    must reach it."""
    from uratori.lang.build import as_json

    document = as_json(compile_ok(CARD))
    [bundle] = document["bundles"]
    assert bundle["name"] == "team_person.card"
    assert bundle["version"]
    assert [m["name"] for m in bundle["members"]] == [
        "team_person.to_merge",
        "team_person.wip",
        "work_issue.item",
        "work_issue.backlog",
    ]


# -------------------------------------------------------------- serving --

SERVE_WORLD = Schema(
    kinds=frozenset({"shop_order", "shop_courier"}),
    name_fields={"shop_courier": "name", "shop_order": "ref"},
    bucket_settings=("tenant.timezone",),
    figure_settings=("limits.carrying.over",),
    defaults={
        "tenant": {"hoursPerDay": 8, "timezone": "UTC"},
        "limits": {"carrying": {"over": 3}},
    },
)

SERVE_SOURCE = """
group shop_order.carried_by from courier_id
filter shop_order.open where status != "delivered"
group shop_order.delivered_by_day from (courier_id, delivered_at by day in tenant.timezone)

measure shop_order.riding_seconds = delivered_at - picked_up_at

# Orders in hand right now.
figure shop_courier.carrying:
    display "{value} in hand"
    depends:
        mine = shop_order.carried_by:{shop_courier} & shop_order.open
    calculate:
        count(mine)

# Every delivery's ride time, day by day.
figure shop_courier.ride_times:
    display "{shop_courier} rides"
    depends:
        done = shop_order.delivered_by_day:{shop_courier}
    calculate:
        list(shop_order.riding_seconds over done)

# The typical ride, over a window.
reading shop_courier.typical_ride(range):
    display "{value}"
    depends:
        rides = shop_courier.ride_times in range
    calculate:
        mean(rides)

# One row per order, alphabetically, first two only.
projection shop_order.board:
    sort by ref ascending
    limit 2

    field:
        ref = ref as text

# The whole book of orders, one row.
summarise shop_order.book over shop_order.board:
    count orders

# The courier tile.
bundle shop_courier.card:
    reading shop_courier.typical_ride over 9, 1
    figure shop_courier.carrying
    projection shop_order.board
    summarise shop_order.book
"""


class CountingFacts(MemoryFactStore):
    """The observable for evaluate-once: how many times each kind's records
    were read. A shared projection that evaluated per member would read its
    kind once per member."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: dict[str, int] = {}

    async def of_kind(self, tenant: str, kind: str) -> list[FactRow]:
        self.reads[kind] = self.reads.get(kind, 0) + 1
        return await super().of_kind(tenant, kind)


def _engine() -> tuple[Uratori, CountingFacts]:
    library = compile_against(SERVE_SOURCE, SERVE_WORLD)
    facts = CountingFacts()
    engine = Uratori(
        schema=SERVE_WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    return engine, facts


def _feed(facts: CountingFacts) -> None:
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for n in range(5):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"},
        )
    # Two delivered rides on known UTC days relative to now -- a bundle is
    # served at the moment of asking, so the fixture's days must trail it: a
    # one-hour ride yesterday, a two-hour ride today.
    from datetime import UTC, datetime, timedelta

    def iso(at: datetime) -> str:
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")

    now = datetime.now(tz=UTC)
    facts.put(
        "t1",
        "shop_order",
        "r1",
        {
            "ref": "R-1",
            "courier_id": "c1",
            "status": "delivered",
            "picked_up_at": iso(now - timedelta(hours=25)),
            "delivered_at": iso(now - timedelta(hours=24)),
        },
    )
    facts.put(
        "t1",
        "shop_order",
        "r2",
        {
            "ref": "R-2",
            "courier_id": "c1",
            "status": "delivered",
            "picked_up_at": iso(now - timedelta(hours=2)),
            "delivered_at": iso(now),
        },
    )


async def test_a_bundle_serves_its_members_in_declaration_order_each_with_its_own_version() -> None:
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    library = compile_against(SERVE_SOURCE, SERVE_WORLD)
    card = library.bundle("shop_courier.card")
    assert card is not None
    assert answer.name == "shop_courier.card"
    assert answer.version == card.version

    assert [(r.kind, r.name) for r in answer.results] == [
        ("reading", "shop_courier.typical_ride"),
        ("figure", "shop_courier.carrying"),
        ("projection", "shop_order.board"),
        ("summary", "shop_order.book"),
    ]
    reading_plan = library.reading("shop_courier.typical_ride")
    figure_plan = library.figure("shop_courier.carrying")
    summary_plan = library.summary("shop_order.book")
    projection_plan = library.projection("shop_order.board")
    assert reading_plan is not None and figure_plan is not None
    assert summary_plan is not None and projection_plan is not None
    versions = {r.name: r.version for r in answer.results}
    assert versions["shop_courier.typical_ride"] == reading_plan.version
    assert versions["shop_courier.carrying"] == figure_plan.version
    assert versions["shop_order.board"] == projection_plan.version
    assert versions["shop_order.book"] == summary_plan.version


async def test_the_bundles_windows_reach_the_reading() -> None:
    """`over 9, 1` is the member's argument, and each window must cover its
    own days: nine trailing days hold both rides, one holds only today's --
    any other pair of means says the windows never made it through."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    reading = answer.results[0]
    [subject] = reading.subjects
    assert subject.windows is not None
    wide, narrow = subject.windows
    assert (wide.trailing, narrow.trailing) == (9, 1)
    assert wide.mean == 5400.0, "nine days should cover yesterday's 1h and today's 2h ride"
    assert narrow.mean == 7200.0, "one day should cover only today's 2h ride"


async def test_an_anchored_bundle_is_refused_because_one_instant_is_the_claim() -> None:
    """`at` anchors a reading's windows on a past day, and a bundle's other
    members -- stored figures, live pages -- can only be served as they
    stand. An anchored tile would put June's reading beside August's page
    under a wrapper claiming one clock, so the request is refused with
    directions rather than served misreporting itself."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    with pytest.raises(ValueError, match="one instant"):
        await engine.answer("t1", "shop_courier.card", at="2026-06-30")


async def test_members_keep_their_own_states_and_reasons() -> None:
    """A board with riding orders and no deliveries: the count is a real
    answer and the ride reading has nothing collected. The wrapper must not
    flatten that -- one member ok beside one saying why it is not is the
    whole point of per-member states."""
    engine, facts = _engine()
    facts.put("t1", "shop_courier", "c1", {"name": "Aki"})
    for n in range(3):
        facts.put(
            "t1",
            "shop_order",
            f"o{n}",
            {"ref": f"A-{n}", "courier_id": "c1", "status": "riding"},
        )
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    by_name = {r.name: r for r in answer.results}
    carrying = by_name["shop_courier.carrying"]
    riding = by_name["shop_courier.typical_ride"]
    assert carrying.state.ok is True, carrying.state
    assert carrying.subjects[0].value == 3.0
    assert riding.state.ok is False, "no delivery has ever been bucketed"
    assert not isinstance(riding.state, Ok)
    assert riding.state.because == "nothing-collected"


async def test_a_summary_member_travels_without_rows_and_still_counts_them_all() -> None:
    """The load-bearing rule: the row payload stays home, the population does
    not. The projection's page is limited to two rows; the summary member must
    still say seven orders -- a count of the page would read plausibly and be
    wrong, which is the exact mistake a summary exists to prevent."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    projection = answer.results[2]
    summary = answer.results[3]

    assert summary.kind == "summary"
    assert summary.subjects == [], "a summary member carries no rows"
    assert summary.summary is not None
    assert summary.summary.values["orders"] == 7.0, (
        "the count must cover all seven orders, not the two-row page"
    )

    assert projection.kind == "projection"
    assert len(projection.subjects) == 2, "the page keeps its own sort and limit"
    # The projection's own attached summary and the summary member agree by
    # construction -- one evaluation, two members served from it.
    assert projection.summary is not None
    assert projection.summary.values["orders"] == 7.0


async def test_a_shared_projection_is_evaluated_once_for_the_whole_bundle() -> None:
    """The bundle names both the projection and the summary over it; the
    records they are both about must be read once. Twice would double the
    cost of every tile that shows a page beside its headline."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    facts.reads.clear()
    await engine.answer("t1", "shop_courier.card")
    assert facts.reads.get("shop_order", 0) == 1


async def test_every_member_of_an_unanchored_bundle_shares_one_instant() -> None:
    """One request, one clock: a bundle whose members disagreed about when
    they were evaluated could put a page beside a headline computed over
    different worlds."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    instants = {r.at for r in answer.results} | {answer.at}
    assert len(instants) == 1


async def test_an_unrun_bundle_serves_absences_not_zeroes() -> None:
    """Before any pass, every member must say why it has no answer. A tile of
    confident zeroes over a board that has computed nothing is the wrong
    answer this whole engine exists to avoid."""
    engine, _facts = _engine()

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    for member in answer.results:
        assert member.state.ok is False, f"{member.name} served ok over nothing"
    summary = answer.results[3]
    assert summary.summary is None, "a withheld summary is absent, never zero"


async def test_a_bundle_naming_a_live_reading_raises_like_the_reading_itself_would() -> None:
    """Live readings compile and are versioned, but the engine does not serve
    them yet; a bundle naming one answers the same 501-shaped refusal the
    reading's own route gives, rather than silently dropping the member."""
    source = SERVE_SOURCE + (
        "\nmeasure shop_order.waiting_seconds = now - picked_up_at\n"
        "\n# Orders waiting right now.\n"
        "reading shop_courier.waiting():\n"
        '    display "{value}"\n'
        "    depends:\n"
        "        waiting = shop_order.waiting_seconds over (shop_order.carried_by:{shop_courier} & shop_order.open)\n"
        "    calculate:\n"
        "        count(waiting)\n"
        "\n# A tile over the live queue.\n"
        "bundle shop_courier.live_card:\n"
        "    reading shop_courier.waiting\n"
    )
    library = compile_against(source, SERVE_WORLD)
    facts = MemoryFactStore()
    engine = Uratori(
        schema=SERVE_WORLD, library=library, store=MemoryEngineStore(), facts=facts
    )
    with pytest.raises(NotImplementedError):
        await engine.answer("t1", "shop_courier.live_card")


async def test_a_bundle_member_result_is_the_same_shape_the_members_own_route_serves() -> None:
    """A member's Result must be an ordinary Result -- same figure subjects,
    same values -- so nothing downstream needs a bundle-shaped reader."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)

    answer = await engine.answer("t1", "shop_courier.card")
    assert isinstance(answer, BundleResult)
    alone = await engine.answer("t1", "shop_courier.carrying")
    assert isinstance(alone, Result)
    member = answer.results[1]
    assert [(s.id, s.value) for s in member.subjects] == [
        (s.id, s.value) for s in alone.subjects
    ]
    assert member.version == alone.version


async def test_bundle_evidence_is_a_forwarding_address() -> None:
    """A bundle stores nothing and cites nothing; asking for its evidence must
    say where the evidence actually lives rather than answering bare."""
    engine, facts = _engine()
    _feed(facts)
    await engine.run("t1", full=True)
    with pytest.raises(LookupError, match="member"):
        await engine.evidence("t1", "shop_courier.card", "c1")
