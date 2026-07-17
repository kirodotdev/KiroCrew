# Native Prompt Optimizer — Design

**Author:** yohaseti  
**Date:** 2026-05-07 (updated 2026-05-08)  

---

## Overview

A prompt optimizer triggered via **Cmd+Shift+Enter** keyboard shortcut or a **sparkle button (✨)** in the input toolbar. Rewrites vague prompts to be more specific, scoped, and actionable before the user sends them to the agent.

## UX Flow

1. User types prompt in dashboard textarea
2. User presses **Cmd+Shift+Enter** (Mac) / **Ctrl+Shift+Enter** (Linux), or clicks ✨
3. Dark overlay appears: "✨ Optimizing prompt…" — textarea becomes readOnly + dimmed
4. Backend calls kirocrew-lite with optimizer system prompt
5. Optimized text replaces textarea content — user reviews before sending
6. On error or unchanged → original text restored silently
7. User presses **Enter** to send (no auto-send)

## Architecture

```
KiroCrewWebsite (React)              KiroCrew Gateway (Python)
───────────────────────              ────────────────────────
ChatInput.tsx                        POST /api/optimizer/optimize
  ├─ handleKeyDown                    ├─ Empty check only (no skip logic)
  │   └─ Cmd+Shift+Enter             ├─ Build prompt with <original_prompt> tags
  │       → fetch(/api/optimizer)     ├─ Include context (truncated to 2000 chars)
  │       → dark overlay + readOnly   ├─ asyncio.wait_for(30s) wraps ENTIRE operation:
  │       → user reviews & sends      │   ├─ get_or_create("_optimizer", agent="kirocrew-lite")
  │                                   │   ├─ Stream response, reject tool calls
  ├─ Sparkle button onClick           │   └─ release("_optimizer") in finally
  │   → same fetch logic              ├─ Detect "UNCHANGED" → return original
  │                                   ├─ Strip quotes from LLM response
  └─ optimizingRef (useRef)           ├─ Redact credentials + exfiltration URLs
      → prevents concurrent requests  └─ Return {optimized, changed}
```

**Key design choice:** The optimizer uses a **dedicated `_optimizer` session key** — completely independent from the shared `_bg` session used by title generation and folder categorization. This eliminates semaphore contention that previously caused 30s timeouts.

## Optimizer Rules

The system prompt follows these rules (earlier wins on conflict):

1. **Never** change user intent, add requirements, or invent values
2. If the prompt is already specific, scoped, and actionable — return it unchanged
3. When rewriting, add what the user skipped: scope, constraints, structure, uncertainty handling
4. Replace hedging with direct verbs
5. Add "preserve existing behavior" when modifying existing work
6. Never exceed min(3× original length, 250 words)

**No client-side skip logic.** Since the optimizer is an explicit user action (✨ button or Cmd+Shift+Enter), all prompts go through the LLM. The LLM's own judgment (rule 2) handles the "don't over-optimize" case.

## Frontend — ChatInput.tsx

**Keyboard shortcut** (inside `handleKeyDown` useCallback):
- Checks `e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey`
- Guards with `optimizingRef.current` (useRef) to prevent stale closure + concurrent requests

**Sparkle button** (inline onClick):
- Same fetch logic, same `optimizingRef` guard
- Shows `<Loader2 className="animate-spin" />` while optimizing, `<Sparkles />` when idle
- `disabled={optimizing}` prevents double-clicks

**Loading UX:**
- Dark overlay (`bg-black/60`) with "✨ Optimizing prompt…" text
- Textarea gets `opacity-30` + `readOnly` during optimization
- Original text stays visible underneath the overlay

**Design decisions:**
- `optimizingRef` (useRef) is the guard source of truth — `optimizing` state drives UI only
- No auto-send — user reviews the rewritten text first
- `credentials: 'same-origin'` + `x-session-key: 'dashboard:ui'` header for auth
- Sends conversation context (last ~10 messages, truncated by backend to 2000 chars)

## Backend — optimizer.py

Single endpoint `handle_optimize(request)`:
- Only guard: empty prompt → return unchanged immediately (no LLM call)
- **30s `asyncio.wait_for` wraps entire operation** (get_or_create + stream + release)
- Uses **dedicated `_optimizer` session key** with `agent="kirocrew-lite"` — own process, own semaphore
- Rejects any tool permission requests during streaming
- `try/finally` ensures `state.sessions.release(OPTIMIZER_SESSION_KEY)` even on stream errors
- Explicit `asyncio.TimeoutError` handling returns original prompt gracefully
- Strips surrounding quotes from LLM response
- Detects literal "UNCHANGED" → returns original
- Security: passes output through `redact_exfiltration_urls()` + `redact_credentials()`
- Truncates context to last 2000 chars
- Debug logging at acquire/stream/release points

### Why dedicated session?

The previous approach shared `BACKGROUND_KEY` (`_bg`) with title generation and folder categorization. All three used a single `Semaphore(1)`, causing the optimizer to queue behind title gen (3-8s) + folder gen (2-5s) on every message. With a dedicated `_optimizer` session, response time dropped from 8-30s to 2-5s consistently.

**Cost:** One extra kirocrew-lite process (~30MB RAM).  
**Benefit:** Optimizer latency 2-5s instead of 8-30s (or timeout).

## Testing

| Package | Tests | Coverage |
|---------|-------|----------|
| KiroCrew (backend) | 18 pytest tests | empty prompt, LLM unchanged/optimized, short prompts reach LLM, errors, quote stripping, context truncation |
| KiroCrewWebsite (frontend) | 6 vitest tests | API contract, auth header, error handling, integration |

### Local verification (9 scenarios)

| Scenario | Result | Time |
|----------|--------|------|
| Vague prompt | Optimized with scope + constraints | 2.7s |
| Already-specific | Passed through unchanged | 2.6s |
| Compound task | Broke into numbered steps | 5.2s |
| Long input (5000+) | No overflow/timeout | 3.2s |
| Noisy context + short prompt | Didn't hallucinate from noise | 2.3s |
| Context adds specificity | Used context to scope the fix | 2.3s |
| Yes/no response | LLM correctly left unchanged | 2.3s |
| Empty prompt | Fast-path, no LLM call | 0.0s |
| 3 concurrent requests | All completed, no contention | 7.6s total |

## Alternatives Considered

| Approach | Why Not |
|----------|---------|
| Auto-optimize all prompts via hook | Adds latency to every message, even good ones |
| `//` prefix convention | Relies on agent self-optimizing, inconsistent |
| React Query useMutation | Overkill for 2 call sites, matches existing pattern |
| Shared `_bg` session (original approach) | Semaphore contention with title/folder gen → 30s timeouts |
| Client-side skip logic (`_SKIP_PHRASES`) | Wrong for explicit user action — user pressed ✨, respect that |
