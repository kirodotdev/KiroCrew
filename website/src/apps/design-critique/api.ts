import { AGENT } from './constants'
import type { SlotData } from './types'

// These hit the dashboard's own chat endpoints (NOT an app-scoped reverse proxy),
// so they are plain same-origin fetches — the same convention file-explorer's
// api.ts uses. An empty body (e.g. 204 on DELETE) is treated as success.
async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init })
  if (!r.ok) {
    const body = await r.text().catch(() => '')
    throw new Error(body || `HTTP ${r.status}`)
  }
  if (r.status === 204 || r.status === 205) return undefined as T
  const text = await r.text()
  if (text.trim() === '') return undefined as T
  return JSON.parse(text) as T
}

const postJson = <T>(path: string, body: unknown): Promise<T> =>
  jsonFetch<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body != null ? JSON.stringify(body) : undefined,
  })

export const designCritiqueApi = {
  // Open a throwaway worker slot. memory_mode 'temporary' keeps it out of memory
  // snapshots; mode 'design-critique' keeps it OUT of the chat sidebar (the chat
  // list only renders '' and 'orchestrator').
  openSlot: () =>
    postJson<{ key: string }>('/api/chat/slots', {
      name: 'dc-' + Date.now(), agent: AGENT, memory_mode: 'temporary', mode: 'design-critique',
    }),

  getSlot: (slotKey: string) =>
    jsonFetch<SlotData>('/api/chat/slots/' + encodeURIComponent(slotKey)),

  // Fire a message at a slot. The response body is not JSON we care about, so a
  // parse error is swallowed — only a real HTTP/network error propagates.
  send: (slotKey: string, message: string): Promise<void> =>
    jsonFetch<void>('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // memory_mode must be repeated here, not only at slot creation. POST
      // /api/chat auto-creates a missing slot, and with no memory_mode in the body
      // it falls back to the persistent default — so if the gateway restarts
      // mid-run (the slot is in memory, not on disk) the next send would silently
      // recreate this critique slot with memory reads and writes ENABLED. Passing
      // it is also safe when the slot exists: get_or_create_slot only raises on a
      // mismatch, and this matches what openSlot() asked for.
      body: JSON.stringify({ message, slot: slotKey, agent: AGENT, memory_mode: 'temporary' }),
    }).catch((e: unknown) => {
      if (e instanceof SyntaxError) return
      throw e
    }),

  deleteSlot: (slotKey: string): Promise<void> =>
    jsonFetch<void>('/api/chat/slots/' + encodeURIComponent(slotKey), { method: 'DELETE' }).catch(() => {}),

  uploadFiles: async (files: File[]): Promise<{ paths: string[] }> => {
    const fd = new FormData()
    files.forEach(f => fd.append('file', f))
    const up = await fetch('/api/upload/file', { method: 'POST', body: fd, credentials: 'same-origin' })
    if (!up.ok) throw new Error('upload failed (' + up.status + ')')
    return up.json()
  },
}

export const fileUrl = (p: string): string => '/api/file-raw?path=' + encodeURIComponent(p)
