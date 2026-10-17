import { i18nT } from '../i18n/t'
import type { ChatMessage } from '../types'

/**
 * What the look-preview frame (utils/lookPreview.ts) shows in place of the
 * user's own sessions: one demo session whose short conversation runs under
 * the composer dock. The demo exists for the Translucent panels switch -- the
 * glass only reads as glass when something scrolls under it, and a new user's
 * real session is empty -- but it also gives the mode and theme picks a
 * transcript to show on.
 *
 * Fixtures, not state: the frame answers `GET /api/chat/slots` and the slot
 * detail from here (`api/client/chat.ts`) and ignores every socket frame
 * (`hooks/useWebSocket.ts`), so nothing the user has is read into the preview
 * and nothing in the preview can be acted on.
 */
export const LOOK_PREVIEW_SLOT_KEY = 'look-preview'

export function lookPreviewSlots() {
  const now = new Date().toISOString()
  return [{
    key: LOOK_PREVIEW_SLOT_KEY,
    title: i18nT('components.lookPreview.session_title'),
    agent: 'kirocrew',
    running: false,
    messages: 9,
    tags: [],
    last_ts: now,
    created: now,
  }]
}

export function lookPreviewSlotDetail() {
  const base = Date.now() - 5 * 60_000
  const at = (i: number) => new Date(base + i * 40_000).toISOString()
  const msg = (i: number, role: 'user' | 'assistant', key: string): ChatMessage =>
    ({ role, cls: role, content: i18nT(key), ts: at(i) })
  const messages: ChatMessage[] = [
    msg(0, 'user', 'components.lookPreview.turn_1_user'),
    msg(1, 'assistant', 'components.lookPreview.turn_1_assistant'),
    msg(2, 'assistant', 'components.lookPreview.turn_2_assistant'),
    msg(3, 'user', 'components.lookPreview.turn_2_user'),
    msg(4, 'assistant', 'components.lookPreview.turn_3_assistant'),
    msg(5, 'user', 'components.lookPreview.turn_3_user'),
    msg(6, 'assistant', 'components.lookPreview.turn_4_assistant'),
    msg(7, 'user', 'components.lookPreview.turn_4_user'),
    msg(8, 'assistant', 'components.lookPreview.turn_5_assistant'),
  ]
  return { messages, running: false, stopping: false, has_more: false, total: messages.length, next_before: 0, queue: [] }
}
