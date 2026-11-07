import type { AppDispatch, RootState } from '../../../store'
import { deleteSlot } from '../../../store/chatSlice'
import { awaitCleanupRefusals } from '../../../utils/slotClosingIntent'
import { localComposerHoldsWork } from '../../../hooks/useSlotWorkBeacon'
import { closeConfirmPrompt } from '../../../lib/closeConfirmPrompt'
import { askAppConfirm } from '../../../lib/appConfirm'
import { loadChatConfig } from '../ChatSettings'
import { i18nT } from '../../../i18n/t'
import { forgetClosedSessionRef, rememberClosedSession, trackClosedSessionClose } from '../../../lib/recentlyClosedSessions'

export { slotUnsentWorkSource } from '../../../hooks/useSlotWorkBeacon'

/**
 * Close *slot* from the `<!-- option-action:close=… -->` chip.
 *
 * Asks first, on the same rule as every other close (`closeConfirmPrompt`): a busy slot
 * always asks, an idle one follows the user's setting. A cancelled prompt closes nothing
 * and is not a failure. Then it runs the cross-window closing handshake, so a draft typed
 * anywhere after the chip rendered still stops the close, and re-reads the offer right
 * before deleting: a click made on an offer the transcript has since moved past is refused.
 * A refusal or a failed close rejects with the sentence the chip shows.
 */
export function closeFromActionChip(
  slot: string,
  sourceKeyAtClick: string | null | undefined,
  readCurrentSourceKey: () => string | null,
) {
  return async (dispatch: AppDispatch, getState: () => RootState): Promise<void> => {
    const stale = () => sourceKeyAtClick !== undefined && sourceKeyAtClick !== readCurrentSourceKey()
    if (stale()) throw new Error(i18nT('pages.chatSidebar.close_refused_title'))
    const ask = closeConfirmPrompt(getState(), slot, { settingOn: loadChatConfig().confirmCloseSession })
    if (ask !== null && !(await askAppConfirm(ask))) return
    const guard = await awaitCleanupRefusals([slot], new Set(), key => localComposerHoldsWork(key))
    try {
      if (guard.refused.length > 0 || guard.recheck().length > 0) {
        throw new Error(i18nT(localComposerHoldsWork(slot)
          ? 'components.followUpBar.action_unavailable_while_unsent_draft_elsewhere'
          : 'components.followUpBar.action_unavailable_while_unsent_draft_window'))
      }
      if (stale()) throw new Error(i18nT('pages.chatSidebar.close_refused_title'))
      const ref = rememberClosedSession(getState().dashboard.slots.find(s => s.key === slot))
      const close = dispatch(deleteSlot(slot))
      trackClosedSessionClose(ref, close.then(action => deleteSlot.fulfilled.match(action)))
      try {
        await close.unwrap()
      } catch {
        forgetClosedSessionRef(ref)
        throw new Error(closeRefusedSentence(getState(), slot))
      }
    } finally {
      guard.release()
    }
  }
}

/** The sidebar's own refusal sentence for *slot*, read from the store after the close failed. */
function closeRefusedSentence(state: RootState, slot: string): string {
  const refused = state.chat.closeRefused
  if (!refused || refused.key !== slot || refused.title === null) return i18nT('pages.chatSidebar.close_refused_title')
  return i18nT(refused.reason === 'historyWrite' ? 'pages.chatSidebar.close_refused' : 'pages.chatSidebar.close_refused_other', { title: refused.title })
}
