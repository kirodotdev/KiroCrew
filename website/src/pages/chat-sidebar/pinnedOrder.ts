/** The manual order of pinned sessions: browser-persisted, reconciled against the
 *  server's pin membership, synced across tabs, and reordered by drag or Alt+Arrow. */
import { useMemo, useState, useRef, useEffect, useCallback } from 'react'
import { readPinnedSessionOrder, reconcilePinnedSessionOrder, PINNED_SESSION_ORDER_KEY, PINNED_SESSION_ORDER_CHANGED_EVENT, movePinnedSession, persistPinnedSessionOrder } from '../../utils/pinnedSessionOrder'
import { compareBySort, type SortKey } from '../chat/sessionOrder'
import { pinMutationKeysInFlight } from '../../hooks/useSessionActions'
import { haptic } from '../../lib/haptic'
import type { Slot } from './types'
import type { TagColumn } from '../../types'
import { sessionRowsInScope } from '../chat/sessionRowNav'

/** The pinned-section order: stored, reconciled, synced across tabs, reordered. */
export function usePinnedSessionOrder({ localSlots, sortKey }: {
  localSlots: Slot[]
  sortKey: SortKey
}) {
  // Pinned membership is server-persisted; the order inside that section is a
  // browser preference, matching the sidebar's existing sort/view preferences.
  //
  // Both collections read `localSlots`, NOT the merged `allRows`: pin state and
  // pin order are local sidebar metadata keyed by local slot key, and a peer row
  // has no entry in either. Feeding it merged rows would put a peer key into the
  // persisted order array, where it would survive disconnection forever.
  const pinned = useMemo(() => new Set(localSlots.filter(s => s.pinned).map(s => s.key)), [localSlots])
  const [storedPinnedOrder, setStoredPinnedOrder] = useState(readPinnedSessionOrder)
  const pinnedOrderFromStorage = useRef(false)
  const naturalPinnedOrder = useMemo(
    () => localSlots.filter(s => s.pinned).sort((a, b) => compareBySort(a, b, sortKey)).map(s => s.key),
    [localSlots, sortKey],
  )
  const pinnedOrder = useMemo(
    () => reconcilePinnedSessionOrder(storedPinnedOrder, naturalPinnedOrder),
    [storedPinnedOrder, naturalPinnedOrder],
  )
  const pinnedRank = useMemo(() => new Map(pinnedOrder.map((key, index) => [key, index])), [pinnedOrder])
  useEffect(() => {
    const refresh = (fromStorage: boolean) => {
      const incoming = readPinnedSessionOrder()
      setStoredPinnedOrder(current => {
        const changed = incoming.length !== current.length
          || incoming.some((key, index) => key !== current[index])
        if (!changed) return current
        pinnedOrderFromStorage.current = fromStorage
        return incoming
      })
    }
    const onSameTabChange = () => refresh(false)
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === PINNED_SESSION_ORDER_KEY) refresh(true)
    }
    window.addEventListener(PINNED_SESSION_ORDER_CHANGED_EVENT, onSameTabChange)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(PINNED_SESSION_ORDER_CHANGED_EVENT, onSameTabChange)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  const reorderPinned = useCallback((activeKey: string, overKey: string) => {
    setStoredPinnedOrder(current => {
      const naturalSet = new Set(naturalPinnedOrder)
      const pending = pinMutationKeysInFlight().filter(key => !naturalSet.has(key))
      const reconciled = reconcilePinnedSessionOrder(current, [...naturalPinnedOrder, ...pending])
      const next = movePinnedSession(reconciled, activeKey, overKey)
      persistPinnedSessionOrder(next)
      return next
    })
    // The drop seats: the row's stored position is the thing that moved.
    haptic('light')
  }, [naturalPinnedOrder])
  return {
    pinned, pinnedOrder, pinnedRank, reorderPinned,
    orderState: { storedPinnedOrder, setStoredPinnedOrder, pinnedOrderFromStorage, naturalPinnedOrder },
  }
}

/** The stored order and its reconcile inputs, handed from the order owner to its authority. */
type PinnedOrderState = ReturnType<typeof usePinnedSessionOrder>['orderState']

/** Reconciles the stored pinned order once the slot list and the board have settled. */
export function usePinnedOrderAuthority({ orderState, slotsLoaded, tagColumnsSettled, orderedColumns }: {
  orderState: PinnedOrderState
  slotsLoaded: boolean
  tagColumnsSettled: boolean
  orderedColumns: TagColumn[]
}) {
  const { storedPinnedOrder, setStoredPinnedOrder, pinnedOrderFromStorage, naturalPinnedOrder } = orderState
  const pinnedRankAuthorityEstablished = useRef(storedPinnedOrder.length > 0)
  useEffect(() => {
    if (!slotsLoaded || !tagColumnsSettled || pinMutationKeysInFlight().length > 0) return
    const boardProjection = orderedColumns.length > 0
    if (boardProjection && !pinnedRankAuthorityEstablished.current
      && storedPinnedOrder.length === 0) return
    pinnedRankAuthorityEstablished.current = true
    const next = reconcilePinnedSessionOrder(storedPinnedOrder, naturalPinnedOrder)
    const changed = next.length !== storedPinnedOrder.length
      || next.some((key, index) => key !== storedPinnedOrder[index])
    const fromStorage = pinnedOrderFromStorage.current
    pinnedOrderFromStorage.current = false
    if (!changed) return
    if (!fromStorage) {
      persistPinnedSessionOrder(next)
      window.dispatchEvent(new Event(PINNED_SESSION_ORDER_CHANGED_EVENT))
    }
    setStoredPinnedOrder(next)
  }, [slotsLoaded, tagColumnsSettled, orderedColumns.length, storedPinnedOrder, naturalPinnedOrder, pinnedOrderFromStorage, setStoredPinnedOrder])
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
