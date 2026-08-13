/**
 * Central error journal + "ask the agent about this error" hand-off channel.
 *
 * ## Why a journal instead of a prop
 *
 * Error text is rendered ad-hoc in ~80 files (`setError(e.message)` into a local
 * red `<div>`), so the *string* is everywhere but the *context* — HTTP status,
 * failing endpoint, the backend's machine-readable `code` — is thrown away at
 * the call site. Threading a structured error object through 80 components is a
 * migration; recording it once at the transport is not.
 *
 * `api/client.ts`'s `j`/`jNullable` are the single chokepoint every dashboard
 * API error flows through, so they record here. A UI that only has the message
 * string can then call {@link findReport} to recover the full context by exact
 * message match, which is what lets a shared error banner offer a
 * context-carrying "ask the agent" button with **zero** changes at the call
 * site.
 *
 * ## Redaction
 *
 * The report is fed to an LLM prompt, so it must not carry credentials:
 *  - the recorded endpoint is **path-only** — a `?token=…` query string (the
 *    dashboard's own auth hand-off shape) is dropped, never journaled;
 *  - free-form text (response bodies, component stacks) goes through
 *    {@link redactSecrets} before it is stored.
 *
 * Nothing here imports React or the store, so the transport can record without
 * pulling the UI graph into `api/client.ts`.
 */

import { safeSetSessionItem } from './safeStorage'

/** Where the error was observed. */
export type ErrorSource =
  /** Non-2xx from an `api.*` call (recorded by `j`/`jNullable`). */
  | 'api'
  /** A React render/lifecycle throw caught by an ErrorBoundary. */
  | 'render'
  /** An uncaught error or unhandled rejection on `window`. */
  | 'window'

export interface ErrorReport {
  id: string
  /** Epoch ms. */
  at: number
  source: ErrorSource
  /** Human message exactly as the UI shows it — also the {@link findReport} key. */
  message: string
  /** HTTP status, for `source: 'api'`. */
  status?: number
  /**
   * Machine-readable `code` from a JSON error body. Backend-owned error bodies
   * carry one by convention (AGENTS.md § User-facing strings) precisely so a
   * client can act on the failure instead of regex-matching prose.
   */
  code?: string
  /** Failing request path, query string stripped. */
  endpoint?: string
  /**
   * Route the user was on when it happened. **Path only** — see
   * {@link currentRoute} for why the query string is dropped.
   */
  route: string
  /** Raw body / component stack — redacted and capped. */
  detail?: string
}

/** Journal depth. Deep enough to survive a burst of retries, shallow enough to stay cheap. */
export const MAX_JOURNAL = 20
/** Per-report cap on free-form detail, so a 1MB HTML error page can't bloat a prompt. */
export const MAX_DETAIL = 1200
/**
 * Cap on the message.
 *
 * `friendlyErrText` returns a non-JSON body **verbatim**, so `message` can be a
 * whole HTML error page rather than a sentence. Capping keeps that out of both the
 * prompt and the scrubber's input.
 */
export const MAX_MESSAGE = 2000
/** sessionStorage channel ChatPage drains to seed the composer. */
export const ERROR_HANDOFF_KEY = 'kirocrew_error_handoff'
/** Hand-off TTL. Long enough to survive a full reload, short enough not to ambush a later visit. */
export const HANDOFF_TTL_MS = 60_000

/**
 * Blank out credential-shaped substrings.
 *
 * Deliberately narrow: over-redaction destroys the diagnostic value that is the
 * whole point of attaching the detail. Mirrors the backend's AKIA/ASIA rule and
 * adds the assignment forms (`token=…`, `"authorization": "…"`) that show up in
 * echoed request bodies and config blobs.
 */
const REDACTED = '[redacted]'
const REDACTED_ACCESS_KEY = '[redacted-access-key]'
const TRUNCATION_SUFFIX = '\n[truncated]'

export function redactSecrets(text: string): string {
  return text
    .replace(/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, REDACTED_ACCESS_KEY)
    // The scheme word goes with the credential: less echoed, nothing lost.
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*/gi, REDACTED)
    // URL userinfo — `https://x-access-token:<PAT>@github.com/org/repo.git` is
    // the shape a git/registry remote error echoes back verbatim. Both halves
    // go: the username is itself a credential in several token schemes. The
    // character class cannot cross `/`, so an ordinary URL has no match.
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, (_m, scheme: string) => `${scheme}${REDACTED}@`)
    // key=value / "key": "value" for credential-ish key names. The optional
    // quote after the name is what makes the JSON form ("token": "…") match.
    // The `[\w-]{0,32}` wings catch the prefixed/suffixed forms: `_` is a word
    // character, so a bare `\btoken\b` does NOT match `access_token`.
    //
    // The leading boundary is a CAPTURED GROUP, not a lookbehind, and that is a
    // compatibility requirement rather than a style choice. A regex literal is
    // parsed when the module is parsed, so an unsupported construct is an early
    // SyntaxError that takes the whole bundle down — not a failed match. Safari
    // only gained lookbehind in 16.4, and this module is reachable from
    // `api/client.ts` on the dashboard's first paint, so on 16.3 the entire
    // dashboard would render blank. The captured boundary is re-emitted by the
    // replacer, so the matched text is preserved byte-for-byte.
    //
    // The wings are BOUNDED, and that bound is load-bearing, not cosmetic.
    // Unbounded (`[\w-]*`) they made this quadratic in input length: the engine
    // retries the wing from every start position, so a body that is one long run
    // of word characters blew up to 23s at 500KB and ~6min at 2MB — a frozen main
    // thread. This regex sees whole response bodies (`friendlyErrText` returns a
    // non-JSON body verbatim), so that input is reachable, not theoretical. A
    // credential key name is short; 32 is far past any real one and keeps the
    // scan linear (2MB in ~5ms).
    .replace(
      /(^|[^\w-])([\w-]{0,32}(?:token|secret|passwd|password|api[-_]?key|session[-_]?key|authorization|credentials?)[\w-]{0,32})("?\s*[:=]\s*"?)([^\s"',;&}]{6,})/gi,
      (_m, before: string, key: string, sep: string) => `${before}${key}${sep}${REDACTED}`,
    )
}

/**
 * Scrub, THEN cap. The order is security-critical, not a style choice.
 *
 * Every pattern in {@link redactSecrets} needs a trailing anchor to fire: the
 * userinfo rule needs its `@`, `Bearer` needs 12+ token characters, `AKIA` needs
 * its full 16. Capping first can drop that anchor and leave the credential's head
 * sitting in the kept prefix — `https://x-access-token:<PAT>` with the `@` on the
 * discarded side no longer matches anything, and that prefix is what reaches the
 * LLM prompt. Scrubbing the whole string first has no such cut to exploit.
 *
 * The cost of scrubbing before capping is bounded: the regex is deliberately
 * linear (see the wing bound above — 2MB in ~5ms), so a multi-megabyte body is
 * milliseconds, not the frozen main thread the quadratic version produced.
 */
function redactThenCap(text: string, max: number): string {
  const redacted = redactSecrets(text)
  return redacted.length > max ? redacted.slice(0, max) + TRUNCATION_SUFFIX : redacted
}

/** Trim + scrub + cap free-form detail. Returns undefined for nothing worth keeping. */
function normalizeDetail(detail: string | undefined): string | undefined {
  if (!detail) return undefined
  const trimmed = detail.trim()
  if (!trimmed) return undefined
  return redactThenCap(trimmed, MAX_DETAIL)
}

/**
 * Reduce a request URL to a journalable path.
 *
 * Query strings are dropped wholesale rather than filtered: the dashboard's own
 * auth hand-off is `?token=…`, so an allowlist here would be one forgotten
 * parameter away from journaling a session token into an LLM prompt.
 */
export function requestPath(url: string | undefined): string | undefined {
  if (!url) return undefined
  try {
    return new URL(url, window.location.origin).pathname
  } catch {
    return url.split('?')[0] || undefined
  }
}

/** Pull the backend's machine-readable `code` out of a JSON error body, if present. */
export function parseErrorCode(body: string | undefined): string | undefined {
  if (!body) return undefined
  const trimmed = body.trim()
  if (!trimmed.startsWith('{')) return undefined
  try {
    const parsed = JSON.parse(trimmed) as { code?: unknown }
    return typeof parsed.code === 'string' && parsed.code ? parsed.code : undefined
  } catch {
    return undefined
  }
}

// Newest first. Plain module state: the journal is per-tab, per-page-load
// diagnostic scratch — persisting it would resurrect stale errors as if fresh.
let _journal: ErrorReport[] = []
const _listeners = new Set<(reports: ErrorReport[]) => void>()
let _seq = 0

/**
 * The current route, **path only**.
 *
 * The query string is dropped for the same reason {@link requestPath} drops it,
 * and it matters more here: the dashboard's own auth hand-off is `/?token=<…>`,
 * and the token is stripped from the URL by an effect — so an API failure that
 * lands before that effect runs would otherwise journal a live session token
 * into a report that gets fed to an LLM. `route` is also the one field
 * `recordError` does not pass through `redactSecrets`, so it has to be safe by
 * construction rather than by scrubbing.
 */
function currentRoute(): string {
  try {
    return window.location.pathname
  } catch {
    return ''
  }
}

/** Record an observed error. Returns the stored report (with redaction applied). */
export function recordError(input: {
  source: ErrorSource
  message: string
  status?: number
  code?: string
  endpoint?: string
  detail?: string
  route?: string
}): ErrorReport {
  const report: ErrorReport = {
    id: `err-${Date.now().toString(36)}-${(_seq += 1).toString(36)}`,
    at: Date.now(),
    source: input.source,
    message: redactThenCap(input.message, MAX_MESSAGE),
    status: input.status,
    code: input.code,
    endpoint: input.endpoint,
    route: input.route ?? currentRoute(),
    detail: normalizeDetail(input.detail),
  }
  _journal = [report, ..._journal].slice(0, MAX_JOURNAL)
  for (const fn of _listeners) {
    try { fn(_journal) } catch { /* a bad subscriber must not break error recording */ }
  }
  return report
}

/** Newest-first snapshot of the journal. */
export function recentErrors(): ErrorReport[] {
  return _journal
}

/**
 * Recover the structured report behind a bare message string.
 *
 * The upgrade path for the ~80 existing `setError(e.message)` call sites: the
 * shared banner is handed only the string it always had, and looks the context
 * back up here. Exact match, newest first — an identical message from an older
 * request describes the same failure well enough to prompt with.
 */
export function findReport(message: string | null | undefined): ErrorReport | undefined {
  if (!message) return undefined
  const needle = redactSecrets(message).trim()
  if (!needle) return undefined
  return _journal.find(r => r.message.trim() === needle)
}

/** Subscribe to journal changes. Returns an unsubscribe. */
export function subscribeErrors(fn: (reports: ErrorReport[]) => void): () => void {
  _listeners.add(fn)
  return () => { _listeners.delete(fn) }
}

/** Test seam — the journal is module state, so tests need a way back to zero. */
export function __resetErrorJournalForTests(): void {
  _journal = []
  _seq = 0
  _listeners.clear()
}

// `buildErrorPrompt` deliberately lives in `errorReport.prompt.ts` (the
// `*.prompt.ts` i18n boundary for model-facing text) and is NOT re-exported
// here: that module imports `redactSecrets` from this one, and a re-export would
// close the import graph into a runtime cycle. Import it from
// `./errorReport.prompt` directly.

/**
 * Stage a prompt for the chat composer.
 *
 * sessionStorage rather than a Redux dispatch because the most valuable caller
 * is the root ErrorBoundary: when the React tree has already thrown, the store
 * and router may be exactly what is broken, so the hand-off has to survive a
 * hard `location.assign`. Same channel serves the healthy in-app path, so there
 * is one code path to reason about instead of two.
 */
export function handoffToChat(prompt: string): void {
  safeSetSessionItem(ERROR_HANDOFF_KEY, JSON.stringify({ prompt, ts: Date.now() }))
}

/** Drain the hand-off channel. Returns the prompt, or null when absent/stale. */
export function consumeChatHandoff(): string | null {
  let raw: string | null = null
  try {
    raw = sessionStorage.getItem(ERROR_HANDOFF_KEY)
    if (raw !== null) sessionStorage.removeItem(ERROR_HANDOFF_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const { prompt, ts } = JSON.parse(raw) as { prompt?: unknown; ts?: unknown }
    if (typeof prompt !== 'string' || !prompt) return null
    if (typeof ts !== 'number' || Date.now() - ts > HANDOFF_TTL_MS) return null
    return prompt
  } catch {
    return null
  }
}

// ── Soft-navigation seam ─────────────────────────────────────────────────────
//
// The error hand-off has to work from an ErrorBoundary fallback, and a boundary
// is precisely where the router and store may be the thing that threw — so the
// button that triggers it cannot be allowed to require either. Instead App.tsx
// installs its router `navigate` here while the app is healthy; the hand-off
// uses it when present and falls back to a full page load when it is not.
//
// This is the same shape as `utils/artifactPopout`'s nav-intent handler: an
// imperative seam so non-page modules can navigate without importing a page.

let _softNavigate: ((to: string) => void) | null = null
const _handoffListeners = new Set<() => void>()

/** Install (or clear, with `null`) the in-app navigator. Called by App.tsx. */
export function installSoftNavigate(fn: ((to: string) => void) | null): void {
  _softNavigate = fn
}

/**
 * Subscribe to hand-offs raised while you are already mounted.
 *
 * ChatPage drains the channel on mount, which covers arriving from elsewhere.
 * It also needs this: an error surface *inside* chat (a message that failed to
 * render) hands off without any route change, so nothing would remount.
 */
export function subscribeChatHandoff(fn: () => void): () => void {
  _handoffListeners.add(fn)
  return () => { _handoffListeners.delete(fn) }
}

/**
 * Stage the prompt and get the user to the composer.
 *
 * `hard: true` forces a full page load — for the root ErrorBoundary, where a
 * soft navigation would re-render the very tree that just threw.
 */
export function sendErrorToChat(prompt: string, opts: { hard?: boolean } = {}): void {
  handoffToChat(prompt)
  if (!opts.hard) {
    // Notify before navigating: an already-mounted ChatPage drains here, and a
    // not-yet-mounted one drains on mount instead.
    for (const fn of _handoffListeners) {
      try { fn() } catch { /* a bad subscriber must not strand the hand-off */ }
    }
    if (_softNavigate) {
      _softNavigate('/chat')
      return
    }
  }
  try { window.location.assign('/chat') } catch { /* nothing left to try */ }
}

/** Test seam — the seam is module state. */
export function __resetNavSeamForTests(): void {
  _softNavigate = null
  _handoffListeners.clear()
}
