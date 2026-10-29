/**
 * Chat message reactions API client.
 *
 * Routes through the shared transport (`apiTransport`), same rationale as
 * `pins.ts`: the `X-Session-Key` header, an `ApiError` carrying the HTTP
 * status + raw backend body, and a recorded entry in the shared error journal.
 */

import { apiTransport } from './apiTransport'

const { get, post, del, j } = apiTransport

/** Fixed reaction set — kept in sync with the backend's `_VALID_EMOJI`
 *  (`chat_reactions.py`). A full emoji picker is out of scope: the set stays
 *  small so reacting is a single click. */
export const REACTION_EMOJI = ['👍', '✅', '👀', '⭐'] as const
export type ReactionEmoji = typeof REACTION_EMOJI[number]

export interface ChatReaction {
  id: string
  slot_key: string
  mid: string
  emoji: string
  reacted_at: string
}

export interface ReactMessageBody {
  slot_key: string
  mid: string
  emoji: string
}

export const reactionsApi = {
  list: (slotKey: string): Promise<{ reactions: ChatReaction[] }> =>
    get(`/api/chat/reactions?slot=${encodeURIComponent(slotKey)}`).then(j) as Promise<{ reactions: ChatReaction[] }>,

  create: (body: ReactMessageBody): Promise<ChatReaction> =>
    post('/api/chat/reactions', body).then(j) as Promise<ChatReaction>,

  remove: (slotKey: string, mid: string, emoji: string): Promise<{ ok: boolean }> =>
    del(`/api/chat/reactions?slot=${encodeURIComponent(slotKey)}&mid=${encodeURIComponent(mid)}&emoji=${encodeURIComponent(emoji)}`).then(j) as Promise<{ ok: boolean }>,
}
