"""Every word a person reads, in one place (plain English, so a translation can be added later).

Texts for the team member are plain text. Texts for the approver's card are HTML (they carry
links), so anything inserted into them must go through ``html.escape``.
"""

from __future__ import annotations

DEEP_LINK = "https://t.me/sixto_t6bot?start=onboard"

# --- for the person who pressed Start -----------------------------------------------------

WAITING = (
    "Hi {first_name}! Thanks for pressing Start. Cyril will confirm who you are soon. "
    "After that, you will get your briefings here."
)
PENDING_AGAIN = "Thanks, we already have your request. Cyril will confirm soon."
IN_A_GROUP = "Please message me directly to set up your briefings: {deep_link}"

# --- for the approver's card ----------------------------------------------------------------

CARD_TITLE = "New /start"
CARD_TELEGRAM = "Telegram: {name} · {handle} · id {user_id}"
CARD_MATCH = "Match: {link} (by handle)"
CARD_MATCHES = "Matches:"
CARD_NO_MATCH = "Match: none found"
CARD_NO_HANDLE = "(no @handle)"
BUTTON_APPROVE = "Approve {name}"
BUTTON_PICK = "Pick {name}"
BUTTON_CREATE = "Create new Contact"
BUTTON_REJECT = "Reject"


def first_name_or_there(first_name: str | None) -> str:
    return (first_name or "").strip() or "there"
