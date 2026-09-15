// Measured heights for widget iframes, so a widget reserves the right box
// BEFORE its iframe builds and reports its real height.
//
// Without this, a freshly mounted widget has to reserve a guessed height and
// then correct once the iframe reports — visible as a one-time jump, and inside
// a virtualized list as a steady stream of re-layouts while scrolling, because
// every card entering the viewport corrects on arrival.
//
// Two things make the correction small instead of large:
//   * a HIT is exact — the same content at the same layout width reserves the
//     height it had last time, so there is no correction at all;
//   * a MISS reserves the MEDIAN of what has already been measured in the same
//     key space, so a never-seen widget starts at a typical height rather than
//     at some fixed constant that is wrong for most content.
//
// KEY SPACES. Heights are only comparable between widgets laid out at the same
// width, so callers that measure at different widths must not share entries. A
// caller passes its own `space` and only ever sees its own heights, including in
// the median. The chat/panel frame lays out at its container's width and uses
// the empty space (which is also what its persisted entries were written under,
// so they survive); the artifacts thumbnail lays out at a fixed base width and
// uses its own.
import { safeSetItem } from './safeStorage'

// localStorage key — a storage identifier, never rendered.
const CACHE_KEY = 'mc-widget-heights'
// Entries retained across a persist. Bounds the serialized blob.
const MAX_ENTRIES = 200
// localStorage.setItem is synchronous and serializing the map is not free, so a
// burst of reports (several widgets mounting, one widget settling) coalesces
// into at most one write per window.
const PERSIST_DEBOUNCE_MS = 1000
// Reserve for the very first widget ever measured, when the median has no
// sample to draw on.
const DEFAULT_WIDGET_HEIGHT = 200

const cache: Map<string, number> = (() => {
  try {
    const stored = localStorage.getItem(CACHE_KEY)
    return stored ? new Map<string, number>(JSON.parse(stored)) : new Map<string, number>()
  } catch {
    return new Map<string, number>()
  }
})()

let persistTimer: ReturnType<typeof setTimeout> | null = null

function persist(): void {
  try {
    safeSetItem(CACHE_KEY, JSON.stringify([...cache.entries()].slice(-MAX_ENTRIES)))
  } catch (e) {
    // Best-effort (quota / private mode / serialize failure). There is no
    // recovery to attempt; the next report retries the write. Surfaced in dev so
    // a persistent failure is not completely invisible.
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('widget height cache persist failed', e)
  }
}

function schedulePersist(): void {
  if (persistTimer) return
  persistTimer = setTimeout(() => {
    persistTimer = null
    persist()
  }, PERSIST_DEBOUNCE_MS)
}

/**
 * Bounds applied to a height a frame reports for itself.
 *
 * Not a security boundary — these documents are agent-authored, and an agent that
 * wanted to wreck the page has easier ways than a height report. The bounds exist
 * because ordinary CSS makes a frame's height depend on the frame's own viewport,
 * and a self-sizing frame then feeds its own measurement:
 *
 * - `min-height:100vh`, the common idiom, is SELF-CONSISTENT — every height is a
 *   fixed point, so it settles (measured: 204px, stable). Nothing to bound.
 * - A multiplier above 1 diverges. Measured in Chromium with `min-height:110vh`:
 *   690 → 3838 → 21341 → 100000px in four reports. Rare, and it takes a deliberate
 *   construction, but unbounded growth has no natural stopping point.
 *
 * The floor is the mirror case: 0 would collapse the frame to an invisible box the
 * reader cannot recover from.
 *
 * The ceiling sits far above any real document (a 20,000-word page lays out around
 * 30,000px) and far below where engine layout degrades, so it cannot truncate
 * legitimate content. Owned here rather than per-component so the readers of the
 * same protocol cannot drift apart.
 */
export const MIN_REPORTED_FRAME_HEIGHT = 80
export const MAX_REPORTED_FRAME_HEIGHT = 100_000

/** Bring a reported (or previously cached) frame height inside those bounds.
 *
 * Applied on the way IN from a report and on the way OUT of the cache, because a
 * value persisted before a bound existed is just as able to blow the layout up as
 * a fresh one. */
export function clampFrameHeight(height: number): number {
  if (!Number.isFinite(height)) return MIN_REPORTED_FRAME_HEIGHT
  return Math.min(Math.max(Math.round(height), MIN_REPORTED_FRAME_HEIGHT), MAX_REPORTED_FRAME_HEIGHT)
}

/** Stable key for a piece of widget HTML within one layout-width space. */
export function widgetHeightKey(html: string, space = ''): string {
  let h = 0
  for (let i = 0; i < html.length; i++) {
    h = ((h << 5) - h + html.charCodeAt(i)) | 0
  }
  return space ? `${space}:${h}` : String(h)
}

export function getWidgetHeight(key: string): number | undefined {
  return cache.get(key)
}

export function setWidgetHeight(key: string, height: number): void {
  if (cache.get(key) === height) return
  cache.set(key, height)
  schedulePersist()
}

/**
 * Median measured height within one key space, or `fallback` when that space has
 * no samples yet.
 *
 * Median rather than mean: one pathological widget (a tall dashboard among
 * mostly small cards) moves a mean enough to make every unmeasured widget
 * reserve too much, and over-reserving is the direction that produces the
 * accumulating one-way drift while scrolling.
 */
export function estimateWidgetHeight(space = '', fallback = DEFAULT_WIDGET_HEIGHT): number {
  const prefix = space ? `${space}:` : ''
  const vals: number[] = []
  for (const [k, v] of cache) {
    // The empty space owns every unprefixed key; a named space owns only its own.
    const inSpace = space ? k.startsWith(prefix) : !k.includes(':')
    if (inSpace) vals.push(v)
  }
  if (vals.length === 0) return fallback
  vals.sort((a, b) => a - b)
  return vals[Math.floor(vals.length / 2)]
}
