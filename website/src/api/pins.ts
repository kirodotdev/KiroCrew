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

/**
 * Structured error from the pins API: a plain `Error` carrying the
 * machine-readable `code` field the backend returns (e.g. `pin_limit_reached`,
 * `preview_too_large`, `persist_failed`) so callers can branch on the specific
 * failure rather than showing a single generic message.
 *
 * Deliberately a TYPE, not a class: consumers branch structurally on `code`
 * (see `pinErrorCode` in useChatPins), which stays correct across module-mock
 * boundaries in tests, where an `instanceof` against a re-exported class would
 * not — and a type adds no runtime export a partial mock could drop.
 */
export type PinApiError = Error & { code?: string }

export const pinsApi = {
  list: (slotKey: string): Promise<{ pins: ChatPin[] }> =>
    fetch(`/api/chat/pins?slot=${encodeURIComponent(slotKey)}`, { headers: _sk })
      .then(r => { if (!r.ok) throw new Error(`Pin list failed: ${r.status}`); return r.json() }),

  create: async (body: PinMessageBody): Promise<ChatPin> => {
    const r = await fetch('/api/chat/pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._sk },
      body: JSON.stringify(body),
    })
    if (!r.ok) {
      let code: string | undefined
      try {
        const data = await r.json() as Record<string, unknown>
        if (typeof data.code === 'string') code = data.code
      } catch {
        // Body parse failure — fall through to the generic error below
      }
      const err: PinApiError = new Error(`Pin create failed: ${r.status}`)
      err.code = code
      throw err
    }
    return r.json() as Promise<ChatPin>
  },

  remove: (id: string): Promise<{ ok: boolean }> =>
    fetch(`/api/chat/pins/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      headers: _sk,
    }).then(r => { if (!r.ok) throw new Error(`Pin delete failed: ${r.status}`); return r.json() }),
}
