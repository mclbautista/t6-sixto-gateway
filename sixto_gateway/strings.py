"""Every word a person reads, in one place (plain English, so a translation can be added later).

Texts for the team member are plain text. Texts for the approver's card are HTML (they carry
links), so anything inserted into them must go through ``html.escape``.
"""

from __future__ import annotations

DEEP_LINK = "https://t.me/sixto_t6bot?start=onboard"

# --- for the person who pressed Start -----------------------------------------------------

WAITING = (
    "Hi {first_name}! Thanks for pressing Start. Cyril will confirm who you are soon. "
    "After that, you will get your messages here."
)
ALREADY_ONBOARDED = "You are already set up. Your messages will come to this chat."
PENDING_AGAIN = "Thanks, we already have your request. Cyril will confirm soon."
REJECTED = "Thanks. There is nothing more you need to do here."
APPROVED = (
    "You are all set, {first_name}. Your messages will come to this chat. "
    "Your Telegram user ID has been logged in the T6 Notion. Your Contact page: {contact_url}"
)
IN_A_GROUP = "Please message me directly to set up your messages: {deep_link}"

# --- for /whereami (HTML: values go through html.escape) ----------------------------------

WHEREAMI_IN_DM = "Send this in the group topic you want."
WHEREAMI_CHAT = "Chat id: <code>{chat_id}</code>"
WHEREAMI_TOPIC = "Topic id: <code>{topic_id}</code>"
WHEREAMI_GENERAL_TOPIC = "Topic id: <code>1</code> (General)"
WHEREAMI_NO_TOPIC = "Topic id: none (this group has no topics)"

# --- for /project <code> notes (plain text) ---------------------------------------------------

PROJECT_NOTES_TITLE = "PM Notes history for {code}"
PROJECT_NOTES_LINE = "{as_of} · {text}"
PROJECT_NOTES_REMOVED = " (removed {date})"
PROJECT_NOTES_NONE = "No PM Notes history for {code} yet."
PROJECT_UNKNOWN_CODE = "I don't know the Project code {code}."
T6_NOT_ANSWERING = "T6 is not answering right now. Please try again in a few minutes."

# --- for /project <code> drives (plain text) --------------------------------------------------

PROJECT_DRIVES_TITLE = "Drive movements for {code}"
PROJECT_DRIVES_LINE = (
    "{date} · {direction} · {label} · {role} {person} · Coordinator then {coordinators}"
)
PROJECT_DRIVES_RECIPIENT = "Recipient"
PROJECT_DRIVES_RECEIVER = "Receiver"
PROJECT_DRIVES_REVIEW = " · ⚠ to review"
PROJECT_DRIVES_MORE = "{count} more in the Drives app"
PROJECT_DRIVES_NONE = "No Drive movements for {code} yet."

# --- for /crm snooze, /crm unsnooze and /leads (plain text; the API words its own answers) -----

PROJECT_LINKS_DM_CODE = "In a direct message, say which Project: /project CODE links."
PROJECT_LINKS_TELEGRAM = "Telegram:"
PROJECT_LINKS_WHOLE_CHAT = "whole chat"
PROJECT_LINKS_NOTION = "Notion page"
PROJECT_LINKS_FRAMEIO = "Frame.io project"
PROJECT_LINKS_UNAVAILABLE = "Links unavailable right now"
PROJECT_LINK_STATES = {
    "active": "active",
    "inactive": "inactive",
    "waiting": "waiting for approval",
    "denied": "denied",
    "dead": "dead since {dead_since}",
}
CRM_DM_ONLY = "/{command} works in a direct message with me. Please message me there."
LEADS_TITLE = "Open Leads: {count}"
LEADS_NONE = "No open Leads."
LEAD_LINE_HEAD = "{name} · {stage}"
LEAD_DAYS = " · {days} days in stage"
LEAD_OWNER = "Lead Owner"
LEAD_DELEGATE = "Delegate Producer"
LEAD_LINE = "Line Producer"
LEAD_COOR = "Prod Coor/Supe"
LEAD_STUDIO = "Studio"

# --- for the approver's card ----------------------------------------------------------------

CARD_TITLE = "New /start"
CARD_TELEGRAM = "Telegram: {name} · {handle} · id {user_id}"
CARD_MATCH = "Match: {link}"
CARD_MATCHES = "Possible matches:"
CARD_NO_MATCH = "Match: none found"
CARD_CONFLICT = "⚠ This Contact already has Telegram id {other_id}"
CARD_NO_HANDLE = "(no @handle)"
BUTTON_APPROVE = "Approve {name}"
BUTTON_PICK = "Pick {name}"
BUTTON_REPLACE = "Replace ID"
BUTTON_REPLACE_NAMED = "Replace ID: {name}"
BUTTON_CREATE = "Create new Contact"
BUTTON_REJECT = "Reject"
CARD_KIND_QUESTION = "What is this person?"
BUTTON_KINDS = {"staff": "Staff", "freelancer": "Freelancer", "client": "Client"}
BUTTON_BACK = "Back"
OUTCOME_CREATED_AS = "Created as {kind}"
BUTTON_UNDO = "Undo"
BUTTON_RETRY = "Retry"
CARD_LOOKUP_FAILED = "Lookup failed: Person API did not answer. Tap Retry, or choose below."
CARD_WRITE_FAILED = (
    "Write failed: Notion did not accept the change. Nothing was changed. Tap Retry."
)
OUTCOME_APPROVED = "✅ Approved: {link}"
OUTCOME_REJECTED = "Rejected"
OUTCOME_BLOCKED = "Blocked the bot"
OUTCOME_CONTACT = "Contact"  # link text when the Contact's name is not at hand
TAP_ALREADY_LINKED = "That Contact is already linked to another Telegram user."
TAP_REFUSED = "The t6 API refused the change: {reason} Nothing was changed."
TAP_OTHER_ID = "That Contact already has a different Telegram id. Nothing was changed."
FYI_AUTO = (
    "{person} pressed Start and is linked to {link}: that Contact already had this Telegram id. "
    "Nothing was written to Notion."
)
CREATED_NOTE = "Created by Sixto /start on {date}."
TAP_UNKNOWN_MODULE = "This button is no longer active."
TAP_NOTION_FAILED = "Notion did not answer. Nothing was changed. Tap again to retry."

# --- for the approver's daily summary (HTML) ---------------------------------------------------

SUMMARY_TITLE = "Onboarding summary, {date}"
SUMMARY_COUNTS = (
    "Approved: {approved} · Pending: {pending} · Rejected: {rejected} · Blocked: {blocked}"
)
SUMMARY_PENDING = "Waiting for you:"
SUMMARY_NO_PENDING = "No one is waiting for you."
SUMMARY_PENDING_ITEM = "• {person}, waiting {waiting}: {link}"
SUMMARY_WAITING_TODAY = "since today"
SUMMARY_WAITING_DAYS = "{days} day{s}"
SUMMARY_PENDING_MORE = "• and {more} more (open the older cards in this chat)"
SUMMARY_CARD_LINK = "open card"
SUMMARY_NO_CARD = "card not sent yet"
SUMMARY_FAILURES = "Messages that failed in the last 24 hours: {count}"
SUMMARY_NO_FAILURES = "No messages failed in the last 24 hours."
SUMMARY_FAILURE_MORE = "• and {more} more (see the audit table)"
SUMMARY_MISSING = "Still missing, no approved member yet: {handles}"
SUMMARY_NOT_MISSING = "Everyone expected has joined."


def first_name_or_there(first_name: str | None) -> str:
    return (first_name or "").strip() or "there"


# --- recording-channel captures ---------------------------------------------------------------

DRIVE_PRACTICE = "Practice"
DRIVE_ASK_DIRECTION = "Please edit the caption to add IN or OUT."
DRIVE_ASK_RECIPIENT = "Please edit the caption to add R: <recipient>."
DRIVE_DROPS = "TRANSMITTAL local check: {count} messages dropped in the last 24 h"
