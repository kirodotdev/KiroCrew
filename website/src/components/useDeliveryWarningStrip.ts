/** The delivery-warning strip's state and exit, shared by both chat surfaces so neither spells
 *  them on its own. */
import { useCallback, useEffect, useRef } from 'react'
import { useAppDispatch, useAppSelector, useAppStore, type AppDispatch, type RootState } from '../store'
import { appendSlotMessage, dropUnconfirmedRow, endLocalTurn, selectSendAccepted, selectSlotMessages, selectSlotRunEpoch } from '../store/chatSlice'
import {
  adoptRecoveredSends, awaitDraftSave, claimRecoveredSends, dismissDeliveryWarnings, pruneDeliveryWarnings, releaseRecoveredSends, removedSendsFor,
  takeRemovedSend, useDeliveryWarnings, type DeliveryWarningSurface, type DraftOwner,
} from '../utils/deliveryWarning'
import { forgetSendRecovery } from '../utils/sendRecoveryStore'
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
  if (how) postNotice(dispatch, slot, how === 'recovered' ? 'pages.chatPage.delivery_recovered_delivered' : 'pages.chatPage.delivery_removed_delivered')
}

const postNotice = (dispatch: AppDispatch, slot: string, key: string) =>
  dispatch(appendSlotMessage({ slot, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT(key), cls: '' } }))

export interface RecoveredSends {
  ids: readonly string[]
  /** Their text is already in the draft. */
  applied: boolean
}

export interface DeliveryWarningStripHost<P> {
  slot: string | null | undefined
  surface: DeliveryWarningSurface
  /** The slot at click time, which can differ from the last render's. */
  currentSlot: () => string | null | undefined
  /** The surface's own send-failure restore. `true`: the text is in the stored draft; a `DraftOwner`:
   *  the instance holding it in memory, whose `draftSaved` retires the record; else the record stays. */
  restore: (slot: string, sent: P, recovered?: RecoveredSends) => boolean | DraftOwner | void
  /** Whether an earlier page load already merged this send into `slot`'s draft, so it is not appended twice. */
  applied?: (slot: string, sendId: string) => boolean
  merge: (earlier: P, later: P) => P
  /** A crewmate pane draws no notice rows, so it could not say a removed send arrived. */
  disabled?: boolean
  /** Whether `slot`'s transcript loaded; a recovered send is then checked against the server's history. */
  ready: boolean
  parse: (sent: unknown) => P | null
}

export function useDeliveryWarningStrip<P>(host: DeliveryWarningStripHost<P>): { show: boolean; onDismiss: () => void } {
  const { slot, surface, currentSlot, restore, merge, disabled, ready, parse, applied: wasApplied } = host
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
  const recoverWith = useRef({ restore, merge, parse, wasApplied })
  recoverWith.current = { restore, merge, parse, wasApplied }

  useEffect(() => {
    if (!slot || disabled || !ready) return
    const claims = claimRecoveredSends(slot, surface)
    if (!claims.length) return
    const ids = claims.map(r => r.sendId)
    const wasIn = new Set(claims.filter(r => recoverWith.current.wasApplied?.(slot, r.sendId) === true).map(r => r.sendId))
    let live = true
    let adopted: string[] = []
    void acceptedInHistory(slot, ids).then(seen => {
      if (!live) return
      // Undecidable: hand nothing back and keep every record for the next load to check again.
      if (seen === null) { releaseRecoveredSends(slot, ids); return }
      const { restore: put, merge: fold, parse: read, wasApplied: already } = recoverWith.current
      const back: Array<{ sendId: string; sent: P }> = []
      let delivered = false
      for (const rec of claims) {
        const accepted = seen.has(rec.sendId) || selectSendAccepted(store.getState(), slot, rec.sendId)
        const sent = accepted || (wasIn.has(rec.sendId) && already?.(slot, rec.sendId) !== true) ? null : read(rec.sent)
        if (sent === null) { delivered ||= accepted && wasIn.has(rec.sendId); forgetSendRecovery(slot, rec.sendId); continue }
        back.push({ sendId: rec.sendId, sent })
      }
      if (delivered) postNotice(dispatch, slot, 'pages.chatPage.delivery_recovered_delivered')
      adopted = back.map(b => b.sendId)
      releaseRecoveredSends(slot, ids.filter(id => !adopted.includes(id)))
      if (!back.length) return
      adoptRecoveredSends(slot, adopted)
      const handBack = (group: Array<{ sendId: string; sent: P }>, isApplied: boolean) => {
        if (!group.length) return
        const groupIds = group.map(b => b.sendId)
        const held = put(slot, group.map(b => b.sent).reduce(fold), { ids: groupIds, applied: isApplied })
        if (held === true) for (const id of groupIds) forgetSendRecovery(slot, id)
        else if (held) awaitDraftSave(slot, surface, groupIds, held)
      }
      const inDraft = back.filter(b => wasIn.has(b.sendId))
      handBack(back.filter(b => !inDraft.includes(b)), false)
      handBack(inDraft, true)
      if (inDraft.length === back.length) return
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
      restore: sent => { restore(at, sent) },
      merge,
    })
  }, [currentSlot, disabled, surface, dispatch, store, restore, merge])

  return { show: pending && !disabled, onDismiss }
}
