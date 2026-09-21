"""Pure name-comparison helper for the verified locator fallback ladder.

Nothing here touches a browser, a frame, a screen buffer or any live surface --
this function only compares accessible names/labels/headers a surface has
already observed. See `browser.py`'s `BrowserSurface.find_fallback`,
`terminal.py`'s `ScreenSurface.find_fallback` for where a candidate name comes
from, and `engine.py`'s `Execution._rescue` for the one gated call site that
may use it -- never `observe()`, `condition()`, a click's own postcondition,
or a checkpoint. See REPORT.md sections 2-4 and docs/DESIGN_CHOICES.md for the
design this implements: the ladder may rescue a control by NORMALIZATION
ONLY, never by a fuzzy or partial match. A name that only gained or lost a
whole word, or that only differs in a member id or other embedded value, is
NOT a match here -- it stops the run and asks a person to review it.
"""

from __future__ import annotations

import re
import unicodedata

# Runs of whitespace and the small set of purely decorative punctuation a
# relabel is likely to add or drop (an ellipsis, a trailing colon, a leading/
# trailing asterisk, guillemets, parentheses) collapse to a single space
# before comparing. Nothing else is stripped: normalize() never drops a
# letter, a digit, or a whole word, so real wording differences -- an added
# word, a changed word, an embedded id -- always still differ after this
# runs. NFKC runs first (below) and decomposes a literal "…" into three "."
# characters -- `\.{2,}` catches that decomposed form; a single "." (a real
# abbreviation period) is left alone.
_DECORATION = re.compile(r"[…:*»›()]|\.{2,}")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Casefold, Unicode-NFKC, strip decorative punctuation, collapse whitespace.

    Two accessible names/labels/headers that differ only by this kind of
    surface decoration normalize identically; two names that differ in real
    wording -- an added or removed word, a different verb, an embedded value
    such as a member id -- never do. For example "Find member 00123",
    "Find a member", "Do not Find member", "Find member and delete" and
    "Find members" all still differ from "Find member" after normalization.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _DECORATION.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def names_equal(candidate: str, reviewed: str) -> bool:
    """The `normalized` rung -- the ONLY rung the verified fallback ladder may
    ever act on: identical once decoration and case are stripped. A
    postcondition verifies the intended effect happened, but it cannot rule
    out an ADDITIONAL, unintended one, so a broader match is never dispatched
    automatically; see docs/DESIGN_CHOICES.md.
    """
    return bool(candidate) and bool(reviewed) and normalize(candidate) == normalize(reviewed)
