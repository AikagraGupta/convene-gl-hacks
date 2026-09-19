#!/usr/bin/env python3
"""channels.py — where the argument happens, abstracted.

v1 was Telegram and only Telegram: 92 references to the Bot API across bot.py
alone. Moving to WhatsApp is not a find-and-replace, because the two platforms
disagree about the two mechanisms this project is actually built on.

WHAT WHATSAPP TAKES AWAY (checked against Meta's Groups API docs, not memory):

  * No native poll. Groups accept "text messages, media messages, text-based
    templates, media-based templates". Interactive messages -- buttons, lists,
    polls -- are NOT supported in groups. Telegram's sendPoll, with its real
    vote counts, was the closing mechanism in the pitch. It is gone.
  * No approval button. The human-in-the-loop gate was an inline keyboard whose
    callback was the only path to dialling. That gate has to survive in another
    form, because it is the safety property, not a UI flourish.
  * Groups cap at 8 participants, and there can be only one Cloud API business
    per group.
  * The business must CREATE the group; people join by invite link. You cannot
    drop this bot into an existing friend group, which is the exact deployment
    story v1 told on stage.
  * Access needs an Official Business Account. That is a verification process,
    not a signup form.

WHAT IT GIVES BACK, and this is not nothing: inbound group messages arrive by
webhook with the sender identified, which is all the constraint extraction ever
needed. And WhatsApp is where Hong Kong group chats actually live, which was
always the weakest part of the Telegram story.

THE DESIGN ANSWER: stop treating taps as the primitive. Both the vote and the
approval become TEXT, parsed out of how people already reply in a group chat --
"2", "im down for the second one", "anything but hotpot", "yeah call them".
A number-reply ballot works identically on both platforms, so the core stops
caring which channel it is on, and the WhatsApp version is arguably truer to
the original claim: it reads the conversation instead of asking for a form.

Telegram keeps its native poll where it has one. The ballot below is the
common denominator, not a lowest one.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from envlite import env

GRAPH_VERSION = "v21.0"
TIMEOUT = 20


# --------------------------------------------------------------------------
# The text protocol: what a vote looks like when there is no poll widget
# --------------------------------------------------------------------------

# Ordinals people actually type in a group chat instead of a bare digit.
_ORDINALS = {
    "first": 1, "1st": 1, "one": 1, "a": 1,
    "second": 2, "2nd": 2, "two": 2, "b": 2,
    "third": 3, "3rd": 3, "three": 3, "c": 3,
}

# "+1" means "I agree with the last thing said", not "option 1". Getting this
# backwards silently miscounts every ballot, so it is handled before digits.
_AGREEMENT = re.compile(r"^\s*(\+1|same|me too|agreed?|ditto|sounds good)\s*$", re.I)

_VETO = re.compile(
    r"\b(?:not|no|anything but|except|rather not|hate|can'?t do|cannot do)\b",
    re.I,
)


@dataclass
class Ballot:
    """A numbered choice posted as plain text, tallied from replies.

    Deliberately dumb: options are 1..n, replies are parsed leniently, and a
    message that is not a vote is left alone rather than guessed at. A ballot
    that miscounts is worse than a ballot that asks again.
    """

    question: str
    options: list[str]
    deadline_text: str = ""
    votes: dict[str, int] = field(default_factory=dict)   # sender -> option index

    def render(self) -> str:
        lines = [self.question, ""]
        for i, option in enumerate(self.options, 1):
            lines.append(f"{i}. {option}")
        lines.append("")
        lines.append("Reply with the number. " + (self.deadline_text or ""))
        return "\n".join(lines).strip()

    def record(self, sender: str, text: str, last_vote_by_anyone: int | None = None) -> bool:
        """Returns True if this message was counted as a vote."""
        choice = parse_vote(text, len(self.options), last_vote_by_anyone)
        if choice is None:
            return False
        self.votes[sender] = choice
        return True

    def tally(self) -> list[tuple[str, int]]:
        counts = [0] * len(self.options)
        for choice in self.votes.values():
            counts[choice - 1] += 1
        return sorted(zip(self.options, counts), key=lambda p: -p[1])

    def winner(self) -> tuple[str, int] | None:
        """The single winner, or None when there isn't one.

        None covers two different failures and the caller must tell them
        apart with `is_tied()`: nobody voted at all, or the top is tied. A
        ballot exists to close a decision, so quietly returning whichever
        option happened to sort first would reintroduce the exact problem
        this project claims to solve.
        """
        ranked = self.tally()
        if not ranked or ranked[0][1] == 0:
            return None
        if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
            return None
        return ranked[0]

    def is_tied(self) -> bool:
        ranked = self.tally()
        return (len(ranked) > 1
                and ranked[0][1] > 0
                and ranked[0][1] == ranked[1][1])

    def resolve(self, default_index: int = 1) -> tuple[str, str]:
        """Close the ballot no matter what. Returns (option, why).

        The deadline default is what makes silence resolve instead of stall,
        and a tie is just a different flavour of silence.
        """
        won = self.winner()
        if won:
            return won[0], f"{won[1]} vote(s)"
        if self.is_tied():
            top = self.tally()[0][1]
            tied = [o for o, c in self.tally() if c == top]
            # Earliest vote breaks it: whoever committed first gets it.
            for sender, choice in self.votes.items():
                option = self.options[choice - 1]
                if option in tied:
                    return option, f"tied on {top}, broken by first vote ({sender})"
            return tied[0], f"tied on {top}"
        return self.options[default_index - 1], "nobody voted, fell back to the default"


def parse_vote(text: str, n_options: int,
               last_vote_by_anyone: int | None = None) -> int | None:
    """Pull an option number out of a chat message, or None.

    None means "this was not a vote", and the caller must treat it as ordinary
    conversation. Being conservative here is the whole point: in a live group
    chat most messages are not votes, and a parser that guesses turns "table
    for 4 at 7" into a vote for option 4.
    """
    if not text:
        return None
    stripped = text.strip()

    if _AGREEMENT.match(stripped):
        return last_vote_by_anyone

    # A bare number, optionally with punctuation or a trailing emoji.
    bare = re.match(r"^\s*#?([1-9])\b[.)!\s]*$", stripped)
    if bare:
        choice = int(bare.group(1))
        return choice if 1 <= choice <= n_options else None

    lowered = stripped.lower()
    if _VETO.search(lowered):
        return None  # "not 2" is a veto, and a veto is not a vote

    # "im down for 2", "lets do the second one", "option 3 pls"
    phrase = re.search(
        r"\b(?:option|number|no\.?|#|lets do|let's do|down for|going with|"
        r"vote for|ill take|i'll take|prefer)\s*(?:the\s+)?"
        r"([1-9]|first|1st|second|2nd|third|3rd|one|two|three)\b",
        lowered,
    )
    if phrase:
        token = phrase.group(1)
        choice = int(token) if token.isdigit() else _ORDINALS.get(token)
        if choice and 1 <= choice <= n_options:
            return choice
    return None


# The approval gate. On Telegram this was a button whose callback_data was the
# only route to dialling. Without buttons the same property has to hold: the
# phrase must be unambiguous, deliberate, and impossible to produce by accident
# in the middle of a conversation about dinner.
_APPROVAL = re.compile(r"^\s*(call them|call now|yes call|confirm call|go ahead and call)\s*[.!]?\s*$", re.I)
_CANCEL = re.compile(r"^\s*(cancel|stop|don'?t call|abort|hold on)\s*[.!]?\s*$", re.I)


def parse_approval(text: str) -> str | None:
    """'approve' | 'cancel' | None. Anything ambiguous is None, on purpose.

    A loose matcher here would let "yeah we should call them at some point"
    dial a real business. The cost of an unrecognised approval is that someone
    types it again; the cost of a false positive is a stranger's phone ringing.
    """
    if not text:
        return None
    if _APPROVAL.match(text):
        return "approve"
    if _CANCEL.match(text):
        return "cancel"
    return None


# --------------------------------------------------------------------------
# The channel interface
# --------------------------------------------------------------------------

class Channel(Protocol):
    name: str

    def send_text(self, chat_id: str, text: str) -> bool: ...
    def post_ballot(self, chat_id: str, ballot: Ballot) -> bool: ...
    def ask_approval(self, chat_id: str, summary: str) -> bool: ...
    def supports_native_poll(self) -> bool: ...
    def supports_buttons(self) -> bool: ...


class WhatsAppChannel:
    """WhatsApp Cloud API, Groups.

    Everything here goes through the Graph API with a system-user token. The
    group must already exist and have been created by this business number.

    NOT RUNNABLE WITHOUT AN OFFICIAL BUSINESS ACCOUNT. That is a Meta
    verification, measured in days at best, so this adapter is written to be
    correct rather than to be demoed this week. `preflight.py` should refuse
    to claim WhatsApp is ready until a real send succeeds.
    """

    name = "whatsapp"

    def __init__(self, phone_number_id: str | None = None, token: str | None = None):
        self.phone_number_id = phone_number_id or env("WA_PHONE_NUMBER_ID") or ""
        self.token = token or env("WA_ACCESS_TOKEN") or ""

    def supports_native_poll(self) -> bool:
        return False   # interactive messages are not supported in groups

    def supports_buttons(self) -> bool:
        return False

    def _post(self, payload: dict) -> bool:
        if not (self.phone_number_id and self.token):
            print("[whatsapp] no WA_PHONE_NUMBER_ID / WA_ACCESS_TOKEN, "
                  f"would have sent:\n{payload.get('text', {}).get('body', '')}")
            return False
        url = (f"https://graph.facebook.com/{GRAPH_VERSION}/"
               f"{self.phone_number_id}/messages")
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                json.loads(resp.read().decode("utf-8"))
            return True
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            print(f"[whatsapp] HTTP {exc.code}: {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"[whatsapp] {type(exc).__name__}: {exc}")
        return False

    def send_text(self, chat_id: str, text: str) -> bool:
        return self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "group",
            "to": chat_id,
            "type": "text",
            "text": {"body": text[:4096], "preview_url": False},
        })

    def post_ballot(self, chat_id: str, ballot: Ballot) -> bool:
        return self.send_text(chat_id, ballot.render())

    def ask_approval(self, chat_id: str, summary: str) -> bool:
        return self.send_text(
            chat_id,
            f"{summary}\n\nReply \"call them\" to go ahead, or \"cancel\".",
        )

    @staticmethod
    def parse_webhook(body: dict) -> list[dict]:
        """Flatten a Cloud API webhook into {chat_id, sender, text, ts}.

        Group messages carry the participant in the message's `from` field, so
        attribution -- which is what constraint extraction depends on -- works
        the same as it did on Telegram.
        """
        out: list[dict] = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue
                    out.append({
                        "chat_id": (message.get("group_id")
                                    or value.get("group_id")
                                    or message.get("from", "")),
                        "sender": message.get("from", ""),
                        "text": (message.get("text") or {}).get("body", ""),
                        "ts": message.get("timestamp", ""),
                    })
        return out
