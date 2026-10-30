/**
 * The "New" tag a feature intro hands to its rail item when the user picks
 * "Not now", and the flight that carries it there.
 *
 * The intro closes and unmounts the moment the user decides, so the tag cannot
 * live in React state inside it: it is stored per browser in localStorage and
 * the rail reads it through `useFeatureNewTag`, the only React-facing part. A
 * tag clears the first time its route is opened, by any path (see `NavBadge`),
 * so a stale entry costs one pill at most.
 *
 * The flight is split the same way. The intro, which knows where the avatar is
 * on screen, measures it and hands the picture over here (`deliverFeatureNewTag`);
 * the rail row, which knows where its own pill is, draws the flyer and lands it
 * (`NavBadge`, through `takeFeatureNewTagFlight`). In between the tag is stored
 * as `arriving`: given, but drawn invisible so the pill can be measured before
 * the flyer reaches it. A reload mid-flight reads `arriving` as shown.
 *
 * Beside the tag, the intro's "Try now" route is kept per tag, so opening the
 * page from the tag lands where "Try now" would have.
 */
import { useSyncExternalStore } from 'react'

import { safeGetItem, safeSetItem } from './safeStorage'

const STORAGE_KEY = 'mc-feature-new-tags'
const ROUTE_KEY = 'mc-feature-new-tag-routes'
const CHANGE_EVENT = 'mc-feature-new-tags-change'

/** `arriving`: the flyer is on its way, the pill is rendered but invisible. */
export type FeatureNewTagState = 'arriving' | 'shown'

function readTags(): Record<string, FeatureNewTagState> {
  try {
    const raw = JSON.parse(safeGetItem(STORAGE_KEY) ?? '{}') as unknown
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, FeatureNewTagState> = {}
    // Any truthy value is a tag that was given. `arriving` is only ever meant
    // for the frames of one flight, so a stored one (a reload mid-flight) and
    // an older build's `true` both read as shown.
    for (const [id, v] of Object.entries(raw as Record<string, unknown>)) if (v) out[id] = 'shown'
    return out
  } catch {
    return {}
  }
}

let cache: Record<string, FeatureNewTagState> | null = null

function writeTag(navId: string, state: FeatureNewTagState | null) {
  const next = { ...(cache ?? readTags()) }
  if (state) next[navId] = state
  else delete next[navId]
  cache = next
  safeSetItem(STORAGE_KEY, JSON.stringify(next))
  window.dispatchEvent(new Event(CHANGE_EVENT))
}

function subscribe(onChange: () => void) {
  const onStorage = (e: StorageEvent) => {
    if (e.key !== STORAGE_KEY) return
    cache = null
    onChange()
  }
  window.addEventListener(CHANGE_EVENT, onChange)
  window.addEventListener('storage', onStorage)
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange)
    window.removeEventListener('storage', onStorage)
  }
}

/** The "New" tag on `navId`'s rail item right now: its state, or null for none. */
export function useFeatureNewTag(navId: string): FeatureNewTagState | null {
  return useSyncExternalStore(subscribe, () => {
    cache ??= readTags()
    return cache[navId] ?? null
  })
}

function readRoutes(): Record<string, string> {
  try {
    const raw = JSON.parse(safeGetItem(ROUTE_KEY) ?? '{}') as unknown
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, string> : {}
  } catch {
    return {}
  }
}

/** The route a tag's page should open on, when the intro named one. */
export function featureNewTagRoute(navId: string): string | null {
  const route = readRoutes()[navId]
  return typeof route === 'string' && route.startsWith('/') ? route : null
}

export function clearFeatureNewTag(navId: string) {
  const routes = readRoutes()
  if (navId in routes) {
    delete routes[navId]
    safeSetItem(ROUTE_KEY, JSON.stringify(routes))
  }
  if ((cache ?? readTags())[navId]) writeTag(navId, null)
}

/** Where the flyer starts: the avatar's box on screen at click time, and the
 *  picture it shows, so the flyer IS that avatar and not a stand-in. */
export interface FeatureNewTagOrigin {
  left: number
  top: number
  width: number
  height: number
  src: string
}

/** Measure an `<img>` as a flight origin; null when it is not laid out. */
export function featureNewTagOriginOf(img: HTMLImageElement | null): FeatureNewTagOrigin | null {
  if (!img || img.getClientRects().length === 0) return null
  const { left, top, width, height } = img.getBoundingClientRect()
  return { left, top, width, height, src: img.currentSrc || img.src }
}

/** The one flight waiting for a rail row to pick it up. One at a time: an intro
 *  is one dialog, and a second "Not now" cannot happen before the first lands. */
let pending: { navId: string; from: FeatureNewTagOrigin } | null = null

/**
 * Give `navId` its "New" tag, remembering `route` as the page to open from it,
 * flown there from `from` by the rail row (see `NavBadge`).
 *
 * The tag is stored FIRST, before anything about the flight is decided: the
 * tag is the promise to the user, the flight is decoration on it. Falls back to
 * showing the tag with no flight when motion is reduced, when there is no
 * origin, or when the rail row is not on screen (a closed phone drawer, a
 * hidden preview surface): there is nowhere visible to fly to.
 */
export function deliverFeatureNewTag(navId: string, from: FeatureNewTagOrigin | null, route?: string) {
  if (route) safeSetItem(ROUTE_KEY, JSON.stringify({ ...readRoutes(), [navId]: route }))
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  // A row can render twice (the phone drawer's copy and the rail's); any one
  // that is laid out is a destination.
  const rowOnScreen = [...document.querySelectorAll<HTMLElement>(`[data-onboarding-nav="${CSS.escape(navId)}"]`)]
    .some(row => row.getClientRects().length > 0)
  if (reduce || !from || !rowOnScreen) {
    pending = null
    writeTag(navId, 'shown')
    return
  }
  pending = { navId, from }
  writeTag(navId, 'arriving')
  // The row re-renders its pill synchronously on the store change and claims
  // the flight in its layout effect. If nothing claimed it by the next frame --
  // the pill is not rendered, say a collapsed rail whose corner the unread dot
  // already holds -- there is no flyer to wait for, so the tag just shows.
  requestAnimationFrame(() => {
    if (pending?.navId !== navId) return
    pending = null
    landFeatureNewTag(navId)
  })
}

/** Claim the flight waiting for `navId`, once. The row whose pill is on screen
 *  calls this when it sees the tag `arriving`; a second row finds nothing. */
export function takeFeatureNewTagFlight(navId: string): FeatureNewTagOrigin | null {
  if (pending?.navId !== navId) return null
  const { from } = pending
  pending = null
  return from
}

/** The flyer has arrived (or the flight was skipped): show the pill. A tag
 *  cleared mid-flight -- the route opened before the landing -- stays cleared. */
export function landFeatureNewTag(navId: string) {
  if ((cache ?? readTags())[navId] === 'arriving') writeTag(navId, 'shown')
}
