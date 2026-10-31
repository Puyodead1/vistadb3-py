"""Sort order used by VistaDB indexes for text keys.

VistaDB compares strings with the database culture (LCID 1033 for Redbox databases) using
Windows "word sort" rules.  This module reproduces that order for ASCII text, which was
mapped by inserting keys through the real engine and reading back the index order:

* first level: characters by the weights in ``_PRIMARY_ORDER``; letters ignore case;
  hyphen and apostrophe carry no first-level weight,
* second level (case-sensitive databases only): lowercase sorts before uppercase,
* last level: hyphens/apostrophes, by position.

Trailing spaces are not significant ("a" and "a " are the same key).

Other characters are approximated: accents are stripped for the first level and the
code point is used as a tie breaker.  Keys outside ASCII may therefore be ordered
differently from the engine; ``is_exact`` tells whether a key is fully covered.
"""
from __future__ import annotations

import unicodedata

# ASCII characters in ascending first-level order (letters listed in lowercase only).
_PRIMARY_ORDER = (" !\"#$%&()*,./:;?@[\\]^_`{|}~+<=>"
                  "0123456789abcdefghijklmnopqrstuvwxyz")
_IGNORED = "'-"
_WEIGHT = {c: i + 1 for i, c in enumerate(_PRIMARY_ORDER)}
_OTHER_BASE = 1000


def is_exact(s: str) -> bool:
    return all(32 <= ord(c) < 127 for c in s)


def _primary(c: str) -> int:
    w = _WEIGHT.get(c.lower())
    if w is not None:
        return w
    base = unicodedata.normalize("NFKD", c)[0]
    w = _WEIGHT.get(base.lower())
    if w is not None and base != c:
        return w
    return _OTHER_BASE + ord(c.lower())


def sort_key(s: str, case_sensitive: bool) -> tuple:
    prim = []
    case = []
    accent = []
    special = []
    for i, c in enumerate(s.rstrip(" ")):
        if c in _IGNORED:
            special.append((-i, _IGNORED.index(c)))
            continue
        prim.append(_primary(c))
        accent.append(0 if unicodedata.normalize("NFKD", c) == c else ord(c))
        if case_sensitive:
            case.append(1 if c != c.lower() else 0)
    return (tuple(prim), tuple(accent), tuple(case), tuple(special))
