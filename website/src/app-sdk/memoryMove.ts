/**
 * `requestMemoryMove()`: how an app asks to move memory from one provider to another
 * (App Kit level 8).
 *
 * An app may plan a move (`POST /api/memory/provider/migrations` with `dry_run`), but
 * never run one: the gateway refuses a run, a resume and a rollback from every app
 * token. This call opens the host-rendered move sheet
 * (`components/MemoryMoveSheet.tsx`), which plans the move, shows the owner what it
 * would write, and runs it only on the owner's press. The promise settles with what
 * happened: `'moved'` after a run the owner started finished, `'cancelled'` when the
 * owner closed the sheet without one, `'unavailable'` when no sheet host is mounted.
 * A move never switches which memory Crew uses; `requestMemoryProviderChange()` does.
 */

export interface MemoryMoveTarget {
  /** `'builtin'` or `app:<app id>/<provider id>`. */
  from: string
  /** `'builtin'` or `app:<app id>/<provider id>`; never the same as `from`. */
  to: string
}

export type MemoryMoveOutcome = 'moved' | 'cancelled' | 'unavailable'

type SheetHost = (target: MemoryMoveTarget) => Promise<MemoryMoveOutcome>

let host: SheetHost | null = null

/** Mount point for the host sheet; returns the unregister function. */
export function registerMemoryMoveSheetHost(next: SheetHost): () => void {
  host = next
  return () => {
    if (host === next) host = null
  }
}

function providerId(value: unknown): value is string {
  return value === 'builtin' || (typeof value === 'string' && value.startsWith('app:'))
}

function validTarget(target: unknown): target is MemoryMoveTarget {
  if (!target || typeof target !== 'object') return false
  const { from, to } = target as Record<string, unknown>
  return providerId(from) && providerId(to) && from !== to
}

export function requestMemoryMove(target: MemoryMoveTarget): Promise<MemoryMoveOutcome> {
  if (!validTarget(target) || host === null) return Promise.resolve('unavailable')
  return host({ from: target.from, to: target.to })
}
