/** The delivery-warning strip's state and exit, shared by both chat surfaces so neither spells
 *  them on its own. */
import { useCallback, useEffect, useRef } from 'react'
import { useAppDispatch, useAppSelector, useAppStore, type AppDispatch, type RootState } from '../store'
import { appendSlotMessage, dropUnconfirmedRow, endLocalTurn, selectSendAccepted, selectSlotMessages, selectSlotRunEpoch } from '../store/chatSlice'
import {
  adoptRecoveredSends, claimRecoveredSends, dismissDeliveryWarnings, pruneDeliveryWarnings, releaseRecoveredSends, removedSendsFor,
  takeRemovedSend, useDeliveryWarnings, type DeliveryWarningSurface,
} from '../utils/deliveryWarning'
import { forgetRestoredSendRecoveries, forgetSendRecovery, loadSendRecoveries, markSendRecoveryRestored } from '../utils/sendRecoveryStore'
import { acceptedInHistory } from '../utils/recoveredSendHistory'
import { i18nT } from '../i18n/t'

/** Pending means the optimistic row is still on screen and no row anywhere confirms or queues
 *  the send: confirmed, queued, cleared and refetched-away sends have nothing left to hand back. */
const sendPending = (state: RootState, slot: string, sendId: string): boolean =>
  !selectSendAccepted(state, slot, sendId) &&
  selectSlotMessages(state, slot).some(m => m.role === 'user' && m.meta?.sendId === sendId && !!m.meta?.optimistic)

/** For the receipt paths: a removed send whose late receipt still lands must say so, because the
 *  composer now holds a copy of a delivered message. */
export function announceIfRemovedSendDelivered(dispatch: AppDispatch, slot: string, sendId: string): void {
  const how = takeRemovedSend(slot, sendId)
  if (!how) return
  const key = how === 'recovered' ? 'pages.chatPage.delivery_recovered_delivered' : 'pages.chatPage.delivery_removed_delivered'
  dispatch(appendSlotMessage({
    slot,
    message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT(key), cls: '' },
  }))
}

/** Retire `slot`'s handed-back recovered sends once its composer goes from holding something to
 *  empty: the user sent or cleared it.
 *
 *  The holding observation must be one the slot owned: settled (`settled`: the switch onto `slot`
 *  has landed), and preceded by an observation of the same slot, or by none at all on mount, where
 *  the composer starts from the slot's own draft. Right after a switch the composer can still carry
 *  the chat left behind until its draft restore runs, and that hand-over is not an emptying. */
export function useRetireRecoveredWhenEmptied(
  slot: string | null | undefined, surface: DeliveryWarningSurface, empty: boolean, settled: boolean,
): void {
  const was = useRef<{ slot: string | null | undefined; empty: boolean; owned: boolean } | null>(null)
  useEffect(() => {
    const prev = was.current
    const owned = !!slot && settled && (prev === null || prev.slot === slot)
    was.current = { slot, empty, owned }
    if (owned && empty && prev?.slot === slot && prev.owned && !prev.empty) forgetRestoredSendRecoveries(slot, surface)
  }, [slot, surface, empty, settled])
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
  /** Whether `slot`'s transcript has loaded. A send from an earlier page load is then checked
   *  against the server's history, not only the loaded window, before its text is handed back. */
  ready: boolean
  /** The surface's payload shape, applied to what an earlier page load persisted. */
  parse: (sent: unknown) => P | null
  /** Whether `slot`'s composer holds anything. A send an earlier load already handed back keeps its
   *  record until the composer is emptied; a later load hands it back again only when the composer
   *  came back empty, so a draft that survived is not appended to a second time. */
  holdsText: (slot: string) => boolean
}

export function useDeliveryWarningStrip<P>(host: DeliveryWarningStripHost<P>): { show: boolean; onDismiss: () => void } {
  const { slot, surface, currentSlot, restore, merge, disabled, ready, parse, holdsText } = host
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const records = useDeliveryWarnings(slot)
  const pending = useAppSelector(state =>
    !!slot && records.some(r => r.surface === surface && sendPending(state, slot, r.sendId)))
  const removedIds = removedSendsFor(slot)
  const acceptedRemoved = useAppSelector(state =>
    slot ? removedIds.filter(id => selectSendAccepted(state, slot, id)).join('\n') : '')

  useEffect(() => {
    if (slot && records.length) pruneDeliveryWarnings(slot, id => sendPending(store.getState(), slot, id))
  }, [slot, records, pending, store])

  // Read through a ref: a host re-render must not cancel a history check already in flight.
  const recoverWith = useRef({ restore, merge, parse, holdsText })
  recoverWith.current = { restore, merge, parse, holdsText }

  useEffect(() => {
    if (!slot || disabled || !ready) return
    const claims = claimRecoveredSends(slot, surface)
    if (!claims.length) return
    const ids = claims.map(r => r.sendId)
    let live = true
    let adopted: string[] = []
    void acceptedInHistory(slot, ids, Math.min(...claims.map(r => r.parkedAt))).then(seen => {
      if (!live) return
      // Undecidable: hand nothing back and keep every record for the next load to check again.
      if (seen === null) { releaseRecoveredSends(slot, ids); return }
      const { restore: put, merge: fold, parse: read, holdsText: holds } = recoverWith.current
      const kept = holds(slot)
      // A record retired while the history was read (its composer was sent or cleared) is gone on
      // purpose: handing its text back would undo the user's clear.
      const still = new Set(loadSendRecoveries(slot).map(r => r.sendId))
      const gone = ids.filter(id => !still.has(id))
      releaseRecoveredSends(slot, gone)
      const back: Array<{ sendId: string; sent: P }> = []
      // Handed back by an earlier load and still in the composer: not appended again, but still
      // tracked, so a late echo says it was delivered after all.
      const surviving: string[] = []
      for (const rec of claims) {
        if (!still.has(rec.sendId)) continue
        const sent = seen.has(rec.sendId) || selectSendAccepted(store.getState(), slot, rec.sendId) ? null : read(rec.sent)
        if (sent === null) {
          // Delivered while a draft holding its text survived: say so, or the next Enter sends it twice.
          if (rec.restored && kept && read(rec.sent) !== null) {
            dispatch(appendSlotMessage({
              slot,
              message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_delivered'), cls: '' },
            }))
          }
          forgetSendRecovery(slot, rec.sendId)
          continue
        }
        if (rec.restored && kept) { surviving.push(rec.sendId); continue }
        back.push({ sendId: rec.sendId, sent })
      }
      adopted = [...back.map(b => b.sendId), ...surviving]
      releaseRecoveredSends(slot, ids.filter(id => !adopted.includes(id)))
      adoptRecoveredSends(slot, adopted)
      if (!back.length) return
      put(slot, back.map(b => b.sent).reduce(fold))
      for (const b of back) markSendRecoveryRestored(slot, b.sendId)
      dispatch(appendSlotMessage({
        slot,
        message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_recovered_after_reload'), cls: '' },
      }))
    })
    return () => {
      live = false
      releaseRecoveredSends(slot, ids.filter(id => !adopted.includes(id)))
    }
  }, [slot, disabled, ready, surface, store, dispatch])

  useEffect(() => {
    if (!slot || !acceptedRemoved) return
    for (const id of acceptedRemoved.split('\n')) announceIfRemovedSendDelivered(dispatch, slot, id)
  }, [slot, acceptedRemoved, dispatch])

  const onDismiss = useCallback(() => {
    const at = currentSlot()
    if (!at || disabled) return
    dismissDeliveryWarnings<P>(at, surface, {
      stillPending: sendId => sendPending(store.getState(), at, sendId),
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
