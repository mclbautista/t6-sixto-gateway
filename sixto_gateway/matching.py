"""How onboarding finds Contacts for a new member.

``matching: core`` (``onboarding.matching`` in the config) uses t6-core's studio matching: Telegram
id, handle, alias, then fuzzy names. A name that is not confidently one Contact falls back to up
to 3 suggestions at WRatio 60 or more, for the approver to pick from. Nothing here approves
anyone: the approval rules stay in ``onboarding``.

``matching: shadow`` (the default until the comparison is reviewed) still uses the gateway's own
matching from before t6-core, and writes one ``matching`` audit line per lookup that says whether
the two agree (counts only, no names). ``legacy_*`` and the shadow mode go with the switch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Literal

from rapidfuzz import fuzz
from t6.domain import Contact
from t6.lookup import Index, Lookup, Normalizers

log = logging.getLogger(__name__)

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


def legacy_matches_handle(contact: Contact, handle: str) -> bool:
    """True when ``handle`` (any case, with or without ``@``) is the Contact's handle or alias."""
    wanted = _handle(handle)
    if not wanted:
        return False
    if _handle(contact.telegram_handle) == wanted:
        return True
    return any(alias.lstrip("@") == wanted for alias in _aliases(contact))


def legacy_closest_contacts(
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


def holds_handle(contact: Contact, handle: str) -> bool:
    """True when ``handle`` is the Contact's handle or one of its aliases (t6-core's rules)."""
    n = Normalizers()
    wanted = n.handle(handle)
    if not wanted:
        return False
    held = [
        n.handle(contact.telegram_handle or ""),
        *(n.name(a).lstrip("@") for a in contact.aliases),
    ]
    return wanted in held


def legacy_candidates(contacts: list[Contact], handle: str | None, name: str) -> list[Contact]:
    """Handle or alias matches; with none, the Contacts closest by name. Never picks one."""
    exact = [c for c in contacts if handle and legacy_matches_handle(c, handle)]
    return exact or legacy_closest_contacts(contacts, name)


def core_candidates(
    contacts: list[Contact], user_id: int, handle: str | None, name: str
) -> tuple[list[Contact], str]:
    """The candidates from t6-core's matching, and the method that produced them."""
    lookup = Lookup(Index(contacts=tuple(contacts)))
    for mention in (str(user_id), handle or "", name):
        if not mention.strip():
            continue
        found = lookup.contact(mention)
        if found.entity is not None:
            return [found.entity], found.method
        if (
            found.needs_review
            and found.candidates
            and all(c.score == 1.0 for c in found.candidates)
        ):
            return [c.entity for c in found.candidates], found.method  # a tie: the approver picks
    return [c.entity for c in lookup.suggest_contacts(name)], "suggest"


class Matcher:
    """Finds the candidate Contacts for a member, in the configured mode."""

    def __init__(
        self,
        mode: Literal["shadow", "core"],
        audit: Callable[[str, str], None] = lambda event, detail: None,
    ):
        self._mode = mode
        self._audit = audit

    def candidates(self, contacts: list[Contact], member: dict) -> list[Contact]:
        user = member["telegram_user_id"]
        handle = member["handle"]
        name = " ".join(p for p in (member["first_name"], member["last_name"]) if p)
        core, method = core_candidates(contacts, user, handle, name)
        if self._mode == "core":
            return core
        legacy = legacy_candidates(contacts, handle, name)
        old, new = [c.page_id for c in legacy], [c.page_id for c in core]
        self._audit(
            "matching",
            f"user={user} same={old == new} same_set={set(old) == set(new)} "
            f"legacy={len(old)} core={len(new)} method={method}",
        )
        return legacy
