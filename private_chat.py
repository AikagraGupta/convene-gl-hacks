"""Telegram DM experience; never forwards messages to a group or an LLM."""
from __future__ import annotations

import re

import negotiation
import private_inputs as store
from tgtext import esc

KEYBOARD = {"keyboard": [["Budget", "Time window"], ["Vegetarian", "Vegan", "No pork"],
                          ["Step-free", "Review"], ["Save privately", "Forget my inputs"]],
            "resize_keyboard": True}


def is_member(tg, group_id, user_id) -> bool:
    member = tg.call("getChatMember", chat_id=group_id, user_id=user_id) or {}
    return member.get("status") in {"creator", "administrator", "member"} or (
        member.get("status") == "restricted" and member.get("is_member") is True)


def preview(value: dict) -> str:
    lines = ["<b>Your private draft</b>"]
    if value.get("budget") is not None:
        lines.append(f"Maximum total per person: HK${value['budget']:g}")
    if value.get("start") is not None:
        lines.append(f"Start time: {negotiation.clock(value['start'])}–{negotiation.clock(value['end'])}")
    lines.extend(esc(store.REQUIREMENTS[k]) for k in value.get("requirements", []))
    if len(lines) == 1:
        lines.append("No requirements yet.")
    lines.append("\nTap <b>Save privately</b> to use this draft. Your name and messages stay out of the group. "
                 "The call operator and voice provider can see the combined requirements, and the agent may discuss them with the venue without your name. "
                 "This is not end-to-end encrypted; others may infer a requirement from the resulting plan. Inputs expire after 24 hours.")
    return "\n".join(lines)


def handle(tg, message: dict) -> None:
    user_id = (message.get("from") or {}).get("id")
    dm_id = (message.get("chat") or {}).get("id")
    if not isinstance(user_id, int) or dm_id != user_id:
        return
    text = (message.get("text") or "").strip()
    if text.startswith("/start "):
        token = text.split(maxsplit=1)[1].removeprefix("private_")
        plan = store.plan(token)
        if not plan or not is_member(tg, plan["chat_id"], user_id):
            tg.send(dm_id, "That link expired or I cannot verify your membership. Ask the group for a fresh /private link; the bot may need group admin rights to check membership.")
            return
        store.join(user_id, token)
        tg.send(dm_id, f"<b>Just between you and Convene</b>\nPlan: {esc(plan['title'])}\n\n"
                "Tell me a limit using <code>budget 150</code> or <code>time 19:00-20:00</code>, "
                "or tap a requirement below. These apply to this outing only. Review and save before they take effect.", reply_markup=KEYBOARD)
        return
    plan = store.session(user_id)
    if not plan or not is_member(tg, plan["chat_id"], user_id):
        tg.send(dm_id, "Open the private link from your group first. Send /private in that group to get one.")
        return
    token = plan["token"]
    low = text.lower().lstrip("/")
    aliases = {"vegetarian": "vegetarian", "vegan": "vegan", "no pork": "no_pork", "step-free": "step_free"}
    try:
        if low in ("forget", "forget my inputs"):
            store.forget(token, user_id)
            tg.send(dm_id, "Your saved inputs and draft for this plan were deleted. Previous call records are not erased. Existing approvals must be refreshed.", reply_markup=KEYBOARD)
            return
        if low in ("save", "save privately"):
            if not store.draft(token, user_id):
                raise ValueError("Add a requirement first, or use Forget my inputs to remove saved ones.")
            store.save(token, user_id)
            tg.send(dm_id, "<b>Saved privately.</b> Your limits will be checked before accepting a venue offer. "
                    "No message was posted to the group. If a call was already approved, it needs a fresh /close and approval.", reply_markup=KEYBOARD)
            return
        if low == "budget":
            tg.send(dm_id, "Send <code>budget 150</code> for a maximum of HK$150 per person including mandatory charges.")
            return
        if low in ("time", "time window"):
            tg.send(dm_id, "Send <code>time 19:00-20:00</code> for an acceptable start between 7 and 8pm on the requested day.")
            return
        if low in aliases:
            value = store.draft(token, user_id)
            chosen = set(value.get("requirements", []))
            key = aliases[low]
            chosen.symmetric_difference_update({key})
            store.edit(token, user_id, {"requirements": sorted(chosen)})
        elif re.fullmatch(r"budget\s+\d+(?:\.\d{1,2})?", low):
            store.edit(token, user_id, {"budget": negotiation.money(low.split()[1])})
        elif low.startswith("time "):
            start, end = negotiation.window(low[5:])
            store.edit(token, user_id, {"start": start, "end": end})
        elif low not in ("review", "status"):
            raise ValueError("Use budget 150, time 19:00-20:00, or the buttons. Free-text explanations and voice notes are not stored or interpreted in this version.")
        tg.send(dm_id, f"Plan: {esc(plan['title'])}\n" + preview(store.draft(token, user_id)), reply_markup=KEYBOARD)
    except ValueError as error:
        tg.send(dm_id, esc(str(error)))
