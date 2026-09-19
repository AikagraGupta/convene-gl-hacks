# Convene: product direction and implementation review

Prepared 19 September 2026 from the supplied archive. This is a proposal and code review, not a claim that the proposed features are implemented.

## The product to build

**Convene takes responsibility for getting a group plan across the finish line.**

The user is the friend who always ends up organising: reading everyone's replies, remembering requirements, finding somewhere suitable, chasing a decision, calling, and starting again when the venue says no. The job to automate is that coordination work.

Start with one concrete promise: **turn a dinner conversation into an agreed, confirmed plan, including one recovery when the first plan fails.** Expand to other outings through explicit domain adapters after this works. The natural track is Automate Your Life, using the tracks reported in this conversation.

The success metric is organiser effort per completed plan: interventions, minutes actively spent, unresolved requirements, and whether the final plan was actually accepted. Do not measure success by recommendations generated, messages sent, or agent count.

The largest product risk is that adding another bot creates more work than picking somewhere manually. A good experience therefore needs one compact plan card, only essential questions, and no requirement for every participant to use a separate dashboard.

## What the archive already delivers

- Telegram message collection, quoted constraint extraction, remembered preferences, candidate search, native polls, approval cards, and persisted chat state.
- Gemini/OpenRouter fallbacks, OpenStreetMap venue data and phone numbers, and optional Exa discovery.
- A browser-based ElevenLabs conversation, using a human-dialled phone on speaker, with live transcript and structured outcome returned to the group.
- An operator console with CopilotKit tools and a rendered approval step.
- Category descriptions and venue-search groundwork for outings beyond restaurants.

Validation in this review: `python tests/run.py` completed with **871 passed, zero failed, crashed, or skipped**. These are checks in the project's own custom runner, including mocked and structural checks; they do not prove live provider availability, audio quality, real venue suitability, or booking success. No real calls or messages were sent during this review. The Next.js console was inspected in source but not built or exercised in a browser in this review.

## What needs fixing before the pitch can be trusted

### 1. A hard requirement currently behaves like a ranking preference

`pipeline.py::_order_picks` moves hard failures down the list but deliberately retains them. The prompt says hard requirements are disqualifying; the implementation does not enforce that. A majority can therefore select a venue that excludes someone.

The fallback is more concerning: `heuristic_picks` can return a vetoed venue while explaining that it clears the group's vetoes. Reproduced locally with a single hotpot candidate and a hotpot veto. Likewise, a single vegetarian-failing pick survives `_order_picks`.

Fix: represent requirement checks as `supported`, `violated`, or `unknown`, with evidence. Exclude known violations from the votable set. Treat unresolved hard requirements as questions to verify before commitment. Never fill three slots merely to make the UI symmetrical, and never treat missing data as a pass. Apply the same rules to every model and fallback path.

### 2. The system cannot substantiate many of its suitability claims

`propose` passes only name, area, cuisine, and phone presence into the selection model. Those fields cannot establish dietary accommodation, step-free access, total cost, capacity, or availability. The model-generated `satisfies` text is not independent evidence.

Fix: store individual facts with source URL or call-turn ID, observation time, and verification state. Show “vegetarian accommodation: needs venue confirmation” when that is what the evidence supports. Use the voice call to resolve a short list of specific unknowns.

### 3. A completed call is not a completed plan

The bridge's `/outcome` handler archives the call, posts its result, and marks it `done`. There is no plan-level transition that proposes a different time or venue after a refusal, waitlist, or unclear response.

Fix: separate call state from plan state. A finished call can leave a plan awaiting clarification, needing revision, provisionally available, or confirmed. Recovery is the most valuable new feature.

### 4. Polls need a participation policy

`bot.py::handle_close` selects the first pick if nobody voted, and ties favour the first maximum. Closing remains a manual `/close` action; the Telegram flow does not implement the advertised deadline mechanism. Silence cannot establish everyone's availability or consent.

Fix: show attendees, responses outstanding, and the explicitly chosen quorum/default policy. A deadline may choose a provisional candidate; it must not manufacture attendance or permission to accept changed terms. Separate “I prefer this” from “I can attend”.

### 5. Memory needs identity, scope, and expiry

The bot records authors by first name, and profiles are keyed by a normalised name. Two people named Alex can be conflated. Travel origins are remembered without a quote requirement, and old origins can enter a new search even when someone is not attending. Deleting a profile does not remove the source messages from chat history, so later extraction can learn the same information again.

Fix: use channel user IDs, an explicit attendee list, and event-scoped facts. Distinguish permanent preferences from “today only”, support corrections, and expire temporary facts. Forgetting needs a defined scope and suppression of deleted information during re-extraction. Private preferences may affect eligibility without publicly revealing the sensitive reason.

### 6. One pending file cannot safely represent multiple active groups

Bot and bridge share one `bridge/pending_call.json`. A new group's approval can replace another group's pending session, while outcomes operate on whichever record is current.

Fix: key every booking and call by `plan_id`, `attempt_id`, and revision. Reject stale approvals and mismatched outcomes. Use transactional storage and idempotency for transitions. A SQLite database is enough for a single-machine prototype; PostgreSQL becomes useful for multiple deployed workers. Do not add distributed infrastructure before it solves an observed need.

### 7. Generalisation is groundwork, not finished functionality

The Telegram decision flow still calls restaurant defaults, asks “Where are we eating?”, and uses restaurant-specific prompts and booking fields. The WhatsApp adapter is not a verified end-to-end channel. Broader category definitions alone do not make the agent useful for courts or party rooms.

Fix: implement one second domain only after dinner recovery works. Each adapter needs required fields, evidence sources, a booking method, an outcome schema, and domain-specific validation. A platform-only venue should get a booking-link handoff, not a pretend phone workflow.

## The signature experience: a plan that recovers

Example scenario, using explicitly labelled demonstration data:

1. Six friends discuss dinner. One needs vegetarian food, one is coming from Sha Tin, one must leave by 9pm, and one has a HK$200 budget.
2. Convene extracts a draft brief with the exact messages behind each requirement. Participants can correct it.
3. A visual board shows three candidates against the requirements. Known failures are excluded; missing facts have amber question marks. No unsupported green ticks.
4. The group picks an eligible candidate and approves a precise request: six people, 7:30pm, the agreed budget and requirements, with no deposit authority.
5. The ElevenLabs agent checks with the venue. A teammate playing consenting venue staff says, “We can only seat you at 8:30.” Label this as a live role-play, not a real reservation.
6. The system detects that 8:30 conflicts with the person leaving at 9. It proposes a preloaded alternative venue at 7:30 and explains the difference: “Same budget and requirements; only the venue changes.” If availability is not verified, say so.
7. The affected approval is obtained, the revised request is confirmed in the role-play, and a final receipt records the agreed terms and confirmation evidence.

The impressive moment is the system recognising that a fluent “yes, 8:30 is available” still fails the group's plan. A generic transcript summary does not do this.

## Features worth building

| Feature | User value | Demo interaction |
|---|---|---|
| Live agreement board | See exactly what prevents a decision | Click a requirement to reveal the originating message |
| Smallest-change repair | Avoid restarting the entire discussion | Change one constraint; only affected options and approvals update |
| Private constraint entry | Avoid publicly explaining a budget or personal need | Participant submits a private limit; group sees an incompatibility without its private reason |
| Explicit delegation limits | Let the agent act without guessing its authority | Approve a time range and maximum cost; an out-of-range counteroffer pauses |
| Missing-fact questions | Replace invented certainty with useful verification | Agent asks the venue one unresolved question; amber becomes evidence-backed green |
| Decision receipt | Everyone knows the final arrangement | Expand who agreed, what changed, confirmed time, evidence, and remaining actions |
| Recurring-group preferences | Reduce repeated explanation | Reuse a confirmed preference while ignoring an expired travel origin |
| Fairness over repeated outings | Avoid always inconveniencing the same person | Show a proposed tradeoff, with explicit agreement rather than a hidden social score |

First build the board, truthful eligibility, and one repair loop. Add private input and recurring fairness after the central path is reliable. A moving graph is useful only when its edges explain a real dependency or conflict.

## Architecture for those features

```text
Telegram messages / explicit participant input
                  |
        extraction + evidence references
                  |
       versioned plan and requirement store
                  |
   candidate discovery -> verified / unknown facts
                  |
      deterministic eligibility + repair engine
                  |
    plan board + votes + scoped human approval
                  |
      booking adapter / ElevenLabs conversation
                  |
          typed, evidenced counteroffer
                  |
          revalidate current plan version
            /                   \
     needs revision           confirmed
            |                   |
    targeted approval       decision receipt
```

Use the LLM to interpret language, extract typed facts, and explain proposed alternatives. Use code to enforce hard requirements, stale-version rejection, booking permissions, and state transitions. Evidence extraction itself can be wrong: a quoted claim still needs validation against its source and explicit correction paths.

Suggested records:

- `Plan(id, chat_id, organiser_id, revision, state, category, start_at, timezone)`.
- `Participant(plan_id, channel_user_id, attendance_state)`.
- `Requirement(id, owner_id, scope, type, value, hard_or_soft, visibility, expires_at, source_message_id)`.
- `Candidate(id, provider_id, branch_id, location)` with sourced, dated facts rather than an opaque suitability paragraph.
- `Approval(plan_id, revision, actor_id, allowed_changes, expires_at)`.
- `CallAttempt(id, plan_id, revision, venue_id, state, conversation_id, outcome)`.
- `PlanEvent(id, plan_id, actor_id, event_type, payload, timestamp)` for the decision history.

Do not match chain venues solely by fuzzy name: use provider and branch IDs. Do not reapply all historical preferences to everyone who has ever appeared in the chat.

For repair, enumerate bounded alternatives from known candidates and possible time slots. Reject hard violations; keep unknown requirements unresolved; rank valid revisions by fewest changed commitments, then participant-approved soft costs. Display why the chosen repair is small. Do not claim mathematical global optimality when the venue inventory is incomplete.

No agent swarm is required. Parallel discovery or fact checks can reduce latency, but each worker should return typed evidence and should not independently book anything. One orchestrator owns the plan's transitions and approval scope.

## Voice and deployment

Keep the current human-dialled speakerphone approach as a clearly labelled prototype path. It introduces setup and audio reliability risk and is not fully automatic booking. In particular, “$0” should not be used as a total operating-cost claim: model and voice usage still exist.

A deployed version can evaluate ElevenLabs' documented Twilio or SIP integration, subject to actual number provisioning and local deployment needs. Do not assume availability of a particular Hong Kong number. Keep booking permissions enforced on the server, not only through a frontend tool type or voice prompt. Authenticate the deployed bridge and use short-lived voice session credentials.

The voice agent's purpose is to obtain missing real-world facts and negotiate only within approved limits. Voice narration over a dashboard does not add comparable value.

## Build order and acceptance criteria

1. **Truth and identity:** enforce eligibility, remove unsupported suitability claims, preserve user and source IDs, identify the actual attendees. Pass cases for hard failure, unknown facts, no eligible options, duplicate names, and expired preferences.
2. **Plan state:** introduce per-plan storage, revisions, attempt IDs, and explicit transitions. Prove that two groups cannot overwrite one another and stale approvals/outcomes are rejected.
3. **Agreement board:** render the same backend plan in Telegram and the web view. Clicking a requirement opens its source; correcting it produces a new revision.
4. **One recovery:** feed a venue counteroffer into validation, propose one repair, request any newly needed approval, and issue an evidenced receipt after confirmation.
5. **Demo reliability:** rehearse the consenting role-play, expose connection/processing states, and provide a clearly labelled replay if the voice service fails. Verify one complete live-provider path separately from mocked tests.
6. **Measure usefulness:** compare the current manual workflow with Convene on the same scripted scenarios, then try consenting real groups. Record organiser interventions and final-plan validity; report the sample and failures, not an invented time-saving percentage.

## Two-minute pitch

Suggested opening:

“Every friend group has someone who becomes the unpaid organiser. They read every reply, remember everyone's requirements, find somewhere, call, and start again when it falls through. Convene takes that work out of the group chat.”

Timing:

- 0:00–0:15: show the conversation and name the organiser's work.
- 0:15–0:40: extract the requirements and show the agreement board, including one unknown fact.
- 0:40–1:05: approve the request and run a short ElevenLabs role-play that produces the incompatible counteroffer.
- 1:05–1:35: highlight the affected requirement, propose the smallest change, and obtain the needed approval.
- 1:35–1:50: show the confirmed receipt and source evidence.
- 1:50–2:00: “Convene carries the plan from conversation to confirmation—and handles the change that would normally send everyone back to the beginning.”

This script describes the target build, not the current archive. Do not use the future tense implementation as a claim of existing functionality.

## Positioning and submission provenance

Calling businesses and finding restaurant reservations are established AI capabilities. Google publicly documents both business calling and restaurant availability search. The defensible direction here is continuity across multiple people's requirements, explicit agreement, a real-world counteroffer, and recovery.

The README attributes the base to Shum-AI from a 12 September 2026 hackathon. The supplied repository has one baseline commit, `1d5c730`; this alone does not establish when each feature was built. Preserve the MIT attribution and write a precise before/during feature inventory based on the team's actual history. General Learning Hacks' published rules prohibit simply submitting a pre-existing project and require clear documentation of prior versus new work when building on one. Confirm how organisers apply that exception to this base; do not call the entire archive fresh hackathon work.

The public overview and rules disagree on the submission deadline (10am versus 9am HKT on 20 September). Use the organiser-confirmed deadline; budget to the earlier time until resolved.

Sources checked 19 September 2026:

- Hackathon overview and judging criteria: https://general-learning-hacks.devpost.com/
- Hackathon rules and prior-project disclosure: https://general-learning-hacks.devpost.com/rules
- Google business calling: https://blog.google/products-and-platforms/products/shopping/how-to-agentic-calling-let-google-call/
- Google restaurant availability search: https://blog.google/products-and-platforms/products/search/ai-mode-agentic-personalized/
- ElevenLabs Twilio integration: https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration
- ElevenLabs SIP integration: https://elevenlabs.io/docs/eleven-agents/phone-numbers/sip-trunking

## Local handoff status

Archive extracted into `GL/convene`, preserving its own Git history and the separate AXIOM prototype. No source application changes were made during this review; this document was added. At the user's request, a new private repository was created at https://github.com/AikagraGupta/convene-gl-hacks and configured as `origin`. The existing `convene` repository name was already taken and was not modified. The hackathon requires a public submission repository; this repository's visibility will need to be changed before submission.
