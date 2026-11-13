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
import { useSyncExternalStore } from 'react'

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
  if (set.size > 0) safeSetItem(LOCKED_CONTROLS_CACHE_KEY, JSON.stringify([...set]))
  else safeRemoveItem(LOCKED_CONTROLS_CACHE_KEY)
  for (const listener of listeners) listener()
  return { added, removed }
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
  for (const listener of listeners) listener()
}
