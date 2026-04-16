# JobWingman

> A personal AI-powered job hunter that finds, scores, and delivers the right opportunities straight to my Telegram. Every morning it saves me the hours I used to lose wading through job boards.

---

## Why this project exists

I'm a senior backend engineer who wants his next role in **AI / LLM / Agentic Systems**.

The problem: finding those roles is painful. Job boards are flooded with consulting shops, outsourcing gigs, misleading titles, and "AI-washed" postings. Filtering manually eats hours every week, and most of it is noise.

So I built JobWingman: a little engineering partner that does the grunt work for me:

- It scans several job boards **every morning**.
- It throws away the obvious non-fits (outsourcing, on-site-only, below my salary floor) **before spending a single LLM token**.
- It asks the LLM to score the survivors against my CV, with my priorities baked into the prompt.
- It delivers the top matches as rich Telegram cards — with a one-tap **💾 Save** button for the ones I want to come back to.
- I can also **paste any job URL** at any time of day, and JobWingman scrapes it, scores it, and sends back a full analysis card.

Two honest motivations behind this repo:

1. **I want the next job to be a good one.** The pipeline understands what I actually care about: remote-first, AI-focused, own product over consulting, 4-day weeks are a ⭐, and salary below €100k/year is a hard stop.
2. **I wanted to learn by building.** My day job is mostly C#/.NET microservices. This project pushed me into a production-minded Python service, a real LLM prompt engineering loop with eval, and an end-to-end async pipeline — areas I'd been wanting to go deeper on for a while.

And honestly, the best part is that it *works*. A short, curated list of genuinely interesting roles lands in my Telegram, and what used to be a Sunday-afternoon grind has collapsed into a 2-minute scroll. That shift is what I get excited about every time I open this repo.

---

## What it looks like in action

### After triggering a manual run

Sending `/run` to the bot kicks off the full pipeline on demand. The first screenshot shows the bot acknowledging the trigger and streaming the run-summary footer (`X scanned → Y passed → Z worth your time`). The second is one of the resulting job cards — match score, green/red flags, role bullets, company snapshot, and the 💾 Save button, rendered exactly as described above.

<img width="517" height="674" alt="Telegram chat showing the /run command acknowledgement followed by the scanned/passed/worth-your-time digest footer" src="https://github.com/user-attachments/assets/86065749-0661-46d5-8ec1-e22c9ec2c5e2" />

<img width="477" height="709" alt="A single rich job card from the digest: role title, company, match score, green and red flags, role bullets, strengths and gaps, benefits, verdict, and a Save button" src="https://github.com/user-attachments/assets/d953c25b-aecd-4bd9-bde8-d14f9118d9e5" />

### List saved jobs

`/list-jobs` returns every role I've tapped 💾 on, as a compact list with one row per saved job and a link back to the original posting. This is my "weekend review" view — during the week I save aggressively, and on Sunday I open this and actually apply.

<img width="729" height="715" alt="Telegram output of the /list-jobs command: a numbered list of previously saved roles with company, title, and link to the posting" src="https://github.com/user-attachments/assets/907ee847-47f0-4ba5-aa8c-f6ef7b977038" />

### Specific job analyze

Pasting any job URL directly into the chat triggers the on-demand flow: the bot scrapes the page, extracts the fields via the LLM, scores the result against my CV, and sends back the same card format as the digest. Zero context-switching between "daily" and "I just saw this on LinkedIn".

<img width="793" height="896" alt="Bot response to a pasted job URL: an Analyzing... status followed by a full scored card in the same format as the daily digest" src="https://github.com/user-attachments/assets/b3d77a7b-94d4-4706-93b5-0c0b16e328bb" />

### Saving an interesting job

Tapping 💾 on any card sends a Telegram callback; the bot looks the job up in the `pending_jobs` SQLite table by hash, inserts it into `saved_jobs`, and edits the message to confirm. The screenshot captures the post-tap confirmation — and because the lookup is SQLite-backed (not in-memory), the button still works the next morning even if the service was restarted overnight.

<img width="793" height="896" alt="Job card after tapping the Save button, showing a confirmation that the job was stored in saved_jobs" src="https://github.com/user-attachments/assets/450a5f2e-4e6c-4773-90e1-bcb8cee640e8" />


**A typical Telegram card:**

```
1. Senior AI Engineer — Acme GmbH (Remote · EU)
⭐ 4-day week  🏠 Full remote  📈 8.7/10 match  🎯 high
🟢 Building agentic systems on top of LLMs
🔴 Stack is Python-heavy (David is intermediate)

📝 Role: build agent orchestration · integrate tool use · ship to prod
🏢 Acme is a 40-person EU startup building an agentic copilot for data teams.
✅ Strong: distributed systems, LLM pipelines, Docker | ⚡ Gaps: production Kubernetes
🎁 €5k learning budget, ESOP, Deutschlandticket

💬 Clear AI focus, real product, remote-first — apply today.
🔗 View posting #A link to the job opening
```

**The daily summary footer:**

```
📊 Today: 312 scanned → 41 passed → 8 worth your time
```

---

## How it works (one-screen mental model)

```
        ┌──────────────────────────────────────────────────────────────┐
        │                        Telegram (me)                          │
        │   daily digest  │  /run  │  paste a URL  │  💾 Save  │  /list-jobs │
        └──────────────────────────────────────────────────────────────┘
                     ▲                                  │
                     │ send message                     │ long-poll updates
                     │                                  ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                      FastAPI (Python)                         │
        │                                                              │
        │   ┌──────────────┐   ┌─────────────┐   ┌─────────────────┐   │
        │   │ Job sources  │──▶│  Pipeline   │──▶│  LLM scoring    │   │
        │   │ (5 fetchers) │   │ dedup+filter│   │   (see below)   │   │
        │   └──────────────┘   └─────────────┘   └────────┬────────┘   │
        │                          │                      │            │
        │                          ▼                      ▼            │
        │                     ┌───────────────────┐  ┌──────────────┐  │
        │                     │ SQLite            │  │ LLM factory  │  │
        │                     │ seen/saved/pending│  │ Gemma (def.) │  │
        │                     └───────────────────┘  │ Gemini (alt.)│  │
        │                                            └──────┬───────┘  │
        │                                                   │          │
        │                                     ┌─────────────┴────────┐ │
        │                                     ▼                      ▼ │
        │                             ┌────────────────┐   ┌─────────┐ │
        │                             │ OpenRouter API │   │ Gemini  │ │
        │                             │ (gemma:free)   │   │  API    │ │
        │                             └────────────────┘   └─────────┘ │
        └──────────────────────────────────────────────────────────────┘
                     ▲
                     │ HTTP trigger (cron)
                     │
        ┌──────────────────────────────────────────────────────────────┐
        │                         n8n                                  │
        │   7am cron  →  POST /jobs/send-digest  →  (pipeline runs)     │
        └──────────────────────────────────────────────────────────────┘
```

The pipeline itself is six small stages, each replaceable on its own:

```
fetch (5 sources, concurrent)  →  dedup (MD5, 30-day window)
      ↓
hard discard (keyword filter, zero LLM cost)
      ↓
LLM scoring (Gemma via OpenRouter by default, Gemini as fallback — CV + job + my priorities)
      ↓
sort by match_score  →  top N  →  Telegram
```

Job sources currently wired up: **Joblyst**, **RemoteRocketship**, **WeWorkRemotely**, **RemoteOK**, **Arbeitnow**. Each one lives in its own module under [python-service/job_sources/](python-service/job_sources/) and only needs to return a `list[Job]` — the rest of the pipeline doesn't care where the jobs came from.

---

## Tech stack (and why, briefly)

| Layer | Choice | Why |
|-------|--------|-----|
| API | **FastAPI** + `uvicorn` | Async-native, tiny, auto OpenAPI at `/docs`. |
| HTTP client | **httpx** (async) | Plays nicely with the event loop so source fetches run concurrently via `asyncio.gather`. |
| LLM | **Gemma** (default) or **Gemini** — both free tier | The project launched Gemini-only; I added **Gemma via OpenRouter** (`google/gemma-4-31b-it:free`) after Gemini's free tier started throwing sustained 503s. Both providers sit behind the same `LLMClient` interface, so switching is a one-line `LLM_PROVIDER=` flip in `.env`. |
| Storage | **SQLite** | Zero infrastructure. Single file, `sqlite3` from stdlib. Intentional: when this merges into my other project (DailyLifeMate), it'll graduate to Postgres. |
| HTML scraping | **BeautifulSoup** + `lxml` | For the paste-a-URL flow — the LLM extracts the job fields from cleaned page text, so I don't have to maintain a parser per job board. |
| Orchestration | **n8n** (Docker) | Owns the 7am cron and the `/run` webhook — keeps the triggering layer physically separated from the Python service, and gave me hands-on time with n8n. |
| Interface | **Telegram Bot API** (long-polling) | Works from localhost with no public URL. Dead-simple UX: I already live in Telegram. |
| Packaging | **Docker Compose** | One `docker compose up` and the whole stack is running: Python service, n8n, bot. |

Full dependency list: [python-service/requirements.txt](python-service/requirements.txt).

---

## Engineering decisions I'm most proud of

A handful of calls from building this that I'm particularly happy with — both because they solved real problems and because I enjoyed figuring them out.

### 1. Hard discard before the LLM — "zero wasted tokens"

Every job runs through a cheap keyword filter **before** any LLM call. Consulting/outsourcing signals, on-site mandates, relocation requirements — all dropped for free. This is enforced as an invariant: [python-service/pipeline/filters.py](python-service/pipeline/filters.py).

Why it matters: LLM cost scales with the data flow and processing. If you're scoring 300 jobs a day on a free tier, you want to eliminate the 80% of obvious rejects in microseconds, not burn tokens on them.

### 2. Provider-agnostic LLM client — that actually earned its keep

[python-service/llm/base.py](python-service/llm/base.py) defines an abstract `LLMClient` with one method: `generate(prompt) -> str`. The scoring module depends only on that interface. Provider-specific concerns (payload shape, 429/503/timeout retries with independent counters, auth mechanics) are sealed inside each concrete subclass.

**The project started Gemini-only.** That worked fine until Gemini's free tier started returning sustained 503s — sometimes for hours at a time — and the morning digest would reliably fail on the days I most wanted it to work. Rather than paper over it with bigger retry budgets, I added a second provider: **Google Gemma** served through the OpenRouter free tier (model `google/gemma-4-31b-it:free`), a mostly independent availability pool. New subclass, same `LLMClient` interface, zero changes to the pipeline, filters, or scoring prompt.

Selection is done at startup by [python-service/llm/factory.py](python-service/llm/factory.py), which maps the `LLM_PROVIDER` env var (`gemini` or `gemma`) to the right concrete client. Gemma is the current default; Gemini is one `.env` line away.

This is the kind of decision where the abstraction felt over-engineered on day one and paid for itself some time later.

### 3. Fault-tolerant multi-source aggregation

The orchestrator fetches all sources with `asyncio.gather(..., return_exceptions=True)`. If RemoteOK rate-limits or RemoteRocketship returns a 403, the rest of the run continues — the failing source contributes 0 jobs and the incident is logged. One broken board never kills my morning digest. See [python-service/pipeline/orchestrator.py](python-service/pipeline/orchestrator.py).

### 4. A real eval harness for the scoring prompt

This is the piece I'm happiest with. Prompt engineering without measurement is vibes-based development.

[python-service/eval/](python-service/eval/) is a standalone test harness that:

- Runs the live scoring prompt against **15 hand-labelled fixtures** covering edge cases (AI vs ML research, on-site penalty caps, consulting red flags, salary floor, freelance hard-discard, etc.).
- Asserts each fixture lands inside an expected score band.
- Optionally runs an **LLM-as-judge** over each result, rating output quality on seven dimensions (score correctness, AI priority respected, office penalty applied, output concision, etc.).
- Downgrades passes to FAIL if judge quality drops below a threshold — the judge is a real quality gate, not a diagnostic.
- Runs mechanical verbosity + structure checks (max-words per field, exact list lengths).
- Writes a markdown report per run, plus an append-only JSONL history so I can diff prompt versions over time.

Every prompt edit bumps `PROMPT_VERSION` in [python-service/constants.py](python-service/constants.py), and reports are grouped by version. I can answer "did v2.0 actually improve over v1.4?" with data.

Run it via [python-service/eval/run_eval.sh](python-service/eval/run_eval.sh) — one script, works from any directory, auto-detects whether it's inside the dev container (runs Python directly) or on the host (delegates to `docker-compose.eval.yml`):

```bash
./python-service/eval/run_eval.sh                           # full mode (score + judge)
./python-service/eval/run_eval.sh --no-judge                # fast mode (score only)
./python-service/eval/run_eval.sh --fixture f004            # a single fixture
./python-service/eval/run_eval.sh --fixture f004 --no-judge # single fixture, fast
```

Reports are written to `python-service/eval/test_results/` and grouped by prompt version.

**A typical report looks like this** *(excerpt from `v2.0_2026-04-13_080120.md`)*:

<!-- TODO: replace with a screenshot of a recent eval report run -->


```markdown
# Eval Report — v2.0 — 2026-04-13 08:01:20
Mode: **full**

## Summary
- Fixtures run: 15 | Passed: 13 | Failed: 2
- Avg score delta from expected midpoint: 0.50
- Judge avg quality score: 4.2 / 5.0
- Prompt version: v2.0

| ID   | Label                                                        | Expected     | Actual       | Status | Judge |
|------|--------------------------------------------------------------|--------------|--------------|--------|-------|
| f001 | Senior LLM Engineer — 100% remote, strong AI focus           | 8.5–10.0     | 9.5          | ✅     | 5/5   |
| f004 | AI Engineer — hybrid 3 days/week in office (penalty zone)    | 6.0–7.0      | 6.5          | ✅     | 5/5   |
| f008 | IT Consultant — body leasing / outsourcing (hard discard)    | hard_discard | hard discard | ✅     | —     |
| f011 | ML Research Scientist — PhD-level model training (discard)   | < 5.9        | discarded    | ✅     | 5/5   |
| f014 | AI Engineer — salary explicitly below €95k threshold         | < 0.1        | discarded    | ❌     | 1/5   |

### ❌ f014 — Failed fixture detail
**Judge verdict:** The scoring result was not provided, and the system failed to
apply the mandatory salary-based discard rule.
**Judge issues:** Missing scoring result; mandatory salary-based discard
(hard minimum €95k vs €65–80k offer) not triggered.
```

That last row is the kind of thing I'd miss on eyeballing alone, the score band passed, but the judge flagged the discard reason as wrong. This is why the judge acts as a gate, not a diagnostic.

### 5. Restart-safe Telegram buttons

When a digest is sent, each job card carries a 💾 Save button with a `callback_data` payload like `save:<md5>`. Telegram limits `callback_data` to 64 bytes, so the full `Job` object can't fit — instead, the job is upserted into a `pending_jobs` SQLite table keyed by that hash.

If the service restarts overnight, the button still works tomorrow: the callback handler looks up the job by hash, saves it, and removes the pending row. A 14-day TTL prunes stale entries on startup. See [python-service/telegram/bot.py](python-service/telegram/bot.py) and [python-service/storage/database.py](python-service/storage/database.py).

---

## Project layout

```
JobWingman/
├── docker-compose.yml              # full stack: FastAPI + n8n
├── docker-compose.eval.yml         # eval runner (standalone, no services)
├── .env.example                    # copy to .env and fill in
├── python-service/
│   ├── main.py                     # FastAPI controller — routing only, no business logic
│   ├── constants.py                # every magic value lives here
│   ├── models/
│   │   └── job.py                  # canonical Job dataclass used across every stage
│   ├── job_sources/                # one module per source
│   │   ├── arbeitnow.py
│   │   ├── joblyst.py
│   │   ├── remoteok.py
│   │   ├── remoterocketship.py
│   │   ├── weworkremotely.py
│   │   └── url_scraper.py          # paste-a-URL flow (HTML → LLM extraction → score)
│   ├── llm/                        # provider-agnostic client
│   │   ├── base.py                 # LLMClient ABC — single generate() method
│   │   ├── factory.py              # LLM_PROVIDER env → concrete client (dict dispatch)
│   │   ├── gemini/
│   │   │   └── client.py           # Gemini implementation w/ retries
│   │   └── openrouter/
│   │       └── gemma.py            # google/gemma-4-31b-it:free via OpenRouter (default)
│   ├── pipeline/
│   │   ├── orchestrator.py         # fetch → dedup → filter → score → top N
│   │   ├── filters.py              # hard-discard (zero-token pre-filter)
│   │   └── scoring.py              # the big scoring prompt + JSON parsing
│   ├── storage/
│   │   └── database.py             # SQLite: seen_jobs, saved_jobs, pending_jobs
│   ├── telegram/
│   │   ├── bot.py                  # long-polling listener, /run, /list-jobs, URL analysis
│   │   ├── client.py               # send_message wrapper
│   │   └── formatter.py            # digest + single-job card formatting (HTML parse mode)
│   ├── eval/
│   │   ├── run_eval.py             # fixture runner + report writer
│   │   ├── judge.py                # LLM-as-judge
│   │   ├── verbosity.py            # word/structure checks
│   │   ├── fixtures/jobs.json      # 15 labelled fixtures
│   │   └── test_results/           # generated reports + JSONL history
│   └── data/
│       ├── cv.txt                  # loaded once at startup, injected into every prompt
│       └── jobwingman.db           # SQLite file (gitignored)
└── n8n-workflows/                  # exported JSON workflows (cron + webhook)
```

---

## Getting started

You'll need Docker, a Telegram bot token, your Telegram chat ID, and an LLM API key — either an **OpenRouter** key (for the default Gemma provider) or a **Gemini** key.

1. **Clone and copy the env file.**
   ```bash
   git clone <this-repo>
   cd JobWingman
   cp .env.example .env
   ```
2. **Fill in `.env`** with your Telegram bot token (from `@BotFather`), chat ID (from `/getUpdates`), the LLM provider you want (`LLM_PROVIDER=gemma` — the default — or `LLM_PROVIDER=gemini`) and the matching API key (`OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys), or `GEMINI_API_KEY` from [aistudio.google.com](https://aistudio.google.com/app/apikey)), and the n8n webhook URL (`http://n8n:5678/webhook/trigger-digest` is the default).
3. **Drop your CV** as plain text into [python-service/data/cv.txt](python-service/data/cv.txt). It's loaded once at startup and injected into every scoring prompt.
4. **Start the stack.**
   ```bash
   docker compose up --build
   ```
5. **In Telegram**, send the bot any of:
   - `/run` — kick off the pipeline now
   - `/list-jobs` — see everything you've saved
   - paste a job URL — get an instant scored card

### Running the eval suite

One script does everything — it auto-detects host vs container and delegates to `docker-compose.eval.yml` when you're on the host, so you don't need a local Python install:

```bash
./python-service/eval/run_eval.sh                           # full mode (score + judge)
./python-service/eval/run_eval.sh --no-judge                # fast mode (score only)
./python-service/eval/run_eval.sh --fixture f004            # a single fixture
```

Reports land in `python-service/eval/test_results/` grouped by prompt version.

---

## Roadmap

Everything described above runs today — the five job sources with cross-source dedup, the hard-discard pre-filter, the full LLM scoring engine with its eval harness, rich Telegram cards with save buttons, and the paste-a-URL on-demand flow. The whole pipeline is what I use personally, every morning.

**In progress:**

- **LinkedIn as another source** — scraping-based, so it needs care. Current active phase (`feature/add_linkedin`).

Later, when the mood strikes:

- **Merge into DailyLifeMate** — swap SQLite for Postgres and expose the pipeline through my existing C# API and React dashboard.

**Optional:**

- **Deploy to a small VPS** — a 7am cron on a Hetzner box or any other online server, so the digest runs without my laptop being on. Today I trigger it on demand, which covers me fine, so this is a nice-to-have rather than a priority.

---

## Where the interesting bits live

If you're poking around the code, the spots I spent most of my time on — and that are probably the most interesting read — are:

- [python-service/llm/gemini/client.py](python-service/llm/gemini/client.py) and [python-service/llm/openrouter/gemma.py](python-service/llm/openrouter/gemma.py) — the two concrete providers, both with retry logic and independent counters per error type. Selected at startup by [python-service/llm/factory.py](python-service/llm/factory.py) from the `LLM_PROVIDER` env var.
- [python-service/eval/](python-service/eval/) together with the `_SCORING_PROMPT_TEMPLATE` in [python-service/pipeline/scoring.py](python-service/pipeline/scoring.py) — the prompt itself, and the harness that keeps it honest across versions.
- [python-service/telegram/bot.py](python-service/telegram/bot.py) — every external dependency is injected as a callback, zero circular imports.
- [python-service/main.py](python-service/main.py) — deliberately thin FastAPI controller; all pipeline behaviour lives in [python-service/pipeline/](python-service/pipeline/).

Built for myself, because the alternative was losing another Sunday to job boards.
