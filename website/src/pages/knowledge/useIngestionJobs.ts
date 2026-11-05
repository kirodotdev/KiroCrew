import { useCallback, useEffect, useRef, useState } from 'react'
import type { IngestionJob } from './types'

/** How long a finished upload's progress rows stay on screen. */
export const INGESTION_ROWS_LINGER_MS = 5000

/**
 * The upload progress rows the Sources tab shows, and the timer that clears
 * them once an upload settles.
 *
 * The rows belong to the newest upload. `start` returns that upload's batch
 * number; `update` and `scheduleClear` from an older batch do nothing, so a
 * previous upload can neither overwrite nor clear the newer one's rows. The
 * clear timer is kept so a new upload and unmount can cancel it.
 */
export function useIngestionJobs() {
  const [jobs, setJobs] = useState<IngestionJob[]>([])
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const batch = useRef(0)

  const cancelClear = useCallback(() => {
    if (clearTimer.current !== null) {
      clearTimeout(clearTimer.current)
      clearTimer.current = null
    }
  }, [])

  // On unmount, cancel the clear and retire the current batch, so an upload
  // still in flight cannot arm a new timer for a page that is gone.
  useEffect(() => () => {
    cancelClear()
    batch.current += 1
  }, [cancelClear])

  /** Show a new upload's rows, cancelling any pending clear. */
  const start = useCallback((rows: IngestionJob[]): number => {
    cancelClear()
    batch.current += 1
    setJobs(rows)
    return batch.current
  }, [cancelClear])

  /** Replace the rows, if `owner` is still the newest upload. */
  const update = useCallback((owner: number, rows: IngestionJob[]) => {
    if (owner === batch.current) setJobs(rows)
  }, [])

  /** Clear the rows after the linger window, if `owner` is still the newest upload. */
  const scheduleClear = useCallback((owner: number) => {
    if (owner !== batch.current) return
    cancelClear()
    clearTimer.current = setTimeout(() => {
      clearTimer.current = null
      setJobs([])
    }, INGESTION_ROWS_LINGER_MS)
  }, [cancelClear])

  return { jobs, start, update, scheduleClear }
}
