/**
 * `requestMemoryMove()`: how an app asks to move memory from one provider to another
 * (App Kit level 10).
 *
 * An app may plan a move (`POST /api/memory/provider/migrations` with `dry_run`), but
 * never run one: the gateway refuses a run, a resume and a rollback from every app
 * token. This call opens the host-rendered move sheet
 * (`components/MemoryMoveSheet.tsx`), which plans the move, shows the owner what it
 * would write, and runs it only on the owner's press. The promise settles with what
 * happened: `'moved'` after a run the owner started finished, `'started'` when the
 * owner closed the sheet while the run was still copying (it keeps going and may still
 * write into the target), `'cancelled'` when the owner closed the sheet without a run
 * or after pressing Undo, `'unavailable'` when no sheet host is mounted, `'not_enabled'`
 * when this Crew does not run the memory provider Feature preview.
 * A move never switches which memory Crew uses; `requestMemoryProviderChange()` does.
 */

import { memoryProviderPreviewOff } from './memoryProviderPreview'

export interface MemoryMoveTarget {
  /** `'builtin'` or `app:<app id>/<provider id>`. */
  from: string
  /** `'builtin'` or `app:<app id>/<provider id>`; never the same as `from`. */
  to: string
}

export type MemoryMoveOutcome = 'moved' | 'started' | 'cancelled' | 'unavailable' | 'not_enabled'

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
  if (memoryProviderPreviewOff()) return Promise.resolve('not_enabled')
  if (!validTarget(target) || host === null) return Promise.resolve('unavailable')
  return host({ from: target.from, to: target.to })
}
