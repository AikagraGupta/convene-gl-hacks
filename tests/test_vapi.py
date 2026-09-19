"""Outbound Vapi boundary: account checks, approval, and one-shot dispatch."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness import Suite
import vapi_calls
import setup_vapi
from bridge import server


def dispatch(path: str):
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    responses = []
    handler._json = lambda data, code=200: responses.append((code, data))
    handler.do_POST()
    return responses[-1]


class NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def run() -> Suite:
    s = Suite("vapi", expect_at_least=14)
    phone_id = "aeaefa96-b275-480a-83a2-00db92e0e92d"
    assistant_id = "336070b4-5682-46e3-90cb-39fe4a8c9c43"
    phone = {"id": phone_id, "number": "+19594568660", "provider": "twilio", "status": "active"}
    agent = {"id": assistant_id, "model": {"messages": [{"content": vapi_calls.PROMPT_MARKER}]}}
    pending = {"id": "approved-1", "status": "approved", "call_provider": "vapi",
               "chat_id": -100, "dial_number": "+85260894121", "demo_override": True,
               "restaurant_name": "Example", "party_size": 6, "when_text": "19:30",
               "booking_name": "Kai", "negotiation_brief": "under HK$200",
               "callback_number": "+85260001111",
               "negotiation": {"requirements": ["Vegetarian meal available"]}}
    with patch.dict(os.environ, {"VAPI_API_KEY": "test-key", "VAPI_PHONE_NUMBER_ID": phone_id,
                               "VAPI_ASSISTANT_ID": assistant_id, "DEMO_PHONE": "+85260894121"}):
        with patch.object(vapi_calls, "request", return_value={**phone, "provider": "vapi"}):
            s.raises("free Vapi number cannot be used for outbound", vapi_calls.VapiError,
                     vapi_calls.phone_number)
        with patch.object(vapi_calls, "request", return_value={**phone, "status": "pending"}):
            s.raises("inactive imported number cannot dial", vapi_calls.VapiError,
                     vapi_calls.phone_number)
        with patch.object(vapi_calls, "request", return_value={"model": {"messages": [{"content": "sales"}]}}):
            s.raises("sales template cannot make a restaurant call", vapi_calls.VapiError,
                     vapi_calls.assistant)
        with patch.object(vapi_calls, "request", side_effect=[phone, agent]):
            s.eq("Twilio number passes account preflight", vapi_calls.preflight(pending)["provider"], "twilio")
        s.raises("unapproved payload cannot launch a call", vapi_calls.VapiError,
                 vapi_calls.preflight, {**pending, "status": "awaiting_approval"})
        s.raises("bad target number cannot launch a call", vapi_calls.VapiError,
                 vapi_calls.preflight, {**pending, "dial_number": "60894121"})
        with patch.dict(os.environ, {"DEMO_PHONE": "+85269999999"}):
            s.raises("changed demo number invalidates the approval", vapi_calls.VapiError,
                     vapi_calls.preflight, pending)
        payload = vapi_calls.call_payload(pending, phone)
        s.eq("Vapi dials the approved demo number", payload["customer"]["number"], "+85260894121")
        s.eq("Vapi uses the imported Twilio number", payload["phoneNumberId"], phone_id)
        s.eq("Vapi uses the configured assistant", payload["assistantId"], assistant_id)
        s.check("private identities are absent from call variables", "private" not in json.dumps(payload).lower())
        s.contains("relevant requirement reaches the agent", json.dumps(payload), "Vegetarian meal")
        s.eq("configured callback number reaches the agent", payload["assistantOverrides"]["variableValues"]["callback_number"], "+85260001111")
        s.eq("approved negotiation reaches the booking agent", payload["assistantOverrides"]["variableValues"]["negotiation_brief"], "under HK$200")
        s.check("budget is not passed to the inquiry agent",
                "approved_limits" not in payload["assistantOverrides"]["variableValues"])
        s.contains("agent asks whether a deposit is required", setup_vapi.SYSTEM_PROMPT,
                   "whether a deposit is")
        s.contains("agent asks for an FPS number when needed", setup_vapi.SYSTEM_PROMPT,
                   "FPS payment number")
        s.contains("agent ends after a booking confirmation", setup_vapi.SYSTEM_PROMPT,
                   "end the call immediately")
        s.contains("agent thanks staff without a goodbye loop", setup_vapi.SYSTEM_PROMPT,
                   "goodbye phrase")
        s.contains("agent does not ask for a meal price", setup_vapi.SYSTEM_PROMPT,
                   "Do not ask for a price")
        s.eq("transcript keeps staff and agent roles", vapi_calls.turns({"artifact": {"messages": [
            {"role": "bot", "message": "Hello"}, {"role": "user", "message": "Yes"},
            {"role": "system", "message": "internal"}]}}),
            [{"source": "ai", "message": "Hello"}, {"source": "user", "message": "Yes"}])

        with TemporaryDirectory() as directory, \
             patch.object(server, "PENDING_PATH", Path(directory) / "pending.json"), \
             patch.object(server.vapi_calls, "preflight", return_value=phone), \
             patch.object(server.vapi_calls, "start_call", return_value={"id": "vapi-call-1"}) as create, \
             patch.object(server, "send_telegram_returning_id", return_value=5), \
             patch.object(server.threading, "Thread", NoThread):
            server.write_pending({**pending, "status": "awaiting_approval"})
            s.eq("endpoint refuses a call before approval", dispatch("/dial")[0], 409)
            s.eq("refusal makes no Vapi request", create.call_count, 0)
            server.write_pending(pending)
            code, response = dispatch("/dial")
            s.eq("approved call is created", code, 200)
            s.eq("call ID is persisted", server.read_pending()["vapi_call_id"], "vapi-call-1")
            s.eq("Vapi mode does not arm the ElevenLabs page", server.read_pending()["dial"], False)
            s.eq("duplicate dial is refused", dispatch("/dial")[0], 409)
            s.eq("only one outbound call was requested", create.call_count, 1)
        s.contains("Vapi result reports an unconfirmed call", server.format_vapi_result(pending, []),
                   "Could not book")
        mismatch_turns = [
            {"source": "user", "message": "Sorry, we only have 5 seats at that time."},
        ]
        mismatch_text = server.format_vapi_result(pending, mismatch_turns)
        s.contains("Vapi explains a smaller capacity", mismatch_text,
                   "did not have the requested table")
        s.check("mismatched Vapi hold has no calendar link",
                "calendar.google.com" not in mismatch_text)
        # Book by default: staff saying "Okay" / "Done" is a booking.
        okay_turns = [{"source": "user", "message": m} for m in (
            "When is it?", "9:00 PM.", "How did you spell—",
            "Okay. Kai, K-A-I, for 9:00 PM, two people, yes?", "Okay.", "Done.")]
        okay_text = server.format_vapi_result(pending, okay_turns)
        s.contains("okay/done from staff counts as booked", okay_text, "Booked")
        s.contains("okay/done booking gets a calendar link", okay_text, "calendar.google.com")
        for refusal in ("Sorry, we're fully booked tonight.", "We don't take reservations.",
                        "Sorry, I can't book that.", "We are closed that day.", "今晚冇位"):
            s.contains(f"explicit refusal is not booked: {refusal}",
                       server.format_vapi_result(pending, [{"source": "user", "message": refusal}]),
                       "Could not book")
        s.eq("refusal then a clear yes is booked",
             server.booking_verdict(pending, [{"source": "user", "message": "9 is full."},
                                              {"source": "user", "message": "9:30 then? Done."}]),
             "confirmed")
        s.eq("ElevenLabs 'unclear' outcome is booked when staff did not refuse",
             server.lean_to_booked(pending, {"status": "unclear"}, okay_turns)["status"], "confirmed")
        s.eq("ElevenLabs decline with a refusal stays declined",
             server.lean_to_booked(pending, {"status": "declined"},
                                   [{"source": "user", "message": "Sorry, no tables tonight."}])["status"],
             "declined")
        matching_turns = [
            {"source": "user", "message": "Your reservation is booked for 6 people at 8:00 pm. There is a HK$100 deposit. FPS number is 60894121."},
        ]
        matching_text = server.format_vapi_result(pending, matching_turns)
        s.contains("matching Vapi booking says booked", matching_text,
                   "Booked")
        s.contains("matching Vapi booking records the deposit", matching_text,
                   "Deposit: HK$100")
        s.contains("matching Vapi booking records FPS", matching_text,
                   "60894121")
        s.contains("matching Vapi booking offers a calendar link", matching_text,
                   "calendar.google.com")
        s.contains("calendar link is tappable in Telegram", matching_text,
                   'href=\"https://calendar.google.com')
        # A completed Vapi call must actually close the Telegram live message
        # and send a result. Vapi's real artifacts label agent turns "bot".
        ended = {"status": "ended", "endedReason": "customer-ended-call", "artifact": {
            "messages": [{"role": "bot", "message": "I'm an AI assistant."},
                         {"role": "user", "message": "Your table is booked for 6 people at 7:30 pm. No deposit is required."}]}}
        with TemporaryDirectory() as directory, \
             patch.object(server, "PENDING_PATH", Path(directory) / "pending.json"), \
             patch.object(server.vapi_calls, "get_call", return_value=ended), \
             patch.object(server, "archive_call", return_value=Path(directory) / "call.json"), \
             patch.object(server, "send_telegram", return_value=True) as send, \
             patch.object(server, "edit_telegram", return_value=True) as edit:
            server.write_pending({**pending, "status": "vapi_calling", "vapi_call_id": "call-1",
                                  "live_message_id": 5, "live_turns": []})
            server.monitor_vapi_call("call-1")
            s.eq("completed call closes local state", server.read_pending()["status"], "done")
            s.eq("completed call posts result and full transcript", send.call_count, 2)
            s.contains("Telegram live transcript includes Vapi's bot turn",
                       str(edit.call_args_list), "I'm an AI assistant")
            s.check("group result asserts the confirmed booking", "Booked" in send.call_args_list[0].args[1])
            s.contains("final transcript includes staff after booking",
                       send.call_args_list[1].args[1], "Your table is booked")
            s.check("transcript delivery is recorded", server.read_pending()["transcript_sent"])
            send.reset_mock()
            edit.reset_mock()
            server.write_pending({**pending, "status": "vapi_calling", "vapi_call_id": "call-2",
                                  "live_message_id": 6, "private_mode": True, "live_turns": []})
            server.monitor_vapi_call("call-2")
            s.check("private call transcript is visible in Telegram",
                    "Your table is booked" in str(edit.call_args_list)
                    and "Your table is booked" in str(send.call_args_list))
        long_turns = [{"source": "user", "message": "R&B <test> " * 900}]
        chunks = server.transcript_messages(pending, long_turns)
        s.check("long transcript is split within Telegram limits",
                len(chunks) > 1 and all(len(chunk.encode("utf-16-le")) // 2 < 4096
                                        for chunk in chunks))
        s.check("transcript HTML escapes venue speech",
                all("<test>" not in chunk for chunk in chunks)
                and "&lt;test&gt;" in "".join(chunks))
        s.contains("a call with no speech still has a transcript notice",
                   server.transcript_messages(pending, [])[0], "No speech transcript")
        # The browser/ElevenLabs path uses the same completion rule: a declined
        # call still yields its transcript, not just a status message.
        with TemporaryDirectory() as directory, \
             patch.object(server, "PENDING_PATH", Path(directory) / "pending.json"), \
             patch.object(server, "collect_outcome", return_value=({"status": "declined"}, "transcript (derived)")), \
             patch.object(server, "archive_call", return_value=Path(directory) / "call.json"), \
             patch.object(server, "send_telegram", return_value=True) as send:
            server.write_pending({**pending, "status": "dialing", "call_provider": "elevenlabs",
                                  "negotiation": None,
                                  "live_turns": [{"source": "user", "message": "Sorry, we're full."}]})
            code, result = dispatch("/outcome")
            s.eq("declined browser call completes", code, 200)
            s.check("declined browser call posts its transcript",
                    send.call_count == 2 and "we're full" in send.call_args_list[1].args[1])
            s.check("declined browser call records transcript delivery",
                    server.read_pending()["transcript_sent"])
    return s
