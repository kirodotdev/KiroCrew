/**
 * Chat message pins API client.
 * Follows the same transport pattern as client.ts (same-origin fetch with X-Session-Key).
 */

const _sk = { 'X-Session-Key': 'dashboard:ui' }

// Keep transport bounded while leaving ample look-ahead beyond the 200-character
// stored preview for server-side credential and URL redaction.
export const PIN_PREVIEW_INPUT_MAX_CHARS = 4096

export interface ChatPin {
  id: string
  slot_key: string
  message_ts: string
  role: 'user' | 'assistant'
  preview: string
  pinned_at: string
}

export interface PinMessageBody {
  slot_key: string
  message_ts: string
  role: 'user' | 'assistant'
  preview: string
}

export const pinsApi = {
  list: (slotKey: string): Promise<{ pins: ChatPin[] }> =>
    fetch(`/api/chat/pins?slot=${encodeURIComponent(slotKey)}`, { headers: _sk })
      .then(r => { if (!r.ok) throw new Error(`Pin list failed: ${r.status}`); return r.json() }),

  create: (body: PinMessageBody): Promise<ChatPin> =>
    fetch('/api/chat/pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._sk },
      body: JSON.stringify(body),
    }).then(r => { if (!r.ok) throw new Error(`Pin create failed: ${r.status}`); return r.json() }),

  remove: (id: string): Promise<{ ok: boolean }> =>
    fetch(`/api/chat/pins/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      headers: _sk,
    }).then(r => { if (!r.ok) throw new Error(`Pin delete failed: ${r.status}`); return r.json() }),
}
