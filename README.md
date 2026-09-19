# RainCheck

**The group-chat agent that turns “what should we do?” into a plan everyone can act on.**

RainCheck is a standalone hackathon project for the moment when a group has a
dozen opinions, hidden constraints, and no decision. It reads the conversation,
understands what each person actually said, finds strong options, gets the group
to a decision, and carries the plan through to a venue call, transcript, and
calendar event.

It feels like adding a highly organised friend to the chat: nobody has to fill
out a form, repeat themselves, or become the project manager.

## The two-minute demo

1. Start the bridge and bot.
2. Add RainCheck to a Telegram group and talk naturally: “Thai food, around 7:30,
   somewhere easy from Central and Kennedy Town.”
3. Send `/decide`.
4. RainCheck extracts the real constraints with the supporting quotes, proposes
   three places, and opens a native Telegram poll.
5. Send `/close` after the vote. RainCheck turns the winner into a clear booking
   brief and asks the group to approve it.
6. Approve the call. The voice agent speaks with the venue, checks the agreed
   details, posts the transcript to the group, and creates a one-tap calendar
   link for everyone.

The whole story is visible in the chat: messy conversation → shared decision →
completed plan.

## Why RainCheck matters

Recommendations are easy. **Convergence is hard.** The useful information is
usually scattered across ordinary messages:

- “I am coming from Sha Tin.”
- “I cannot do pork.”
- “Somewhere after 7 works.”
- “Please do not make me call the restaurant.”

RainCheck turns those fragments into a shared, inspectable decision. It preserves
the words behind each extracted constraint, keeps private requirements private,
and gives the group a closing mechanism so plans actually happen.

## What it does

### Understands the conversation

The pipeline reads recent group messages, identifies people, places, times,
budgets, dietary needs, travel context, and preferences, and attaches each
constraint to the message that supports it.

### Finds options that fit

It searches live place data, enriches discovery with semantic search, ranks
options against the group's actual language, and returns a short shortlist with
callable venue details.

### Makes a decision in the chat

`/decide` opens a native Telegram poll. `/close` closes the loop, records the
winner, and prepares the exact request that will be sent to the venue.

### Handles sensitive requirements privately

Participants can send `/private` and save a budget, time window, or dietary and
accessibility requirement in a DM. RainCheck combines those inputs when checking
the venue while keeping the details out of the group approval card.

### Completes the last mile

After approval, an ElevenLabs voice agent calls the venue through the operator
call desk. It asks for the agreed time, availability, deposit terms, and
relevant requirements. The complete transcript and outcome return to Telegram,
and a calendar link lets every participant add the plan in one tap.

## Commands

```text
/decide       Read the discussion, propose options, and open a poll
/close        Close the poll and prepare the winning request
/private      Save a private requirement in a DM
/negotiate    Set the group's approved time and deposit boundaries
/status       Show the current outing and active approval state
/who          Show remembered preferences and their source messages
/forget NAME  Remove one person's remembered preferences
/forget all   Clear the group's remembered preferences
```

On the call desk: **Arm microphone** → confirm the level bar moves → dial →
speakerphone → **Start agent**.

## Architecture

```text
Telegram group
      │  messages, /decide, poll votes, /close
      ▼
bot.py ───────────────► pipeline.py ───► Gemini
   │                         │             └── OpenRouter fallback
   │                         ├──────────► places.py / Overpass / OpenStreetMap
   │                         └──────────► exa_search.py / Exa
   │
   │  native poll → winner → human approval
   ▼
bridge/pending_call.json ─► bridge/server.py :8080
                                  │
                                  ├── ElevenLabs voice agent over WebRTC
                                  ├── optional Vapi + Twilio outbound path
                                  └── transcript, result, calendar link → Telegram
```

The design keeps the conversation, reasoning, approval, voice call, and result
separate. Each stage has a clear input and output, so the whole flow is easy to
inspect, extend, and demo.

## Run it locally

```bash
git clone https://github.com/AikagraGupta/convene-gl-hacks.git
cd convene-gl-hacks
cp .env.example .env
```

Fill in the credentials in `.env`, then start the bridge and bot in two
terminals:

```bash
python3 bridge/server.py
python3 bot.py
```

Open <http://localhost:8080/> for the operator call desk. To rehearse the full
decision flow without a Telegram group, run:

```bash
python3 demo.py
```

For a clean dependency and configuration check:

```bash
python3 preflight.py
```

The project uses the Python standard library for its local services. The
operator console is available under `console/`:

```bash
cd console
npm install
npm run dev
```

## Configure the voice agent

Create an ElevenLabs agent, then provision its prompt, data collection fields,
and outcome schema from the checked-in specification:

```bash
python3 setup_agent.py --dry-run
python3 setup_agent.py
```

The optional Vapi path can be enabled with an imported Twilio number:

```bash
python3 setup_vapi.py --apply
```

Set `CALL_PROVIDER=vapi` in `.env` to use that provider. RainCheck keeps the
approved booking details consistent across Telegram, the call desk, the voice
agent, and the final calendar event.

## Stack

| Layer | Technology |
|---|---|
| Group chat | Telegram Bot API and native polls |
| Reasoning | Gemini with OpenRouter fallback |
| Place discovery | Overpass / OpenStreetMap and Exa |
| Voice | ElevenLabs Agents over WebRTC; Vapi + Twilio option |
| Operator UI | Local bridge and Next.js console |
| Calendar | One-tap event links generated from the confirmed plan |

## Tests

```bash
python3 tests/run.py
```

The test suite covers the conversation pipeline, place discovery, private
requirements, negotiation, Telegram commands, voice outcomes, calendar links,
and the operator console contracts.

## License

MIT.
