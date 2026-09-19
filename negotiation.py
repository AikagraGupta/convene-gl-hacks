"""Deterministic delegation limits. An offer is permission to ask for a booking,
not proof that staff have actually confirmed one.
"""
from __future__ import annotations

import math
import re

import invite
from private_inputs import REQUIREMENTS


def minutes(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use a 24-hour time such as 19:30.")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def window(value: str) -> tuple[int, int]:
    parts = value.strip().split("-")
    if len(parts) != 2:
        raise ValueError("Use a same-day range such as 19:00-20:00.")
    start, end = map(minutes, parts)
    if start > end:
        raise ValueError("End time must follow start time on the same day.")
    return start, end


def money(value) -> float:
    if isinstance(value, bool):
        raise ValueError("Enter an amount in HKD.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("Enter an amount in HKD.") from None
    if not math.isfinite(number) or number < 0 or number > 100000:
        raise ValueError("Enter a valid non-negative amount in HKD.")
    return number


def parse_command(text: str) -> dict:
    match = re.fullmatch(r"(\d{2}:\d{2}-\d{2}:\d{2})\s+budget\s+(\d+(?:\.\d{1,2})?)", text.strip(), re.I)
    if not match:
        raise ValueError("Use /negotiate 19:00-20:00 budget 200 (HKD per person). Deposits always need a new approval.")
    start, end = window(match[1])
    return {"start": start, "end": end, "budget": money(match[2])}


def build(when_text: str, party_size: int, public: dict, private: dict, hard: list[str]) -> dict:
    explicit = public.get("start") is not None
    parsed = invite._parse_clock(when_text)
    if not explicit and not parsed:
        raise ValueError("Set an exact time or use /negotiate 19:00-20:00 budget 200.")
    start = public["start"] if explicit else parsed[0] * 60 + parsed[1]
    end = public["end"] if explicit else start
    budget = public.get("budget")
    requirements = list(hard)
    for item in private.get("inputs", []):
        if item.get("budget") is not None:
            budget = min(budget, item["budget"]) if budget is not None else item["budget"]
        if item.get("start") is not None:
            start, end = max(start, item["start"]), min(end, item["end"])
        requirements.extend(REQUIREMENTS[k] for k in item.get("requirements", []) if k in REQUIREMENTS)
    if start > end:
        raise ValueError("The proposed time does not fit all saved requirements. Agree a new /negotiate window, or ask participants to review their private inputs.")
    return {"start": start, "end": end, "budget": budget, "party_size": party_size,
            "requirements": list(dict.fromkeys(requirements)), "max_deposit": 0, "currency": "HKD"}


def brief(policy: dict) -> str:
    price = f"HK${policy['budget']:g} per person, INCLUDING all mandatory charges" if policy.get("budget") is not None else "not authorised: ask the group to set a budget before committing to any price"
    return (f"Same requested day only. Start between {clock(policy['start'])} and {clock(policy['end'])}. "
            f"Exactly {policy['party_size']} people. Maximum price: {price}. No deposits authorised. "
            "Verify these requirements with staff: " + ("; ".join(policy["requirements"]) or "none") + ". "
            "Call evaluate_offer before accepting ANY offer. Never identify who supplied a requirement.")


def evaluate(policy: dict, offer: dict) -> dict:
    checks = []
    def check(label, state):
        checks.append({"label": label, "state": state})
    try:
        offered_time = minutes(offer.get("time"))
        check("Start time", "pass" if policy["start"] <= offered_time <= policy["end"] else "fail")
    except ValueError:
        check("Start time", "unknown")
    size = offer.get("party_size")
    check("Party size", "unknown" if not isinstance(size, int) or isinstance(size, bool) else "pass" if size == policy["party_size"] else "fail")
    for key, label in (("same_day", "Requested day"), ("requirements_met", "Group requirements")):
        value = offer.get(key)
        check(label, "pass" if value is True else "fail" if value is False else "unknown")
    for key, label, limit in (("price_per_person", "Total price per person", policy.get("budget")), ("deposit_total", "Deposit", 0)):
        try:
            amount = money(offer.get(key))
            check(label, "unknown" if limit is None else "pass" if amount <= limit else "fail")
        except ValueError:
            check(label, "unknown")
    currency = offer.get("currency")
    check("Currency", "pass" if currency == "HKD" else "unknown" if currency is None else "fail")
    states = [c["state"] for c in checks]
    action = "counter" if "fail" in states else "clarify" if "unknown" in states else "accept"
    instruction = {
        "accept": "Within approved limits. You may ask staff to hold this exact offer, then read it back. This check is not a booking confirmation.",
        "clarify": "Do not commit yet. Ask staff for the unknown fields. If a budget is not authorised, end the call and ask the group to set one.",
        "counter": "Do not accept. Ask once for an alternative within the approved limits. If unavailable, explain you need group approval and end the call. Never accept a deposit.",
    }[action]
    return {"action": action, "checks": checks, "instruction": instruction}


def client_tool() -> dict:
    props = {
        "time": {"type": "string", "description": "Venue's offered start in 24-hour HH:MM; omit if unknown."},
        "party_size": {"type": "integer", "description": "Headcount explicitly accommodated by staff."},
        "same_day": {"type": "boolean", "description": "True only if offer is on the group's requested date."},
        "price_per_person": {"type": "number", "description": "HKD per person including service charge, minimum spend allocation and mandatory extras. Omit if unknown."},
        "deposit_total": {"type": "number", "description": "Total deposit requested. Zero only if staff confirm no deposit."},
        "currency": {"type": "string", "description": "Currency explicitly quoted, e.g. HKD."},
        "requirements_met": {"type": "boolean", "description": "True only when staff confirmed ALL requirements in the brief, or brief has none. Omit if unresolved."},
    }
    return {"type": "client", "name": "evaluate_offer", "expects_response": True,
            "response_timeout_secs": 10,
            "description": "MANDATORY before accepting any restaurant offer. Checks time, price, deposit and requirements against the approved delegation. Never invent missing fields.",
            "parameters": {"type": "object", "properties": props, "required": []}}
