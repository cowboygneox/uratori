"""The guide's examples, held to the compiler.

`docs/language.md` is the authoring guide and the design rationale in one
file, which makes its `.fig` blocks the piece of source most people will ever
copy -- and the piece most likely to rot, because it is prose to everybody
editing the language and code to everybody reading it. The flagship `figure`
example went on banding against tenant dials for a release after the dials
were deleted, three sections below a paragraph explaining that a threshold is
never a dial. One example was pinned by a test; that one was not.

Every block is compiled now. The blocks are fragments by design -- a figure
shown to explain `depends` names a group introduced three sections earlier,
or one the guide never shows because it is not what that section is about --
so each is compiled on top of `docs/language.fixture.fig`, which carries the
scaffolding and nothing the guide is teaching. Where a block declares
something the fixture also has, the fixture's copy stands aside: the guide is
the authority on anything it shows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from uratori import Schema, compile_source

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "language.md"
FIXTURE = ROOT / "docs" / "language.fixture.fig"

DECLARES = re.compile(
    r"^(?:fact|group|filter|measure|figure|reading|projection|summarise|bundle)"
    r"\s+([a-z_][a-z0-9_.]*)",
    re.M,
)
WORLD = Schema(kinds=frozenset())

_BOUNDARY = re.compile(
    r"\n\s*\n(?=#|fact |group |filter |measure |figure |reading |projection "
    r"|summarise |bundle )"
)


def _blocks(source: str) -> list[tuple[frozenset[str], str]]:
    """The fixture, split into the declarations it holds.

    A block is an explanation and its declaration. They are separated by a
    blank line followed by something at column zero -- a `#` or a keyword --
    because a declaration's own body has blank lines in it too, and splitting
    on every one of them cuts a figure away from its `calculate`.
    """
    out: list[tuple[frozenset[str], str]] = []
    for chunk in re.split(_BOUNDARY, source):
        names = frozenset(DECLARES.findall(chunk))
        if names:
            out.append((names, chunk))
    return out


def _snippets(path: Path) -> list[tuple[int, str]]:
    text = path.read_text()
    return [
        (text[: m.start()].count("\n") + 1, m.group(2))
        for m in re.finditer(r"```(\w*)\n(.*?)```", text, re.S)
        if m.group(1) in ("", "fig") and DECLARES.search(m.group(2))
    ]


GUIDE_SNIPPETS = _snippets(GUIDE)


def test_the_guide_has_examples_to_check() -> None:
    """The guard on the guard. A regex that stopped matching would turn this
    whole file green over nothing, which is the failure mode of every test
    that finds its own input."""
    assert len(GUIDE_SNIPPETS) >= 20, (
        f"only {len(GUIDE_SNIPPETS)} blocks found in the guide -- the extractor "
        "has stopped seeing them, and every case below is passing vacuously"
    )


@pytest.mark.parametrize(
    ("line", "body"), GUIDE_SNIPPETS, ids=[str(n) for n, _ in GUIDE_SNIPPETS]
)
def test_every_fig_block_in_the_guide_compiles(line: int, body: str) -> None:
    shown = frozenset(DECLARES.findall(body))
    preamble = "\n".join(
        chunk
        for names, chunk in _blocks(FIXTURE.read_text())
        if not (names & shown)
    )
    try:
        compile_source(preamble + "\n" + body, WORLD)
    except Exception as refusal:
        pytest.fail(
            f"docs/language.md:{line} does not compile:\n  {refusal}\n\n"
            "Either the example is wrong, or the scaffolding it assumes is "
            "missing from docs/language.fixture.fig."
        )


def test_the_fixture_itself_compiles() -> None:
    """It is a `.fig` file that nothing builds, so nothing else would notice a
    typo in it -- and a broken preamble fails every case above with a message
    about the wrong file."""
    compile_source(FIXTURE.read_text(), WORLD)
