/**
 * `requestMemoryBench()`: how an app asks to compare memory providers on a benchmark
 * (App Kit level 10, beside the other memory provider verbs).
 *
 * An app may plan a run (`POST /api/memory/provider/bench/runs` with `dry_run`), but
 * never start one: the gateway refuses a run, a resume and a cancel from every app
 * token. This call opens the host-rendered sheet (`components/MemoryBenchSheet.tsx`),
 * which plans the run, shows the owner each provider's egress, cleanup and model call
 * budget, and starts it only on the owner's press. The promise settles with
 * `'started'` and the run id after the owner started it, `'cancelled'` when the owner
 * closed the sheet, `'unavailable'` when no sheet host is mounted or the request is
 * malformed, `'not_enabled'` when this Crew does not run the memory provider Feature
 * preview. Results are read from `GET /api/memory/provider/bench/runs/{id}/report`.
 */

import type { MemoryBenchRequest } from '../api/client/memory'
import { memoryProviderPreviewOff } from './memoryProviderPreview'

export type MemoryBenchOutcome =
  | { outcome: 'started'; runId: string }
  | { outcome: 'cancelled' | 'unavailable' | 'not_enabled' }

type SheetHost = (request: MemoryBenchRequest) => Promise<MemoryBenchOutcome>

let host: SheetHost | null = null

/** Mount point for the host sheet; returns the unregister function. */
export function registerMemoryBenchSheetHost(next: SheetHost): () => void {
  host = next
  return () => {
    if (host === next) host = null
  }
}

function validRequest(request: unknown): request is MemoryBenchRequest {
  if (!request || typeof request !== 'object') return false
  const { suite, providers } = request as Record<string, unknown>
  return (
    typeof suite === 'string' &&
    suite.length > 0 &&
    Array.isArray(providers) &&
    providers.length > 0 &&
    providers.length <= 8 &&
    providers.every(p => typeof p === 'string' && p.length > 0)
  )
}

export function requestMemoryBench(request: MemoryBenchRequest): Promise<MemoryBenchOutcome> {
  if (memoryProviderPreviewOff()) return Promise.resolve({ outcome: 'not_enabled' })
  if (!validRequest(request) || host === null) return Promise.resolve({ outcome: 'unavailable' })
  // The sheet always plans first; whatever the app passed, it cannot skip that. The
  // arrays are copied so the app cannot change the run after the owner saw its plan.
  const { dry_run: _ignored, ...rest } = request
  return host({ ...rest, providers: [...rest.providers], ...(rest.k ? { k: [...rest.k] } : {}) })
}
