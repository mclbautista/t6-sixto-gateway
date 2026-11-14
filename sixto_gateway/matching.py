"""How onboarding finds Contacts for a new member.

``legacy_*`` is the gateway's own matching from before t6-core: handle or alias equals the
member's handle, else the closest names at WRatio 60. It stays only while the new matching runs
beside it in shadow mode (see ``onboarding.matching`` in the config); remove it with the switch.
"""

from __future__ import annotations

from collections.abc import Iterable

from rapidfuzz import fuzz
from t6.domain import Contact

LEGACY_HONORIFICS = ("ate", "kuya", "sir")
CLOSEST_MIN_SCORE = 60.0  # a candidate on a card only needs to be worth a look
CLOSEST_LIMIT = 3


def _handle(handle: str | None) -> str:
    return (handle or "").strip().lstrip("@").casefold()


def _name(text: str) -> str:
    """Casefold, trim, collapse whitespace, and drop honorific words."""
    words = [w for w in text.casefold().split() if w.strip(".") not in LEGACY_HONORIFICS]
    return " ".join(words)


def _aliases(contact: Contact) -> list[str]:
    return [a for alias in contact.aliases if (a := _name(alias))]


def matches_handle(contact: Contact, handle: str) -> bool:
    """True when ``handle`` (any case, with or without ``@``) is the Contact's handle or alias."""
    wanted = _handle(handle)
    if not wanted:
        return False
    if _handle(contact.telegram_handle) == wanted:
        return True
    return any(alias.lstrip("@") == wanted for alias in _aliases(contact))


def closest_contacts(
    contacts: Iterable[Contact],
    name: str,
    *,
    limit: int = CLOSEST_LIMIT,
    min_score: float = CLOSEST_MIN_SCORE,
) -> list[Contact]:
    """Contacts whose name or an alias is closest to ``name``, best first."""
    wanted = _name(name)
    if not wanted:
        return []
    scored: list[tuple[float, Contact]] = []
    for c in contacts:
        titles = [_name(c.name), *_aliases(c)]
        best = max((fuzz.WRatio(wanted, t) for t in titles if t), default=0.0)
        if best >= min_score:
            scored.append((best, c))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:limit]]
