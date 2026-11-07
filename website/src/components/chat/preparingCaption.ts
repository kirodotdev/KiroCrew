import { i18next } from '../../i18n'
import { i18nT } from '../../i18n/t'
import type { ChatSlot } from '../../types'

/** The caption for a slot the gateway is preparing a message for.
 *
 * The gateway's own caption names what is being checked, in the language it
 * picked from the owner's messages (`preparing_lang`). It shows only when that
 * is the UI's language (`zh-CN` matches `zh`); otherwise, or when the gateway
 * sent none, the UI's own translated caption shows instead. */
export function preparingCaption(slot: Pick<ChatSlot, 'preparing_text' | 'preparing_lang'> | undefined): string {
  const ui = (i18next.language || '').split('-')[0].toLowerCase()
  if (slot?.preparing_text && slot.preparing_lang && slot.preparing_lang === ui) return slot.preparing_text
  return i18nT('components.chatPane.preparing_catch_up')
}
