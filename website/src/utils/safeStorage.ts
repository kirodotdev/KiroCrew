/**
 * Quota-safe Web Storage writes.
 *
 * Why this exists: the dashboard accumulates a lot of per-origin localStorage —
 * most notably the per-session virtualizer height caches (`vc_heights_<sid>`,
 * up to 2000 entries each), which pile up across every chat session ever opened
 * and are never reclaimed. Over time the ~5-10 MB origin quota fills up. Once it
 * does, the NEXT `localStorage.setItem` anywhere in the app throws
 * `QuotaExceededError` — and many call sites write raw, unguarded. When one of
 * those fires synchronously on the websocket `onmessage` -> Redux dispatch ->
 * re-render path, the exception bubbles out of an event handler / commit phase
 * (which a React ErrorBoundary cannot catch) and white-screens the whole app.
 *
 * `safeSetItem` makes every write defensive:
 *   1. Try the write.
 *   2. On a quota error, reclaim disposable space one tier at a time
 *      (cheapest-to-lose first — see RECLAIM_TIERS) and retry after each tier,
 *      escalating until the write succeeds or nothing reclaimable is left. The
 *      reclaimed data is pure derived/cache state that rebuilds from the DOM or
 *      the server; user input (drafts) and config are never touched.
 *   3. If it still fails, swallow it (best-effort persistence) and warn in dev.
 *
 * This mirrors the existing guard patterns already used by `chatDrafts`,
 * `pasteTokens`, `commentDrafts`, `HeightCache`, and `dashboardSlice` — this
 * module just centralizes them so the dozens of remaining raw call sites can
 * adopt a single, well-tested helper.
 */

/** Prefix of the per-session virtualizer height caches. These hold pure
 *  derived pixel measurements and are safe to drop under storage pressure;
 *  the virtualizer re-measures from the DOM and repopulates them. */
const HEIGHT_CACHE_PREFIX = 'vc_heights_'

/**
 * Disposable-cache tiers reclaimed under quota pressure, cheapest-to-lose
 * first. Every key matched here is pure derived/cache data that rebuilds from
 * source (the DOM, the gateway, or the server transcript) — NEVER unsaved user
 * input (drafts) or config. `reclaimSpace` drops one tier at a time so a write
 * sacrifices only as much as it must to succeed.
 *
 * Why tiers (not just `vc_heights_*`): in practice the quota hog is often a
 * DIFFERENT key — e.g. the `mc-paste-store-v1` sent-paste side table can reach
 * multiple MB on its own. When no `vc_heights_*` keys exist,
 * single-tier reclaim freed nothing, `safeSetItem` gave up, and the write was
 * silently lost — defeating the whole point of this module.
 */
const RECLAIM_TIERS: ReadonlyArray<(key: string) => boolean> = [
  // Tier 1 — per-session virtualizer height caches. Pure pixel measurements,
  // re-measured from the DOM. Dominant source of growth, cheapest to lose.
  (k) => k.startsWith(HEIGHT_CACHE_PREFIX),
  // Tier 2 — sent-paste rehydration side table + per-session touched-file
  // lists. Both derive from server state: dropping them only un-collapses
  // already-sent paste tokens / clears file chips until they rebuild. Exclude
  // the `:toolClearedAt` clear-watermark (managed by useTouchedFiles): it is
  // tiny, and evicting it would reset toolClearedAtRef to 0 so previously
  // cleared agent-touched files re-surface on the next load after a sweep.
  (k) =>
    k === 'mc-paste-store-v1' ||
    (k.startsWith('kirocrew:touched-files:') && !k.endsWith(':toolClearedAt')),
]

/**
 * Detect a storage-quota exception across browsers.
 *
 * Chrome/Safari throw a DOMException named `QuotaExceededError` (code 22).
 * Firefox throws `NS_ERROR_DOM_QUOTA_REACHED` (code 1014). We check name and
 * code defensively because the name is the most reliable signal but some
 * engines historically only set the legacy numeric code.
 */
export function isQuotaExceededError(err: unknown): boolean {
  if (!(err instanceof DOMException)) return false
  return (
    err.name === 'QuotaExceededError' ||
    err.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
    err.code === 22 ||
    err.code === 1014
  )
}

/**
 * Drop one tier of disposable localStorage entries to free space when the
 * quota is hit. Tiers are defined in `RECLAIM_TIERS`, cheapest-to-lose first;
 * each call drops the first tier that actually removes something and stops, so
 * a write sacrifices only as much cache as it needs. `safeSetItem` calls this
 * repeatedly (retry/reclaim loop) to escalate through the tiers on demand.
 *
 * Returns true if anything was removed, so the caller knows a retry is
 * worthwhile (and that further escalation may still be possible).
 */
function reclaimSpace(): boolean {
  if (typeof localStorage === 'undefined') return false
  try {
    for (const matches of RECLAIM_TIERS) {
      // Collect first, then delete: removing while iterating by index shifts
      // subsequent indices and would skip keys.
      const doomed: string[] = []
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (k && matches(k)) doomed.push(k)
      }
      let removed = false
      for (const k of doomed) {
        try {
          localStorage.removeItem(k)
          removed = true
        } catch {
          /* best-effort */
        }
      }
      // Escalate one tier per call: as soon as a tier frees anything, stop and
      // let the caller retry the write before sacrificing the next tier.
      if (removed) return true
    }
  } catch {
    /* enumerating storage can throw in locked-down environments */
  }
  return false
}

function warnDev(key: string, err: unknown): void {
  if (import.meta.env.DEV) {
    // Pass key + err as trailing args, NOT interpolated into the first
    // (format-string) arg: a key containing console format specifiers (%s/%d/%c)
    // would otherwise be interpreted as a format directive (CodeQL: use of
    // externally-controlled format string).
    // eslint-disable-next-line no-console
    console.warn('safeStorage: persist of "%s" failed', key, err)
  }
}

/**
 * Write to localStorage without ever throwing.
 *
 * Returns true if the value was persisted, false if it was dropped (quota
 * exhausted after reclaim, storage disabled, or serialization error upstream).
 * Callers that need to know whether persistence succeeded can branch on the
 * return value; most can ignore it (best-effort persistence).
 */
/**
 * Non-throwing localStorage read. Returns null when storage access is denied
 * (SecurityError in locked-down embedding contexts / browser policies) or
 * unavailable — a read failure must degrade to "no cached value", never crash
 * the caller.
 */
export function safeGetItem(key: string): string | null {
  try {
    if (typeof localStorage === 'undefined') return null
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeSetItem(key: string, value: string): boolean {
  try {
    if (typeof localStorage === 'undefined') return false
    localStorage.setItem(key, value)
    return true
  } catch (err) {
    // Only attempt reclaim+retry for genuine quota errors — a SecurityError
    // (storage disabled) or anything else won't be fixed by freeing space.
    if (!isQuotaExceededError(err)) {
      warnDev(key, err)
      return false
    }
    // Escalate through the reclaim tiers: each reclaimSpace() frees one tier,
    // then we retry. A single freed tier may not be enough (e.g. the height
    // caches are tiny but mc-paste-store-v1 is multi-MB), so keep escalating
    // until the write succeeds or there is nothing left to reclaim. The loop
    // is bounded structurally by the tier count (one reclaimSpace() drains at
    // most one tier), so termination never depends on removeItem actually
    // freeing space — a Storage backend that silently no-ops removal cannot
    // spin this loop. RECLAIM_TIERS.length iterations cover every tier.
    let lastErr: unknown = err
    for (let i = 0; i < RECLAIM_TIERS.length && reclaimSpace(); i++) {
      try {
        localStorage.setItem(key, value)
        return true
      } catch (retryErr) {
        lastErr = retryErr
      }
    }
    warnDev(key, lastErr)
    return false
  }
}

/**
 * Non-throwing sessionStorage read — the per-tab mirror of `safeGetItem`.
 * Returns null when storage access is denied (SecurityError in locked-down
 * embedding contexts / browser policies) or unavailable.
 *
 * The `typeof` availability probe is INSIDE the try on purpose: `typeof` only
 * suppresses ReferenceError for an undeclared identifier, it does not suppress
 * an exception thrown by a property getter. `sessionStorage` is an accessor on
 * the global, and browsers that deny storage (Chrome with cookies blocked, a
 * sandboxed iframe) throw SecurityError from that getter — so probing outside
 * the try would throw on the very platform the probe exists to survive.
 */
export function safeGetSessionItem(key: string): string | null {
  try {
    if (typeof sessionStorage === 'undefined') return null
    return sessionStorage.getItem(key)
  } catch {
    return null
  }
}

/**
 * Write to sessionStorage without ever throwing. sessionStorage is per-tab and
 * far less prone to filling up, so there is nothing useful to reclaim — we just
 * swallow failures so a full/disabled store can't crash the app.
 */
export function safeSetSessionItem(key: string, value: string): boolean {
  try {
    if (typeof sessionStorage === 'undefined') return false
    sessionStorage.setItem(key, value)
    return true
  } catch (err) {
    warnDev(key, err)
    return false
  }
}
