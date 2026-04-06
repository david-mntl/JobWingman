# JobWingman — Job Intelligence Agent

> Senior engineering partner context. Read this file at the start of every session.

---

## Collaboration Rules (apply to every task, every session)

### Rule 1 — Constants over magic values
Never use raw strings, numbers, or repeated literals inline. Every constant (URLs, paths, thresholds, config values) must be defined as a named constant at the top of the file or in a dedicated `constants.py` module. Name must make the intent obvious (`TELEGRAM_API_BASE`, `MIN_MATCH_SCORE`, `CV_PATH`).

### Rule 2 — Teach by doing
For every file created or modified, provide a full explanation of:
- What the file/function/class does and why it exists
- Why a specific library was chosen over alternatives
- Why the code is structured the way it is
The goal is that David can read the explanation and fully understand the decision, not just accept the output.

## What We're Building
Standalone Python project that automatically finds, scores, and delivers AI/backend engineering jobs via Telegram daily, plus manual job URL analysis on demand.
Designed to later merge into DailyLifeMate as a module.

---

## Current Status
**Active Phase: 1 — First Source + Dedup** (in progress)
~~Phase 0 — Foundation: complete~~

---

## Stack (locked — no re-discussion needed)
| Layer | Choice | Notes |
|-------|--------|-------|
| Orchestration | n8n (self-hosted, Docker) | |
| AI logic | Python FastAPI | |
| Storage | SQLite | Swap to PostgreSQL when merging with DailyLifeMate |
| LLM | Gemini free tier / Claude | Decide per phase |
| Hosting | Local → Hetzner VPS | VPS after Phase 4 |
| Interface | Telegram bot | Built from scratch |

---

## Project Layout
```
JobWingman/
├── docker-compose.yml
├── .env                    # never commit — copy from .env.example
├── .env.example
├── python-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   └── data/
│       └── cv.txt          # full CV — loaded at startup, injected into every prompt
└── n8n-workflows/          # exported workflow JSON files
```

---

## My Profile (David — for job scoring context)
- Senior Backend Engineer, 5+ years production
- Primary: C#/.NET, distributed systems, microservices, gRPC, RabbitMQ, Docker
- AI/agent: DailyLifeMate — Architect/Executor multi-agent, Gemini + Claude + OpenAI, LlamaIndex
- Secondary: Python (intermediate), TypeScript, React, Java
- Target roles: AI Engineer / LLM Engineer / Agentic Systems Engineer / Backend AI-focused
- Location: Berlin, Germany — open to full remote EU or worldwide
- 4 years proven 100% remote

---

## Scoring Rules

### Hard Discard (pre-LLM, no cost)
Discard if ALL of:
- Title/description contains: "consultant", "outsourcing", "staff augmentation", "body leasing", "loaned to client" AND company has no own product
- OR requires 100% on-site OR requires relocation

### LLM Scoring Output (JSON)
```json
{
  "match_score": 7.5,
  "salary_signal": "No range posted — estimated €85-100k based on company size and role seniority",
  "red_flags": ["No salary range published", "Mentions client-facing work"],
  "green_flags": ["4-day week", "100% remote", "Own product", "Agent/LLM focus"],
  "fit_breakdown": {
    "strong": ["Distributed systems", "Multi-agent architecture", "Docker"],
    "gaps": ["Python depth — they want senior, you have intermediate"]
  },
  "company_snapshot": "3-sentence description.",
  "role_summary": ["bullet 1", "bullet 2", "bullet 3"],
  "company_benefits": ["job bike", "train ticket"]
  "verdict": "1-sentence honest recommendation"
}
```
**Hard discard if match_score < 6 — never shown to user.**

### Green Flags (boost score)
- 4-day work week — always flag ⭐ explicitly
- 100% remote or remote + 1 month abroad
- Learning budget, own product, agent/LLM focus
- Low-ego culture, worker wellbeing, equity/ESOP

### Red Flags (lower score, flag — don't auto-discard)
- No salary range → flag ⚠️, estimate from context
- Pure ML/data science, AI-washing, vague remote, "fast-paced startup" fluff

---

## Deduplication
- Hash = MD5(normalized company + title)
- SQLite `seen_jobs` table, 30-day expiry
- Multi-source: process each job exactly once
- Built in Phase 1 — not deferred

---

## Telegram Output Format

**Daily digest:**
```
🤖 Good morning David — X new jobs worth your attention

━━━━━━━━━━━━━━━━━━━━━
1. [Role] — [Company] ([Location])
⭐ 4-day week  🏠 Full remote  📈 X.X/10 match  🎯 high confidence
⚠️ No salary posted (est. €XX-XXk)
🟢 [top green flag]
🔴 [top red flag if any]

📝 Role: bullet 1 · bullet 2 · bullet 3
🏢 [3-sentence company snapshot]
✅ Strong: skill1, skill2 | ⚡ Gaps: gap1, gap2
🎁 benefit1, benefit2

💬 [1-sentence verdict]
🔗 View posting
━━━━━━━━━━━━━━━━━━━━━

📊 Today: X scanned → X passed → X worth your time
```

**Manual URL flow:**
```
You: [paste URL]
Bot: 🔍 Analyzing...
     [same card format]
     [✅ Interested] [❌ Skip]
```

---

## Phase Roadmap
| Phase | Name | Goal | Est. |
|-------|------|------|------|
| **0** | Foundation | n8n + FastAPI + Telegram "hello" message | 2-3h |
| 1 | First Source + Dedup | Remotive API, SQLite dedup, basic scoring, top 3 in Telegram | 3-4h |
| 2 | Full Scoring Engine | Full LLM prompt, JSON output, hard discard, rich Telegram format | 3-4h |
| 2.5 | Eval Layer | eval_labels table, prompt versioning, weekly metrics report | 2h |
| 3 | More Sources | WeWorkRemotely, RemoteOK, HN Who's Hiring, cross-source dedup | 3-4h |
| 4 | Daily Digest + Deploy | 7am cron, batch digest, deploy to Hetzner | 2h |
| 5 | Manual URL Flow | Bot listens for URLs, /analyze-url, fallback to raw text | 2-3h |
| 6 | Storage + Pipeline | `jobs` table, /pipeline command | 1-2h |
| 7 | Risky Sources | LinkedIn, Welcome to the Jungle, WorkingNomads | 2-3h |

**Job Sources by Phase:**
- Phase 1: Arbeitnow API (free, EU-focused, no auth required)
- Phase 3+: Sources will be evaluated when we arrive — candidates include WeWorkRemotely RSS, RemoteOK API, HN Who's Hiring, Wellfound
- Phase 7: LinkedIn, Welcome to the Jungle, WorkingNomads (risky/scraping — evaluated at that phase)

---

## Eval Layer (Phase 2.5)
- `eval_labels` SQLite table — every Interested/Skip button press = ground truth
- Every scoring prompt has a version tag; scores tagged with prompt version
- Weekly Sunday script: Precision > 70%, Recall > 80%, worst mistakes → Telegram report
- Enables old vs new prompt comparison on same labeled dataset

---

## Key Invariants (never break)
- `cv.txt` loaded once at startup, injected into every prompt — never hardcode inline
- Deduplication in Phase 1 — never deferred
- Hard discard runs before LLM — zero wasted tokens
- match_score < 6 → job never reaches user

---

## Future: Merge into DailyLifeMate (after Phase 6)
- Add python-service to DailyLifeMate docker-compose
- Swap SQLite → existing PostgreSQL
- Expose data via C# API
- Add React dashboard tab
