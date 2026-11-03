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
APPROVED = "You are all set, {first_name}. Your briefings will come to this chat."
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
OUTCOME_APPROVED = "✅ Approved: {link}"
OUTCOME_REJECTED = "Rejected"
OUTCOME_BLOCKED = "Blocked the bot"
OUTCOME_CONTACT = "Contact"  # link text when the Contact's name is not at hand
TAP_ALREADY_LINKED = "That Contact is already linked to another Telegram user."
TAP_OTHER_ID = "That Contact already has a different Telegram id. Nothing was changed."
TAP_NOTION_FAILED = "Notion did not answer. Nothing was changed. Tap again to retry."


def first_name_or_there(first_name: str | None) -> str:
    return (first_name or "").strip() or "there"
