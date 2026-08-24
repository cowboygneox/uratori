"""A declaration, split into what it *says* and what it *does*.

"Every number, backed by evidence" means traceable to the event that moved it
*and* to a written definition of how it was computed, and v1 half-shipped the
second: the source reached two of its six declaration kinds, both two clicks
deep. Everything added after that -- joins, presence tests, extremes, flag
templates, spans -- was live, versioned, cited on screen, and readable only by
checking the repository out. It was reported by the maintainer as *"I click on
it but don't see a formula so I cannot review the language changes"*, which is
the honest description of what that gap looks like from outside.

So there is one extractor, it covers every kind, and the API serves it.

## Prose and formula are separated here, not by the reader

The first version answered with the whole block -- the comment above it, the
docstring, the `display` template and the calculation -- under one heading. Read
on screen that is a page of paragraphs with four lines of arithmetic somewhere in
the middle, and the reader who came to check the arithmetic has to find it. It
was reported the same way the first gap was: *"all I want to see is the meat and
potatoes of the formula"*.

Two functions now, and the split is the same one the screen already draws:

- `declaration_prose` is the explanation -- the `#` lines above the declaration
  and the `\"\"\"docstring\"\"\"` inside it, which are the same thing written in two
  places for two kinds of declaration. A figure carries a docstring because the
  parser demands one; an index and a measure cannot have one, and their comment
  above *is* their documentation. Both arrive as the description, so a reader
  never has to know which kind they are looking at to find out what it means.
- `declaration_source` is the calculation, with the prose and the `display`
  template removed. `display` is a sentence for a card to print; on a page
  showing the formula it is one more paragraph in the way of it.

Neither is in the version hash, which is why this can be reorganised freely:
rewording a docstring must not fork a version and recompute every value.

## The scan crosses one blank line

v1's first version required the comment to be contiguous with the declaration,
and a review measured what that cost: a third of its declarations rendered as a
bare line with their explanation stranded one blank line above.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .plan import Library

_HEADERS = (
    r"^index\s+{name}\s",
    r"^measure\s+{name}\s*=",
    r"^figure\s+{name}(\s+across\s+\w+)?\s*:",
    r"^reading\s+{name}\s*\(",
    r"^projection\s+{name}\s*:",
    r"^summarise\s+{name}\s+over\s",
)


@lru_cache(maxsize=8)
def _lines(source: str) -> tuple[str, ...]:
    """Split once per library rather than per lookup.

    A projection's pane asks for its own block plus every definition it rests
    on, which for a large one is a couple of dozen lookups over a quarter of a
    megabyte.
    """
    return tuple(source.split("\n"))


_BANNER = re.compile(r"^#\s*-{3,}")
_DISPLAY = re.compile(r'^\s*display\s+"')


def _is_prose(line: str) -> bool:
    """A comment line, but not the banner between two files.

    The build concatenates the `.fig` files with a `# ---- name.fig ----` rule
    between them, and without this the first declaration in every file adopts
    the previous file's banner as its own explanation. It shows up immediately
    on the Data screen as a heading that belongs to the wrong file.
    """
    return line.startswith("#") and not _BANNER.match(line)


def _locate(library: Library, name: str) -> tuple[tuple[str, ...], int, int, int] | None:
    """The lines, and where this declaration's prose, header and body sit.

    Returned as one tuple because both public functions need the same three
    boundaries, and computing them twice is how the two drift into disagreeing
    about which lines belong to which -- at which point a paragraph is either
    printed under both headings or under neither.
    """
    lines = _lines(library.source)
    pattern = re.compile("|".join(h.format(name=re.escape(name)) for h in _HEADERS))

    at = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if at is None:
        return None

    start = at
    if start > 0 and lines[start - 1].strip() == "" and start > 1 and _is_prose(lines[start - 2]):
        start -= 1
    while start > 0 and _is_prose(lines[start - 1]):
        start -= 1
    if not any(line.startswith("#") for line in lines[start:at]):
        start = at

    end = at + 1
    while end < len(lines) and (lines[end].strip() == "" or lines[end][:1].isspace()):
        end += 1

    return lines, start, at, end


def declaration_source(library: Library, name: str) -> str | None:
    """The calculation, and nothing else.

    Everything a reader would call documentation comes out: the comment above,
    the docstring, and the `display` template. What is left is what the engine
    actually evaluates, which is the thing somebody clicking "the definition, as
    written" came to check.
    """
    found = _locate(library, name)
    if found is None:
        return None
    lines, _, at, end = found

    body: list[str] = []
    in_doc = False
    for line in lines[at:end]:
        stripped = line.strip()
        if in_doc:
            # A one-line docstring opens and closes on the same line, so the
            # close is only a close when it is not also the open.
            if stripped.endswith('"""'):
                in_doc = False
            continue
        if stripped.startswith('"""'):
            in_doc = not (len(stripped) > 3 and stripped.endswith('"""'))
            continue
        if _DISPLAY.match(line):
            continue
        body.append(line)

    # A docstring leaves a run of blank lines behind it where the paragraph was.
    # Trimming them here rather than in the browser keeps the served text and
    # the rendered text the same thing, which is the whole point of this route.
    return "\n".join(_collapse(body)).rstrip() or None


def _collapse(lines: list[str]) -> list[str]:
    """Squeeze runs of blanks to one, and none directly under the header.

    The header is `figure x:` and the docstring came straight after it, so
    without the second rule every declaration in the library opens with a gap
    where the paragraph used to be -- which reads as a formula missing its first
    line rather than as one with its prose moved.
    """
    out: list[str] = []
    for line in lines:
        if line.strip():
            out.append(line)
        elif len(out) > 1 and out[-1].strip():
            out.append("")
    return out


def declaration_prose(library: Library, name: str) -> str:
    """What this declaration means, in the author's words.

    The comment above it and the docstring inside it, in that order, joined as
    one paragraph run. Both, rather than whichever the kind happens to have: a
    figure has a docstring and usually a section comment, an index has only a
    comment, and a reader should not have to know the difference to find out
    what a line does.

    Empty string rather than None when there is neither, because the caller
    puts this in a `doc` field that is already a string for every other kind and
    a second absent-value spelling would be a second branch on every screen.
    """
    found = _locate(library, name)
    if found is None:
        return ""
    lines, start, at, end = found

    above = [line.lstrip("#").strip() for line in lines[start:at] if _is_prose(line)]

    inside: list[str] = []
    in_doc = False
    for line in lines[at:end]:
        stripped = line.strip()
        if in_doc:
            if stripped.endswith('"""'):
                break
            inside.append(stripped)
        elif stripped.startswith('"""'):
            if len(stripped) > 3 and stripped.endswith('"""'):
                inside.append(stripped[3:-3].strip())
                break
            in_doc = True

    return "\n".join(_paragraphs(above + ([""] if above and inside else []) + inside)).strip()


def _paragraphs(lines: list[str]) -> list[str]:
    """Squeeze blank runs, so two sources joined do not leave a gap between."""
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return out
