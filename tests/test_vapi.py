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
        s.contains("Vapi result does not claim a booking", server.format_vapi_result(pending, []),
                   "No reservation is verified")
    return s
