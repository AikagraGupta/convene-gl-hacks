"""The text ballot, and the approval phrase that stands between a model and
a stranger's telephone.

Two asymmetries drive every case in here.

For votes: in a live group chat most messages are NOT votes. A parser that
guesses turns "table for 4 at 7" into a vote for option 4 and miscounts the
ballot silently. So the bar is "unambiguously a vote", and everything else is
left alone as ordinary conversation.

For approval: a missed approval costs someone retyping two words. A false
positive rings a real business with a synthetic voice. Those are not the same
mistake, so the matcher is anchored and deliberately unforgiving.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import channels
from channels import Ballot, parse_approval, parse_vote
from harness import Suite


def run() -> Suite:
    s = Suite("channels", expect_at_least=45)

    # --- votes that must count -------------------------------------------
    for text, expected in [
        ("2", 2), ("  3 ", 3), ("1.", 1), ("#2", 2), ("3!", 3),
        ("im down for 2", 2), ("I'm down for 2", 2),
        ("lets do the second one", 2), ("let's do the third", 3),
        ("option 3 pls", 3), ("going with 1", 1), ("vote for 2", 2),
        ("prefer third", 3), ("number 2", 2), ("ill take 1", 1),
    ]:
        s.eq(f"counts {text!r}", parse_vote(text, 3), expected)

    # --- messages that must NOT count ------------------------------------
    # Every one of these is something a real group chat produces constantly.
    for text in [
        "table for 4 at 7",        # a fact about the booking, not a vote
        "there are 6 of us",
        "can we do 8pm",
        "what time though",
        "not 2",                   # a veto is not a vote
        "anything but 1",
        "cant do 3",
        "rather not 2",
        "",
        "hmm",
        "ok",
        "i hate thai food",
    ]:
        s.eq(f"ignores {text!r}", parse_vote(text, 3), None)

    s.eq("an out-of-range number is not a vote", parse_vote("9", 3), None)
    s.eq("option 9 with only 3 options is not a vote", parse_vote("option 9", 3), None)

    # "+1" means "same as the last person", not "option 1". Reading it as the
    # latter silently miscounts every ballot it appears in.
    s.eq("'+1' follows the previous vote", parse_vote("+1", 3, 3), 3)
    s.eq("'+1' with nothing to follow is not a vote", parse_vote("+1", 3, None), None)
    s.eq("'same' follows too", parse_vote("same", 3, 2), 2)

    # --- the approval gate -----------------------------------------------
    for text in ["call them", "CALL THEM", "call now", "yes call",
                 "confirm call", "go ahead and call", " call them. "]:
        s.eq(f"approves {text!r}", parse_approval(text), "approve")
    for text in ["cancel", "stop", "dont call", "don't call", "abort", "hold on"]:
        s.eq(f"cancels {text!r}", parse_approval(text), "cancel")

    # The ones that must NOT dial. Each is a plausible thing to say while
    # discussing a booking that has not been approved.
    for text in [
        "yeah we should call them at some point",
        "did anyone call them?",
        "maybe call them tomorrow",
        "i'll call them myself",
        "call them?",
        "",
    ]:
        s.eq(f"does not approve {text!r}", parse_approval(text), None)

    # --- the ballot -------------------------------------------------------
    b = Ballot("Where are we playing?",
               ["Kennedy Town courts", "Victoria Park", "Sai Ying Pun"],
               "Closes at 6pm.")
    rendered = b.render()
    s.contains("the ballot numbers its options", rendered, "2. Victoria Park")
    s.contains("and says how to answer", rendered, "Reply with the number")
    s.contains("and carries the deadline", rendered, "Closes at 6pm")

    s.check("a vote is recorded", b.record("kai", "2"))
    s.check("a non-vote is not recorded", not b.record("mei", "what time"))
    s.eq("and does not appear in the tally", len(b.votes), 1)
    s.check("one person voting twice replaces their vote, not adds one",
            b.record("kai", "3") and len(b.votes) == 1)

    # --- closing: the whole point of the thing ---------------------------
    clear = Ballot("q", ["A", "B"])
    clear.record("kai", "2")
    clear.record("akshay", "2")
    clear.record("prakhar", "1")
    s.eq("a clear winner wins", clear.winner()[0], "B")
    s.eq("and resolve agrees", clear.resolve()[0], "B")
    s.check("resolve explains itself", "vote" in clear.resolve()[1])

    tied = Ballot("q", ["A", "B"])
    tied.record("kai", "2")
    tied.record("mei", "1")
    s.check("a tie is detected", tied.is_tied())
    s.eq("winner() refuses to invent one", tied.winner(), None)
    resolved, why = tied.resolve()
    s.check("but resolve still closes it", resolved in {"A", "B"})
    s.contains("and says it was a tie", why, "tied")

    empty = Ballot("q", ["A", "B"])
    s.eq("silence falls back to the default option", empty.resolve(default_index=1)[0], "A")
    s.contains("and says so", empty.resolve()[1], "nobody voted")
    s.eq("a second default can be chosen", empty.resolve(default_index=2)[0], "B")

    # --- what WhatsApp cannot do -----------------------------------------
    # Asserted rather than assumed: Meta's Groups API does not support
    # interactive messages, so any code branching on a poll must see False.
    wa = channels.WhatsAppChannel(phone_number_id="", token="")
    s.check("WhatsApp reports no native poll", not wa.supports_native_poll())
    s.check("WhatsApp reports no buttons", not wa.supports_buttons())
    s.check("and sending without credentials fails loudly rather than silently",
            wa.send_text("group-1", "hello") is False)

    # --- webhook flattening ----------------------------------------------
    body = {"entry": [{"changes": [{"value": {
        "messages": [
            {"from": "85261234567", "type": "text", "timestamp": "1700000000",
             "text": {"body": "cant do hotpot again"}, "group_id": "g-1"},
            {"from": "85267654321", "type": "image", "timestamp": "1700000001"},
        ]}}]}]}
    parsed = channels.WhatsAppChannel.parse_webhook(body)
    s.eq("one text message is extracted", len(parsed), 1)
    s.eq("the sender is preserved, which constraint extraction needs",
         parsed[0]["sender"], "85261234567")
    s.eq("so is the group", parsed[0]["chat_id"], "g-1")
    s.eq("and the text", parsed[0]["text"], "cant do hotpot again")
    s.eq("an empty webhook is empty, not a crash",
         channels.WhatsAppChannel.parse_webhook({}), [])

    return s
