#!/usr/bin/env python3
"""Configure the supplied Vapi assistant for restaurant booking calls.

Run with --apply to change the remote assistant. A local ignored backup is
written first. This never dials a phone number.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from envlite import env, load_env
import vapi_calls

ROOT = Path(__file__).resolve().parent

FIRST_MESSAGE = (
    "Hello, I'm an AI assistant calling on behalf of {{booking_name}}. "
    "I'd like to book a table at {{restaurant_name}} for {{party_size}} "
    "people at {{when_text}}. Is that possible?"
)

SYSTEM_PROMPT = """RAIN_CHECK_BOOKING_V3
You are RainCheck, an AI assistant making a short restaurant booking call.
The person on the line is venue staff. Disclose that you are an AI assistant in
your first sentence. Be friendly, concise, and pause for answers.

Approved request: {{party_size}} people at {{restaurant_name}} at {{when_text}},
under {{booking_name}}.
Approved delegation: {{negotiation_brief}}

Ask staff to book the requested table. If the requested time is full or there
are not enough seats, negotiate one alternative start time inside the approved
same-day window and ask staff to book that alternative. Never change the date
or headcount. The request above already contains the booking date: say that
exact day and date. Never say "today" or "tonight" unless the request does. Do not ask for a price per person, menu price, minimum spend, or
other meal cost; the group orders from the menu.

Ask whether a deposit is required. If there is one, ask for its total amount
and the FPS payment number. Tell staff: "Thank you, I'll transfer the deposit
to the FPS number you provided." Never claim payment has already been made.
If staff asks for a callback or contact number, you may read the configured
callback number exactly as provided: {{callback_number}}. Say no other number;
never invent, alter, or expose the venue's internal number.
Ask about these requirements only if relevant to the venue: {{requirements}}.
Do not disclose private identities or another person's budget. Never invent
staff answers or infer a deposit or FPS number from silence.

When staff explicitly says the reservation is booked, reserved, or confirmed,
say "Thank you for helping us book it" and, if a deposit is required, say the
FPS transfer sentence above. Then say goodbye and end the call immediately.
Do not ask another question, make small talk, or keep the line open after the
booking confirmation.
If this is a wrong number or staff cannot help, apologize and end politely.
"""


def main() -> int:
    load_env(ROOT / ".env")
    assistant_id = env("VAPI_ASSISTANT_ID")
    if not assistant_id:
        print("VAPI_ASSISTANT_ID is missing")
        return 1
    agent = vapi_calls.request("GET", f"/assistant/{assistant_id}")
    model = agent.get("model") or {}
    if not model.get("provider") or not model.get("model"):
        print("The existing assistant has no supported model configuration")
        return 1
    update = {
        "name": "RainCheck Restaurant Booking",
        "firstMessage": FIRST_MESSAGE,
        "model": {
            "provider": model["provider"],
            "model": model["model"],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "toolIds": [],
        },
        "maxDurationSeconds": 180,
        "endCallFunctionEnabled": True,
    }
    if "--apply" not in sys.argv:
        print("Ready to configure Vapi assistant for booking calls. Run with --apply.")
        return 0
    backup_dir = ROOT / "logs"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"vapi-assistant-{assistant_id}-{stamp}.json"
    backup.write_text(json.dumps(agent, indent=2), encoding="utf-8")
    vapi_calls.request("PATCH", f"/assistant/{assistant_id}", update)
    vapi_calls.assistant()  # verify the safety marker was published
    print(f"Configured Vapi assistant for booking calls; backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
