/** The manual order of pinned sessions: stored on the gateway (each slot row
 *  carries `pin_rank`), reconciled against pin membership, and reordered by
 *  drag or Alt+Arrow. */
import { useMemo, useRef, useEffect, useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { clearLegacyPinnedSessionOrder, movePinnedSession, rankedPinnedKeys, readLegacyPinnedSessionOrder, reconcilePinnedSessionOrder } from '../../utils/pinnedSessionOrder'
import { compareBySort, type SortKey } from '../chat/sessionOrder'
import { haptic } from '../../lib/haptic'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { fetchSlots, setPendingPinOrder, setPinOrderError, setPinRanks } from '../../store/dashboardSlice'
import type { Slot } from './types'
import { sessionRowsInScope } from '../chat/sessionRowNav'
import { pinMutationKeysInFlight, pinWritesSettled } from '../../hooks/useSessionActions'

/** What the person reads when a move was not saved. The server's own `error`
 *  is written for logs ("the gateway could not write its order file"), so it
 *  is not shown. Every failure re-reads the slots, so both texts say the rows
 *  went back to the saved order. A 409 means the pinned set changed under
 *  the move (a session unpinned, closed or replaced). */
export function pinnedMoveFailureText(err: unknown): string {
  return (err as { status?: number } | null)?.status === 409
    ? i18nT('pages.chatSidebar.pinned_order_move_conflict')
    : i18nT('pages.chatSidebar.pinned_order_move_failed')
}

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
  // gesture derived from them would drop the one in between. It is kept in
  // the store beside the per-store write queue, so a sidebar that remounts
  // while writes are queued still builds on it.
  const pendingPinnedOrder = useAppSelector(state => state.dashboard.pendingPinOrder ?? null)
  const setPendingPinnedOrder = useCallback(
    (order: string[] | null) => { dispatch(setPendingPinOrder(order)) },
    [dispatch],
  )
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
  // The failure notice lives in the store with the pending order, so a write
  // that fails after the sidebar remounted still shows in the reopened one.
  const pinnedOrderError = useAppSelector(state => state.dashboard.pinOrderError ?? '')
  const setPinnedOrderError = useCallback(
    (message: string) => { dispatch(setPinOrderError(message)) },
    [dispatch],
  )
  const pinnedOrderMutation = useMutation({
    mutationFn: ({ keys, onlyIfUnset = false, expectedCreated }: {
      keys: string[]; onlyIfUnset?: boolean; expectedCreated?: Record<string, string>
    }) => (expectedCreated && Object.keys(expectedCreated).length > 0
      ? api.setPinnedOrder(keys, onlyIfUnset, expectedCreated)
      : api.setPinnedOrder(keys, onlyIfUnset)),
  })
  const { mutateAsync: mutatePinnedOrder } = pinnedOrderMutation
  const store = useAppStore()
  const queue = pinnedOrderQueue(store)
  // Writes queue per store, not per sidebar mount: collapsing and reopening
  // the sidebar remounts this hook, and a fresh queue there would let a newer
  // gesture land before an older mount's queued write, which would then
  // overwrite it. Each answer carries the gateway's order revision, and the
  // store applies it only if no newer revision (a later write's frame) has
  // landed, so a slots frame arriving mid-save cannot discard the answer.
  const savePinnedOrder = useCallback((vars: { keys: string[]; onlyIfUnset?: boolean; expectedCreated?: Record<string, string> }) => {
    const request = queue.tail.catch(() => undefined)
      .then(() => pinWritesSettled())
      .then(() => mutatePinnedOrder(vars))
    queue.tail = request
    return { request, isLatest: () => queue.tail === request }
  }, [mutatePinnedOrder, queue])
  // Each row's `created` pins the reorder to the slot this sidebar saw, so a
  // key closed and recreated for another conversation is refused, not ranked.
  const createdByKey = useMemo(
    () => new Map(localSlots.filter(s => s.created).map(s => [s.key, s.created as string])),
    [localSlots],
  )
  const applyAnswer = useCallback((res: unknown) => {
    const answer = res as { order?: unknown; rev?: unknown } | null | undefined
    if (Array.isArray(answer?.order) && typeof answer?.rev === 'number') {
      dispatch(setPinRanks({ order: answer.order as string[], rev: answer.rev }))
    }
  }, [dispatch])
  const commitPinnedOrder = useCallback((next: string[]) => {
    setPinnedOrderError('')
    setPendingPinnedOrder(next)
    dispatch(setPinRanks(next))
    const expectedCreated: Record<string, string> = {}
    for (const key of next) {
      const created = createdByKey.get(key)
      if (created) expectedCreated[key] = created
    }
    const { request, isLatest } = savePinnedOrder({ keys: next, expectedCreated })
    request
      .then(res => {
        // The gateway's answer is its stored order, applied by revision. A
        // later gesture already queued behind this write still owns the
        // painted order, so only the last write hands it back to the rows.
        applyAnswer(res)
        if (isLatest()) setPendingPinnedOrder(null)
      })
      .catch((err: unknown) => {
        setPinnedOrderError(pinnedMoveFailureText(err))
        // Re-read only when no later gesture is queued: that write's own
        // answer, or its own re-read, is newer than anything read now. The
        // pending gesture is dropped, not restored: the rows show whatever
        // the gateway holds.
        if (!isLatest()) return
        setPendingPinnedOrder(null)
        dispatch(fetchSlots())
      })
  }, [dispatch, savePinnedOrder, createdByKey, applyAnswer, setPendingPinnedOrder, setPinnedOrderError])
  const reorderPinned = useCallback((activeKey: string, overKey: string) => {
    // The drop seats: the row's position is the thing that moved.
    haptic('light')
    const next = movePinnedSession(pinnedOrder, activeKey, overKey)
    if (next.every((key, index) => key === pinnedOrder[index])) return
    commitPinnedOrder(next)
  }, [pinnedOrder, commitPinnedOrder])
  return {
    pinned, pinnedOrder, pinnedRank, reorderPinned, pinnedOrderError, setPinnedOrderError,
    orderState: { storedPinnedOrder, naturalPinnedOrder, savePinnedOrder, applyAnswer, setPinnedOrderError },
  }
}

/** One write queue per dashboard store, shared by every sidebar mount. */
const pinnedOrderQueues = new WeakMap<object, { tail: Promise<unknown> }>()
function pinnedOrderQueue(store: object): { tail: Promise<unknown> } {
  let queue = pinnedOrderQueues.get(store)
  if (!queue) {
    queue = { tail: Promise.resolve() }
    pinnedOrderQueues.set(store, queue)
  }
  return queue
}

/** The stored order and its write queue, handed from the order owner to its authority. */
type PinnedOrderState = ReturnType<typeof usePinnedSessionOrder>['orderState']

/** First-load seed of the gateway's pinned order: the order this browser kept
 *  in localStorage before the gateway stored it, else the natural order it
 *  shows. Only an unranked gateway adopts it (`only_if_unset`), so a second
 *  browser cannot overwrite a newer shared order. As on `main`, it waits for
 *  the slots, the tag columns and any pin write in flight, and a board view
 *  does not seed the natural order: a board's projection is not the order the
 *  person sees in the list. */
export function usePinnedOrderAuthority({ orderState, slotsLoaded, tagColumnsSettled, boardProjection }: {
  orderState: PinnedOrderState
  slotsLoaded: boolean
  tagColumnsSettled: boolean
  boardProjection: boolean
}) {
  const { storedPinnedOrder, naturalPinnedOrder, savePinnedOrder, applyAnswer, setPinnedOrderError } = orderState
  const legacyPinnedOrderMigrated = useRef(false)
  useEffect(() => {
    if (!slotsLoaded || !tagColumnsSettled || pinMutationKeysInFlight().length > 0) return
    if (legacyPinnedOrderMigrated.current) return
    const legacy = readLegacyPinnedSessionOrder()
    // Row ranks are not proof the gateway stored an order: an optimistic
    // drag paints ranks before its POST answers, and that POST can fail. So
    // ranked rows end the hand-off only when there is no local copy to lose.
    // A local copy always goes through the conditional hand-off below, and
    // is cleared only on its success or on the gateway's 409.
    if (storedPinnedOrder.length > 0 && legacy.length === 0) {
      legacyPinnedOrderMigrated.current = true
      return
    }
    // A board view hands over only an order this browser already kept; it
    // leaves the natural-order seed to the next list view.
    if (boardProjection && legacy.length === 0) return
    // Nothing ranked yet. As before the gateway stored the order, the first
    // load freezes the pinned section in the sort the person is looking at:
    // the old local order if this browser kept one, then the natural order,
    // so later pins append instead of re-sorting. An empty roster waits for
    // its first pins.
    if (legacy.length === 0 && naturalPinnedOrder.length === 0) return
    // The whole old order goes to the gateway, not only the sessions this frame
    // shows: the gateway restores open sessions after it starts serving, so a
    // key missing here may be one it has not restored yet, and dropping it
    // would lose its place for good. The gateway keeps such keys.
    const next = [...new Set([...legacy, ...naturalPinnedOrder])]
    legacyPinnedOrderMigrated.current = true
    // Conditional: another browser may have saved an order since this one
    // read the slots, and this one must not replace it.
    const { request: handOff } = savePinnedOrder({ keys: next, onlyIfUnset: true })
    handOff
      .then(res => {
        applyAnswer(res)
        if (legacy.length > 0) clearLegacyPinnedSessionOrder()
      })
      .catch((err: unknown) => {
        // 409: the gateway already has an order, which the next slots frame
        // carries. Any other failure keeps the local copy for the next page
        // load (retrying here would re-send on every slots frame) and says
        // the order was not saved.
        if ((err as { status?: number } | null)?.status === 409) {
          if (legacy.length > 0) clearLegacyPinnedSessionOrder()
        } else setPinnedOrderError(i18nT('pages.chatSidebar.pinned_order_handoff_failed'))
      })
  }, [slotsLoaded, tagColumnsSettled, boardProjection, storedPinnedOrder, naturalPinnedOrder, savePinnedOrder, applyAnswer, setPinnedOrderError])
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
  // callback is a member of the `actions` object every SessionRow compares,
  // so one unstable reference voids all N memo boundaries per frame and
  // defeats both the row memo and the displacement window for any membership
  // change. The handler runs only on
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
