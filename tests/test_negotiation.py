"""Exercise delegation decisions and real bridge dispatch without any network."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from harness import Suite
import negotiation as n
import private_inputs
from bridge import server
import setup_agent


def dispatch(path, body):
    handler = object.__new__(server.Handler)
    handler.path = path
    raw = json.dumps(body).encode()
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    responses = []
    handler._json = lambda data, code=200: responses.append((code, data))
    handler.do_POST()
    return responses[-1]


def run():
    s = Suite("negotiation", expect_at_least=40)
    policy = n.build("Friday 7pm", 6, n.parse_command("19:00-20:00 budget 200"),
                     {"inputs": [{"budget": 150, "start": 1170, "end": 1260, "requirements": ["vegetarian"]}]}, [])
    s.eq("private budget narrows public authority", policy["budget"], 150)
    s.eq("private and public time windows intersect", (policy["start"], policy["end"]), (1170, 1200))
    s.eq("private requirement becomes a venue question without identity", policy["requirements"], ["Vegetarian meal available"])
    s.raises("disjoint time windows stop the call", ValueError, n.build, "7pm", 6, {}, {"inputs": [{"start": 1200, "end": 1260}]}, [])
    for value in ("25:00", "7pm", "19:99", None):
        s.raises("invalid tool clock rejected: " + str(value), ValueError, n.minutes, value)
    s.raises("overnight windows need an explicit future design", ValueError, n.window, "23:00-01:00")
    for value in (True, float("nan"), float("inf"), -1):
        s.raises("invalid amount cannot grant authority: " + str(value), ValueError, n.money, value)
    s.raises("ambiguous public delegation is not guessed", ValueError, n.parse_command, "sometime tonight cheap")
    offer = {"time": "19:45", "party_size": 6, "price_per_person": 150, "deposit_total": 0,
             "currency": "HKD", "same_day": True, "requirements_met": True}
    s.eq("boundary price and all requirements permit a booking request", n.evaluate(policy, offer)["action"], "accept")
    for key, value in (("time", "20:30"), ("price_per_person", 151),
                       ("party_size", 5), ("currency", "USD"), ("same_day", False), ("requirements_met", False)):
        s.eq("counteroffer needed for " + key, n.evaluate(policy, {**offer, key: value})["action"], "counter")
    for key in offer:
        partial = {k: v for k, v in offer.items() if k != key}
        expected = "accept" if key == "price_per_person" else "clarify"
        s.eq("missing " + key + " is handled", n.evaluate(policy, partial)["action"], expected)
    s.eq("a string yes is not a verified requirement", n.evaluate(policy, {**offer, "requirements_met": "yes"})["action"], "clarify")
    s.eq("without a budget the agent can still book", n.evaluate({**policy, "budget": None}, offer)["action"], "accept")
    s.eq("a stated deposit is accepted and recorded", n.evaluate(policy, {**offer, "deposit_total": 80})["action"], "accept")
    s.check("tool waits for a real response", n.client_tool()["expects_response"])
    first, prompt, schema = setup_agent.parse_doc()
    s.eq("voice prompt and page variables match", setup_agent.variables_in(prompt, first), setup_agent.variables_the_page_sends())
    s.check("provisioning includes final price evidence", {"price_per_person", "deposit_total", "requirements_met"} <= schema.keys())
    with TemporaryDirectory() as directory:
        with patch.object(server, "PENDING_PATH", Path(directory) / "pending.json"), patch.object(private_inputs, "DB_PATH", Path(directory) / "private.db"):
            token = private_inputs.create(-100, "Dinner")
            pending = {"id": "call-a", "status": "dialing", "private_plan": token, "private_revision": 0,
                       "private_mode": True, "negotiation": policy, "negotiation_events": [],
                       "restaurant_name": "Example", "when_text": "Friday 7pm", "party_size": 6,
                       "live_turns": [
                           {"source": "ai", "message": "The limit is HK$150 per person with no deposit."},
                           {"source": "user", "message": "Okay."},
                       ]}
            server.write_pending(pending)
            code, result = dispatch("/negotiate", {"booking_id": "wrong", "offer": offer})
            s.eq("stale call ID rejected at endpoint", code, 409)
            s.eq("stale request does not append an event", server.read_pending()["negotiation_events"], [])
            code, result = dispatch("/negotiate", {"booking_id": "call-a", "offer": offer})
            s.eq("agent repeating the approved ceiling is not venue evidence", result["action"], "clarify")
            s.eq("unsupported price is removed", server.read_pending()["negotiation_events"][-1]["offer"].get("price_per_person"), None)
            pending["live_turns"].append({"source": "user", "message": "Yes, HK$150 per person, no deposit, for six at 7:45 and a vegetarian meal is available."})
            pending["negotiation_events"] = []
            server.write_pending(pending)
            code, result = dispatch("/negotiate", {"booking_id": "call-a", "offer": offer})
            s.eq("live tool request succeeds", code, 200)
            s.eq("endpoint returns actual evaluator result", result["action"], "accept")
            pending = server.read_pending()
            s.eq("the live UI gets a persisted offer event", len(pending["negotiation_events"]), 1)
            changed = {**pending, "live_turns": pending["live_turns"] + [
                {"source": "user", "message": "Actually, HK$180 per person and a HK$20 deposit."}
            ]}
            s.eq("earlier price cannot justify a changed offer",
                 server.ground_offer(changed, offer).get("price_per_person"), None)
            s.eq("earlier no-deposit claim cannot survive a later deposit",
                 server.ground_offer(changed, offer).get("deposit_total"), None)
            collected = {"status": "confirmed", "confirmed_time": "7:45pm", "confirmed_party_size": 6,
                         **{k: v for k, v in offer.items() if k not in ("time", "party_size")}}
            s.eq("checked matching final terms may confirm", server.validate_outcome(pending, collected)["status"], "confirmed")
            s.eq("later price change cannot inherit previous approval", server.validate_outcome(pending, {**collected, "price_per_person": 180})["status"], "needs_approval")
            s.eq("missing final menu price is accepted", server.validate_outcome(pending, {**collected, "price_per_person": None})["status"], "confirmed")
            s.eq("unchecked voice confirmation is not accepted", server.validate_outcome({**pending, "negotiation_events": []}, collected)["status"], "needs_approval")
            s.eq("amending terms cannot bypass group approval", dispatch("/amend", {"party_size": 4})[0], 409)
            private_inputs.edit(token, 7, {"budget": 100})
            private_inputs.save(token, 7)
            s.eq("changed private input stops the live agent", dispatch("/negotiate", {"booking_id": "call-a", "offer": offer})[1]["action"], "stop")
            s.eq("changed private input also blocks final confirmation", server.validate_outcome(pending, collected)["status"], "needs_approval")
            private_text = "Sensitive private budget explanation"
            live = server.render_live(pending, [{"source": "user", "message": private_text}])
            s.check("private-mode group transcript remains visible", private_text in live)
            report = server.format_outcome(pending, {"collected": {**collected, "staff_notes": private_text}})
            s.check("private staff notes do not leak through result or calendar", private_text not in report and "Sensitive+private" not in report)
            server.write_pending({**pending, "status": "done"})
            s.eq("finished calls cannot accept another tool request", dispatch("/negotiate", {"booking_id": "call-a", "offer": offer})[0], 409)
    return s
