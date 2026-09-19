#!/usr/bin/env python3
"""Local, credential-free rehearsal of the real private-input and offer rules.

Run `python demo.py` and open http://127.0.0.1:8090/. Nothing here calls a
venue, Telegram, ElevenLabs, or an LLM. The staff dialogue is scripted; the
private save, policy intersection, and offer evaluation use production modules.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import negotiation
import private_inputs

HERE = Path(__file__).resolve().parent
HTML = HERE / "demo" / "index.html"
PRIVATE_USER = 101
GROUP_ID = -100
PUBLIC = negotiation.parse_command("19:00-20:00 budget 200")
FIRST_OFFER = {"time": "20:30", "party_size": 6, "price_per_person": 180,
               "deposit_total": 0, "currency": "HKD", "same_day": True,
               "requirements_met": True}
SECOND_OFFER = {**FIRST_OFFER, "time": "19:30", "price_per_person": 145}

_data_dir = tempfile.TemporaryDirectory(prefix="raincheck-rehearsal-")
private_inputs.DB_PATH = Path(_data_dir.name) / "private.sqlite3"
_lock = threading.RLock()
_state = {"token": private_inputs.create(GROUP_ID, "Friday dinner"),
          "offers": [], "confirmed": False}


def snapshot() -> dict:
    private = private_inputs.snapshot(_state["token"])
    policy = negotiation.build("Friday 7:30pm", 6, PUBLIC, private, [])
    saved = private["inputs"]
    return {
        "public": {"time": "19:00–20:00", "budget": 200, "party_size": 6,
                   "private_count": len(saved), "venue": "The Friday Table"},
        "private_saved": saved[0] if saved else None,
        "policy": policy,
        "offers": list(_state["offers"]),
        "confirmed": _state["confirmed"],
    }


def save_private(body: dict) -> dict:
    try:
        budget = negotiation.money(body.get("budget"))
    except ValueError as error:
        raise ValueError("Enter a private HKD budget between 0 and 100,000.") from error
    if not isinstance(body.get("vegetarian"), bool):
        raise ValueError("Choose whether vegetarian food is required.")
    value = {"budget": budget,
             "requirements": ["vegetarian"] if body["vegetarian"] else []}
    trial = negotiation.build("Friday 7:30pm", 6, PUBLIC, {"inputs": [value]}, [])
    if not trial:
        raise ValueError("Those limits cannot fit the plan.")
    private_inputs.edit(_state["token"], PRIVATE_USER, value)
    private_inputs.save(_state["token"], PRIVATE_USER)
    _state["offers"] = []
    _state["confirmed"] = False
    return snapshot()


def check_offer(body: dict) -> dict:
    current = snapshot()
    if _state["confirmed"]:
        raise ValueError("This rehearsal is complete. Reset to try another offer.")
    preset = body.get("preset")
    if preset == "first":
        offer = dict(FIRST_OFFER)
        staff = "We can do 8:30, but it would be HK$180 per person."
    elif preset == "second":
        offer = dict(SECOND_OFFER)
        staff = "We could do 7:30 instead. HK$145 all in, no deposit."
    elif preset == "custom":
        offer = {"time": body.get("time"), "party_size": 6,
                 "price_per_person": body.get("price"),
                 "deposit_total": body.get("deposit"), "currency": "HKD",
                 "same_day": True, "requirements_met": body.get("requirements_met")}
        staff = "Here are the terms you entered for this role-play."
    else:
        raise ValueError("Choose one of the rehearsal offers or enter custom terms.")
    result = negotiation.evaluate(current["policy"], offer)
    _state["offers"].append({"offer": offer, "result": result, "staff": staff})
    return snapshot()


def confirm() -> dict:
    events = _state["offers"]
    if not events or events[-1]["result"]["action"] != "accept":
        raise ValueError("An offer must pass every check before staff can confirm it.")
    _state["confirmed"] = True
    return snapshot()


def reset() -> dict:
    private_inputs.retire(_state["token"])
    _state.update(token=private_inputs.create(GROUP_ID, "Friday dinner"), offers=[], confirmed=False)
    return snapshot()


class Handler(BaseHTTPRequestHandler):
    def _json(self, value: dict, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/":
            raw = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/state":
            with _lock:
                self._json(snapshot())
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path not in ("/api/private", "/api/offer", "/api/confirm", "/api/reset"):
            self._json({"error": "Not found"}, 404)
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json({"error": "JSON required"}, 415)
            return
        origin = self.headers.get("Origin")
        allowed = {"http://127.0.0.1:" + str(self.server.server_port),
                   "http://localhost:" + str(self.server.server_port)}
        if origin and origin not in allowed:
            self._json({"error": "Other origins are not allowed"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                raise ValueError("Request is too large.")
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError("JSON object required.")
            with _lock:
                result = {
                    "/api/private": lambda: save_private(body),
                    "/api/offer": lambda: check_offer(body),
                    "/api/confirm": confirm,
                    "/api/reset": reset,
                }[self.path]()
            self._json(result)
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="RainCheck's interactive local rehearsal")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"RainCheck rehearsal: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _data_dir.cleanup()


if __name__ == "__main__":
    main()
