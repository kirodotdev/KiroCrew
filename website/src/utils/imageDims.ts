// Learned natural dimensions for image artifacts, so an image card reserves
// its final contain-fit box BEFORE the bytes arrive.
//
// The save-time header sniff records width/height into the artifact's image
// metadata, and ImageThumb passes those straight through as <img> attributes.
// Artifacts saved before the sniff existed have no dimensions, so their cards
// mount ~16px tall and grow by up to ~280px when the lazy load lands — inside
// the virtualized gallery that late growth shoves everything below it
// mid-scroll, on EVERY pass: the virtualizer's persisted row-height cache
// sizes placeholders and spacers, but a remounted card's own <img> box starts
// empty again regardless.
//
// This cache closes that gap client-side: the first successful load records
// the image's natural size under its slug, and every later mount reserves from
// it. The first-ever view of a legacy image still grows once (nothing knows
// its size yet); every pass after that is stable. Entries are keyed by slug —
// a re-uploaded image (new bytes, same slug) overwrites on its next load, so a
// stale entry self-corrects after one render.
import { safeSetItem } from './safeStorage'

// localStorage key — a storage identifier, never rendered.
const CACHE_KEY = 'mc-image-dims'
// Entries retained across a persist. Bounds the serialized blob.
const MAX_ENTRIES = 300
// Bursts of loads (a scroll mounting several image cards) coalesce into at
// most one synchronous localStorage write per window.
const PERSIST_DEBOUNCE_MS = 1000

export interface ImageDims {
  w: number
  h: number
}

const cache: Map<string, ImageDims> = (() => {
  try {
    const stored = localStorage.getItem(CACHE_KEY)
    return stored ? new Map<string, ImageDims>(JSON.parse(stored)) : new Map<string, ImageDims>()
  } catch {
    return new Map<string, ImageDims>()
  }
})()

let persistTimer: ReturnType<typeof setTimeout> | null = null

function persist(): void {
  try {
    safeSetItem(CACHE_KEY, JSON.stringify([...cache.entries()].slice(-MAX_ENTRIES)))
  } catch (e) {
    // Best-effort (quota / private mode / serialize failure). The next report
    // retries the write. Surfaced in dev so a persistent failure is visible.
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    if (import.meta.env.DEV) console.warn('image dims cache persist failed', e)
  }
}

function schedulePersist(): void {
  if (persistTimer !== null) return
  persistTimer = setTimeout(() => {
    persistTimer = null
    persist()
  }, PERSIST_DEBOUNCE_MS)
}

/** Dimensions learned from a prior load of this slug's image, if any. */
export function getImageDims(slug: string): ImageDims | undefined {
  return cache.get(slug)
}

/** Record an image's natural size after a successful load. Zero/negative
 * dimensions are refused — jsdom and a decode-failed image both report 0. */
export function rememberImageDims(slug: string, w: number, h: number): void {
  if (!(w > 0) || !(h > 0)) return
  const prev = cache.get(slug)
  if (prev && prev.w === w && prev.h === h) return
  // Re-insert so Map order stays LRU-ish and the persist slice keeps the
  // most recently seen entries.
  cache.delete(slug)
  cache.set(slug, { w, h })
  schedulePersist()
}
