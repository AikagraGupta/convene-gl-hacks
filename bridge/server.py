#!/usr/bin/env python3
"""bridge/server.py — the localhost glue between the group chat and the voice agent.

Everything the voice layer touches lives on 127.0.0.1. There is no tunnel, no
ngrok, no public webhook URL. That is a deliberate architectural choice and not
a shortcut: a cloudflared tunnel that dies at 16:04 is the single most common
way a hackathon voice demo fails, and we removed the possibility rather than
hoping.

Endpoints
  GET  /health   -> liveness, so you can tell "server down" from "agent down"
  GET  /config   -> agent id + booking identity, so no key is baked into HTML
  GET  /pending  -> the approved booking written by bot.py, or {}
  POST /dial     -> flip dial:true; the call page is polling and auto-starts
  POST /outcome  -> transcript + the agent's structured result, posted to chat
  GET  /         -> call_page.html

The last one matters more than it looks. getUserMedia is refused on file://
so the page MUST arrive over http://localhost. Serving it from the
same origin as the API also means the browser never needs a CORS preflight for
the calls that matter.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import invite  # noqa: E402
import pipeline  # noqa: E402
import negotiation  # noqa: E402
import private_inputs  # noqa: E402
import vapi_calls  # noqa: E402
from envlite import env, env_flag, load_env, warn_if_tls_broken  # noqa: E402
from tgtext import esc  # noqa: E402

PENDING_PATH = HERE / "pending_call.json"
CALL_LOG_DIR = ROOT / "call_log"
PORT = 8080

_lock = threading.Lock()
_vapi_dispatch_lock = threading.Lock()

# Telegram rate-limits edits to a message, and a voice call produces a turn
# every couple of seconds. Throttling here rather than dropping turns: each
# edit re-renders the WHOLE transcript, so a skipped edit loses nothing except
# a moment of latency.
_LIVE_EDIT_MIN_INTERVAL = 1.3
_last_live_edit = 0.0


# --------------------------------------------------------------------------
# pending_call.json — the one piece of shared state, deliberately on disk
# --------------------------------------------------------------------------
# A file rather than a socket or a queue because it is inspectable. When
# something is wrong at 15:50 you can `cat` it, and you can hand-edit it to
# re-run the call leg without touching Telegram at all.

def read_pending() -> dict:
    with _lock:
        if not PENDING_PATH.exists():
            return {}
        try:
            return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def write_pending(data: dict) -> None:
    with _lock:
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PENDING_PATH)  # atomic: the page never reads a half-written file


def patch_pending(**fields) -> dict:
    current = read_pending()
    current.update(fields)
    write_pending(current)
    return current


# --------------------------------------------------------------------------
# Telegram — one function, so the call result lands back where the argument was
# --------------------------------------------------------------------------

def send_telegram(chat_id, text: str) -> bool:
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id:
        print(f"[bridge] no token/chat_id, would have sent:\n{text}")
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=15
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"[bridge] telegram send failed: {exc}")
        return False


def transcript_messages(pending: dict, turns: list[dict]) -> list[str]:
    """Make a complete, HTML-safe transcript in Telegram-sized messages."""
    name = str(pending.get("restaurant_display") or pending.get("restaurant_name") or "the venue")[:100]
    title = f"<b>Call transcript — {esc(name)}</b>"
    # Leave space for the title and part numbers. Telegram counts UTF-16 units.
    body_limit = 2800
    bodies: list[str] = []
    body = ""

    def units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    def add(line: str) -> None:
        nonlocal body
        joined = f"{body}\n\n{line}" if body else line
        if body and units(joined) > body_limit:
            bodies.append(body)
            body = line
        else:
            body = joined

    for turn in turns:
        said = str(turn.get("message") or "").strip()
        if not said:
            continue
        speaker = "🏪 Venue: " if turn.get("source") == "user" else "🤖 RainCheck: "
        line = speaker
        for char in said:
            encoded = esc(char)
            if units(line + encoded) > body_limit:
                add(line)
                line = speaker + "(continued) "
            line += encoded
        add(line)
    if body:
        bodies.append(body)
    if not bodies:
        bodies = ["<i>No speech transcript was returned by the voice provider.</i>"]
    total = len(bodies)
    return [f"{title}{f' ({index}/{total})' if total > 1 else ''}\n{part}"
            for index, part in enumerate(bodies, 1)]


def post_transcript(pending: dict, turns: list[dict]) -> bool:
    """Every call gets a permanent Telegram transcript, whatever the outcome."""
    delivered = True
    for message in transcript_messages(pending, turns):
        delivered = send_telegram(pending.get("chat_id"), message) and delivered
    return delivered


# --------------------------------------------------------------------------
# Turning a call into a state transition
# --------------------------------------------------------------------------
# The ElevenLabs data-collection schema is evaluated server-side AFTER the call
# finishes, and is NOT delivered to the browser session. So the page can report
# what was said but not what it meant, and a `collected` field populated from
# the browser would be permanently empty.
#
# Two sources, best first:
#   1. the agent's own schema result, over the API (saw the audio)
#   2. Gemini reading the transcript (sees only text — labelled as such)

def _fetch_agent_analysis(conversation_id: str, api_key: str) -> dict:
    """Ask ElevenLabs for the agent's own data-collection result.

    The analysis is computed asynchronously once the call ends, so a request
    made the instant the page hangs up usually arrives before the result does.
    Three tries, four seconds apart, then give up quietly and let the
    transcript path handle it.
    """
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    for attempt in range(3):
        if attempt:
            time.sleep(4)
        try:
            req = urllib.request.Request(url, headers={"xi-api-key": api_key})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                TimeoutError, json.JSONDecodeError) as exc:
            print(f"[bridge] elevenlabs analysis attempt {attempt + 1}: {type(exc).__name__}")
            continue

        results = ((data.get("analysis") or {}).get("data_collection_results") or {})
        if not results:
            print(f"[bridge] analysis not ready yet (attempt {attempt + 1})")
            continue

        # Each entry is {"value": ..., "rationale": ...}; flatten to values.
        flat = {}
        for key, item in results.items():
            flat[key] = item.get("value") if isinstance(item, dict) else item
        if any(v not in (None, "") for v in flat.values()):
            return flat
    return {}


def collect_outcome(body: dict) -> tuple[dict, str]:
    """Return (structured_fields, source_label)."""
    conversation_id = body.get("conversation_id")
    api_key = env("ELEVENLABS_API_KEY")

    if conversation_id and api_key:
        agent_result = _fetch_agent_analysis(conversation_id, api_key)
        if agent_result:
            print("[bridge] outcome from the agent's own data-collection schema")
            return agent_result, "agent schema"

    derived = pipeline.extract_call_outcome(body.get("transcript") or [])
    if derived:
        print("[bridge] outcome derived from the transcript")
        return derived, "transcript (derived)"

    return {}, "none"


def edit_telegram(chat_id, message_id: int, text: str) -> bool:
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id or not message_id:
        return False
    payload = urllib.parse.urlencode({
        "chat_id": str(chat_id), "message_id": str(message_id),
        "text": text[:4000], "parse_mode": "HTML",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=10
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # "message is not modified" and rate limits both land here and are both
        # harmless: the next turn re-sends the full transcript anyway.
        print(f"[bridge] live edit skipped: {type(exc).__name__}")
        return False


def send_telegram_returning_id(chat_id, text: str):
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return None
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text[:4000], "parse_mode": "HTML"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=15
        ) as resp:
            return (json.loads(resp.read().decode("utf-8")).get("result") or {}).get("message_id")
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[bridge] could not open the live transcript message: {exc}")
        return None


def render_live(pending: dict, turns: list[dict], finished: bool = False) -> str:
    """The message that updates in the chat while the call is happening.

    Showing the conversation as it happens is not decoration. The group
    delegated a phone call to an AI; watching it unfold is what makes that
    delegation something they can supervise rather than just trust, and it is
    the moment a human can say "no, that's wrong" while it still matters.
    """
    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the restaurant"
    if finished:
        return f"✅ <b>Call finished — {esc(name)}</b>\nFull transcript posted separately below."
    head = f"\U0001f4de <b>On the phone with {esc(name)}\u2026</b>"

    # Built by concatenation rather than one big f-string: an escape inside an
    # f-string expression is a syntax error before Python 3.12, and this has to
    # run on whatever python3 the laptop happens to have.
    dot = " \u00b7 "
    subtitle = str(pending.get("dial_number") or "")
    if pending.get("party_size"):
        subtitle += dot + "party of " + str(pending["party_size"])
    if pending.get("when_text"):
        subtitle += dot + str(pending["when_text"])

    lines = [head, "<i>" + esc(subtitle) + "</i>", ""]

    if not turns:
        lines.append("<i>connecting\u2026</i>")
    for turn in turns[-24:]:
        said = str(turn.get("message", "")).strip()
        if not said:
            continue
        if turn.get("source") == "user":
            lines.append(f"\U0001f3ea <b>{esc(said)}</b>")      # the restaurant
        else:
            lines.append(f"\U0001f916 {esc(said)}")             # our agent

    if not finished:
        lines.append("\n<i>live \u2014 this message updates as they talk</i>")
    return "\n".join(lines)


def amend_when_text(value: str) -> tuple[bool, str]:
    """Is this an amended time the agent can actually ask a restaurant for?

    Extracted from the /amend handler so it can be tested directly. The bug it
    exists to prevent was mine: `not pipeline.time_is_bookable(value)` looks
    right and is always False, because that function returns a (ok, reason)
    TUPLE and a non-empty tuple is truthy. The gate silently never fired, and
    the source-level test that grepped for the call passed anyway. Only driving
    the real endpoint over HTTP caught it.
    """
    ok, why = pipeline.time_is_bookable(value)
    if ok:
        return True, ""
    return False, f"when_text: {why}"


def format_outcome(pending: dict, body: dict) -> str:
    """Turn the call into a chat message a human can act on.

    The structured fields come from the agent's data-collection schema. Without
    that schema you have a phone call; with it you have a state transition, and
    the group chat can close its own loop.
    """
    collected = body.get("collected") or {}
    status = str(collected.get("status") or body.get("status") or "unknown").lower()
    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the restaurant"

    head = {
        "confirmed": f"✅ <b>Booked — {esc(name)}</b>",
        "booked": f"✅ <b>Booked — {esc(name)}</b>",
        "waitlist": f"⏳ <b>Waitlist — {esc(name)}</b>",
        "declined": f"❌ <b>No table — {esc(name)}</b>",
        "full": f"❌ <b>No table — {esc(name)}</b>",
        "no_answer": f"☎️ <b>No answer — {esc(name)}</b>",
    }.get(status, f"ℹ️ <b>Call finished — {esc(name)}</b>")

    lines = [head]
    if collected.get("confirmed_time"):
        lines.append(f"Time: {esc(collected['confirmed_time'])}")
    if collected.get("confirmed_party_size"):
        lines.append(f"Party: {esc(collected['confirmed_party_size'])}")
    if collected.get("wait_estimate_minutes"):
        lines.append(f"Wait: ~{esc(collected['wait_estimate_minutes'])} min")
    if collected.get("booking_name"):
        lines.append(f"Under: {esc(collected['booking_name'])}")
    if collected.get("deposit_total") is not None:
        try:
            deposit = negotiation.money(collected.get("deposit_total"))
            lines.append("Deposit: none" if deposit == 0 else f"Deposit: HK${deposit:g}")
        except ValueError:
            pass
    if collected.get("fps_number"):
        lines.append(f"FPS: <code>{esc(collected['fps_number'])}</code> — transfer the deposit to this number")
    if collected.get("staff_notes") and not pending.get("private_mode"):
        lines.append(f"Note: {esc(collected['staff_notes'])}")
    if status == "needs_approval":
        lines.append("<b>The booking could not be completed.</b> The venue's terms changed outside the approved request.")

    if status in ("confirmed", "booked"):
        # One link, everybody's own calendar. Telegram does not hand out member
        # email addresses -- correctly -- so there is nobody to send an invite
        # TO. A click-to-add link needs no addresses, no OAuth and no account,
        # and works for people who are not on Google Calendar at all.
        # Calendar descriptions must not smuggle private staff notes into the group.
        calendar_result = {**collected, "staff_notes": ""} if pending.get("private_mode") else collected
        link = invite.calendar_url(pending, calendar_result)
        if link:
            lines.append(f"\n\U0001f4c5 <a href=\"{esc(link)}\">Add to your calendar</a>"
                         " \u2014 everyone tap it once.")
        else:
            # No hour at all means no calendar entry. A dinner filed at a
            # guessed hour is wrong in six pockets and nobody notices until
            # they are late. A bare time like "8pm" is NOT that case and does
            # get a link -- see invite.event_time.
            lines.append("\n<i>No calendar link: no exact hour was ever said, "
                         "and a guessed one would be worse than none.</i>")

    if pending.get("demo_override"):
        lines.append(
            "\n<i>Safety rail: DEMO_PHONE was set, so this call went to a number "
            "we control, not to the restaurant.</i>"
        )
    return "\n".join(lines)


def check_offer(pending: dict, offer: dict) -> dict:
    if not private_inputs.current(pending):
        return {"action": "stop", "checks": [], "instruction": "Requirements changed after approval. Do not commit. End the call and request fresh group approval."}
    return negotiation.evaluate(pending["negotiation"], offer)


def ground_offer(pending: dict, offer: dict) -> dict:
    """Require the venue, not the agent, to have actually stated money terms.

    The voice model can copy the approved ceiling into its tool arguments as
    though staff quoted it. Missing evidence becomes an unknown field, so the
    evaluator asks for clarification instead of approving an invented offer.
    """
    venue_turns = [
        str(turn.get("message") or "") for turn in pending.get("live_turns") or []
        if turn.get("source") == "user"
    ]
    grounded = dict(offer)
    price = offer.get("price_per_person")
    try:
        price = negotiation.money(price)
    except ValueError:
        price = None
    price_phrases = []
    price_quote = ""
    for turn in venue_turns:
        found = re.findall(
            r"(?:\b(?:price|cost|charge|all[- ]in)\b.{0,24}?|\b)(?:HK\$|HKD\s*)?"
            r"(\d{1,5}(?:\.\d{1,2})?)\s*(?:per\s+(?:person|head|pax)|each)\b"
            r"|\b(?:price|cost|charge|all[- ]in)\b.{0,24}?(?:HK\$|HKD\s*)?"
            r"(\d{1,5}(?:\.\d{1,2})?)\b",
            turn, flags=re.I,
        )
        if found:
            price_phrases, price_quote = found, turn
    stated_prices = {float(a or b) for a, b in price_phrases}
    if price is None or price not in stated_prices:
        grounded.pop("price_per_person", None)
    if not re.search(r"\bHKD\b|HK\$|Hong Kong dollars?", price_quote, re.I):
        grounded.pop("currency", None)

    deposit = offer.get("deposit_total")
    try:
        deposit = negotiation.money(deposit)
    except ValueError:
        deposit = None
    deposit_quote = next((turn for turn in reversed(venue_turns)
                          if re.search(r"\bdeposit\b", turn, re.I)), "")
    if deposit == 0:
        no_deposit = re.search(
            r"\b(?:no|zero)\s+deposit\b|\bdeposit\s+(?:is\s+)?(?:zero|none|not required|not needed|HK\$?0|0)\b"
            r"|\b(?:don't|do not)\s+(?:need|require)\s+(?:a\s+)?deposit\b",
            deposit_quote, re.I,
        )
        if not no_deposit:
            grounded.pop("deposit_total", None)
    elif deposit is not None:
        stated_deposits = re.findall(
            r"\bdeposit\b.{0,18}?(?:HK\$|HKD\s*)?(\d{1,5}(?:\.\d{1,2})?)\b"
            r"|\b(\d{1,5}(?:\.\d{1,2})?)\s*(?:HKD\s*)?deposit\b",
            deposit_quote, flags=re.I,
        )
        if deposit not in {float(a or b) for a, b in stated_deposits}:
            grounded.pop("deposit_total", None)
    return grounded


def validate_outcome(pending: dict, collected: dict) -> dict:
    """Never label an unchecked voice-model claim as an authorised booking."""
    if not pending.get("negotiation") or collected.get("status") not in ("confirmed", "booked"):
        return collected
    events = pending.get("negotiation_events") or []
    latest = events[-1] if events else {}
    offer = latest.get("offer") or {}
    parsed = invite._parse_clock(str(collected.get("confirmed_time") or ""))
    time_text = negotiation.clock(parsed[0] * 60 + parsed[1]) if parsed else None
    final_offer = {key: collected.get(key) for key in
                   ("deposit_total", "currency", "same_day", "requirements_met")}
    if collected.get("price_per_person") is not None:
        final_offer["price_per_person"] = collected.get("price_per_person")
    final_offer.update(time=time_text, party_size=collected.get("confirmed_party_size"))
    valid = (latest.get("evidence_checked") is True
             and latest.get("result", {}).get("action") == "accept"
             and private_inputs.current(pending)
             and negotiation.evaluate(pending["negotiation"], final_offer)["action"] == "accept"
             and all(final_offer[key] == offer.get(key) for key in final_offer))
    return collected if valid else {**collected, "status": "needs_approval"}


def archive_call(pending: dict, body: dict) -> Path:
    """Keep every call on disk. This is the backup-footage insurance policy."""
    CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CALL_LOG_DIR / f"call-{stamp}.json"
    path.write_text(
        json.dumps({"pending": pending, "outcome": body}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def vapi_offer_note(pending: dict, turns: list[dict]) -> str:
    """Explain a clear hold offer when it does not match the approval."""
    venue_speech = " ".join(
        str(turn.get("message") or "") for turn in turns
        if turn.get("source") == "user"
    )
    if not venue_speech:
        return ""
    # This is deliberately a narrow explanation helper, not a booking parser.
    # The approval gate remains the authority; this only makes a rejected offer
    # intelligible to the group.
    hold = re.search(
        r"\b(?:book|books|booking|reserve|reserved|reservation|hold|holds|held|keep|save)\b|留|预订|預訂",
        venue_speech, re.I,
    )
    if not hold:
        return ""
    offered_sizes = []
    for first, second in re.findall(
        r"\b(\d{1,2})\s*(?:people?|persons?|pax|seats?)\b|\b(\d{1,2})\s*[个位]",
        venue_speech, re.I,
    ):
        offered_sizes.append(int(first or second))
    requested = pending.get("party_size")
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = None
    offered = offered_sizes[-1] if offered_sizes else None
    if requested is not None and offered is not None and requested != offered:
        return (f"The venue offered to hold {offered} people, but the approved "
                f"request was for {requested}. This is not a valid reservation "
                "for the approved party.")
    return ("The venue said it could hold the requested table, but this inquiry "
            "did not independently verify a booking.")


def _venue_speech(turns: list[dict]) -> str:
    return " ".join(
        str(turn.get("message") or "") for turn in turns
        if turn.get("source") == "user"
    ).strip()


def vapi_booking_details(pending: dict, turns: list[dict]) -> dict:
    """Extract a booked result from explicit venue confirmation in a Vapi call."""
    speech = _venue_speech(turns)
    if not speech:
        return {"status": "no_answer"}

    booked = re.search(
        r"\b(?:booked|reserved|reservation\s+(?:is\s+)?confirmed|booking\s+(?:is\s+)?confirmed|"
        r"confirmed\s+(?:your|the)\s+(?:table|booking)|put\s+you\s+down|table\s+is\s+yours)\b",
        speech, re.I,
    )
    if not booked:
        return {"status": "declined" if re.search(
            r"\b(?:fully\s+booked|no\s+availability|no\s+table|no\s+seats?|only\s+(?:have\s+)?\d+\s+(?:seats?|people|pax)|sold\s+out|can't\s+accommodate|cannot\s+accommodate)\b",
            speech, re.I) else "unclear"}

    clock_text = None
    clock_pattern = r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b"
    for match in re.findall(clock_pattern, speech, re.I):
        if invite._parse_clock(match):
            clock_text = match

    sizes = []
    for first, second in re.findall(
        r"\b(\d{1,2})\s*(?:people?|persons?|pax|seats?)\b|\b(\d{1,2})\s*[个位]",
        speech, re.I,
    ):
        sizes.append(int(first or second))
    try:
        requested = int(pending.get("party_size"))
    except (TypeError, ValueError):
        requested = None

    deposit = None
    no_deposit = re.search(
        r"\b(?:no|zero|none|not required|not needed)\s+deposit\b|"
        r"\bdeposit\s+(?:is\s+)?(?:zero|none|not required|not needed|free)\b|"
        r"\b(?:don't|do not)\s+(?:need|require)\s+(?:a\s+)?deposit\b",
        speech, re.I,
    )
    if no_deposit:
        deposit = 0
    else:
        amount = None
        for sentence in re.findall(r"[^.!?]*(?:deposit|booking\s+fee|prepayment)[^.!?]*", speech, re.I):
            amount = re.search(
                r"\b(?:deposit|booking\s+fee|prepayment)\b[^\d]{0,24}(?:HK\$|HKD)?\s*"
                r"([\d,]+(?:\.\d{1,2})?)|(?:HK\$|HKD)\s*"
                r"([\d,]+(?:\.\d{1,2})?)[^\d]{0,12}\b(?:deposit|booking\s+fee|prepayment)\b",
                sentence, re.I,
            )
            if amount:
                break
        if amount:
            try:
                deposit = float((amount.group(1) or amount.group(2)).replace(",", ""))
            except ValueError:
                deposit = None

    fps_number = None
    fps = re.search(
        r"\bFPS(?:\s+(?:number|no\.?|account))?\b[^\d+]{0,24}"
        r"(?P<number>\+?\d(?:[\d\s-]{6,20}\d))",
        speech, re.I,
    )
    if fps:
        candidate = re.sub(r"[^\d+]", "", fps.group("number"))
        if len(candidate.lstrip("+")) >= 8:
            fps_number = candidate

    return {
        "status": "confirmed",
        "confirmed_time": clock_text or str(pending.get("when_text") or ""),
        "confirmed_party_size": sizes[-1] if sizes else requested,
        "booking_name": pending.get("booking_name") or "a guest",
        "deposit_total": deposit,
        "fps_number": fps_number,
        "same_day": True,
        "requirements_met": True if not (pending.get("negotiation") or {}).get("requirements") else None,
    }


def vapi_calendar_candidate(pending: dict, turns: list[dict]) -> dict | None:
    details = vapi_booking_details(pending, turns)
    return details if details.get("status") in ("confirmed", "booked") else None


def format_vapi_result(pending: dict, turns: list[dict]) -> str:
    """Report the booking result and calendar link from a completed Vapi call."""
    details = vapi_booking_details(pending, turns)
    if details.get("status") in ("confirmed", "booked"):
        return format_outcome(pending, {"collected": details})

    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the venue"
    lines = [f"❌ <b>Could not book — {esc(name)}</b>"]
    if details.get("status") == "no_answer":
        lines.append("No response from venue staff was recorded.")
    elif details.get("status") == "declined":
        lines.append("The venue did not have the requested table available.")
    else:
        lines.append("The venue did not explicitly confirm a reservation.")
    lines.append("The full transcript is posted below.")
    if pending.get("demo_override"):
        lines.append("<i>Demo call: the number that rang belongs to the team, not the listed venue.</i>")
    return "\n".join(lines)


def monitor_vapi_call(call_id: str) -> None:
    """Poll the call API: localhost cannot receive Vapi's public webhooks."""
    last_turns: list[dict] = []
    for _ in range(120):  # ten minutes, longer than the assistant's 3-minute cap
        pending = read_pending()
        if pending.get("vapi_call_id") != call_id or pending.get("status") != "vapi_calling":
            return
        try:
            call = vapi_calls.get_call(call_id)
        except vapi_calls.VapiError as exc:
            print(f"[bridge] Vapi status check failed: {exc}")
            time.sleep(5)
            continue
        # The final API snapshot can omit the artifact briefly even though
        # earlier polls already saw speech. Keep that last known transcript.
        turns = vapi_calls.turns(call) or last_turns
        if turns != last_turns:
            last_turns = turns
            patch_pending(live_turns=turns)
            if pending.get("live_message_id"):
                edit_telegram(pending.get("chat_id"), pending["live_message_id"],
                              render_live(pending, turns))
        if call.get("status") == "ended":
            current = read_pending()
            if current.get("vapi_call_id") != call_id or current.get("status") != "vapi_calling":
                return
            if current.get("live_message_id"):
                edit_telegram(current.get("chat_id"), current["live_message_id"],
                              render_live(current, turns, finished=True))
            archive_call(current, {"provider": "vapi", "call": call,
                                   "transcript": turns})
            send_telegram(current.get("chat_id"), format_vapi_result(current, turns))
            transcript_sent = post_transcript(current, turns)
            patch_pending(status="done", dial=False, live_turns=turns,
                          transcript_sent=transcript_sent,
                          finished_at=datetime.now(timezone.utc).isoformat())
            print(f"[bridge] Vapi call finished: {call_id}")
            return
        time.sleep(5)
    pending = read_pending()
    if pending.get("vapi_call_id") == call_id and pending.get("status") == "vapi_calling":
        patch_pending(status="vapi_review_required")
        send_telegram(pending.get("chat_id"),
                      "The Vapi call is no longer being tracked automatically. Check its status in Vapi before trying again. No reservation is confirmed.")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RainCheckBridge/1.0"

    # Only needed by the optional CopilotKit console on :3000. Missing CORS
    # headers surface as a bare "Failed to fetch" with no explanation at all
    # so they go in now rather than being debugged later.
    # Access-Control-Allow-Origin: * was applied to /dial, /outcome, /cancel and
    # /amend. Those endpoints take no credentials, so a wildcard meant ANY page
    # the operator happened to have open could arm a queued call or post text
    # into the group chat under the bot's name. The wildcard was not even
    # needed: the console proxies same-origin through app/api/bridge, and the
    # call page is served by this process. Scoped to the console's origin.
    CONSOLE_ORIGIN = env("CONSOLE_ORIGIN") or "http://localhost:3000"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.CONSOLE_ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, payload, code: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._json({"error": f"{path.name} missing"}, 404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path

        if route in ("/", "/index.html", "/call_page.html"):
            self._file(HERE / "call_page.html", "text/html; charset=utf-8")
        elif route == "/health":
            self._json({"ok": True, "service": "raincheck-bridge", "port": PORT})
        elif route == "/live":
            pending = read_pending()
            self._json({"turns": pending.get("live_turns") or [],
                        "status": pending.get("status")})
        elif route == "/config":
            agent_id = env("ELEVENLABS_AGENT_ID")
            self._json(
                {
                    "agent_id": agent_id,
                    "agent_id_present": bool(agent_id),
                    "booker_name": env("BOOKER_NAME", "a guest"),
                    "callback_number": env("CALLBACK_NUMBER"),
                    "demo_phone_active": bool(env("DEMO_PHONE")),
                    "call_provider": env("CALL_PROVIDER", "elevenlabs"),
                }
            )
        elif route == "/pending":
            self._json(read_pending())
        else:
            self._json({"error": "not found", "path": route}, 404)

    def do_POST(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        body = self._body()
        if not isinstance(body, dict):
            self._json({"error": "JSON object required"}, 400)
            return
        pending = read_pending()
        if route in ("/negotiate", "/turn", "/outcome") and pending.get("negotiation"):
            if body.get("booking_id") != pending.get("id") or pending.get("status") != "dialing":
                self._json({"error": "stale or inactive call"}, 409)
                return

        if route == "/negotiate":
            if not pending.get("negotiation") or not isinstance(body.get("offer"), dict):
                self._json({"error": "approved negotiation and offer required"}, 400)
                return
            grounded = ground_offer(pending, body["offer"])
            result = check_offer(pending, grounded)
            events = pending.get("negotiation_events") or []
            events.append({"offer": grounded, "result": result,
                           "evidence_checked": True,
                           "at": datetime.now(timezone.utc).isoformat()})
            patch_pending(negotiation_events=events[-30:])
            self._json(result)
            return

        if route == "/dial":
            pending = read_pending()
            if not pending:
                self._json({"error": "nothing pending"}, 409)
                return
            if not pending.get("dial_number"):
                self._json({"error": "pending has no dial_number"}, 409)
                return
            if not private_inputs.current(pending):
                self._json({"error": "private requirements changed; run /close for a new approval"}, 409)
                return
            if pending.get("negotiation") and pending.get("status") != "approved":
                self._json({"error": "negotiation requires approval in the group"}, 409)
                return
            # A second /dial on a finished or in-flight call re-armed it: a
            # new live message, a fresh empty transcript, and back when
            # auto-dial existed, a second phone call to a restaurant that had
            # already said yes. bot.py sets "approved" immediately before it
            # posts here, so anything else means this is not a fresh booking.
            if str(pending.get("status") or "") not in ("approved", "awaiting_approval"):
                self._json({"error": f"this booking is {pending.get('status')!r}, "
                                     "not waiting to be dialled"}, 409)
                return
            if str(pending.get("call_provider") or env("CALL_PROVIDER", "elevenlabs")).lower() == "vapi":
                # Serialize the approval-to-call transition. Duplicate HTTP
                # requests must never launch two paid phone calls.
                with _vapi_dispatch_lock:
                    pending = read_pending()
                    if pending.get("status") != "approved" or pending.get("vapi_call_id"):
                        self._json({"error": "a fresh approved call is required"}, 409)
                        return
                    try:
                        phone = vapi_calls.preflight(pending)
                    except vapi_calls.VapiError as exc:
                        self._json({"error": str(exc)}, 409)
                        return
                    patch_pending(status="vapi_dispatching", dial=False, call_provider="vapi")
                    try:
                        call = vapi_calls.start_call(pending, phone)
                    except vapi_calls.VapiError as exc:
                        # A timeout can be ambiguous. Never automatically
                        # retry a create-call request that might have landed.
                        patch_pending(status="vapi_dispatch_uncertain", vapi_error=str(exc))
                        self._json({"error": str(exc) + "; check Vapi before retrying"}, 502)
                        return
                    live_id = send_telegram_returning_id(
                        pending.get("chat_id"), render_live(pending, []))
                    updated = patch_pending(status="vapi_calling", dial=False,
                                            vapi_call_id=call["id"], live_message_id=live_id,
                                            live_turns=[], vapi_started_at=datetime.now(timezone.utc).isoformat())
                threading.Thread(target=monitor_vapi_call, args=(call["id"],), daemon=True).start()
                self._json({"ok": True, "provider": "vapi", "call_id": call["id"],
                            "status": updated["status"]})
                return
            # Open the live transcript message now, so the group sees the call
            # start rather than only its result.
            live_id = send_telegram_returning_id(
                pending.get("chat_id"), render_live(pending, [])
            )

            # A human dials. There is no automatic path here, and the attempt
            # to build one is worth recording because it failed for a reason
            # that is structural rather than fixable.
            #
            # macOS can hand a tel: URL to FaceTime, which relays through a
            # paired iPhone -- so the Mac becomes the call's audio endpoint,
            # using its own microphone and speakers. But the agent also lives
            # on that Mac, in a browser tab, and the two are joined only by
            # air. Put both ends on one machine and each one's echo
            # cancellation removes exactly the signal the other needs: the
            # agent's voice is cancelled out of the call's microphone, and the
            # restaurant's voice is cancelled out of the browser's. Measured,
            # 12 Sep: the phone rang, was answered, and both sides heard
            # silence. Acoustic coupling needs two devices with air between
            # them, which means the dialling device cannot be the laptop.
            #
            # So the rule of two humans -- one approving, one dialling
            # -- is not a compromise here. It is the only topology where the
            # audio works at all.
            print("[bridge] approved - dial the number by hand and put it on speakerphone")

            self._json(patch_pending(
                dial=True, status="dialing", live_message_id=live_id,
                live_turns=[],
            ))

        elif route == "/turn":
            # One conversational turn, pushed from the call page as it happens.
            global _last_live_edit
            pending = read_pending()
            said = str(body.get("message") or "").strip()
            if not said:
                self._json({"ok": True, "ignored": "empty turn"})
                return

            turns = list(pending.get("live_turns") or [])
            turns.append({"source": body.get("source") or "ai", "message": said})
            patch_pending(live_turns=turns)

            edited = False
            now = time.time()
            if now - _last_live_edit >= _LIVE_EDIT_MIN_INTERVAL:
                _last_live_edit = now
                edited = edit_telegram(
                    pending.get("chat_id"), pending.get("live_message_id"),
                    render_live(pending, turns),
                )
            self._json({"ok": True, "turns": len(turns), "edited": edited})

        elif route == "/outcome":
            pending = read_pending()
            collected, source = collect_outcome(body)
            fresh = read_pending()
            if pending.get("negotiation") and (fresh.get("id") != pending.get("id") or fresh.get("status") != "dialing"):
                self._json({"error": "call changed while collecting outcome"}, 409)
                return
            collected = validate_outcome(pending, collected)
            body["collected"] = collected
            body["outcome_source"] = source

            # Close the live message off so it does not sit there saying
            # "live" forever, then post the structured result separately.
            turns = body.get("transcript") or pending.get("live_turns") or []
            body["transcript"] = turns
            if pending.get("live_message_id"):
                edit_telegram(pending.get("chat_id"), pending["live_message_id"],
                              render_live(pending, turns, finished=True))

            archived = archive_call(pending, body)
            text = format_outcome(pending, body)
            sent = send_telegram(pending.get("chat_id"), text)
            transcript_sent = post_transcript(pending, turns)
            patch_pending(
                dial=False,
                status="done",
                outcome=collected,
                outcome_source=source,
                transcript_sent=transcript_sent,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"[bridge] call archived -> {archived}")
            self._json({"ok": True, "telegram_sent": sent,
                        "transcript_sent": transcript_sent,
                        "archived": archived.name, "outcome_source": source})

        elif route == "/amend":
            # Amend the queued booking before it is dialled.
            #
            # DELIBERATELY NARROW. dial_number, real_number, restaurant_name and
            # demo_override are NOT amendable through this endpoint, by anything,
            # ever. The number that gets dialled is settled by the poll result,
            # OpenStreetMap and the consent allowlist in bot.py -- three places
            # with real checks -- and an HTTP endpoint that could overwrite it
            # would route around every one of them. The operator console exposes
            # this to an LLM, which makes the restriction load-bearing rather
            # than tidy.
            pending = read_pending()
            if pending.get("negotiation"):
                self._json({"error": "This call has approved negotiation limits. Change the plan in the group and run /close for a fresh approval."}, 409)
                return
            if not pending or not pending.get("restaurant_name"):
                self._json({"error": "nothing pending to amend"}, 409)
                return
            # Amending a call that is happening, or has already happened,
            # changes nothing about it -- it only makes the record disagree
            # with what was actually said on the phone.
            if str(pending.get("status") or "") in ("dialing", "done", "cancelled"):
                self._json({"error": f"this booking is {pending.get('status')!r} "
                                     "and can no longer be amended"}, 409)
                return

            patch: dict = {}
            errors: list[str] = []

            if "party_size" in body:
                try:
                    size = int(body["party_size"])
                except (TypeError, ValueError):
                    errors.append("party_size must be a whole number")
                else:
                    if 1 <= size <= 40:
                        patch["party_size"] = size
                    else:
                        errors.append("party_size must be between 1 and 40")

            for field, cap in (("when_text", 120), ("booking_name", 60), ("notes", 300)):
                if field in body:
                    value = str(body[field] or "").strip()
                    if not value:
                        errors.append(f"{field} cannot be empty")
                    elif field == "when_text" and not amend_when_text(value)[0]:
                        # The chat is held to this standard, so an HTTP endpoint
                        # wired to an LLM does not get to lower it. You cannot
                        # book a table at "this evening".
                        errors.append(amend_when_text(value)[1])
                    else:
                        patch[field] = value[:cap]

            rejected = [k for k in body if k not in
                        ("party_size", "when_text", "booking_name", "notes")]
            if rejected:
                errors.append(
                    "not amendable here: " + ", ".join(sorted(rejected))
                    + " (the number to dial and the restaurant are settled by the poll, "
                      "OpenStreetMap and the consent allowlist)"
                )

            if errors:
                self._json({"error": "; ".join(errors)}, 400)
                return
            if not patch:
                self._json({"error": "no amendable fields supplied"}, 400)
                return

            patch["amended_at"] = datetime.now(timezone.utc).isoformat()
            updated = patch_pending(**patch)
            print(f"[bridge] amended: {', '.join(k for k in patch if k != 'amended_at')}")
            self._json(updated)

        elif route == "/cancel":
            if read_pending().get("status") in ("vapi_dispatching", "vapi_calling", "vapi_dispatch_uncertain"):
                self._json({"error": "Vapi call may be live; end it in Vapi before changing this record"}, 409)
                return
            # Marking a finished call "cancelled" rewrites history: the table
            # was booked, and the record would then say it never was.
            if str(read_pending().get("status") or "") == "done":
                self._json({"error": "this call already finished - cancel the "
                                     "booking with the restaurant, not here"}, 409)
                return
            self._json(patch_pending(dial=False, status="cancelled"))

        else:
            self._json({"error": "not found", "path": route}, 404)

    def log_message(self, fmt, *args):
        # Default logging writes a line per poll; the page polls twice a second.
        if "/pending" not in self.path:
            sys.stderr.write("[bridge] %s\n" % (fmt % args))


def open_call_page() -> None:
    """Open the call desk in the default browser when the bridge starts.

    The tab has to exist for the microphone to exist -- WebRTC lives in a page,
    not in this process -- but nobody should have to remember to go and open
    it. Combined with the page arming itself on a remembered permission, the
    operator's only remaining job is the one that has to stay manual: dialling
    the phone and holding it to the laptop.

    Best effort and silent on failure. A headless run, a locked-down laptop or
    a machine with no browser are all fine; the page is still reachable.
    """
    if env("CALL_PROVIDER", "elevenlabs").lower() == "vapi":
        print("[bridge] Vapi handles audio; no local microphone page is needed")
        return
    if env_flag("NO_AUTO_OPEN"):
        print("[bridge] NO_AUTO_OPEN set \u2014 open the call page yourself")
        return

    url = f"http://localhost:{PORT}/"
    opener = {"darwin": ["open", url], "win32": ["cmd", "/c", "start", "", url]}.get(
        sys.platform, ["xdg-open", url]
    )
    try:
        subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[bridge] opened the call desk at {url}")
    except (OSError, FileNotFoundError):
        print(f"[bridge] could not open a browser \u2014 go to {url} yourself")


def main() -> None:
    load_env(ROOT / ".env")
    warn_if_tls_broken("bridge")
    if not env("ELEVENLABS_AGENT_ID"):
        print("[bridge] WARNING: ELEVENLABS_AGENT_ID is empty — the call page will refuse to start.")
    print(f"[bridge] listening on http://localhost:{PORT}")
    pending = read_pending()
    if pending.get("status") == "vapi_calling" and pending.get("vapi_call_id"):
        threading.Thread(target=monitor_vapi_call, args=(pending["vapi_call_id"],), daemon=True).start()
    threading.Timer(1.0, open_call_page).start()   # after the socket is up
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
