/** The delivery-warning strip's state and exit, shared by both chat surfaces so neither spells
 *  them on its own. */
import { useCallback, useEffect } from 'react'
import { useAppDispatch, useAppSelector, useAppStore, type AppDispatch, type RootState } from '../store'
import { appendSlotMessage, dropUnconfirmedRow, endLocalTurn, selectSendAccepted, selectSlotMessages, selectSlotRunEpoch } from '../store/chatSlice'
import {
  dismissDeliveryWarnings, pruneDeliveryWarnings, removedSendsFor, takeRemovedSend, useDeliveryWarnings,
  type DeliveryWarningRecord, type DeliveryWarningSurface,
} from '../utils/deliveryWarning'
import { i18nT } from '../i18n/t'

/** Pending means no row anywhere confirms or queues the send, and either its optimistic row is
 *  still on screen or it never drew one (a busy-slot Queue send, `rowless`). Confirmed, queued,
 *  cleared and refetched-away sends have nothing left to hand back. */
const sendPending = (state: RootState, slot: string, { sendId, rowless }: Pick<DeliveryWarningRecord, 'sendId' | 'rowless'>): boolean =>
  !selectSendAccepted(state, slot, sendId) &&
  (!!rowless || selectSlotMessages(state, slot).some(m => m.role === 'user' && m.meta?.sendId === sendId && !!m.meta?.optimistic))

/** For the receipt paths: a removed send whose late receipt still lands must say so, because the
 *  composer now holds a copy of a delivered message. */
export function announceIfRemovedSendDelivered(dispatch: AppDispatch, slot: string, sendId: string): void {
  if (!takeRemovedSend(slot, sendId)) return
  dispatch(appendSlotMessage({
    slot,
    message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_removed_delivered'), cls: '' },
  }))
}

export interface DeliveryWarningStripHost<P> {
  slot: string | null | undefined
  surface: DeliveryWarningSurface
  /** The slot at click time, which can differ from the last render's. */
  currentSlot: () => string | null | undefined
  /** The surface's own send-failure restore, given the slot the send went to. */
  restore: (slot: string, sent: P) => void
  merge: (earlier: P, later: P) => P
  /** A crewmate pane draws no notice rows, so it could not say a removed send arrived. */
  disabled?: boolean
}

export function useDeliveryWarningStrip<P>(host: DeliveryWarningStripHost<P>): { show: boolean; onDismiss: () => void } {
  const { slot, surface, currentSlot, restore, merge, disabled } = host
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const records = useDeliveryWarnings(slot)
  const pending = useAppSelector(state =>
    !!slot && records.some(r => r.surface === surface && sendPending(state, slot, r)))
  const removedIds = removedSendsFor(slot)
  const acceptedRemoved = useAppSelector(state =>
    slot ? removedIds.filter(id => selectSendAccepted(state, slot, id)).join('\n') : '')

  useEffect(() => {
    if (slot && records.length) pruneDeliveryWarnings(slot, r => sendPending(store.getState(), slot, r))
  }, [slot, records, pending, store])

  useEffect(() => {
    if (!slot || !acceptedRemoved) return
    for (const id of acceptedRemoved.split('\n')) announceIfRemovedSendDelivered(dispatch, slot, id)
  }, [slot, acceptedRemoved, dispatch])

  const onDismiss = useCallback(() => {
    const at = currentSlot()
    if (!at || disabled) return
    dismissDeliveryWarnings<P>(at, surface, {
      stillPending: r => sendPending(store.getState(), at, r),
      dropRow: (sendId, epoch) => {
        dispatch(dropUnconfirmedRow({ slot: at, sendId }))
        if (selectSlotRunEpoch(store.getState(), at) === epoch) dispatch(endLocalTurn(at))
      },
      restore: sent => restore(at, sent),
      merge,
    })
  }, [currentSlot, disabled, surface, dispatch, store, restore, merge])

  return { show: pending && !disabled, onDismiss }
}
