"""Vapi outbound phone leg. This module never starts a call on import.

The caller must have a freshly approved Convene payload. The free Vapi number
is inbound-only, so an imported, active provider number is required.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from envlite import env, env_flag, env_list
import places

BASE = "https://api.vapi.ai"
PROMPT_MARKER = "CONVENE_INQUIRY_V1"
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class VapiError(Exception):
    pass


def request(method: str, path: str, payload: dict | None = None) -> dict:
    key = env("VAPI_API_KEY")
    if not key:
        raise VapiError("VAPI_API_KEY is missing")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Vapi's edge rejects urllib's default User-Agent on this account.
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Never log request headers or the response body: the latter can echo
        # account credentials, phone details, or private assistant settings.
        raise VapiError(f"Vapi returned HTTP {exc.code} for {path}") from exc
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise VapiError(f"Vapi request failed for {path}: {type(exc).__name__}") from exc


def phone_number() -> dict:
    number_id = env("VAPI_PHONE_NUMBER_ID")
    if not number_id:
        raise VapiError("VAPI_PHONE_NUMBER_ID is missing")
    phone = request("GET", f"/phone-number/{number_id}")
    if phone.get("id") != number_id:
        raise VapiError("Vapi returned a different phone number")
    if phone.get("provider") == "vapi":
        raise VapiError("This is a free Vapi number; import a Twilio number for outbound calls")
    if phone.get("status") != "active":
        raise VapiError("The imported Vapi phone number is not active")
    return phone


def assistant() -> dict:
    assistant_id = env("VAPI_ASSISTANT_ID")
    if not assistant_id:
        raise VapiError("VAPI_ASSISTANT_ID is missing")
    agent = request("GET", f"/assistant/{assistant_id}")
    messages = (agent.get("model") or {}).get("messages") or []
    prompt = "\n".join(str(message.get("content") or "") for message in messages)
    if PROMPT_MARKER not in prompt:
        raise VapiError("Vapi assistant is not configured for Convene; run setup_vapi.py")
    return agent


def preflight(pending: dict) -> dict:
    if pending.get("status") != "approved":
        raise VapiError("Fresh group approval is required before a Vapi call")
    target = str(pending.get("dial_number") or "")
    if not E164.fullmatch(target):
        raise VapiError("Approved dial number is not in E.164 format")
    if pending.get("demo_override"):
        if places.normalise_phone(env("DEMO_PHONE")) != target:
            raise VapiError("DEMO_PHONE changed since approval; run /close again")
    else:
        if places.normalise_phone(str(pending.get("real_number") or "")) != target:
            raise VapiError("Approved target no longer matches the venue number")
        allow = {places.normalise_phone(number) for number in env_list("CONSENTED_NUMBERS")}
        if not env_flag("ALLOW_ANY_NUMBER") and target not in allow:
            raise VapiError("Venue number is no longer on the consent allowlist")
    phone = phone_number()
    assistant()
    return phone


def call_payload(pending: dict, phone: dict) -> dict:
    """No private identities and no authority to place a booking go to Vapi."""
    policy = pending.get("negotiation") or {}
    requirements = policy.get("requirements") or pending.get("constraints") or []
    if isinstance(requirements, list):
        requirements_text = "; ".join(str(x) for x in requirements)
    else:
        requirements_text = str(requirements)
    return {
        "assistantId": env("VAPI_ASSISTANT_ID"),
        "phoneNumberId": phone["id"],
        "customer": {"number": pending["dial_number"]},
        "assistantOverrides": {
            "variableValues": {
                "restaurant_name": str(pending.get("restaurant_name") or "the venue")[:120],
                "party_size": str(pending.get("party_size") or "unknown"),
                "when_text": str(pending.get("when_text") or "time not stated")[:100],
                "booking_name": str(pending.get("booking_name") or "a guest")[:60],
                "requirements": requirements_text[:500] or "none stated",
                "approved_limits": str(pending.get("negotiation_brief") or "No flexibility was approved")[:500],
            }
        },
    }


def start_call(pending: dict, phone: dict) -> dict:
    result = request("POST", "/call", call_payload(pending, phone))
    if not result.get("id"):
        raise VapiError("Vapi did not return a call ID; check the Vapi dashboard before retrying")
    return result


def get_call(call_id: str) -> dict:
    return request("GET", f"/call/{call_id}")


def turns(call: dict) -> list[dict]:
    artifact = call.get("artifact") or {}
    messages = artifact.get("messages") or call.get("messages") or []
    return [
        {"source": "user" if item.get("role") == "user" else "ai",
         "message": str(item.get("message") or "")}
        for item in messages
        if item.get("role") in ("user", "assistant", "bot") and item.get("message")
    ]
