// Plain same-origin fetch client for the Design Tweak backend, mirroring
// file-explorer/api.ts. The panel runs in the dashboard origin, so no auth
// header is needed — the host proxy signs the upstream call.
//
// Parsing tolerates an empty body: several mutation endpoints (clear, delete,
// dev-server/stop) answer 200 with no JSON, and r.json() would throw on those.
//
// Every URL this app talks to is built HERE, never in the page. Two reasons:
// the page then holds no endpoint literals at all (one place to audit when a
// route moves), and query strings go through `URLSearchParams` rather than
// string templates, so an id containing `&` or `#` cannot smuggle a parameter.

import type {
  AddProjectResponse, ChatSlotResponse, DeleteCommentResponse,
  DetectDevServerResponse, DevServerStartResponse, HealthResponse, HistoryResponse,
  PickFolderResponse, PreviewUrlResponse, Project, ProjectsResponse,
  QueueResponse, RemoveResponse, SendResponse, SimpleResponse, SlotTranscript, SubmitResponse,
} from './types'

async function parse<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.text().catch(() => '')
    throw new Error(body || `HTTP ${r.status}`)
  }
  const text = await r.text()
  return (text ? JSON.parse(text) : null) as T
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  return parse<T>(r)
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  return parse<T>(r)
}

export const api = { get, post }

// ── URL construction ─────────────────────────────────────────────────────────

const API_BASE = '/apps/design-tweak/api'

/** `API_BASE` + path, with an optional query string built by `URLSearchParams`. */
function endpoint(path: string, params?: Record<string, string>): string {
  const query = params ? new URLSearchParams(params).toString() : ''
  return query ? API_BASE + path + '?' + query : API_BASE + path
}

/** Cache-buster for the preview iframe: bumping it is the reload lever. */
function nonceQuery(nonce: number): string {
  return new URLSearchParams({ _t: String(nonce) }).toString()
}

/**
 * Where the preview iframe points for a project framed from one of the app's own
 * loopback servers — its dev-server injecting proxy, or the static preview server.
 * The backend resolves the ephemeral port live and hands it back as previewUrl, so
 * this only has to append the nonce onto whatever query it already carries.
 */
export function loopbackPreviewSrc(previewUrl: string, nonce: number): string {
  return previewUrl + (previewUrl.includes('?') ? '&' : '?') + nonceQuery(nonce)
}

/**
 * There is deliberately NO same-origin fallback, and the dashboard-origin
 * preview route (`/apps/design-tweak/api/proxy/<id>/`) has been DELETED from the
 * backend — it now answers 410.
 *
 * The preview iframe carries `allow-same-origin`, which grants it the loopback
 * preview server's origin: a different origin from the dashboard, because ports
 * separate origins under the same-origin policy (even though they do not for
 * cookies). That is what makes the sandbox safe. Any dashboard-origin URL that
 * served project-controlled content would undo it — the frame could read our
 * origin off `document.referrer`, navigate itself there, and run the project's
 * own script first-party with authenticated API and parent-DOM reach.
 *
 * When the static preview server cannot bind there is nothing safe to frame, so
 * the page renders its existing "preview not reachable" state instead. Do not
 * reintroduce a fallback, or any route that serves project files from our origin.
 */

/** Where the backend writes a request's payload — quoted to the agent, never fetched. */
const QUEUE_SUBDIR = 'queue'

/**
 * Path of a request's payload on disk — quoted to the agent, never fetched.
 *
 * `dataDir` MUST come from what the backend reports (`GET /health` → `dataDir`):
 * the real location depends on `KIROCREW_APP_DATA_DIR` / `KIROCREW_HOME` and is
 * not knowable from the client, and a hardcoded guess (this used to name the
 * retired `~/.kirocrew/…` home) points the agent at a path that does not exist.
 * Returns '' when the backend has not reported one, and callers then quote no
 * path at all rather than a wrong one.
 *
 * Joined with '/' regardless of the host separator: `dataDir` is absolute, and a
 * forward slash resolves on Windows too.
 */
export function requestPayloadPath(dataDir: string, requestId: string): string {
  if (!dataDir) return ''
  return dataDir.replace(/[\\/]+$/, '') + '/' + QUEUE_SUBDIR + '/' + requestId + '.json'
}

/** The per-comment progress endpoint the agent POSTs to (quoted in the prompt). */
export function threadEndpoint(requestId: string, cidPlaceholder: string): string {
  return endpoint('/thread', { id: requestId, cid: cidPlaceholder })
}

// ── Backend calls ────────────────────────────────────────────────────────────

export function fetchProjects(): Promise<ProjectsResponse> {
  return get<ProjectsResponse>(endpoint('/projects'))
}

/**
 * Health probe. The only reason the page calls it: `dataDir` is where the backend
 * ACTUALLY resolved its data home (env-dependent), which is what makes the
 * payload path quoted to the agent correct instead of a guess.
 */
export function fetchHealth(): Promise<HealthResponse> {
  return get<HealthResponse>(endpoint('/health'))
}

export function fetchQueue(): Promise<QueueResponse> {
  return get<QueueResponse>(endpoint('/queue'))
}

export function fetchHistory(): Promise<HistoryResponse> {
  return get<HistoryResponse>(endpoint('/history'))
}

export function selectProject(id: string): Promise<SimpleResponse> {
  return post<SimpleResponse>(endpoint('/projects/select'), { id })
}

export function removeProject(id: string): Promise<RemoveResponse> {
  return post<RemoveResponse>(endpoint('/projects/remove'), { id })
}

export function addProject(path: string): Promise<AddProjectResponse> {
  return post<AddProjectResponse>(endpoint('/projects'), { path })
}

export function pickFolder(): Promise<PickFolderResponse> {
  return post<PickFolderResponse>(endpoint('/pick-folder'), {})
}

export function submitComment(payload: unknown): Promise<SubmitResponse> {
  return post<SubmitResponse>(endpoint('/submit'), payload)
}

export function sendRequest(id: string): Promise<SendResponse> {
  return post<SendResponse>(endpoint('/send', { id }), {})
}

/** Ack that a sealed request's prompt reached the agent. Idempotent server-side. */
export function markDelivered(id: string): Promise<SendResponse> {
  return post<SendResponse>(endpoint('/delivered', { id }), {})
}

export function clearRequest(id: string): Promise<SimpleResponse> {
  return post<SimpleResponse>(endpoint('/clear', { id }), {})
}

export function deleteRequest(id: string): Promise<SimpleResponse> {
  return post<SimpleResponse>(endpoint('/delete', { id }), {})
}

export function deleteComment(id: string, cid: string): Promise<DeleteCommentResponse> {
  return post<DeleteCommentResponse>(endpoint('/delete-comment', { id, cid }), {})
}

export function setPreviewUrl(id: string, previewUrl: string): Promise<PreviewUrlResponse> {
  return post<PreviewUrlResponse>(endpoint('/projects/preview-url'), { id, previewUrl })
}

export function detectDevServer(id: string): Promise<DetectDevServerResponse> {
  return get<DetectDevServerResponse>(endpoint('/detect-dev-server', { id }))
}

export function startDevServer(id: string): Promise<DevServerStartResponse> {
  return post<DevServerStartResponse>(endpoint('/dev-server/start', { id }), {})
}

export function stopDevServer(id: string): Promise<SimpleResponse> {
  return post<SimpleResponse>(endpoint('/dev-server/stop', { id }), {})
}

// ── Host chat API ────────────────────────────────────────────────────────────
//
// Plain same-origin calls to the host chat API — exactly how the dashboard itself
// calls them (no auth header needed; the panel runs in the dashboard origin).

const CHAT_SLOTS = '/api/chat/slots'

/** Slot-detail read; the only endpoint that returns message ENTRIES + queue. */
function slotDetailUrl(key: string): string {
  return CHAT_SLOTS + '/' + encodeURIComponent(key)
}

/**
 * `ws=1` makes the host answer with JSON. Without it the reply is an SSE stream,
 * and the parse error used to be caught and "recovered" by opening a NEW ad-hoc
 * chat — so one request produced two sessions and the app's own per-app session
 * was bypassed.
 */
const CHAT_SEND = '/api/chat' + '?' + new URLSearchParams({ ws: '1' }).toString()

async function chatApi<T = unknown>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(`chat API ${r.status}`)
  const t = await r.text()
  // POST /api/chat answers with an SSE STREAM unless ?ws=1 is set, so the body can
  // legitimately be `data: {...}` rather than JSON. Parsing that threw, and the
  // throw is what diverted a request into a brand-new ad-hoc chat.
  try { return (t ? JSON.parse(t) : null) as T } catch { return { ok: true, raw: t } as T }
}

/** Create (or adopt) this app's deterministic chat slot. Idempotent server-side. */
export function createChatSlot(name: string, title: string): Promise<ChatSlotResponse> {
  return chatApi<ChatSlotResponse>(CHAT_SLOTS, 'POST', { name, agent: '', title })
}

/**
 * Read the slot's recent transcript + pending queue, to check whether a sealed
 * batch actually reached the session.
 *
 * `slotKey` MUST be the same key dispatch used (`slotKeyFor(root)`), not a raw
 * project path. Slot adopt is idempotent BY NAME, so a raw path silently adopts a
 * different, empty slot: every request then reads as undelivered and a junk
 * session is left behind. The name is checked below to make that unmissable.
 *
 * TWO calls, deliberately. `POST /api/chat/slots` adopts the slot and returns
 * `serialize_slot()`, whose `messages` is a COUNT (`len(self.messages)`) with no
 * `queue` at all — reading it as a list silently finds nothing, which would make
 * every sealed request look undelivered and re-enable the duplicate resend this
 * check exists to prevent. Only `GET /api/chat/slots/{key}` returns the prepared
 * message entries and the pending queue.
 *
 * Returns `null` when either call fails, which callers must treat as "unknown"
 * rather than as either answer.
 */
export async function readSlotTranscript(
  slotKey: string,
  title: string,
): Promise<SlotTranscript | null> {
  // A path separator means a caller passed a filesystem path where the slot key
  // belongs. Refusing beats adopting the wrong slot and reporting "missing".
  if (!slotKey || slotKey.includes('/') || slotKey.includes('\\')) return null
  try {
    const created = await createChatSlot(slotKey, title)
    const key = created?.key
    if (!key) return null
    const detail = await chatApi<SlotTranscript>(slotDetailUrl(key), 'GET')
    if (!detail || !Array.isArray(detail.messages)) return null
    return { messages: detail.messages, queue: detail.queue }
  } catch {
    return null
  }
}

/** Send one turn into a slot. */
export function sendChatMessage(message: string, slot: string): Promise<unknown> {
  return chatApi(CHAT_SEND, 'POST', { message, slot, agent: '' })
}

/** Deep link into the Chat tab at a given slot. */
export function chatRoute(slotKey?: string): string {
  return slotKey ? '/chat' + '?' + new URLSearchParams({ sid: slotKey }).toString() : '/chat'
}

export type { Project }
