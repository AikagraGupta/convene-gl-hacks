#!/usr/bin/env python3
"""Configure the supplied Vapi assistant for safe restaurant inquiries.

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
    "May I ask about availability at {{restaurant_name}} for {{party_size}} "
    "people at {{when_text}}?"
)

SYSTEM_PROMPT = """CONVENE_INQUIRY_V2
You are Convene, an AI assistant making a short restaurant availability inquiry.
The person on the line is venue staff. Disclose that you are an AI assistant in
your first sentence. Be friendly, concise, and pause for answers.

Approved request: {{party_size}} people at {{restaurant_name}} at {{when_text}},
under {{booking_name}}. Ask whether that is available and whether a deposit is
required. If there is a deposit, ask its total amount. Do not ask for a price
per person, menu price, minimum spend, or other meal cost; those depend on what
the group orders. Keep the deposit question separate and brief.
If staff asks for a callback or contact number, you may read the configured
callback number exactly as provided: {{callback_number}}. Say no other number;
never invent, alter, or expose the venue's internal number.
Ask about these requirements only if relevant to the venue: {{requirements}}.
Ignore unrelated chat preferences or comparisons to other restaurants (for
example, do not ask whether a salad restaurant serves McDonald's food).

This version of the agent has no live policy-verification tool. You are NOT
authorized to book, accept an offer, place a hold, promise attendance, pay a
deposit, or state that a reservation is confirmed. If staff offers a table,
thank them and say the group will confirm separately. Do not disclose private
identities or another person's budget. If staff gives unclear terms, ask once
for clarification. Never invent staff answers or infer a deposit from silence.
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
        "name": "Convene Restaurant Inquiry",
        "firstMessage": FIRST_MESSAGE,
        "model": {
            "provider": model["provider"],
            "model": model["model"],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "toolIds": [],
        },
        "maxDurationSeconds": 180,
    }
    if "--apply" not in sys.argv:
        print("Ready to configure Vapi assistant for inquiry-only calls. Run with --apply.")
        return 0
    backup_dir = ROOT / "logs"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"vapi-assistant-{assistant_id}-{stamp}.json"
    backup.write_text(json.dumps(agent, indent=2), encoding="utf-8")
    vapi_calls.request("PATCH", f"/assistant/{assistant_id}", update)
    vapi_calls.assistant()  # verify the safety marker was published
    print(f"Configured Vapi assistant for inquiry-only calls; backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
