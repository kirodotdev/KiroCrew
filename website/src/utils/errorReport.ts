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
import { errMessage } from './thunkError'
import { withBase } from '@/lib/basePath'

/** Where the error was observed. */
export type ErrorSource =
  /** Non-2xx from an `api.*` call (recorded by `j`/`jNullable`). */
  | 'api'
  /** A React render/lifecycle throw caught by an ErrorBoundary. */
  | 'render'
  /** An uncaught error or unhandled rejection on `window`. */
  | 'window'
  /**
   * A subsystem the backend reports as broken inside a SUCCESSFUL response — a
   * remote crew whose tunnel is down, carried in a 200 status poll. Distinct
   * from `api` because no request failed: publishing it as `api` would name a
   * failing endpoint that in fact answered, sending a reader to audit the wrong
   * layer.
   */
  | 'system'

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
   * carry one by convention (docs/system-specs/common/code-style.md) precisely so a
   * client can act on the failure instead of regex-matching prose.
   */
  code?: string
  /** Failing request path, query string stripped. */
  endpoint?: string
  /**
   * HTTP method of the failing request, for a transport rejection that produced
   * no `Response` (see {@link recordTransportRejection}) — the method is the one
   * fact such a failure has that an answered one reads off the `Response`. Only
   * set on that path; an answered failure leaves it undefined (the method is not
   * worth a field when a status already names the outcome).
   */
  method?: string
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

/** Pull one non-empty string field out of a JSON error body, if present. The
 *  one parse of a backend error envelope -- trimmed, object-shaped, well-formed
 *  -- so a caller after `code`, or after another field the route names beside
 *  it (`mode` on a refused consolidation target), reads it here rather than
 *  parsing the body again on its own. */
export function parseErrorField(body: string | undefined, field: string): string | undefined {
  if (!body) return undefined
  const trimmed = body.trim()
  if (!trimmed.startsWith('{')) return undefined
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>
    const value = parsed[field]
    return typeof value === 'string' && value ? value : undefined
  } catch {
    return undefined
  }
}

/** Pull the backend's machine-readable `code` out of a JSON error body, if present. */
export function parseErrorCode(body: string | undefined): string | undefined {
  return parseErrorField(body, 'code')
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
  method?: string
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
    method: input.method,
    route: input.route ?? currentRoute(),
    detail: normalizeDetail(input.detail),
  }
  _journal = [report, ..._journal].slice(0, MAX_JOURNAL)
  for (const fn of _listeners) {
    try { fn(_journal) } catch { /* a bad subscriber must not break error recording */ }
  }
  return report
}

/**
 * Per-endpoint burst cap for transport REJECTIONS (a `fetch` that never produced
 * a `Response`), so recording them cannot wipe the journal.
 *
 * `recordError` is called once per answered failure, which is self-limiting. A
 * rejection is not: while the gateway is unreachable, every polling query across
 * ~190 call sites rejects on each tick, and left uncapped a few offline seconds
 * would evict all 20 real reports (`MAX_JOURNAL`) and fill the ring with
 * identical "Failed to fetch" lines — losing the very context this journal
 * exists to keep. The cap records the FIRST rejection for a `method + endpoint`,
 * then suppresses repeats for that key for a cooldown window; a different
 * endpoint still records on its own first miss, so a true outage leaves one
 * entry per affected endpoint rather than one surface's retries drowning them.
 *
 * The key is `method + endpoint`, not endpoint alone: a `GET /api/x` poll and a
 * `POST /api/x` the user triggered are different requests, and sharing a bucket
 * would suppress the POST's report and leave its notice resolving the poll's
 * method. A suppressed repeat is not simply dropped — the report recorded for
 * its key is kept in {@link _rejectionReportByKey} and returned, so the
 * rejection it rode on still gets THIS request's endpoint pinned to it (the
 * caller attaches it) rather than falling back to a message match.
 *
 * A second ceiling bounds the TOTAL distinct keys that may record within one
 * cooldown window ({@link TRANSPORT_REJECTION_WINDOW_CAP}). The per-key cap
 * alone does not stop a long outage across many endpoints from flushing the
 * ring — 20+ distinct polling endpoints each record once and the 20-deep ring
 * is still emptied of answered failures. Past the window ceiling a new key is
 * suppressed (its rejection resolves through the one report already kept for a
 * sibling key), so answered-failure reports from before the outage survive.
 */
export const TRANSPORT_REJECTION_COOLDOWN_MS = 10_000
/**
 * Most distinct transport-rejection keys that may hold a journal slot at once.
 * Half the ring, so a wide outage can take at most half the entries and the
 * answered failures that preceded it keep the rest. Past this, a new key is
 * suppressed until an existing one's cooldown lapses and frees a slot.
 */
export const TRANSPORT_REJECTION_WINDOW_CAP = Math.floor(MAX_JOURNAL / 2)
const _rejectionSeenAt = new Map<string, number>()
/**
 * The report recorded for each key still inside its cooldown, so a suppressed
 * repeat can hand back the entry its first miss created instead of nothing.
 * Dropped in lock-step with `_rejectionSeenAt` as cooldowns lapse.
 */
const _rejectionReportByKey = new Map<string, ErrorReport>()

/** Forget every key whose cooldown has lapsed, so the ceiling counts only keys
 *  that currently hold a slot. Keeps the per-key cooldown an INDEPENDENT timer
 *  (each key expires 10s after its own last miss) while the ceiling bounds how
 *  many may be live together. */
function _pruneRejectionKeys(now: number): void {
  for (const [k, seenAt] of _rejectionSeenAt) {
    if (now - seenAt >= TRANSPORT_REJECTION_COOLDOWN_MS) {
      _rejectionSeenAt.delete(k)
      _rejectionReportByKey.delete(k)
    }
  }
}

/**
 * Record a transport-layer rejection — a `fetch` that rejected before any HTTP
 * `Response` existed (a network drop, a `withDeadline` `TimeoutError`). Returns
 * the stored report — the one its own first miss created, even when the
 * per-endpoint burst cap suppressed this repeat — or `undefined` only when the
 * ceiling suppressed a brand-new key that has no report of its own. `status` is
 * deliberately never set: no response arrived, and a synthetic one would send a
 * reader to audit a server that never spoke.
 *
 * Caller is responsible for excluding deliberate aborts (`AbortError`): an
 * unmount or a superseded react-query key cancels on purpose and is not a
 * failure a user can act on.
 */
export function recordTransportRejection(input: {
  message: string
  endpoint?: string
  method?: string
  code?: string
}): ErrorReport | undefined {
  // Key the cap on method + endpoint; a rejection with no parseable endpoint
  // falls back to its own message so unlike failures do not share one bucket
  // (and no sentinel string is needed, which also keeps this out of the i18n
  // scan). GET and POST on one path are different requests and get their own
  // buckets, so a suppressed poll never answers for the write the user made.
  const target = input.endpoint ?? input.message
  const key = `${(input.method ?? 'GET').toUpperCase()} ${target}`
  const now = Date.now()
  _pruneRejectionKeys(now)

  const last = _rejectionSeenAt.get(key)
  if (last !== undefined && now - last < TRANSPORT_REJECTION_COOLDOWN_MS) {
    // Suppressed repeat: hand back the report this key already produced, so the
    // caller can pin THIS request's endpoint to the rejection instead of letting
    // a notice fall through to a message match that resolves another endpoint.
    return _rejectionReportByKey.get(key)
  }
  // A brand-new key past the ceiling records nothing: the outage has already
  // taken its share of the ring, so the rejection resolves through a sibling
  // key's report rather than evicting an answered failure that is still useful.
  if (_rejectionSeenAt.size >= TRANSPORT_REJECTION_WINDOW_CAP) return undefined

  _rejectionSeenAt.set(key, now)
  // The HTTP method travels in its own `method` field (buildErrorPrompt renders
  // it, and the layer that broke is already named by `code: network|timeout`),
  // so nothing here needs a free-form `detail` marker — the message, endpoint,
  // method and code carry the whole report.
  const report = recordError({
    source: 'api',
    message: input.message,
    endpoint: input.endpoint,
    method: input.method,
    code: input.code,
  })
  _rejectionReportByKey.set(key, report)
  return report
}

/** Newest-first snapshot of the journal. */
export function recentErrors(): ErrorReport[] {
  return _journal
}

/**
 * A transport rejection shares its message with every other network drop — the
 * browser's one `Failed to fetch` / `Load failed` — so matching it by message
 * is matching nothing specific. {@link isTransportRejection} marks those entries
 * so the message-only lookup can refuse them (see {@link findReport}).
 */
function isTransportRejection(report: ErrorReport): boolean {
  return report.code === 'network' || report.code === 'timeout'
}

/**
 * Recover the structured report behind a bare message string.
 *
 * The upgrade path for the ~80 existing `setError(e.message)` call sites: the
 * shared banner is handed only the string it always had, and looks the context
 * back up here. Exact match, newest first — an identical message from an older
 * request describes the same failure well enough to prompt with.
 *
 * **Transport rejections are deliberately NOT findable this way.** Every network
 * drop carries the same message (`Failed to fetch` in Chrome, `Load failed` in
 * Safari), so a message match during an outage returns whichever endpoint
 * journaled LAST — usually some background poll's, not the request the user
 * acted on. Naming the wrong endpoint with confidence is worse than the bare
 * message this journal replaces. Those entries are reachable ONLY through the
 * report {@link attachReport} pins to the rejection, read by
 * {@link reportForError}; a bare message resolves to the newest ANSWERED failure
 * that matches, or to nothing.
 */
export function findReport(message: string | null | undefined): ErrorReport | undefined {
  if (!message) return undefined
  const needle = redactSecrets(message).trim()
  if (!needle) return undefined
  return _journal.find(r => !isTransportRejection(r) && r.message.trim() === needle)
}

/** Where {@link attachReport} pins the report on its error. A string key, not a symbol, so the
 *  entry is visible on the rejection in devtools; defined NON-enumerable so a spread or JSON copy
 *  of the error's fields does not carry the report's route and detail along with them. */
const ATTACHED_REPORT = 'errorReport'

/**
 * Pin a journal entry to the rejection it describes.
 *
 * {@link findReport} resolves by EXACT MESSAGE over a newest-first journal, which is the right
 * precision for the `setError(e.message)` sites it was built for -- but the client's bounded reads
 * all reject with the same contract message (`deadline exceeded`), so two of them failing in
 * sequence both resolve to whichever journaled LAST, and one notice hands the agent the other
 * read's endpoint. The transport holds the report at the moment it rethrows, so it pins it here;
 * {@link reportForError} reads that first and falls back to the message match only for a
 * rejection nothing pinned to.
 */
export function attachReport<E extends object>(error: E, report: ErrorReport): E {
  Object.defineProperty(error, ATTACHED_REPORT, { value: report, enumerable: false, configurable: true })
  return error
}

/** The report {@link attachReport} pinned to this rejection, if any. Shape-checked rather than
 *  trusted, because the error is `unknown` and the key is an ordinary property. */
function attachedReport(error: unknown): ErrorReport | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const pinned = (error as Record<string, unknown>)[ATTACHED_REPORT]
  if (typeof pinned !== 'object' || pinned === null) return undefined
  const r = pinned as Partial<ErrorReport>
  return typeof r.id === 'string' && typeof r.message === 'string' ? (r as ErrorReport) : undefined
}

/**
 * The structured report behind a caught error: its own pinned entry when the transport attached
 * one, else the newest journal entry with the same message. The read every notice that renders a
 * bounded read's failure should use, for the reason {@link attachReport} gives.
 */
export function reportForError(error: unknown): ErrorReport | undefined {
  return attachedReport(error) ?? findReport(errMessage(error))
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
  _rejectionSeenAt.clear()
  _rejectionReportByKey.clear()
}

// `buildErrorPrompt` deliberately lives in `errorReport.prompt.ts` (the
// `*.prompt.ts` i18n boundary for model-facing text) and is NOT re-exported
// here: that module imports `redactSecrets` from this one, and a re-export would
// close the import graph into a runtime cycle. Import it from
// `./errorReport.prompt` directly.

type ChatHandoffEntry = { prompt: string; ts: number }
type ClaimedChatHandoffEntry = { prompt: string }

/**
 * Locally claimed hand-offs awaiting a durable target-slot prefill.
 *
 * This is separate from {@link ERROR_HANDOFF_KEY}: subscribers may append and
 * drain that ingress queue while a session request is pending. Keeping the
 * claimed FIFO under its own key prevents a later diagnostic from consuming
 * the active diagnostic's crash-recovery copy.
 */
const ERROR_HANDOFF_CLAIMED_KEY = 'kirocrew_error_handoff_claimed'

/** Decode both the original single entry and the queued wire shape. */
function decodeChatHandoffs(raw: string | null): ChatHandoffEntry[] {
  if (!raw) return []
  try {
    const decoded: unknown = JSON.parse(raw)
    const entries = Array.isArray(decoded) ? decoded : [decoded]
    const now = Date.now()
    return entries.flatMap((entry): ChatHandoffEntry[] => {
      if (!entry || typeof entry !== 'object') return []
      const { prompt, ts } = entry as { prompt?: unknown; ts?: unknown }
      if (typeof prompt !== 'string' || !prompt) return []
      if (typeof ts !== 'number' || now - ts > HANDOFF_TTL_MS) return []
      return [{ prompt, ts }]
    })
  } catch {
    return []
  }
}

function decodeClaimedChatHandoffs(raw: string | null): ClaimedChatHandoffEntry[] {
  if (!raw) return []
  try {
    const decoded: unknown = JSON.parse(raw)
    const entries = Array.isArray(decoded) ? decoded : [decoded]
    return entries.flatMap((entry): ClaimedChatHandoffEntry[] => {
      if (!entry || typeof entry !== 'object') return []
      const { prompt } = entry as { prompt?: unknown }
      return typeof prompt === 'string' && prompt ? [{ prompt }] : []
    })
  } catch {
    return []
  }
}

/**
 * Mirror ChatPage's active + local FIFO while durable ingress storage is empty.
 * Claimed entries deliberately do not age out: ChatPage may own them while
 * disconnected for longer than the ingress TTL, and ownership must not turn a
 * saved diagnostic into an expiring one.
 */
export function persistClaimedChatHandoffs(prompts: readonly string[]): boolean {
  if (!prompts.length) {
    try {
      sessionStorage.removeItem(ERROR_HANDOFF_CLAIMED_KEY)
      return true
    } catch {
      return false
    }
  }
  return safeSetSessionItem(
    ERROR_HANDOFF_CLAIMED_KEY,
    JSON.stringify(prompts.map(prompt => ({ prompt }))),
  )
}

/**
 * Recover a prior page's claimed FIFO ahead of newer ingress entries.
 *
 * This runs once when this module loads. A same-document ChatPage remount does
 * not replay an active request that the old component instance still owns, but
 * a reload gets a new module instance and atomically promotes the crash copy
 * back to the normal ingress queue. Write before delete: a failed promotion
 * leaves the claimed copy available for the next reload rather than losing it.
 */
export function recoverClaimedChatHandoffs(): void {
  let claimedRaw: string | null
  try {
    claimedRaw = sessionStorage.getItem(ERROR_HANDOFF_CLAIMED_KEY)
  } catch {
    return
  }
  if (claimedRaw === null) return

  const claimed = decodeClaimedChatHandoffs(claimedRaw)
  if (!claimed.length) {
    try { sessionStorage.removeItem(ERROR_HANDOFF_CLAIMED_KEY) } catch { /* best effort */ }
    return
  }

  let queued: ChatHandoffEntry[] = []
  try {
    queued = decodeChatHandoffs(sessionStorage.getItem(ERROR_HANDOFF_KEY))
  } catch { /* best-effort storage: safeSetSessionItem handles the write */ }
  const now = Date.now()
  const recovered = [
    ...claimed.map(({ prompt }) => ({ prompt, ts: now })),
    ...queued,
  ]
  if (!safeSetSessionItem(ERROR_HANDOFF_KEY, JSON.stringify(recovered))) return
  try { sessionStorage.removeItem(ERROR_HANDOFF_CLAIMED_KEY) } catch { /* duplicate-safe on retry */ }
}

/**
 * Stage a prompt for a fresh chat session.
 *
 * sessionStorage rather than a Redux dispatch because the most valuable caller
 * is the root ErrorBoundary: when the React tree has already thrown, the store
 * and router may be exactly what is broken, so the hand-off has to survive a
 * hard `location.assign`. Same channel serves the healthy in-app path, so there
 * is one code path to reason about instead of two.
 *
 * The stored value is a queue. Two error surfaces can hand off before the first
 * session request settles, and replacing a single entry would silently discard
 * one diagnostic if both requests later needed to be re-staged.
 */
export function handoffToChat(prompt: string | readonly string[]): boolean {
  const prompts = typeof prompt === 'string' ? [prompt] : prompt
  if (!prompts.length) return true
  let queued: ChatHandoffEntry[] = []
  try {
    queued = decodeChatHandoffs(sessionStorage.getItem(ERROR_HANDOFF_KEY))
  } catch { /* best-effort storage: safeSetSessionItem handles the write */ }
  const now = Date.now()
  queued.push(...prompts.map(item => ({ prompt: item, ts: now })))
  return safeSetSessionItem(ERROR_HANDOFF_KEY, JSON.stringify(queued))
}

/** Drain one queued hand-off. Returns the oldest prompt, or null when absent/stale. */
export function consumeChatHandoff(): string | null {
  let raw: string | null = null
  try {
    raw = sessionStorage.getItem(ERROR_HANDOFF_KEY)
    // Claim the snapshot before decoding it. If rewriting the remainder fails,
    // a consumed prompt still cannot be delivered twice.
    if (raw !== null) sessionStorage.removeItem(ERROR_HANDOFF_KEY)
  } catch {
    return null
  }
  const queued = decodeChatHandoffs(raw)
  const next = queued.shift()
  if (!next) return null
  if (queued.length) safeSetSessionItem(ERROR_HANDOFF_KEY, JSON.stringify(queued))
  return next.prompt
}

// Module evaluation is the page-lifetime boundary: it repeats on reload but not
// on a same-document React remount, which is exactly when claimed work is orphaned.
recoverClaimedChatHandoffs()

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
let _maySoftNavigate: (() => boolean) | null = null
const _handoffListeners = new Set<() => void>()

/** Install (or clear, with `null`) the in-app navigator and its leave guard. Called by App.tsx. */
export function installSoftNavigate(
  fn: ((to: string) => void) | null,
  mayNavigate: (() => boolean) | null = null,
): void {
  _softNavigate = fn
  _maySoftNavigate = fn ? mayNavigate : null
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
 * Stage the prompt and get the user to a fresh chat session.
 *
 * `hard: true` forces a full page load — for the root ErrorBoundary, where a
 * soft navigation would re-render the very tree that just threw.
 *
 * **Navigating is conditional on the staging having stuck.** With storage
 * unavailable or quota-exhausted the prompt never lands, so a navigation would
 * unmount the surface the user was on and deliver them to an empty chat — it
 * destroys context and gains nothing. Staying put keeps the error, and whatever
 * they had typed, on screen. Returns whether the hand-off proceeded.
 *
 * `leaveGranted: true` says the caller has ALREADY asked the page's leave
 * guard through its own gate (`AskAgentButton`'s `gate`, built on
 * `useGuardedLeave`) and the page agreed. The installed `mayNavigate` is that
 * same channel, so asking it again here would pose the discard question a
 * second time — and a page guard that confirms a draft away does not clear the
 * draft until it unmounts, so the second ask is a live confirm, not a free
 * `true`. Cancelling it would abort a hand-off the user had just accepted.
 * Ungated callers keep the ask.
 */
export function sendErrorToChat(prompt: string, opts: { hard?: boolean; leaveGranted?: boolean } = {}): boolean {
  // The app-level leave guard must run before staging: if the page refuses to
  // leave, a queued prompt would ambush the next chat the user opens even
  // though this click did not navigate. Hard mode is the root-boundary escape
  // hatch and deliberately bypasses the live React tree.
  const softNavigate = !opts.hard ? _softNavigate : null
  if (softNavigate && !opts.leaveGranted && _maySoftNavigate && !_maySoftNavigate()) return false
  if (!handoffToChat(prompt)) return false
  if (softNavigate) {
    // Notify before navigating: an already-mounted ChatPage drains here, and a
    // not-yet-mounted one drains on mount instead.
    for (const fn of _handoffListeners) {
      try { fn() } catch { /* a bad subscriber must not strand the hand-off */ }
    }
    softNavigate('/chat')
    return true
  }
  try { window.location.assign(withBase('/chat')) } catch { /* nothing left to try */ }
  return true
}

/** Test seam — the seam is module state. */
export function __resetNavSeamForTests(): void {
  _softNavigate = null
  _maySoftNavigate = null
  _handoffListeners.clear()
}
