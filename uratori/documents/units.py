"""Version constants and the unit table `extract` hashes and runs against.

Split out of `extract.py` so that `uratori.lang.check` -- core language code,
installable and importable with no `server` extra, ever -- can hash these
into an extract's version (D4.5) without pulling in the documents runtime's
own dependencies (`uratori.server.provenance`'s `asyncpg` import among them).
This module imports nothing but the standard library, on purpose, the same
promise `uratori.lang.ast`/`uratori.lang.plan` keep.
"""

from __future__ import annotations

MATCHER_VERSION = "1"
"""Tokenisation (whitespace-split page words, already positioned by the word
layer), line grouping (the word layer's own `line`, taken as given) and
reading order (word id) -- the implementation behind every matcher in
`uratori.documents.extract`. Bumping this forks every extract's version,
because a change here can change what an *unmoved* declaration reads off an
*unmoved* page."""

UNIT_TABLE_VERSION = "1"
DATE_GRAMMAR_VERSION = "1"

UNIT_TABLE: dict[str, tuple[str, float]] = {
    # unit name -> (dimension, factor to that dimension's fixed target: kg
    # for mass, cm for length). `ft_in` is not one token -- it is read by
    # `extract._read_number`'s own composite case -- but it is a declarable
    # unit name, so it is listed here too.
    "kg": ("mass", 1.0),
    "lb": ("mass", 0.45359237),
    "cm": ("length", 1.0),
    "in": ("length", 2.54),
    "ft_in": ("length", 1.0),
}

FOOT_MARKERS = frozenset({"ft", "'"})
INCH_MARKERS = frozenset({"in", '"'})
CM_PER_INCH = 2.54
CM_PER_FOOT = 30.48

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
