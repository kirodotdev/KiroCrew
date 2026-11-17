/**
 * Controls this build locks, as the gateway reports them (`applyConfigLocks` in
 * `hooks/usePreviewFlagServerSync.ts`). Empty on the oss edition. An LTS build has:
 *
 * - `feature_previews`: every Feature Preview reads off (`utils/previewFlags.ts`).
 *   From `GET /api/config/schema`, which marks each preview path `lockedByPolicy`.
 * - `debug_tools`: Settings > Developer > Debug tools is hidden and its overlays stay
 *   off. From `locked_controls` on `GET /api/config/kirocrew`.
 *
 * The bundle is the same for both editions, so the dashboard learns this from the
 * gateway. The list is cached in the browser so the first paint, before those
 * reads answer, already applies it. The cache only hides things: clearing it
 * shows the surfaces until the next config read puts it back, and the gateway
 * treats every preview as off on its side regardless (RFC D12).
 */
import { useEffect, useState, useSyncExternalStore } from 'react'

import { safeGetItem, safeRemoveItem, safeSetItem } from './safeStorage'

/** The control ids the dashboard acts on. */
export const LOCKED_FEATURE_PREVIEWS = 'feature_previews'
export const LOCKED_DEBUG_TOOLS = 'debug_tools'

/** Browser cache of the last list the gateway sent. Not `mc-preview-` prefixed on purpose. */
export const LOCKED_CONTROLS_CACHE_KEY = 'kc-locked-controls'

function readCache(): ReadonlySet<string> {
  try {
    const raw = safeGetItem(LOCKED_CONTROLS_CACHE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [])
  } catch {
    return new Set()
  }
}

let locked: ReadonlySet<string> = readCache()
// Whether this browser has heard the gateway's Feature Previews lock at least once:
// a schema read this session, or a cached list only a schema read writes (`[]` on
// oss, or one naming the lock). A list naming Debug tools alone came from the config
// body before the schema answered, so it proves nothing. Unknown reads as LTS for
// what must stay off there (conductor mode), so a first visit never shows it early.
function cachedPreviewLockKnown(): boolean {
  if (safeGetItem(LOCKED_CONTROLS_CACHE_KEY) === null) return false
  return locked.size === 0 || locked.has(LOCKED_FEATURE_PREVIEWS)
}
let previewLockKnown = cachedPreviewLockKnown()
const listeners = new Set<() => void>()

/** Whether this build locks *control*. Synchronous, for readers outside React. */
export function controlLocked(control: string): boolean {
  return locked.has(control)
}

/**
 * Apply the list one config body carries (`[]` when it carries none). Returns the
 * ids this call locked and unlocked, so a caller can tell their readers now.
 */
export function applyLockedControls(next: readonly string[]): { added: string[]; removed: string[] } {
  const set = new Set(next)
  const added = [...set].filter(c => !locked.has(c))
  const removed = [...locked].filter(c => !set.has(c))
  if (added.length === 0 && removed.length === 0) return { added, removed }
  locked = set
  if (set.size > 0 || previewLockKnown) safeSetItem(LOCKED_CONTROLS_CACHE_KEY, JSON.stringify([...set]))
  else safeRemoveItem(LOCKED_CONTROLS_CACHE_KEY)
  for (const listener of listeners) listener()
  return { added, removed }
}

/** Whether the gateway's Feature Previews lock is known here (see `previewLockKnown`). */
export function featurePreviewLockKnown(): boolean {
  return previewLockKnown
}

/** Record that the config schema answered, so the Feature Previews lock is known.
 *  Kept in the cache (`[]` on oss) so the next page load knows it at first paint. */
export function noteFeaturePreviewLockKnown(): void {
  if (previewLockKnown) return
  previewLockKnown = true
  safeSetItem(LOCKED_CONTROLS_CACHE_KEY, JSON.stringify([...locked]))
  for (const listener of listeners) listener()
}

/** How long a one-shot action waits for the lock before it goes ahead as if conductor
 *  mode were off. One config schema read; a gateway that never answers it must not
 *  hold the page's first chat or a new chat's default for longer. */
export const FEATURE_PREVIEW_LOCK_WAIT_MS = 5000

/**
 * Resolve once the Feature Previews lock is known (`true`), or after *timeoutMs*
 * (`false`). For a one-shot action that reads conductor mode and is never redone (the
 * page's first pick of a chat, a new chat's conductor mode default): it waits for
 * this, then reads `crewModeEnabled()`, so it never acts on a lock nobody has heard yet.
 */
export function whenFeaturePreviewLockKnown(timeoutMs = FEATURE_PREVIEW_LOCK_WAIT_MS): Promise<boolean> {
  if (previewLockKnown) return Promise.resolve(true)
  return new Promise(resolve => {
    const done = (known: boolean) => { listeners.delete(onChange); clearTimeout(timer); resolve(known) }
    const onChange = () => { if (previewLockKnown) done(true) }
    const timer = setTimeout(() => done(false), timeoutMs)
    listeners.add(onChange)
  })
}

/** {@link whenFeaturePreviewLockKnown} for a component: `true` once the lock is known
 *  or the wait has lapsed. */
export function useFeaturePreviewLockSettled(): boolean {
  const known = useFeaturePreviewLockKnown()
  const [lapsed, setLapsed] = useState(false)
  useEffect(() => {
    if (known) return
    let live = true
    void whenFeaturePreviewLockKnown().then(() => { if (live) setLapsed(true) })
    return () => { live = false }
  }, [known])
  return known || lapsed
}

/** {@link featurePreviewLockKnown} for a component, re-rendering when it turns true. */
export function useFeaturePreviewLockKnown(): boolean {
  return useSyncExternalStore(subscribe, () => previewLockKnown, () => false)
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** {@link controlLocked} for a component, re-rendering when the list changes. */
export function useControlLocked(control: string): boolean {
  return useSyncExternalStore(subscribe, () => locked.has(control), () => false)
}

/** Reset the module state between tests. */
export function __resetLockedControlsForTests(): void {
  locked = readCache()
  previewLockKnown = cachedPreviewLockKnown()
  for (const listener of listeners) listener()
}

/** Set whether the Feature Previews lock is known, without touching the cache. Test helper. */
export function __setFeaturePreviewLockKnownForTests(known: boolean): void {
  previewLockKnown = known
  for (const listener of listeners) listener()
}
