import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * How long an armed Delete stays armed before decaying back to its disarmed
 * label. Long enough to move the pointer back to the same button and click
 * again; short enough that an accidental first click does not leave a live
 * destructive button waiting on the row.
 */
export const ARM_REVERT_MS = 3000

export interface ArmedDelete {
  /** The row whose Delete is armed (showing its confirm label), or null. */
  armedId: string | null
  /** Rows whose delete request is still in flight. */
  pendingIds: ReadonlySet<string>
  /**
   * First click: arm the row. Arming a row disarms any other armed row.
   * A no-op while the same row's delete is pending — arming a row that is
   * already deleting would paint a live confirm label whose second click
   * does nothing.
   */
  arm: (id: string) => void
  /**
   * Second click: run the delete. A no-op unless this row is CURRENTLY
   * armed, so a click that lands after the decay window (or any call for an
   * unarmed row) can never delete — arming again is the only way back to a
   * live confirm. Also a no-op while the same row is already pending, so a
   * re-click can never fire a duplicate request even if the caller's
   * `disabled` wiring lapses. Never rejects: `deleteFn` owns its error
   * surfacing (see the hook doc).
   */
  confirm: (id: string) => Promise<void>
  /** Whether this row's delete is still in flight (`pendingIds.has(id)`). */
  isDeleting: (id: string) => boolean
}

/**
 * The arm→Confirm→decay state machine for a row-level destructive button:
 * the first click arms the row (its label states what the second click will
 * destroy), a second click within the revert window deletes, and the armed
 * state decays back to disarmed on its own.
 *
 * Why the in-flight set is a SET keyed by id, not a scalar "deleting id":
 * two rows can be in flight at once, and a scalar is overwritten by the
 * second delete — whichever settles first then clears it, re-enabling the
 * other still-pending row so a re-click fires a duplicate request.
 *
 * Why `armedId` IS a scalar: only one row may be confirmable at a time, so
 * arming a row deliberately disarms any other armed row. Settling a delete,
 * however, disarms only its OWN row — a sweep (`setArmedId(null)`) would
 * silently disarm an unrelated row the user had just armed.
 *
 * `deleteFn` owns its error surfacing (report failures to the user inside
 * it); `confirm` drops a rejection after removing the pending id, so it
 * never rejects — it is invoked from event handlers, where a rejection has
 * no handler and would only surface as an unhandled-rejection report.
 */
export function useArmedDelete(deleteFn: (id: string) => Promise<unknown>): ArmedDelete {
  const [armedId, setArmedId] = useState<string | null>(null)
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set())
  // Ref mirrors of both states, readable synchronously inside `confirm`: the
  // re-entrancy guard and the keyed disarm cannot wait a render for state,
  // and a setState-updater must stay side-effect free (StrictMode runs it
  // twice), so the timer clear cannot live inside one either.
  const armedRef = useRef<string | null>(null)
  const pendingRef = useRef<Set<string>>(new Set())
  const revertTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setArmed = useCallback((id: string | null) => {
    armedRef.current = id
    setArmedId(id)
  }, [])

  const arm = useCallback((id: string) => {
    if (pendingRef.current.has(id)) return
    setArmed(id)
    if (revertTimer.current) clearTimeout(revertTimer.current)
    revertTimer.current = setTimeout(() => setArmed(null), ARM_REVERT_MS)
  }, [setArmed])

  useEffect(() => () => { if (revertTimer.current) clearTimeout(revertTimer.current) }, [])

  const confirm = useCallback(async (id: string) => {
    if (pendingRef.current.has(id)) return
    // The machine enforces its own arm-before-delete contract: a click that
    // lands after the decay timer fired but before React repaints the
    // disarmed label reaches here with a stale armed closure, and must not
    // delete. Only the currently armed row may confirm.
    if (armedRef.current !== id) return
    if (revertTimer.current) {
      clearTimeout(revertTimer.current)
      revertTimer.current = null
    }
    setArmed(null)
    pendingRef.current.add(id)
    setPendingIds(new Set(pendingRef.current))
    try {
      await deleteFn(id)
    } catch {
      // Dropped by contract: deleteFn owns its error surfacing (see hook doc).
    } finally {
      pendingRef.current.delete(id)
      setPendingIds(new Set(pendingRef.current))
    }
  }, [deleteFn, setArmed])

  const isDeleting = useCallback((id: string) => pendingIds.has(id), [pendingIds])

  return { armedId, pendingIds, arm, confirm, isDeleting }
}
