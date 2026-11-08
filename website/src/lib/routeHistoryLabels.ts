/**
 * A name for each entry of the app's own history stack, so the Back/Forward
 * arrows can list where they lead (#17453): right-click (or long-press) Back
 * for the previous destinations, Forward for the next ones, each named by its
 * session or page rather than its address.
 *
 * The platform keeps no titles for history entries an app can read, so the
 * shell writes one here for the entry on screen whenever its tab title label
 * settles (`labelCurrentRouteEntry`, fed from `appShell.tsx`). The key is
 * react-router's stack position (`routerEntry().idx`), the same index
 * `routeHistoryPosition` reasons with, so a name and a step count always
 * describe the same entry.
 *
 * Kept in `sessionStorage` for the same reason as the Forward watermark: it is
 * per tab and dies with the tab, which is the stack's own lifetime, and it
 * survives a reload, which keeps the stack too. Any other arrival (a fresh
 * `navigate`, a `back_forward` return, a BFCache restore) may mean the stored
 * indices name another document's entries, so the names are dropped.
 *
 * What it never claims:
 *
 *  - an entry with no name (a route that matches no rail row) is left out of
 *    the list rather than shown as an address;
 *  - a `NavigationBackGuard` duplicate is never named, because it is not a
 *    destination;
 *  - nothing above the Forward watermark is listed, because the watermark is
 *    the one place that knows a PUSH truncated those entries.
 */

import { forwardFrontier, isTrapEntry, navigationArrivalType, routerEntry } from './routeHistoryPosition'
import { safeGetSessionItem, safeSetSessionItem } from '../utils/safeStorage'

const LABELS_KEY = 'mc-route-history-labels'

/** How many destinations each list shows, as in the mainstream browsers. */
export const ROUTE_HISTORY_MENU_LIMIT = 10

/** Stored names are bounded so a long-lived tab cannot grow the entry forever. */
const MAX_STORED = 200

/** A destination the menu offers: how far to move, and what to call it. */
export interface RouteHistoryMenuEntry {
  /** Steps from the current entry, negative for Back. */
  delta: number
  label: string
}

function load(): Map<number, string> {
  const out = new Map<number, string>()
  // Only a reload keeps the stack exactly as it was. A fresh `navigate`
  // restarts react-router at idx 0, and a `back_forward` arrival may come back
  // from another document of this app whose indices meant other pages — the
  // same two cases where `routeHistoryPosition` drops its watermark.
  const arrival = navigationArrivalType()
  if (arrival === 'navigate' || arrival === 'back_forward') {
    try { window.sessionStorage.removeItem(LABELS_KEY) } catch { /* no storage */ }
    return out
  }
  const raw = safeGetSessionItem(LABELS_KEY)
  if (!raw) return out
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        const idx = Number(k)
        if (Number.isInteger(idx) && idx >= 0 && typeof v === 'string' && v) out.set(idx, v)
      }
    }
  } catch { /* a damaged value names nothing */ }
  return out
}

let labels: Map<number, string> = load()

// A BFCache restore is a `back_forward` arrival without a document start.
if (typeof window !== 'undefined') {
  window.addEventListener('pageshow', (e: PageTransitionEvent) => {
    if (!e.persisted) return
    labels = new Map()
    try { window.sessionStorage.removeItem(LABELS_KEY) } catch { /* no storage */ }
  })
}

function persist(here: number): void {
  // Above the Forward watermark a PUSH has already truncated the stack, so
  // those names describe entries that no longer exist. The entry on screen is
  // always kept: the shell may name a pushed entry before the tracker has
  // moved the watermark up to it.
  const frontier = forwardFrontier()
  if (frontier !== null) {
    const top = Math.max(frontier, here)
    for (const k of [...labels.keys()]) if (k > top) labels.delete(k)
  }
  if (labels.size > MAX_STORED) {
    const keep = [...labels.keys()].sort((a, b) => b - a).slice(0, MAX_STORED)
    labels = new Map(keep.map(k => [k, labels.get(k) as string]))
  }
  safeSetSessionItem(LABELS_KEY, JSON.stringify(Object.fromEntries(labels)))
}

/**
 * Name the entry on screen. An empty label forgets it, so a route with no
 * name never inherits the name an earlier, truncated entry had at that index.
 */
export function labelCurrentRouteEntry(label: string): void {
  const { idx, state } = routerEntry()
  if (idx === null) return
  // A guard duplicate is never a destination. It may reuse the index of an
  // entry a PUSH truncated, so the name that entry had is dropped here.
  const name = isTrapEntry(state) ? '' : label.trim()
  if (name) {
    if (labels.get(idx) === name) return
    labels.set(idx, name)
  } else if (!labels.delete(idx)) {
    return
  }
  persist(idx)
}

/**
 * The named destinations behind (`-1`) or ahead of (`1`) the entry on screen,
 * nearest first, at most `limit` of them. Standing on a guard duplicate counts
 * as standing on the page beneath it, as the arrows do.
 */
export function routeHistoryMenuEntries(direction: -1 | 1, limit = ROUTE_HISTORY_MENU_LIMIT): RouteHistoryMenuEntry[] {
  const { idx, state } = routerEntry()
  if (idx === null) return []
  const here = isTrapEntry(state) ? idx - 1 : idx
  const top = direction === 1 ? forwardFrontier() : 0
  if (top === null) return []
  const out: RouteHistoryMenuEntry[] = []
  for (let at = here + direction; direction === 1 ? at <= top : at >= top; at += direction) {
    const label = labels.get(at)
    if (label) out.push({ delta: at - idx, label })
    if (out.length >= limit) break
  }
  return out
}

/** Test-only: model a page reload — memory is lost, `sessionStorage` is kept. */
export function _simulateLabelsReloadForTest(): void {
  labels = load()
}

/** Test-only: reset module state between cases. */
export function _resetRouteHistoryLabelsForTest(): void {
  labels = new Map()
  try { window.sessionStorage.removeItem(LABELS_KEY) } catch { /* jsdom without storage */ }
}
