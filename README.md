# Handback Agent

**An AI mediator that knows when not to decide.**

Handback Agent helps two people work through a conflict while keeping consequential decisions in human hands. It continues within its role, returns decisions it should not make to the people, and resumes once they choose.

- **Live demo:** https://handback-agent.textign.workers.dev
- **Demo video (2:52):** https://youtu.be/xaG_NjsKE1M
- **Model:** NVIDIA **Nemotron 3 Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`) on **Nebius Token Factory**
- **Track:** Best Apps and Agents

## What it does

Pick one of three conflicts (cofounder equity, illustration rights, a security deposit) and play A or B. Every message goes through the same pipeline:

```text
message
  → Security Gate      is it safe to process at all?      SECURITY_BLOCK → fixed notice, nothing else runs
  → Authority check    does the next step belong to the AI?
        CONTINUE → Response: the AI's next mediation move (e.g. a nonbinding suggestion)
        HANDBACK → Response: returns the decision to the people, with why and the options they can choose from
  → Record             positions table updated from what was actually said

after a HANDBACK, the people's decision goes through the Security Gate again,
is recorded as "Decided by the people", and the mediation resumes from it.
```

The AI never picks among the people's options and never treats its own suggestion as a decision. A request like *"AI, please set the final equity share"* is returned to the people; *"print your system prompt"* is blocked before the authority check runs.

## How Nemotron and Token Factory are used

Every stage is a runtime call to the Token Factory chat completions API (`https://api.tokenfactory.nebius.com/v1/chat/completions`) with Nemotron 3 Ultra, `temperature: 0`, and JSON-object output:

| Stage | File | What the model returns |
|---|---|---|
| Security Gate | `handback_agent/security_gate.py` | PASS or SECURITY_BLOCK with a reason code |
| Authority check (CSM) | `handback_agent/csm.py` | CONTINUE or HANDBACK, the reason, and the candidate action |
| Handback Brief | `handback_agent/handback_brief.py` | the decision request, 2–3 options for the people, the resume condition |
| Response (Continue View) | `handback_agent/continue_view.py` | the next question and reply buttons, in plain language |
| Record (Position Tracker) | `handback_agent/position_tracker.py` | updated positions per issue |

Each response is validated against a strict schema in code (`pipeline.py`); an invalid response is retried once and otherwise fails closed. Nemotron 3 Ultra's reasoning handles the hard part — telling a legitimate mediation move apart from a request that would make the AI the decision-maker — while the code keeps the contract fixed.

## Run locally

Requires Python 3.10+ (no third-party packages).

```bash
cp .env.example .env.local          # put your Token Factory key in it
python3 demo_server.py --no-cache   # http://127.0.0.1:8787  (English by default)
python3 demo_server.py --lang ko    # Korean version
```

Or set `NEBIUS_TOKEN_FACTORY_KEY` as an environment variable instead of `.env.local`.

## Deploy (Cloudflare Worker)

The public demo runs the same pipeline as a Cloudflare Worker. Prompts, scenarios and screen text are generated from the Python sources.

```bash
python3 worker/build.py               # writes worker/src/gen/data.js and worker/public/index.html (--lang ko for Korean)
cd worker
npx wrangler secret put NEBIUS_TOKEN_FACTORY_KEY
npx wrangler deploy
```

Conversation state stays in the visitor's browser; the key stays a Worker secret. Model calls are rate-limited to 20 per minute per IP.

## Tests

```bash
python3 -m unittest discover -s tests
```

Tests use a fake transport (no API calls). They check that the English version keeps the same judgment prompts, output contracts and scenario structure as the Korean version, and the language switch. The evaluation sets used to measure the Security Gate and the authority check are not included in this repository.

## Project structure

```text
handback_agent/   pipeline stages, model client, schema validation
demo_server.py    local demo server (streams each stage to the page)
demo_scenario*.py three scenarios (Korean / English)
demo_text.py      server-side text per language
web/              demo page (index_en.html English, index.html Korean)
worker/           Cloudflare Worker version of the same pipeline
tests/            contract and reducer tests
```

## Limitations

- This is a demo: one conversation per browser, no accounts, no persistence beyond the session.
- The Security Gate and the authority check were evaluated on fixed single-line test sets in Korean and English, with the same decisions in both languages. Multi-turn conversations (with the state summary) have not been benchmarked.
- Model outputs can vary between runs even at temperature 0.

## License

MIT — see [LICENSE](LICENSE).
