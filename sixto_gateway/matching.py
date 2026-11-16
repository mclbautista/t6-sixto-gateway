"""Matching a Telegram member to Contacts by handle, alias or name.

The rules (``normalize_handle``, ``split_aliases``) are ported from ``t6-sixto-watchdog``
``jo_watchdog/core/resolve.py``; that repo is deliberately not imported.
"""

from __future__ import annotations

from collections.abc import Iterable

from rapidfuzz import fuzz

from sixto_gateway.people import Contact

DEFAULT_HONORIFICS = ("ate", "kuya", "sir")
CLOSEST_MIN_SCORE = 60.0  # a candidate on a card only needs to be worth a look
CLOSEST_LIMIT = 3


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


def normalize_name(text: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> str:
    """Casefold, trim, collapse whitespace, and drop honorific words."""
    drop = {h.casefold() for h in honorifics}
    words = [w for w in text.casefold().split() if w.strip(".") not in drop]
    return " ".join(words)


def split_aliases(aliases: str, honorifics: Iterable[str] = DEFAULT_HONORIFICS) -> list[str]:
    """Contacts ``Aliases`` is comma-separated; each entry is normalized like a mention."""
    out = []
    for part in aliases.split(","):
        norm = normalize_name(part, honorifics)
        if norm:
            out.append(norm)
    return out


def matches_handle(contact: Contact, handle: str) -> bool:
    """True when ``handle`` (any case, with or without ``@``) is the Contact's handle or alias."""
    wanted = normalize_handle(handle)
    if not wanted:
        return False
    if normalize_handle(contact.telegram_handle) == wanted:
        return True
    return any(alias.lstrip("@") == wanted for alias in split_aliases(contact.aliases))


def closest_contacts(
    contacts: Iterable[Contact],
    name: str,
    *,
    limit: int = CLOSEST_LIMIT,
    min_score: float = CLOSEST_MIN_SCORE,
) -> list[Contact]:
    """Contacts whose name or an alias is closest to ``name``, best first.

    Ranks with the same ``WRatio`` scorer and name normalization as t6-sixto-watchdog
    (``fuzzy_ranking``), but with a low bar: these are suggestions for the approver to
    choose from, never picked automatically.
    """
    wanted = normalize_name(name)
    if not wanted:
        return []
    scored: list[tuple[float, Contact]] = []
    for c in contacts:
        titles = [normalize_name(c.name), *split_aliases(c.aliases)]
        best = max((fuzz.WRatio(wanted, t) for t in titles if t), default=0.0)
        if best >= min_score:
            scored.append((best, c))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:limit]]
