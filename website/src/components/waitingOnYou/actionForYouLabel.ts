import { i18nT } from '../../i18n/t'
import { actionForYou } from '../../lib/waitingOnYou'
import type { ChatSlot } from '../../types'

export type ActionForYouStatusSlot = Pick<ChatSlot, 'waiting_on_you' | 'pending_approval' | 'needs_input'>

/** The status sentence, or '' when the slot shows no ask. */
export function actionForYouLabel(slot: ActionForYouStatusSlot | null | undefined): string {
  const ask = actionForYou(slot)
  return ask ? i18nT('components.waitingOnYou.status_line', { title: ask.title }) : ''
}
