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
REJECTED = "Thanks. There is nothing more you need to do here."
APPROVED = "You are all set, {first_name}. Your briefings will come to this chat."
IN_A_GROUP = "Please message me directly to set up your briefings: {deep_link}"

# --- for the approver's card ----------------------------------------------------------------

CARD_TITLE = "New /start"
CARD_TELEGRAM = "Telegram: {name} · {handle} · id {user_id}"
CARD_MATCH = "Match: {link} (by handle)"
CARD_MATCHES = "Matches:"
CARD_CLOSEST = "Closest by name:"
CARD_NO_MATCH = "Match: none found"
CARD_CONFLICT = "⚠ This Contact already has Telegram id {other_id}"
CARD_NO_HANDLE = "(no @handle)"
BUTTON_APPROVE = "Approve {name}"
BUTTON_PICK = "Pick {name}"
BUTTON_REPLACE = "Replace ID"
BUTTON_REPLACE_NAMED = "Replace ID: {name}"
BUTTON_CREATE = "Create new Contact"
BUTTON_REJECT = "Reject"
BUTTON_UNDO = "Undo"
OUTCOME_APPROVED = "✅ Approved: {link}"
OUTCOME_REJECTED = "Rejected"
OUTCOME_BLOCKED = "Blocked the bot"
OUTCOME_CONTACT = "Contact"  # link text when the Contact's name is not at hand
TAP_ALREADY_LINKED = "That Contact is already linked to another Telegram user."
TAP_OTHER_ID = "That Contact already has a different Telegram id. Nothing was changed."
FYI_AUTO = (
    "{person} pressed Start and is linked to {link}: that Contact already had this Telegram id. "
    "Nothing was written to Notion."
)
CREATED_NOTE = "Created by Sixto /start on {date}."
TAP_NOTION_FAILED = "Notion did not answer. Nothing was changed. Tap again to retry."

# --- for the approver's daily summary (HTML) ---------------------------------------------------

SUMMARY_TITLE = "Onboarding summary, {date}"
SUMMARY_COUNTS = (
    "Approved: {approved} · Pending: {pending} · Rejected: {rejected} · Blocked: {blocked}"
)
SUMMARY_PENDING = "Waiting for you:"
SUMMARY_NO_PENDING = "No one is waiting for you."
SUMMARY_PENDING_ITEM = "• {person}, waiting {waiting}: {link}"
SUMMARY_CARD_LINK = "open card"
SUMMARY_NO_CARD = "card not sent yet"
SUMMARY_FAILURES = "Messages that failed in the last 24 hours: {count}"
SUMMARY_NO_FAILURES = "No messages failed in the last 24 hours."
SUMMARY_FAILURE_MORE = "• and {more} more (see the audit table)"
SUMMARY_MISSING = "Still missing, no approved member yet: {handles}"
SUMMARY_NOT_MISSING = "Everyone expected has joined."


def first_name_or_there(first_name: str | None) -> str:
    return (first_name or "").strip() or "there"
