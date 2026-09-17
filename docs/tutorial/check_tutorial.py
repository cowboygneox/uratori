#!/usr/bin/env python3
"""The tutorial's examples, held to the compiler.

`docs/tutorial/*.md` is prose to everybody editing it and source to everybody
reading it, which is the pair of properties that lets an example rot quietly.
So every fenced block in every chapter is compiled here, and so is
`support.fig` itself.

A chapter's block is a fragment by design -- it names groups and filters
introduced chapters earlier. `support.fig` is in tutorial order, so each block
is compiled on top of that file **truncated at the first declaration the block
shows**: exactly what the tutorial had introduced by the time the block
appeared. A block that reaches forward to something the tutorial has not
taught yet therefore fails here, which is the other thing this script is for.

Run it from the repository root:

    python docs/tutorial/check_tutorial.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from uratori import Schema, compile_source  # noqa: E402

TUTORIAL = ROOT / "docs" / "tutorial"
FIG = TUTORIAL / "support.fig"
WORLD = Schema(kinds=frozenset())

DECLARES = re.compile(
    r"^(?:fact|group|filter|measure|figure|reading|projection|summarise|bundle)"
    r"\s+([a-z_][a-z0-9_.]*)",
    re.M,
)
BOUNDARY = re.compile(
    r"\n\s*\n(?=#|fact |group |filter |measure |figure |reading |projection "
    r"|summarise |bundle )"
)


def declarations(source: str) -> list[tuple[frozenset[str], str]]:
    """The file, split into the declarations it holds.

    A piece is an explanation and its declaration, separated by a blank line
    followed by something at column zero -- a declaration's own body has blank
    lines in it too, and splitting on every one of them would cut a figure away
    from its `calculate`.
    """
    out: list[tuple[frozenset[str], str]] = []
    for chunk in re.split(BOUNDARY, source):
        names = frozenset(DECLARES.findall(chunk))
        if names:
            out.append((names, chunk))
    return out


def main() -> int:
    pieces = declarations(FIG.read_text())
    if len(pieces) < 40:
        print(
            f"only {len(pieces)} declarations found in support.fig -- the "
            "splitter has stopped seeing them, and every case below would "
            "pass vacuously"
        )
        return 1

    checked = failed = 0
    files = [ROOT / "docs" / "tutorial.md"] + sorted(TUTORIAL.glob("*.md"))
    for path in files:
        text = path.read_text()
        for match in re.finditer(r"```(\w*)\n(.*?)```", text, re.S):
            if match.group(1) not in ("", "fig"):
                continue
            body = match.group(2)
            if not DECLARES.search(body):
                continue
            checked += 1
            line = text[: match.start()].count("\n") + 1
            shown = frozenset(DECLARES.findall(body))
            cut = next(
                (i for i, (names, _) in enumerate(pieces) if names & shown),
                len(pieces),
            )
            preamble = "\n".join(chunk for _names, chunk in pieces[:cut])
            try:
                compile_source(preamble + "\n" + body, WORLD)
            except Exception as refusal:  # noqa: BLE001 -- the message is the point
                failed += 1
                print(
                    f"{path.relative_to(ROOT)}:{line} does not compile:\n"
                    f"  {refusal}\n"
                )

    if checked < 20:
        print(f"only {checked} blocks found -- the extractor has stopped seeing them")
        return 1

    try:
        compile_source(FIG.read_text(), WORLD)
    except Exception as refusal:  # noqa: BLE001
        print(f"support.fig does not compile:\n  {refusal}")
        failed += 1

    print(f"{checked} blocks checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
