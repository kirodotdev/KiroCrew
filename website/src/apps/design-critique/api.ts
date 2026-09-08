import { AGENT } from './constants'
import { toApiError } from '../../api/apiError'
import { sendTurn, type SendWire } from '../../chat-core/transport/sendTurn'
import { i18nT } from '../../i18n/t'
import type { Scope, SlotData } from './types'

// This app's own backend (mounted by the built-in at gateway startup). It does
// the clone / discover / render work server-side so the agent never runs a tool.
const DC = '/api/apps/design-critique'

/** The transport's fetch seam for this app's sends: one plain same-origin
 *  `POST /api/chat?ws=1` (the JSON receipt, not the SSE stream) carrying the
 *  slot-recreation fields `send` documents, resolving the Response on every HTTP
 *  status so the shared classifier reads a 4xx/5xx as a refusal, and honouring
 *  the transport's deadline signal. */
const critiqueSendWire: SendWire = (payload, signal) =>
  fetch('/api/chat?ws=1', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: payload.message, slot: payload.slot, agent: AGENT, memory_mode: 'temporary', mode: 'design-critique',
    }),
    signal,
  })

// These hit the dashboard's own chat endpoints (NOT an app-scoped reverse proxy),
// so they are plain same-origin fetches — the same convention file-explorer's
// api.ts uses. An empty body (e.g. 204 on DELETE) is treated as success.
async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init })
  if (!r.ok) {
    throw await toApiError(r)
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

// A started backend job (discover/render) or, for the trivial discover kinds
// (figma/url/blocked), the finished payload returned inline with no job.
interface JobHandle { job: string }
const isJobHandle = (x: unknown): x is JobHandle =>
  !!x && typeof (x as JobHandle).job === 'string'

// Poll a detached backend job until it is no longer running, then resolve its
// result or throw its error. The scan runs server-side regardless of this loop,
// so a page that navigates away simply stops polling — the job keeps going and a
// later visit reconnects by polling the same id (see pollDiscover / pollRender).
async function pollJob<T>(base: string, jobId: string): Promise<T> {
  let misses = 0
  for (;;) {
    await new Promise(r => setTimeout(r, 1500))
    let r: { status: string; result?: T; error?: string }
    try {
      r = await jsonFetch(base + '?job=' + encodeURIComponent(jobId))
    } catch (e) {
      // Tolerate a transient blip (gateway restart) for a few cycles.
      if (++misses >= 8) throw e instanceof Error ? e : new Error('lost contact with the scan')
      continue
    }
    misses = 0
    if (r.status === 'running') continue
    if (r.status === 'error') throw new Error(r.error || 'that run did not finish')
    return r.result as T
  }
}

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

  // Fire a message at a slot through the chat-core transport. The body carries
  // three fields the generic dashboard wire does not: memory_mode AND mode must
  // be repeated here, not only at slot creation. POST /api/chat auto-creates a
  // missing slot, and with neither in the body it falls back to the persistent
  // default with surface '' — so if the gateway restarts mid-run (the slot is in
  // memory, not on disk) the next send would silently recreate this critique
  // slot with memory reads and writes ENABLED and visible in the chat sidebar
  // (whose allowlist admits surface ''). Passing them is also safe when the slot
  // exists: get_or_create_slot only raises on a memory_mode mismatch and ignores
  // mode for existing slots, and both match what openSlot() asked for. Hence an
  // app-local wire (the same plain same-origin fetch this file uses everywhere)
  // rather than the transport's default one.
  //
  // Receipt policy for a background sender with no composer to restore into:
  // `refused` and `transport-error` reject (nothing is running -- the caller's
  // failWith reports it and drops the pending critique, as it did for a non-2xx
  // before); `unknown` and `response-late` RESOLVE, because the request was or
  // may have been accepted and the poll that follows will find out -- rejecting
  // would invite a retry that runs the critique twice. The old send read the SSE
  // stream the bare endpoint answers with and swallowed its parse error as
  // success, so a `{ok:false}` refusal inside a 200 never surfaced at all.
  send: async (slotKey: string, message: string): Promise<void> => {
    const receipt = await sendTurn({ message, slot: slotKey, wire: critiqueSendWire })
    if (receipt.status === 'refused' || receipt.status === 'transport-error') {
      throw new Error(receipt.reason || (i18nT('pages.chatPage.send_failed') as string))
    }
  },

  deleteSlot: (slotKey: string): Promise<void> =>
    jsonFetch<void>('/api/chat/slots/' + encodeURIComponent(slotKey), { method: 'DELETE' }).catch(() => {}),

  uploadFiles: async (files: File[]): Promise<{ paths: string[] }> => {
    const fd = new FormData()
    files.forEach(f => fd.append('file', f))
    const up = await fetch('/api/upload/file', { method: 'POST', body: fd, credentials: 'same-origin' })
    if (!up.ok) throw await toApiError(up)
    return up.json()
  },

  // STEP 1 — the backend clones (if needed), lists candidate screens, and probes
  // which ones actually render. Heavy kinds (repo/local) run as a detached backend
  // job: the POST returns {job} and we poll it, so navigating away no longer
  // cancels the scan. Trivial kinds (figma/url/blocked) come back inline with no
  // job. `onJob` fires with the job id the moment it exists, so the caller can
  // persist it and reconnect after a navigation.
  discover: async (
    kind: string,
    value: string,
    onJob?: (jobId: string) => void,
  ): Promise<Scope & { handle?: string }> => {
    const started = await postJson<JobHandle | (Scope & { handle?: string })>(
      DC + '/discover', { kind, value })
    if (isJobHandle(started)) {
      onJob?.(started.job)
      return pollJob<Scope & { handle?: string }>(DC + '/discover', started.job)
    }
    return started
  },

  // Reconnect to an in-flight discover job by id (resume path — never re-POSTs).
  pollDiscover: (jobId: string): Promise<Scope & { handle?: string }> =>
    pollJob<Scope & { handle?: string }>(DC + '/discover', jobId),

  // STEP 2 — the backend renders the picked screens to PNGs and returns their
  // absolute paths. Always a detached job: the POST returns {job}, then we poll.
  render: (body: {
    kind: string
    value: string
    handle: string
    picks: Array<{ id: string; label: string; ref?: string }>
  }, onJob?: (jobId: string) => void): Promise<{ screens: Array<{ step: number; label: string; path: string }>; couldNotSee: string[] }> =>
    postJson<JobHandle>(DC + '/render', body).then(started => {
      onJob?.(started.job)
      return pollJob(DC + '/render', started.job)
    }),

  // Reconnect to an in-flight render job by id (resume path — never re-POSTs).
  pollRender: (jobId: string): Promise<{ screens: Array<{ step: number; label: string; path: string }>; couldNotSee: string[] }> =>
    pollJob(DC + '/render', jobId),

  // The critique method text, inlined into the prompt so the agent does not have
  // to read it with a tool.
  method: (): Promise<{ checklist: string }> => jsonFetch(DC + '/method'),
}

export const fileUrl = (p: string): string => '/api/file-raw?path=' + encodeURIComponent(p)
