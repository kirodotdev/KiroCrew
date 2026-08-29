// sessionStorage mirror for the writing-review provider's active-scan
// handle. Companion to the mount-time backend fetch in
// ``../context.tsx``: the mirror survives a same-tab unmount-remount
// cycle (e.g. navigating to Settings and back to change a theme)
// without waiting on an async fetch, so the ScanProgress card stays on
// screen as the user returns rather than flickering to EmptyState until
// the backend query settles.
//
// Mirrors the pattern used by an older internal version of this app's
// WritingReviewPage. ``sessionStorage`` (not ``localStorage``) because the requirement is
// per-tab resume: two tabs each running a scan keep independent handles
// instead of clobbering one shared origin-global slot. The scan is
// pruned server-side after ~1h and lost on a gateway restart anyway,
// so the extra durability ``localStorage`` would add across a full
// browser restart buys nothing here.
//
// All three functions defend against a hostile / sandboxed
// sessionStorage (private-mode browsers, iframed contexts, quota
// exhaustion). A throw here would crash the provider mount, so silent
// fallback to "no persisted state" is the correct posture. The backend
// fetch inside the provider still runs as a second-pass truth check,
// so a mirror miss is recoverable.

export interface PersistedActiveScanFields {
  jobId: string | null
  docName: string | null
  phase: string | null
}

const EMPTY_ACTIVE_SCAN: PersistedActiveScanFields = {
  jobId: null,
  docName: null,
  phase: null,
}

// Namespaced under ``mc:writingReview:`` so the key does not collide
// with any other app's session state and follows the dashboard-wide
// convention: every browser-storage key the dashboard owns lives
// under the ``mc:`` prefix (see ``mc:notif:activeKinds:v2``,
// ``mc:notif:seenChannels`` in ``pages/notifications``). The
// convention is machine-facing -- ``sessionStorage.getItem`` compares
// byte-for-byte -- and the ``eslint.i18n.config.js`` allowlist has a
// dedicated ``^mc:[A-Za-z0-9:._-]+$`` pattern to keep the strict i18n
// gate from reporting these keys as untranslated user copy. Any
// existing dev-mode active-scan mirror entries under the old
// ``writing-review:activeScan`` key are transient (scan handles are
// pruned server-side after ~1h and lost on gateway restart), so
// this rename requires no data migration.
export const writingReviewActiveScanKey = 'mc:writingReview:activeScan'

function isRecordLike(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function coerceStringOrNull(value: unknown): string | null {
  // Any non-string persisted value (undefined, number, object, array)
  // is treated as null so a corrupt entry can never surface as a
  // phantom scan. The provider's backend fetch reconciles truth.
  return typeof value === 'string' ? value : null
}

export function readActiveScanMirror(): PersistedActiveScanFields {
  try {
    const storedJsonString = window.sessionStorage.getItem(writingReviewActiveScanKey)
    if (storedJsonString === null) return { ...EMPTY_ACTIVE_SCAN }
    const parsedValue = JSON.parse(storedJsonString) as unknown
    if (!isRecordLike(parsedValue)) return { ...EMPTY_ACTIVE_SCAN }
    return {
      jobId: coerceStringOrNull(parsedValue.jobId),
      docName: coerceStringOrNull(parsedValue.docName),
      phase: coerceStringOrNull(parsedValue.phase),
    }
  } catch {
    // sessionStorage access can throw in private-mode browsers /
    // sandboxed iframes, and ``JSON.parse`` throws on malformed
    // input. Both degrade to "no persisted state" -- the provider's
    // in-memory + backend paths keep working, we just lose the
    // instant-hydration boost.
    return { ...EMPTY_ACTIVE_SCAN }
  }
}

export function writeActiveScanMirror(nextFields: PersistedActiveScanFields): void {
  // Terminal state (all-null fields) clears the entry rather than
  // writing a JSON-null wrapper. Removing the key means a future
  // ``clear`` on this key from another surface stays a no-op, and a
  // fresh tab that reads sees "not set" instead of "explicitly
  // cleared".
  if (
    nextFields.jobId === null &&
    nextFields.docName === null &&
    nextFields.phase === null
  ) {
    clearActiveScanMirror()
    return
  }
  try {
    // Single ``setItem`` -- one atomic operation, no partial-write
    // window where a fresh jobId could be paired with a stale docName
    // from a prior state.
    window.sessionStorage.setItem(
      writingReviewActiveScanKey,
      JSON.stringify(nextFields),
    )
  } catch {
    // Quota exceeded or storage disabled -- silent. The mirror is a
    // performance optimisation, not a correctness requirement.
  }
}

export function clearActiveScanMirror(): void {
  try {
    window.sessionStorage.removeItem(writingReviewActiveScanKey)
  } catch {
    // Storage disabled -- no-op is the desired outcome anyway.
  }
}
