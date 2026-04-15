# OpenRouter / Gemma — troubleshooting reference

Everything you need when something breaks with the Gemma-via-OpenRouter path.
Pair this with the log line — the body is always printed alongside the hint.

> Log format: `OpenRouter HTTP <code> — <hint> | body: <first 500 chars>`

---

## 1. Quick triage — where does my symptom live?

| Symptom you see | Jump to |
|-----------------|---------|
| HTTP status code in the logs | §2 Status codes |
| `OpenRouterGemmaError` | §3 200-OK-but-broken |
| `httpx.ReadTimeout` / `TimeoutException` | §4 Timeouts |
| `httpx.ConnectError`, DNS, network | §5 Network-level |
| Retries firing forever | §6 Retry behaviour |
| Everything looks fine but the score JSON is malformed | §7 Content / parsing |
| Burning through quota too fast | §8 Rate limits & quota |
| "Where do I look?" | §9 Useful commands |

---

## 2. Status codes

### Retried automatically

| Code | Meaning | What the client does |
|------|---------|----------------------|
| **408** | OpenRouter-side timeout (rare) | Not explicitly retried — our own 60s `ReadTimeout` retry covers the common case |
| **429** | Rate limit — shared `:free` pool OR account quota | Retries 3× with 10s → 20s → 40s back-off |
| **502** | Upstream provider error (e.g. Google AI Studio failed) | Retries 5× with 3s → 6s → 12s → 24s → 48s back-off |
| **503** | No provider available for this model right now | Same policy as 502 |
| **ReadTimeout** | HTTP connection hung | Retries 3× with 60s → 120s → 240s back-off |

### Non-retryable — raised immediately with a hint

| Code | Meaning | Most common cause |
|------|---------|-------------------|
| **400** | Bad request | Wrong model slug, unsupported parameter, empty prompt |
| **401** | Unauthorized | `OPENROUTER_API_KEY` missing, expired, or mistyped |
| **402** | Payment required | Out of credits, or `:free` daily quota hit |
| **403** | Forbidden | Input moderation flagged the prompt |
| **404** | Not found | Model slug does not exist (sometimes surfaced as 400) |

---

## 3. 200 OK but still broken — `OpenRouterGemmaError`

These are the "silent success" traps. The client raises `OpenRouterGemmaError`
so they cannot be mistaken for a valid empty answer.

| Trigger | Message prefix | Likely cause |
|---------|----------------|--------------|
| Body is not valid JSON | `invalid JSON from OpenRouter:` | Gateway returned an HTML error page with 200 status |
| JSON has a top-level `error` object | `upstream error (code=…):` | Provider rejected but OpenRouter forwarded as 200 |
| `choices[0].message.content` is empty | `empty content from model (finish_reason=…)` | Silent moderation, provider cold-start, or token budget = 0 |

`finish_reason` is the single most useful clue when content is empty:

| `finish_reason` | What it means |
|-----------------|---------------|
| `length` | Hit `max_tokens` before the first token. Raise `OPENROUTER_MAX_OUTPUT_TOKENS`. |
| `content_filter` | Provider blocked the response. Rework the prompt. |
| `stop` with empty content | Model genuinely produced nothing. Retry — often a cold start. |
| `error` | Provider-side error during streaming. Check OpenRouter status page. |

---

## 4. Timeouts

- Budget: `OPENROUTER_TIMEOUT_SECONDS = 60` per request, 3 retries → worst-case 60+120+240 = **7 minutes** before giving up.
- If you see a `ReadTimeout` on every attempt: the prompt is likely too long (CV + job description + instructions). Check `len(prompt)` — Gemma starts to slow down above ~30k chars.
- A timeout on the **first** attempt that succeeds on retry is normal cold-start behaviour for `:free` providers.

---

## 5. Network-level failures

These bypass OpenRouter entirely — they mean our container couldn't even reach it.

| Exception | Most likely cause |
|-----------|-------------------|
| `httpx.ConnectError` | No outbound internet in the container. Check `docker compose` network config. |
| `httpx.ConnectTimeout` | Firewall or DNS black-holing `openrouter.ai`. |
| `httpx.RemoteProtocolError` | Proxy or VPN breaking HTTP/2. Try setting `httpx.AsyncClient(http2=False)` if this recurs. |
| `ssl.SSLCertVerificationError` | System CA bundle out of date. `apt-get install ca-certificates` inside the container. |

Sanity test from inside the container:
```bash
curl -sS https://openrouter.ai/api/v1/auth/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY"
```
If this fails with the same error, it's environmental — not our code.

---

## 6. Retry behaviour

| Error type | Counter | Retries | Base delay | Total worst case |
|------------|---------|---------|------------|------------------|
| 429 | `attempts_429` | 3 | 10s | 70s + retries |
| 502/503 | `attempts_503` | 5 | 3s | 93s + retries |
| Timeout | `attempts_timeout` | 3 | 60s | 7 minutes |

Counters are **independent** — each error type has its own budget. A call
that alternates 429 ↔ 503 will *not* quickly exhaust either; that is a
deliberate trade-off so one type of flake can't kill a request.

Tuning knobs (in `constants.py`):
- `OPENROUTER_MAX_RETRIES`, `OPENROUTER_RETRY_BASE_DELAY`
- `OPENROUTER_503_MAX_RETRIES`, `OPENROUTER_503_RETRY_BASE_DELAY`
- `OPENROUTER_TIMEOUT_SECONDS`
- `OpenRouterGemmaClient._TIMEOUT_MAX_RETRIES` (class-level, not a constant)

---

## 7. Content / parsing issues

If HTTP is fine but the scoring pipeline rejects the result:

- **Truncated JSON (missing closing brace)** — `finish_reason: length`. Raise `OPENROUTER_MAX_OUTPUT_TOKENS`.
- **Response wrapped in ```` ```json ```` fences** — Gemma does this more often than Gemini. The pipeline's JSON parser should strip fences; if not, add stripping in `pipeline/scoring.py`.
- **Non-deterministic output between runs** — `temperature` is set to `0.2` in `generate()`. If determinism matters, lower to `0.0`.
- **Model hallucinates fields not in the schema** — tighten the prompt; Gemma is less strict about schemas than Gemini 3.x.

---

## 8. Rate limits & quota

### Reading a 429 body (the important one for `:free` models)

OpenRouter nests the real reason under `error.metadata`:
```json
{
  "error": {
    "message": "Provider returned error",
    "code": 429,
    "metadata": {
      "raw": "google/gemma-4-31b-it:free is temporarily rate-limited upstream...",
      "provider_name": "Google AI Studio",
      "is_byok": false
    }
  }
}
```

| Field | What it tells you |
|-------|-------------------|
| `is_byok: false` | Rate limit is on the **shared** free pool. Remedy: BYOK (see §10). |
| `is_byok: true` | Rate limit is on **your own** Google key. Remedy: wait or upgrade tier. |
| `provider_name` | Which upstream throttled you. For `google/gemma-*` normally `Google AI Studio`. |

### `:free` tier limits (as of 2026)

| Condition | Limit |
|-----------|-------|
| OpenRouter account-level | ~20 requests/minute |
| Account with < $10 credits ever | 50 `:free` requests/day |
| Account with ≥ $10 credits ever | 1000 `:free` requests/day |
| Shared pool throttling (Google AI Studio) | Unpublished, varies by time of day |

Daily counters reset at UTC midnight.

### Check live usage

```bash
curl -sS https://openrouter.ai/api/v1/auth/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | jq
```

Returns your current `usage`, `limit`, and whether BYOK is active.

---

## 9. Useful commands & log grep patterns

Run the connectivity test:
```bash
cd python-service && python -m llm.test_openrouter_connectivity
```

Tail logs for OpenRouter-only events:
```bash
docker compose logs -f python-service | grep -i openrouter
```

Grep patterns worth remembering:
- `grep "OpenRouter HTTP"` — every non-2xx response (includes the hint)
- `grep "retry .* in .*s"` — every retry attempt (shows the back-off schedule firing)
- `grep "exhausted all"` — final give-up line (one per failed request)
- `grep "upstream error"` — 200-OK errors that were translated to `OpenRouterGemmaError`
- `grep "empty content"` — silent empty responses
- `grep "LLM client ready"` — confirms client instantiated at startup

Force-test a specific status without touching code (from inside the container):
```bash
# 401
curl -sS https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer wrong" -d '{}'

# 400 (bad model)
curl -sS https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -d '{"model":"nope","messages":[{"role":"user","content":"hi"}]}'

# Live smoke
curl -sS https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -d '{"model":"google/gemma-4-31b-it:free","messages":[{"role":"user","content":"pong"}]}'
```

---

## 10. Recovery cheat-sheet

| Symptom | First thing to try |
|---------|--------------------|
| Persistent 429 with `is_byok: false` | Add your own Google AI Studio key on OpenRouter (see `BYOK_SETUP.md`) |
| Persistent 401 | Regenerate `OPENROUTER_API_KEY` and restart the container |
| Persistent 402 | Add $10 in credits on OpenRouter, or wait for the daily `:free` reset (UTC midnight) |
| Bursty 502/503 | No action needed — back-off handles it. If it persists for minutes, upstream is down; check <https://status.openrouter.ai> |
| `OpenRouterGemmaError: empty content` | Retry; likely cold start. If it repeats, tighten or simplify the prompt |
| `OpenRouterGemmaError: upstream error` | The body's `code` and `message` are the real diagnosis — use them verbatim when searching |
| Truncated JSON / `finish_reason: length` | Raise `OPENROUTER_MAX_OUTPUT_TOKENS` in `constants.py` |
| Timeout on every attempt | Prompt is too large, or provider is slow — shorten prompt, or raise `OPENROUTER_TIMEOUT_SECONDS` |

---

## 11. Where to look in the code

| Concern | File |
|---------|------|
| Request, retry, error extraction | `llm/openrouter.py` |
| Tunable limits / timeouts / delays | `constants.py` (§ *OpenRouter LLM*) |
| Abstract interface (what scoring depends on) | `llm/base.py` |
| Which client is actually used in prod | `main.py` — look for `GeminiClient(...)` / `OpenRouterGemmaClient(...)` |
| Connectivity test | `llm/test_openrouter_connectivity.py` |
