/** The manual order of pinned sessions: stored on the gateway (each slot row
 *  carries `pin_rank`), reconciled against pin membership, and reordered by
 *  drag or Alt+Arrow. */
import { useMemo, useState, useRef, useEffect, useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { clearLegacyPinnedSessionOrder, movePinnedSession, rankedPinnedKeys, readLegacyPinnedSessionOrder, reconcilePinnedSessionOrder } from '../../utils/pinnedSessionOrder'
import { compareBySort, type SortKey } from '../chat/sessionOrder'
import { haptic } from '../../lib/haptic'
import { api } from '../../api/client'
import { errMessage } from '../../utils/thunkError'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppStore } from '../../store'
import { fetchSlots, setPinRanks } from '../../store/dashboardSlice'
import type { Slot } from './types'
import { sessionRowsInScope } from '../chat/sessionRowNav'
import { pinWritesSettled } from '../../hooks/useSessionActions'

/** The pinned-section order: read from the rows' ranks, written to the gateway. */
export function usePinnedSessionOrder({ localSlots, sortKey }: {
  localSlots: Slot[]
  sortKey: SortKey
}) {
  const dispatch = useAppDispatch()
  // Pinned membership AND the order inside the pinned section live on the
  // gateway: each slot row carries `pin_rank` (see utils/pinnedSessionOrder.ts
  // and dashboard/pinned_session_order.py). Rows with no rank follow the
  // ranked ones in the sidebar's own sort, so a person who never reordered
  // keeps the plain sort.
  //
  // Both collections read `localSlots`, NOT the merged `allRows`: pin state and
  // pin order are local sidebar metadata keyed by local slot key, and a peer row
  // has no entry in either. Feeding it merged rows would put a peer key into the
  // gateway's order, where the next write would only drop it again.
  const pinned = useMemo(() => new Set(localSlots.filter(s => s.pinned).map(s => s.key)), [localSlots])
  const storedPinnedOrder = useMemo(() => rankedPinnedKeys(localSlots), [localSlots])
  const naturalPinnedOrder = useMemo(
    () => localSlots.filter(s => s.pinned).sort((a, b) => compareBySort(a, b, sortKey)).map(s => s.key),
    [localSlots, sortKey],
  )
  // While reorder writes are queued, the latest gesture is the order the
  // gateway is about to store, so it outranks the ranks on the rows: a slots
  // frame serialized before those writes landed carries older ranks, and a
  // gesture derived from them would drop the one in between.
  const [pendingPinnedOrder, setPendingPinnedOrder] = useState<string[] | null>(null)
  const pinnedOrder = useMemo(
    () => reconcilePinnedSessionOrder(pendingPinnedOrder ?? storedPinnedOrder, naturalPinnedOrder),
    [pendingPinnedOrder, storedPinnedOrder, naturalPinnedOrder],
  )
  const pinnedRank = useMemo(() => new Map(pinnedOrder.map((key, index) => [key, index])), [pinnedOrder])
  // A reorder paints the order the person just made, then writes it. Writes
  // go out one at a time, in gesture order, so the gateway stores the last
  // gesture even when two land within one round trip. Each also waits for pin
  // writes already sent (`pinWritesSettled`), so an order naming a session the
  // person just pinned never reaches the gateway before that pin does. The
  // client never
  // rebuilds an order from what it remembers: a failure shows why and re-reads
  // the slots, whose ranks are the gateway's (`fetchSlots` yields to any newer
  // drag through the row-write stamps), and a success paints the order the
  // gateway answered with, so there is no stale order to restore. Only the
  // last write in the queue re-reads.
  const [pinnedOrderError, setPinnedOrderError] = useState('')
  const pinnedOrderMutation = useMutation({
    mutationFn: ({ keys, onlyIfUnset = false, expectedCreated }: {
      keys: string[]; onlyIfUnset?: boolean; expectedCreated?: Record<string, string>
    }) => (expectedCreated && Object.keys(expectedCreated).length > 0
      ? api.setPinnedOrder(keys, onlyIfUnset, expectedCreated)
      : api.setPinnedOrder(keys, onlyIfUnset)),
  })
  const { mutateAsync: mutatePinnedOrder } = pinnedOrderMutation
  const pinnedOrderTail = useRef<Promise<unknown>>(Promise.resolve())
  const store = useAppStore()
  // Each write records the slots generation at the moment it is sent. A slots
  // frame that lands after that carries the gateway's order as of a later
  // write (this one's own, or another browser's), so it outranks this write's
  // HTTP answer and any re-read this write would start.
  const savePinnedOrder = useCallback((vars: { keys: string[]; onlyIfUnset?: boolean; expectedCreated?: Record<string, string> }) => {
    let sentGeneration = 0
    const request = pinnedOrderTail.current.catch(() => undefined)
      .then(() => pinWritesSettled())
      .then(() => {
        sentGeneration = store.getState().dashboard.slotsGeneration ?? 0
        return mutatePinnedOrder(vars)
      })
    pinnedOrderTail.current = request
    const framesSinceSent = () => (store.getState().dashboard.slotsGeneration ?? 0) !== sentGeneration
    return { request, framesSinceSent }
  }, [mutatePinnedOrder, store])
  // Each row's `created` pins the reorder to the slot this sidebar saw, so a
  // key closed and recreated for another conversation is refused, not ranked.
  const createdByKey = useMemo(
    () => new Map(localSlots.filter(s => s.created).map(s => [s.key, s.created as string])),
    [localSlots],
  )
  const commitPinnedOrder = useCallback((next: string[]) => {
    setPinnedOrderError('')
    setPendingPinnedOrder(next)
    dispatch(setPinRanks(next))
    const expectedCreated: Record<string, string> = {}
    for (const key of next) {
      const created = createdByKey.get(key)
      if (created) expectedCreated[key] = created
    }
    const { request, framesSinceSent } = savePinnedOrder({ keys: next, expectedCreated })
    request
      .then(res => {
        // The gateway's answer is its stored order. A later gesture already
        // queued behind this write carries a newer one, so only the last
        // write in the queue paints its answer and hands the order back to
        // the rows. A slots frame since the send is newer still, and the rows
        // already hold it.
        if (pinnedOrderTail.current !== request) return
        if (Array.isArray(res?.order) && !framesSinceSent()) dispatch(setPinRanks(res.order))
        setPendingPinnedOrder(null)
      })
      .catch((err: unknown) => {
        setPinnedOrderError(errMessage(err) || i18nT('components.errorBoundary.something_went_wrong'))
        // Re-read only when no later gesture is queued: that write's own
        // answer, or its own re-read, is newer than anything read now. The
        // pending gesture is dropped, not restored: the rows show whatever
        // the gateway holds.
        if (pinnedOrderTail.current !== request) return
        setPendingPinnedOrder(null)
        if (!framesSinceSent()) dispatch(fetchSlots())
      })
  }, [dispatch, savePinnedOrder, createdByKey])
  const reorderPinned = useCallback((activeKey: string, overKey: string) => {
    // The drop seats: the row's position is the thing that moved.
    haptic('light')
    const next = movePinnedSession(pinnedOrder, activeKey, overKey)
    if (next.every((key, index) => key === pinnedOrder[index])) return
    commitPinnedOrder(next)
  }, [pinnedOrder, commitPinnedOrder])
  return {
    pinned, pinnedOrder, pinnedRank, reorderPinned, pinnedOrderError, setPinnedOrderError,
    orderState: { storedPinnedOrder, naturalPinnedOrder, savePinnedOrder, pinnedOrderTail, setPinnedOrderError },
  }
}

/** The stored order and its write queue, handed from the order owner to its authority. */
type PinnedOrderState = ReturnType<typeof usePinnedSessionOrder>['orderState']

/** One-time hand-off of the order this browser kept in localStorage before the
 *  gateway stored it. Only an unranked gateway adopts it, so a second browser
 *  with an older local order cannot overwrite a newer shared one. */
export function usePinnedOrderAuthority({ orderState, slotsLoaded }: {
  orderState: PinnedOrderState
  slotsLoaded: boolean
}) {
  const dispatch = useAppDispatch()
  const { storedPinnedOrder, naturalPinnedOrder, savePinnedOrder, pinnedOrderTail, setPinnedOrderError } = orderState
  const legacyPinnedOrderMigrated = useRef(false)
  useEffect(() => {
    if (!slotsLoaded || legacyPinnedOrderMigrated.current) return
    const legacy = readLegacyPinnedSessionOrder()
    if (legacy.length === 0) { legacyPinnedOrderMigrated.current = true; return }
    if (storedPinnedOrder.length > 0) {
      legacyPinnedOrderMigrated.current = true
      clearLegacyPinnedSessionOrder()
      return
    }
    // The whole old order goes to the gateway, not only the sessions this frame
    // shows: the gateway restores open sessions after it starts serving, so a
    // key missing here may be one it has not restored yet, and dropping it
    // would lose its place for good. The gateway keeps such keys.
    const next = [...new Set([...legacy, ...naturalPinnedOrder])]
    legacyPinnedOrderMigrated.current = true
    // Conditional: another browser may have saved an order since this one
    // read the slots, and a stale local order must not replace it.
    const { request: handOff, framesSinceSent } = savePinnedOrder({ keys: next, onlyIfUnset: true })
    handOff
      .then(res => {
        if (pinnedOrderTail.current === handOff && Array.isArray(res?.order) && !framesSinceSent()) {
          dispatch(setPinRanks(res.order))
        }
        clearLegacyPinnedSessionOrder()
      })
      .catch((err: unknown) => {
        // 409: the gateway already has an order, which the next slots frame
        // carries. Any other failure keeps the local copy for the next page
        // load (retrying here would re-send on every slots frame) and says
        // the order was not saved.
        if ((err as { status?: number } | null)?.status === 409) clearLegacyPinnedSessionOrder()
        else setPinnedOrderError(errMessage(err) || i18nT('components.errorBoundary.something_went_wrong'))
      })
  }, [slotsLoaded, storedPinnedOrder, naturalPinnedOrder, dispatch, savePinnedOrder, pinnedOrderTail, setPinnedOrderError])
}

/** Where the automatic section starts, and Alt+Arrow pinned reorder. */
export function usePinnedKeyboardReorder({ searchRanked, pinned, pinnedOrder, slotFolders, reorderPinned }: {
  searchRanked: Map<string, number> | null
  pinned: Set<string>
  pinnedOrder: string[]
  slotFolders: Record<string, string>
  reorderPinned: (activeKey: string, overKey: string) => void
}) {
  const startsAutomaticSection = useCallback((list: readonly Slot[], index: number) => (
    !searchRanked && index > 0 && pinned.has(list[index - 1].key) && !pinned.has(list[index].key)
  ), [searchRanked, pinned])
  // Read through a ref, not the dependency array: `slotFolders` and
  // `pinnedOrder` are rebuilt whenever the slot list changes, so a callback
  // closing over them takes a new identity on EVERY slots frame — and this
  // callback is a prop of every SessionRow, so one unstable reference voids
  // all N memo boundaries per frame and defeats both the row memo and the
  // displacement window for any membership change. The handler runs only on
  // a keypress, where the latest values are what it wants anyway.
  const keyboardReorderInputsRef = useRef({ searchRanked, pinnedOrder, slotFolders, reorderPinned })
  keyboardReorderInputsRef.current = { searchRanked, pinnedOrder, slotFolders, reorderPinned }
  const reorderPinnedByKeyboard = useCallback((
    key: string,
    container: string,
    delta: -1 | 1,
    row: HTMLElement,
  ) => {
    const { searchRanked, pinnedOrder, slotFolders, reorderPinned } = keyboardReorderInputsRef.current
    if (searchRanked) return
    const rendered = new Set(sessionRowsInScope(row).map(el => el.dataset.sessionRow || ''))
    const peers = pinnedOrder.filter(candidate => rendered.has(candidate) && (container === 'flat'
      || (slotFolders[candidate] || 'root') === container))
    const index = peers.indexOf(key)
    const target = peers[index + delta]
    if (index < 0 || !target) return
    reorderPinned(key, target)
  }, [])
  return { startsAutomaticSection, reorderPinnedByKeyboard }
}
