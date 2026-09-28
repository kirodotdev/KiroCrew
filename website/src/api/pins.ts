/**
 * Chat message pins API client.
 *
 * Routes through the blessed shared transport (`apiTransport`) rather than raw
 * `fetch`, so pin failures get the SAME pipeline every other dashboard API call
 * already has: the `X-Session-Key` header, an `ApiError` carrying the HTTP
 * status + raw backend body, the backend's machine-readable `code` parsed once
 * at the chokepoint, and a recorded entry in the shared error journal. This
 * replaces the module's own hand-rolled error-body parsing (which duplicated
 * `ApiError` + `parseErrorCode` and never fed the journal) with the one
 * established mechanism. Consumers read the backend `code` off the thrown
 * `ApiError` via `parseErrorCode(err.body)` — see `pinErrorCode` in useChatPins.
 */

import { apiTransport } from './apiTransport'

const { get, post, del, j } = apiTransport

// Keep transport bounded while leaving ample look-ahead beyond the 200-character
// stored preview for server-side credential and URL redaction.
export const PIN_PREVIEW_INPUT_MAX_CHARS = 4096

export interface ChatPin {
  id: string
  slot_key: string
  mid: string
  message_ts: string
  role: 'user' | 'assistant'
  preview: string
  pinned_at: string
}

export interface PinMessageBody {
  slot_key: string
  mid: string
  message_ts: string
  role: 'user' | 'assistant'
  preview: string
}

export const pinsApi = {
  list: (slotKey: string): Promise<{ pins: ChatPin[] }> =>
    get(`/api/chat/pins?slot=${encodeURIComponent(slotKey)}`).then(j) as Promise<{ pins: ChatPin[] }>,

  create: (body: PinMessageBody): Promise<ChatPin> =>
    post('/api/chat/pins', body).then(j) as Promise<ChatPin>,

  remove: (id: string): Promise<{ ok: boolean }> =>
    del(`/api/chat/pins/${encodeURIComponent(id)}`).then(j) as Promise<{ ok: boolean }>,
}
