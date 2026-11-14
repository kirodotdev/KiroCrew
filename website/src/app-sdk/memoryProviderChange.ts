/**
 * `requestMemoryProviderChange()`: the only way an app asks to change which memory
 * Crew uses (App Kit level 8).
 *
 * An app never switches memory itself: `PUT /api/memory/provider` refuses every app
 * token. This call opens a host-rendered confirmation sheet
 * (`components/MemoryProviderSheet.tsx`), and only the owner's press there performs
 * the switch. The promise settles with what happened: `'switched'` after the owner
 * confirmed and the gateway accepted, `'cancelled'` when the owner closed the sheet,
 * `'unavailable'` when no sheet host is mounted (an embedded surface without one).
 */

export interface MemoryProviderChangeTarget {
  /** `'builtin'`, `'replace'` or `'augment'`. */
  mode: 'builtin' | 'replace' | 'augment'
  /** `app:<app id>/<provider id>`; omitted for `'builtin'`. */
  id?: string
}

export type MemoryProviderChangeOutcome = 'switched' | 'cancelled' | 'unavailable'

type SheetHost = (target: MemoryProviderChangeTarget) => Promise<MemoryProviderChangeOutcome>

let host: SheetHost | null = null

/** Mount point for the host sheet; returns the unregister function. */
export function registerMemoryProviderSheetHost(next: SheetHost): () => void {
  host = next
  return () => {
    if (host === next) host = null
  }
}

function validTarget(target: unknown): target is MemoryProviderChangeTarget {
  if (!target || typeof target !== 'object') return false
  const { mode, id } = target as Record<string, unknown>
  if (mode === 'builtin') return id === undefined || typeof id === 'string'
  return (mode === 'replace' || mode === 'augment') && typeof id === 'string' && id.startsWith('app:')
}

export function requestMemoryProviderChange(
  target: MemoryProviderChangeTarget,
): Promise<MemoryProviderChangeOutcome> {
  if (!validTarget(target) || host === null) return Promise.resolve('unavailable')
  return host({ mode: target.mode, ...(target.id ? { id: target.id } : {}) })
}
