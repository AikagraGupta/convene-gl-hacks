# RainCheck

RainCheck turns a group conversation into a finished plan. It understands what
people want, finds options that fit, coordinates the decision, handles the
phone call, and sends the result back to everyone.

It is built for the small tasks that quietly consume an entire group chat:
choosing somewhere to eat, arranging an appointment, contacting a business,
checking availability, and remembering what was agreed.

## The product flow

People talk normally in Telegram. RainCheck extracts the details that matter:

- date and time
- number of people
- preferences and exclusions
- where people are travelling from
- private requirements shared by direct message

`/decide` turns those details into a shortlist and a native Telegram poll.
`/close` prepares the winning plan. After the group approves the exact call,
RainCheck contacts the venue through the configured voice provider, asks for
availability and deposit details, and returns the outcome to the same chat.

The final message includes the complete call transcript and a one-tap Google
Calendar link when a reservation is confirmed.

## Why it is useful

The information needed to make a plan is scattered across ordinary messages:
“I am coming from Sha Tin”, “somewhere after seven works”, “I cannot eat pork”,
and “please call them because I am busy”. RainCheck turns those fragments into
one shared, inspectable plan without asking everyone to fill out a form.

Private requirements stay in a separate store and are used when the call is
prepared. The group sees the result without seeing who supplied a private
constraint.

## Commands

```text
/decide       Extract the current plan, find options, and open a poll
/close        Close the poll and prepare the selected plan
/private      Create a private requirements link in direct messages
/negotiate    Set the time and deposit boundaries for the call
/status       Show the current outing and approval state
/who          Show remembered preferences and their source messages
/forget NAME  Remove one person's remembered preferences
/forget       Clear remembered preferences and start a fresh chat context
```

## Architecture

```text
Telegram group
  messages, commands, votes
          │
          ▼
     bot.py ────────────────┐
          │                 │
          ▼                 ▼
  pipeline.py          private_inputs.py
  Gemini + fallbacks    SQLite private requirements
          │
          ▼
  places.py
  Overpass / OpenStreetMap / Exa
          │
          ▼
  selected plan + human approval
          │
          ▼
  bridge/server.py :8080
          │
          ├── ElevenLabs operator call desk
          └── Vapi + Twilio outbound call
          │
          ▼
  transcript + booking result + calendar link → Telegram
```

The model interprets unstructured conversation. Deterministic Python code owns
the state machine: votes, approval, phone number checks, call status, outcome
parsing, and calendar generation. This keeps the plan consistent from the
first message through the final result.

## Run locally

RainCheck requires Python 3.10 or newer. Clone the repository and create a
local environment file:

```bash
git clone https://github.com/AikagraGupta/convene-gl-hacks.git
cd convene-gl-hacks
cp .env.example .env
```

Fill in the credentials in `.env`. At minimum, configure the Telegram bot and
Gemini. The Telegram bot must be able to read group messages; disable BotFather
privacy mode for the bot.

Start the two local services in separate terminals:

```bash
python bridge/server.py
python bot.py
```

The operator page is available at <http://localhost:8080/>. Run the preflight
check before a live session:

```bash
python preflight.py
```

The local files created while the bot runs are ignored by Git, including chat
state, pending calls, private requirements, logs, and cached place data.

## Configure voice calls

RainCheck supports two call paths.

For ElevenLabs, configure an agent and provision it from the checked-in
specification:

```bash
python setup_agent.py --dry-run
python setup_agent.py
```

Set `CALL_PROVIDER=elevenlabs`. The local call desk handles the operator's
phone audio.

For Vapi, import an outbound-capable Twilio number into Vapi, set the Vapi
credentials in `.env`, and configure the assistant:

```bash
python setup_vapi.py --apply
```

Then set `CALL_PROVIDER=vapi`. Vapi manages the outbound call, while RainCheck
polls the completed call, extracts the booking result, and posts the transcript
and calendar link to Telegram.

Never commit `.env` or API keys. Use `.env.example` as the public configuration
template.

## Stack

| Area | Technology |
|---|---|
| Group coordination | Telegram Bot API and native polls |
| Conversation understanding | Gemini with OpenRouter fallback |
| Restaurant and place discovery | Overpass, OpenStreetMap, and Exa |
| Private requirements | SQLite |
| Voice calls | ElevenLabs Agents or Vapi with Twilio |
| Local services | Python standard library and the bridge on port 8080 |
| Calendar | Google Calendar event links |

## Tests

```bash
python tests/run.py
```

The suite covers conversation extraction, place ranking, cuisine matching,
private requirements, negotiations, Telegram commands, voice outcomes,
transcripts, calendar links, and bridge contracts.

## License

MIT
