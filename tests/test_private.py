"""Drive the Telegram DM path and prove private data never enters group history."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness import Suite
from test_bot import FakeTelegram
import bot
import private_inputs as store


def dm(text, user_id=7):
    return {"chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "first_name": "Alex"}, "text": text}


def run():
    s = Suite("private", expect_at_least=24)
    with TemporaryDirectory() as directory:
        with patch.object(store, "DB_PATH", Path(directory) / "private.db"), patch.object(bot, "STATE", {}), patch.object(bot, "PEOPLE", {}), patch.object(bot, "STATE_PATH", Path(directory) / "chat.json"):
            token = store.create(-100, "Friday dinner")
            other = store.create(-200, "Saturday lunch")
            tg = FakeTelegram({"getChatMember": {"status": "member"}})
            bot.handle_message(tg, dm("/start private_" + token))
            s.eq("DM deep link chooses the correct group", store.session(7)["chat_id"], -100)
            bot.handle_message(tg, dm("budget 150"))
            s.eq("draft does not silently change negotiation", store.snapshot(token)["inputs"], [])
            bot.handle_message(tg, dm("time 19:00-20:00"))
            bot.handle_message(tg, dm("Vegetarian"))
            bot.handle_message(tg, dm("Save privately"))
            snapshot = store.snapshot(token)
            s.eq("save records the explicit budget", snapshot["inputs"][0]["budget"], 150)
            s.eq("save records time window", snapshot["inputs"][0]["start"], 1140)
            s.eq("save records requirement", snapshot["inputs"][0]["requirements"], ["vegetarian"])
            s.eq("private updates do not create shared history", bot.STATE, {})
            s.eq("private updates do not create people memory", bot.PEOPLE, {})
            s.check("all outgoing messages stayed in the DM", all(p["chat_id"] == 7 for method, p in tg.calls if method == "sendMessage"))
            s.check("booking snapshot contains no identity", "Alex" not in json.dumps(snapshot) and "user_id" not in json.dumps(snapshot))
            s.eq("another group sees no saved inputs", store.snapshot(other)["inputs"], [])
            bot.handle_message(tg, dm("budget 999", 8))
            s.eq("another user cannot write through someone else's session", len(store.snapshot(token)["inputs"]), 1)
            denied = FakeTelegram({"getChatMember": {"status": "left"}})
            bot.handle_message(denied, dm("/start private_" + token, 9))
            s.eq("forwarded links cannot admit outsiders", store.session(9), None)
            unavailable = FakeTelegram()
            bot.handle_message(unavailable, dm("/start private_" + token, 10))
            s.eq("unverifiable membership fails closed", store.session(10), None)
            bot.handle_message(tg, dm("/start private_" + other))
            bot.handle_message(tg, dm("Review"))
            s.eq("switching groups starts with that group's draft", store.draft(other, 7), {})
            s.check("DM identifies the active plan", "Saturday lunch" in tg.sent_text())
            bot.handle_message(tg, dm("/start private_" + token))
            before = store.snapshot(token)["revision"]
            bot.handle_message(tg, dm("Forget my inputs"))
            s.eq("forget removes the saved data", store.snapshot(token)["inputs"], [])
            s.eq("forget removes draft too", store.draft(token, 7), {})
            s.check("forget invalidates existing approvals", store.snapshot(token)["revision"] > before)
            bot.handle_message(tg, dm("my personal explanation must not be kept"))
            s.eq("unsupported free text is never persisted", store.draft(token, 7), {})
            backlog = FakeTelegram({"getUpdates": [[{"update_id": 1, "message": dm("secret budget 85")}], []]})
            bot.drain_backlog(backlog)
            s.eq("queued DMs do not enter shared history on restart", bot.STATE, {})
            group = {"chat": {"id": -100, "type": "group", "title": "Dinner"}, "from": {"id": 7}, "text": "/private"}
            public = FakeTelegram({"getMe": {"username": "convene_bot"}})
            bot.handle_message(public, group)
            s.check("group gets a Telegram DM link", "https://t.me/convene_bot?start=private_" in str(public.calls))
            s.check("group invitation includes no participant identity", "Alex" not in public.sent_text())
            bot.handle_message(public, {**group, "text": "/negotiate 19:00-20:00 budget 200"})
            s.eq("group command stores explicit public limits", bot.STATE[-100].negotiation_limits["budget"], 200)
            s.check("group sees terms before approval", "HK$200" in public.sent_text())
            # A saved private limit can change after the group saw an approval
            # card. The old button must never arm a call under stale terms.
            booking = bot.state_for(-100)
            booking.picks = [{"name": "Consenting venue", "phone": "+85228033960", "area": "Central", "why": ""}]
            booking.poll_options = ["Consenting venue", bot.NONE_OPTION]
            booking.poll_message_id = 99
            booking.private_plan = other
            booking.constraints = {"party_size": 6, "when_text": "Friday 7pm", "hard": []}
            booking.negotiation_limits = {"start": 1140, "end": 1200, "budget": 200}
            with patch.object(bot, "PENDING_PATH", Path(directory) / "pending.json"), patch.dict(os.environ, {"CONSENTED_NUMBERS": "+85228033960"}):
                proposal = FakeTelegram({"stopPoll": {"options": [
                    {"voter_count": 3}, {"voter_count": 0}]}})
                bot.handle_close(proposal, -100, booking)
                s.check("fresh proposal has an approval card", bool(booking.approval_token))
                old_token = booking.approval_token
                store.edit(other, 7, {"budget": 100})
                store.save(other, 7)
                clicked = FakeTelegram()
                bot.handle_callback(clicked, {"id": "old", "data": "ok:" + old_token,
                                              "from": {"first_name": "Alex"},
                                              "message": {"chat": {"id": -100}}})
                s.check("old card reports changed private inputs", "Private requirements changed" in str(clicked.calls))
                s.check("old card did not queue a call", not bot.PENDING_PATH.exists())
            with store.connect() as db:
                db.execute("UPDATE plans SET expires=0 WHERE token=?", (token,))
            s.eq("expired sessions cannot receive new private inputs", store.session(7), None)
            s.raises("expired private limits cannot silently vanish from approval", ValueError, store.snapshot, token)
    return s
