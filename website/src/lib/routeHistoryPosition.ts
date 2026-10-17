/**
 * Where the app sits in its own history stack — the shared answer to "is there
 * a page behind me / ahead of me?" that the top-bar Back/Forward arrows and the
 * ⌘/Ctrl+←/→ chords both read (#8258).
 *
 * The platform never answers this directly: `history.length` counts entries from
 * other documents, and the dashboard mounts a plain `<BrowserRouter>` (no data
 * router), so there is no `useNavigation` state to lean on. What IS reliable is
 * react-router's own per-entry bookkeeping: it stores a stack position in
 * `history.state.idx` (0 for the document's first router entry, +1 per push),
 * and that value survives reloads because it lives in the entry itself. The same
 * source `NavigationBackGuard` reads, for the same reason.
 *
 * From it:
 *
 *  - **Back exists** iff `idx > 0` — exact, including across a reload.
 *  - **Forward exists** iff `idx < maxIdx`, where `maxIdx` is the highest index
 *    this store has SEEN. A PUSH truncates everything above the entry it lands
 *    on, so it resets `maxIdx = idx`; a POP or REPLACE only ever moves within
 *    the stack, so it raises the watermark. The watermark survives a reload
 *    through `sessionStorage` (per-tab, dies with the tab — the stack's own
 *    lifetime), so a Forward branch built before the reload is still offered
 *    afterwards. It is COLLAPSED to the current entry on every arrival that may
 *    have truncated the branch where no app code could see it: a fresh
 *    `navigate` (which restarts the stack), a `back_forward` return (browser
 *    Back after an external same-tab link) and its BFCache twin, a `pageshow`
 *    with `persisted` (see `loadMaxIdx`). The collapse is itself persisted, so
 *    a reload after such a return cannot resurrect the stale branch; Forward
 *    re-appears only for entries this document is then seen on or builds. It
 *    stays conservative where it cannot know: a fresh tab reads as "no Forward"
 *    until the first report — a false "disabled" costs one browser-native
 *    keypress, a false "enabled" is an arrow that lies.
 *
 * Module-level store published through `useSyncExternalStore`, like
 * `loadedTools.ts`: the snapshot object is REPLACED only when a value actually
 * flips, because the hook compares snapshots by identity and a fresh object per
 * read is an infinite render loop.
 *
 * Feeding it is `RouteHistoryTracker` (mounted once inside the router, next to
 * `NavigationBackGuard`), which reports every location change with its
 * navigation type. Reads outside React (the keydown path) use `canGoBack()` /
 * `canGoForward()` directly.
 */

import { safeGetSessionItem, safeSetSessionItem } from '../utils/safeStorage'

export interface RouteHistoryPosition {
  canGoBack: boolean
  canGoForward: boolean
}

/**
 * The router state a history entry `NavigationBackGuard` minted carries.
 *
 * The marker is what makes such an entry recognisable when its location is read
 * back — including across a reload, where it is the only thing that survives to
 * identify one. Declared as a TYPE with an identifier key rather than a string
 * constant: the key is a router contract no user ever reads, and spelling it as a
 * quoted literal would make the i18n gate charge it as untranslated copy.
 */
export type TrapEntryState = { __navigationLeaveTrap?: true }

export const isTrapEntry = (state: unknown): boolean =>
  !!(state && typeof state === 'object' && (state as TrapEntryState).__navigationLeaveTrap === true)

/** react-router's own per-entry bookkeeping, read straight from the platform.
 *
 *  THE single spelling of the `history.state` shape react-router maintains —
 *  `idx` is its stack position (0 for the document's first router entry, +1
 *  per push, survives reloads), `state` is what a navigation carried (`usr`).
 *  `NavigationBackGuard` reads through this same function, so the coupling to
 *  react-router internals lives in exactly one place. */
export function routerEntry(): { idx: number | null; state: unknown } {
  const raw = window.history.state as { idx?: unknown; usr?: unknown } | null
  // Number.isFinite, not typeof: a raw `history.replaceState({}, …)` anywhere
  // in the app wipes react-router's bookkeeping, after which the router
  // computes every later idx as NaN — a `number` by typeof, but garbage that
  // would make `idx > 0` false and `idx < maxIdx` false forever. NaN reads as
  // "no bookkeeping", the same conservative answer as a missing field.
  return { idx: Number.isFinite(raw?.idx) ? (raw!.idx as number) : null, state: raw?.usr }
}

/**
 * The Forward watermark's home across reloads. `sessionStorage`, not
 * `localStorage`, on purpose: it is scoped to ONE tab and dies with it —
 * exactly the lifetime of the history stack it describes. A tab's stack never
 * leaks into another tab's arrow state, and a stack that is gone takes its
 * watermark with it. Without persistence a reload reset the watermark to the
 * current entry, so a Forward branch built before the reload read as absent on
 * both the arrow and the chord — and the desktop app has no browser chrome to
 * reach those entries any other way.
 */
const MAX_IDX_KEY = 'mc-route-history-max-idx'

/** Did this document arrive by a plain navigation, rather than a reload or a
 *  Back/Forward into it? Only then is the entry it landed on known to be the TOP
 *  of the stack — a navigation truncates, a reload preserves whatever was above.
 *  Absent timing data answers "unknown", never "yes". Shared with
 *  `NavigationBackGuard`, which calibrates its own stack arithmetic on it. */
export function arrivedByFreshNavigation(): boolean {
  return navigationArrivalType() === 'navigate'
}

/** The document's navigation-timing type, or `''` when the platform reports
 *  none (older engines, some test runners) — callers must treat that as
 *  "unknown", never as any one kind of arrival. */
function navigationArrivalType(): string {
  try {
    const entries = performance.getEntriesByType('navigation') as { type?: string }[]
    return entries[0]?.type ?? ''
  } catch { return '' }
}

/**
 * The persisted watermark is only TRUE across a `reload` — the one arrival
 * that keeps the stack exactly as it was. The other two kinds discard it:
 *
 *  - A fresh `navigate` arrival (a typed URL, `location.href = …`, a `_blank`
 *    popout whose opener cloned this tab's sessionStorage) TRUNCATES the
 *    Forward branch while react-router restarts at idx 0, so a restored
 *    watermark would enable a Forward that goes nowhere.
 *  - A `back_forward` arrival re-enters this document from ANOTHER document's
 *    history — commonly a return by browser Back after an external same-tab
 *    link — and that other document was reached by a PUSH that truncated this
 *    app's Forward branch where no app code could see it. The stored watermark
 *    may then name entries that no longer exist, or the external page itself.
 *
 * Both are the lying arrow this module exists to avoid, so the stored value is
 * discarded AND cleared: the first report then re-seeds the watermark at the
 * current entry (see `recordRouteNavigation`), and because that seed is what
 * gets persisted, a later reload of THIS document cannot resurrect the stale
 * branch either — the collapse survives reloads by construction. Forward
 * re-appears only for entries this document has since been seen ON (a POP
 * raises the watermark to where it landed) or built (a PUSH). Back is
 * unaffected throughout: it reads `idx > 0` live from the platform. Absent
 * timing data the restore is trusted: the failure there is one dead click that
 * self-heals on the next PUSH.
 */
function loadMaxIdx(): number | null {
  const arrival = navigationArrivalType()
  if (arrival === 'navigate' || arrival === 'back_forward') {
    try { window.sessionStorage.removeItem(MAX_IDX_KEY) } catch { /* no storage */ }
    return null
  }
  const raw = safeGetSessionItem(MAX_IDX_KEY)
  const n = raw === null ? Number.NaN : Number(raw)
  return Number.isFinite(n) ? n : null
}

/**
 * A back/forward-cache restore is the `back_forward` situation WITHOUT a
 * document start: the browser revives this exact page from BFCache after the
 * user went to another document and came back, so module state is whatever it
 * was when the page was frozen and navigation timing still describes the
 * original load. The external document's PUSH may have truncated the Forward
 * branch in the meantime, so on `pageshow` with `persisted` the watermark is
 * collapsed the same way — dropped, then re-seeded at the current entry by the
 * report that follows, which also persists the collapse for any later reload.
 * Registered once at module load; a non-persisted pageshow (the ordinary first
 * show) changes nothing.
 */
if (typeof window !== 'undefined') {
  window.addEventListener('pageshow', (e: PageTransitionEvent) => {
    if (!e.persisted) return
    maxIdx = null
    recordRouteNavigation('POP')
  })
}

/** Highest router index seen for THIS tab's stack — the Forward watermark.
 *  `null` until the first report (and after a fresh tab), which reads as "no
 *  Forward"; restored from `sessionStorage` on reload. */
let maxIdx: number | null = loadMaxIdx()

function setMaxIdx(n: number): void {
  maxIdx = n
  safeSetSessionItem(MAX_IDX_KEY, String(n))
}

let snapshot: RouteHistoryPosition = { canGoBack: false, canGoForward: false }

const listeners = new Set<() => void>()

function publish(next: RouteHistoryPosition): void {
  // Identity-stable: only swap the snapshot when a value changed, so
  // `useSyncExternalStore`'s identity compare sees a steady object across the
  // overwhelmingly common no-change report.
  if (next.canGoBack === snapshot.canGoBack && next.canGoForward === snapshot.canGoForward) return
  snapshot = next
  // Iterated over a copy — a listener may unsubscribe from inside its callback.
  for (const l of [...listeners]) l()
}

/**
 * Record the navigation that just committed. `navigationType` is react-router's
 * own verdict (`useNavigationType()`), which is what distinguishes the one move
 * that destroys a Forward branch (PUSH) from the ones that keep it.
 */
export function recordRouteNavigation(navigationType: 'PUSH' | 'POP' | 'REPLACE'): void {
  const { idx, state } = routerEntry()
  if (idx === null) {
    // No router bookkeeping to reason with: claim nothing rather than guess.
    // The silent degrade is deliberate for users; a router upgrade that changes
    // the `history.state` shape would otherwise read as "arrows never enable"
    // with nothing in the console, so DEV gets one line per PUSH to notice.
    if (import.meta.env.DEV && navigationType === 'PUSH') {
      // eslint-disable-next-line no-console
      console.warn('[routeHistoryPosition] history.state carries no router idx on a PUSH — the Back/Forward arrows will stay disabled')
    }
    publish({ canGoBack: false, canGoForward: false })
    return
  }
  // A NavigationBackGuard trap duplicate is a real stack entry but never a
  // DESTINATION: it renders the same address as the entry beneath it and the
  // guard carries any pop that lands on it straight through. So it must not
  // raise the watermark — otherwise Forward would offer a move onto the trap,
  // the guard's carry-through `forward()` no-ops at the stack top, its
  // self-move flag leaks, and the next native Back skips the draft ask. A trap
  // sits one above its page, so on a trap the Forward frontier is the page
  // itself: watermark = idx - 1.
  const trap = isTrapEntry(state)
  const frontier = trap ? idx - 1 : idx
  // A PUSH truncates everything above the entry it lands on, so it RESETS the
  // watermark; a POP or REPLACE only moves within the stack, so it raises it.
  // After a collapse (`maxIdx === null`: a fresh or back_forward arrival, a
  // BFCache restore) the first report of either kind re-seeds the watermark at
  // the current entry, so Forward reads "none" until this document is seen on
  // or builds an entry above it.
  if (navigationType === 'PUSH') setMaxIdx(frontier)
  else setMaxIdx(maxIdx === null ? frontier : Math.max(maxIdx, frontier))
  // Standing ON a trap: the user is at the page beneath it for every visible
  // purpose, so Back/Forward describe that page's position (idx - 1).
  const here = trap ? idx - 1 : idx
  publish({ canGoBack: here > 0, canGoForward: here < (maxIdx as number) })
}

/** Live read for the keydown path — `idx > 0` straight from the platform, so a
 *  chord pressed before the tracker's first report still answers correctly. */
export function canGoBack(): boolean {
  const { idx, state } = routerEntry()
  if (idx === null) return false
  return (isTrapEntry(state) ? idx - 1 : idx) > 0
}

/** Live read for the keydown path. Forward needs the watermark, so before the
 *  tracker's first report this is `false` — the conservative side. */
export function canGoForward(): boolean {
  const { idx, state } = routerEntry()
  if (idx === null || maxIdx === null) return false
  return (isTrapEntry(state) ? idx - 1 : idx) < maxIdx
}

export function subscribeRouteHistoryPosition(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function getRouteHistoryPosition(): RouteHistoryPosition {
  return snapshot
}

/** Test-only: model a page reload — in-memory state is lost, the tab's
 *  `sessionStorage` (and so the persisted watermark) survives. */
export function _simulateReloadForTest(): void {
  maxIdx = loadMaxIdx()
  snapshot = { canGoBack: false, canGoForward: false }
}

/** Test-only: reset module state between cases. */
export function _resetRouteHistoryPositionForTest(): void {
  maxIdx = null
  try { window.sessionStorage.removeItem(MAX_IDX_KEY) } catch { /* jsdom without storage */ }
  snapshot = { canGoBack: false, canGoForward: false }
  listeners.clear()
}
