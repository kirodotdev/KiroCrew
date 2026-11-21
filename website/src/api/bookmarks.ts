/**
 * Chat session bookmarks API client: the links kept with one chat session.
 *
 * Routes through the shared transport, like `pins.ts`, so a failure is an
 * `ApiError` carrying the backend body and its machine-readable `code`.
 */

import { apiTransport } from './apiTransport'

const { get, post, del, j } = apiTransport

export interface ChatBookmark {
  id: string
  slot_key: string
  url: string
  title: string
  /** Who kept it: the person in the side panel, or the agent's `chat_bookmark` tool. */
  origin: 'user' | 'agent'
  created_at: string
}

export const bookmarksApi = {
  list: (slotKey: string): Promise<{ bookmarks: ChatBookmark[] }> =>
    get(`/api/chat/bookmarks?slot=${encodeURIComponent(slotKey)}`).then(j) as Promise<{ bookmarks: ChatBookmark[] }>,

  create: (body: { slot_key: string; url: string; title?: string }): Promise<ChatBookmark> =>
    post('/api/chat/bookmarks', body).then(j) as Promise<ChatBookmark>,

  remove: (id: string): Promise<{ ok: boolean }> =>
    del(`/api/chat/bookmarks/${encodeURIComponent(id)}`).then(j) as Promise<{ ok: boolean }>,
}
