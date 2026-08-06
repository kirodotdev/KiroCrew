/**
 * panelBridge — the original `window.mochi` preload bridge, re-implemented
 * over same-origin HTTP so ported components keep their exact call surface.
 *
 * Method names and signatures mirror the original preload (see the migration
 * doc's Phase C notes); components ported from the renderer should not need
 * to know the transport changed.
 *
 * `onWatchlistChanged` is PUSH: the backend publishes
 * `mochi:watchlist-changed` on every write and the frame arrives on the same
 * `/api/ws` this bridge already holds open. A slow interval remains only as a
 * safety net for changes missed while the socket is down.
 */
import {
  getPinned,
  getWatchlist,
  markPinnedSeen as apiMarkPinnedSeen,
  unpinFile as apiUnpinFile,
  updateWatchlist,
  type PinnedFileEntry,
  type WatchItem,
  type WatchStatus,
} from '../api'
import { approvalRoute } from './approvalActions'
import { purposeFromToolArgs } from '../../../utils/toolPurpose'
import type { NotificationPayload, PetMood, PetState } from '../src/shared/types'
import type { PackManifest, PackMeta } from '../src/shared/appearanceTypes'

/** Core's speech-to-text config, as the settings panel reads it. */
export interface SttConfig {
  backend?: string
  installed?: boolean
  available?: boolean
  model?: string
  models?: string[]
  language?: string
  prereqs?: Record<string, unknown>
  error?: string
}

type Listener = (items: WatchItem[]) => void
const listeners = new Set<Listener>()
let pollTimer: ReturnType<typeof setInterval> | null = null

/**
 * Last payload handed to listeners, serialized. Publishes are cheap to trigger
 * (the backend re-broadcasts on any watchlist-file mtime change) but NOT cheap
 * to consume: the panel replaces its items array and re-runs its selection
 * reconciliation, which resets the detail view mid-animation and reads as a
 * flicker. Skipping an identical payload makes a no-op publish a no-op here.
 */
let lastPayload = ''

async function refresh(): Promise<void> {
  try {
    const { items } = await getWatchlist()
    const serialized = JSON.stringify(items)
    if (serialized === lastPayload) return
    lastPayload = serialized
    for (const cb of listeners) cb(items)
  } catch {
    // Transient fetch failure — keep last state; next tick retries.
  }
}

/**
 * Slow SAFETY-NET poll, not the primary path.
 *
 * Freshness comes from the `mochi:watchlist-changed` broadcast (see
 * onWatchlistChanged). This interval only covers the window where the socket is
 * down and a change is missed entirely; it is deliberately much slower than the
 * old 10s poll it replaces.
 */
const SAFETY_POLL_MS = 120_000

function ensurePolling(): void {
  if (pollTimer === null && listeners.size > 0) {
    pollTimer = setInterval(refresh, SAFETY_POLL_MS)
  }
  if (pollTimer !== null && listeners.size === 0) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ── mochi-shaped surface ─────────────────────────────────────────────────

/**
 * Trust (approval) level for MOCHI'S OWN SLOT.
 *
 * Slot-scoped on purpose: this is an app settings panel, so it must never move
 * the dashboard's global approval posture. POST /api/chat/mode takes the slot
 * key and is the same endpoint the dashboard's own picker uses.
 */
export type MochiTrustLevel = 'normal' | 'trust_reads' | 'trust' | 'yolo'

export async function setMochiTrustLevel(level: MochiTrustLevel): Promise<void> {
  await fetch('/api/chat/mode', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: level, slot: MOCHI_SLOT }),
  })
}

/** Current level, derived from the slot's own trust flags. */
export async function getMochiTrustLevel(): Promise<MochiTrustLevel> {
  try {
    const res = await fetch('/api/chat/slots', { credentials: 'same-origin' })
    if (!res.ok) return 'normal'
    // The route returns a BARE ARRAY of slot payloads (chat_handlers
    // api_chat_slots ends in `json_response(payloads)`); reading a `slots`
    // wrapper key always yielded undefined, so the level read as 'normal'
    // every time the panel opened no matter what the slot actually was.
    const body = (await res.json()) as unknown
    const rows = (Array.isArray(body)
      ? body
      : Array.isArray((body as { slots?: unknown[] })?.slots)
        ? (body as { slots: unknown[] }).slots
        : []) as Record<string, unknown>[]
    const mine = rows.find((s) => s?.key === MOCHI_SLOT) as
      | { trust?: boolean; trust_reads?: boolean; mode?: string }
      | undefined
    if (mine === undefined) return 'normal'
    if (mine.mode === 'yolo') return 'yolo'
    if (mine.trust === true) return 'trust'
    if (mine.trust_reads === true) return 'trust_reads'
    return 'normal'
  } catch {
    return 'normal'
  }
}

/**
 * Report a countable companion event (Memories view).
 *
 * Fire-and-forget: a dropped stat must never affect the chat or the pet.
 */
export function reportStat(kind: 'message_sent' | 'message_received' | 'screenshot' | 'drag'): void {
  void fetch('/api/apps/mochi/stat', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind }),
  }).catch(() => undefined)
}

/** Chat-lifecycle events the pet state machine understands. */
export type PetEvent =
  | 'user_input' | 'task_start' | 'tool_call' | 'task_complete'
  | 'approval_required' | 'approval_granted' | 'approval_rejected' | 'error'

/**
 * Drive the backend pet state machine from the chat lifecycle.
 *
 * Upstream ran the chat controller in the same process as the state machine, so
 * a send moved the pet to `thinking`, a tool call to `working`, and completion
 * back to `idle`. Here the machine lives in the gateway and no seam publishes
 * chat lifecycle to an app, so it has to be reported. Reported HERE rather than
 * from a component: the pet is its own window, and the state must advance for
 * whichever surface is mounted (and while none is).
 *
 * Fire-and-forget — an animation must never be able to fail a send.
 */
export function reportPetEvent(event: PetEvent): void {
  void fetch('/api/apps/mochi/pet-event', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event }),
  }).catch(() => undefined)
}

export async function getWatchlistItems(): Promise<WatchItem[]> {
  const { items } = await getWatchlist()
  return items
}

// Original name preserved for the ported components' call sites.
export { getWatchlistItems as getWatchlist }

export function onWatchlistChanged(cb: Listener): () => void {
  listeners.add(cb)
  ensurePolling()
  // Push is the real path: the backend publishes mochi:watchlist-changed on
  // every write (hooks.py). Re-read rather than trusting the frame's payload —
  // the event is a bare {} signal, and the list is cheap to fetch.
  const offPush = subscribeAppEvent(WATCHLIST_CHANGED_TYPE, (payload) => {
    // The broadcast CARRIES the items (as upstream's IPC did), so deliver them
    // directly. Re-fetching here added an async round-trip that could land in
    // the middle of the panel's row-expand animation and replace the list
    // under it. Falls back to a fetch only if the payload is shapeless.
    const items = (payload as { items?: unknown } | undefined)?.items
    if (Array.isArray(items)) {
      const serialized = JSON.stringify(items)
      if (serialized === lastPayload) return
      lastPayload = serialized
      for (const cb of listeners) cb(items as WatchItem[])
      return
    }
    void refresh()
  })
  // A NEW subscriber must get a value even when the payload has not changed
  // since the last publish, so clear the dedup snapshot before the priming
  // fetch (the dedup exists for repeat publishes, not for first delivery).
  lastPayload = ''
  void refresh() // first value now, instead of one poll interval later
  return () => {
    offPush()
    listeners.delete(cb)
    ensurePolling()
  }
}

export async function setWatchItemStatus(
  id: string,
  status: WatchStatus,
): Promise<void> {
  await updateWatchlist({ update: [{ id, status }] })
  void refresh()
}

/**
 * Hard-delete a watch item. Distinct from setWatchItemStatus('cancelled'),
 * which keeps the item visible in a terminal state until it archives.
 */
export async function deleteWatchItem(id: string): Promise<void> {
  await updateWatchlist({ remove: [id] })
  void refresh()
}

export async function updateWatchItem(
  id: string,
  payload: Record<string, unknown>,
): Promise<void> {
  await updateWatchlist({ update: [{ id, ...payload }] })
  void refresh()
}

/**
 * Archive + delete every terminal watch item. Returns whether the SERVER agreed.
 *
 * The boolean is the point: this was fire-and-forget with an unchecked response,
 * while the caller had already dropped the items from its local list. A rejected
 * or failed request therefore looked like a successful delete until the next
 * refresh silently brought every item back.
 */
export async function clearCompletedWatchItems(): Promise<boolean> {
  let ok = false
  try {
    const res = await fetch('/api/apps/mochi/watchlist/clear-completed', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    ok = res.ok
  } catch {
    ok = false
  }
  // Refresh either way: on success it confirms, and on failure it is the
  // rollback — the list comes back from the server rather than from a guess.
  void refresh()
  return ok
}

// ── Pinned files ────────────────────────────────────────────────────────────

export async function getPinnedFiles(): Promise<PinnedFileEntry[]> {
  const { pins } = await getPinned()
  return pins
}

export async function markPinnedSeen(path: string): Promise<void> {
  await apiMarkPinnedSeen(path)
}

export async function unpinFile(path: string): Promise<void> {
  await apiUnpinFile(path)
}

/**
 * Reveal the file in the OS file manager.
 *
 * Shell capability (the original invoked `file:preview` in its main process).
 * Guarded because the browser dev preview has no shell to reveal anything with.
 */
export function previewFile(path: string): void {
  shell?.revealFile?.(path)
}

// ── Chat ────────────────────────────────────────────────────────────────────
//
// The original main process fanned three IPC channels to the renderer
// (chat:chunk / chat:done / chat:message). The gateway broadcasts exactly those
// three over the dashboard WebSocket (`chat_chunk` / `chat_done` /
// `chat_message`), so the three-channel model survives the port intact — this
// is why the panel uses the WS rather than POST /api/chat's SSE stream (SSE
// carries only the one merged stream and no slots/context side-channels).
//
// Mochi talks in its OWN slot so the pet's conversation is a distinct thread
// from whatever the user has open in the dashboard; every event is filtered by
// it.

// The app's dedicated chat slot key. Binding the slot to the app's agent (see
// ensureSlot) is what stops the ambient dashboard agent from answering here.
// Slot keys are KiroCrew-internal and never shown to the user.
export const MOCHI_SLOT = 'mochi'
// The agent kiro-cli must resolve. NOT the same string as the slot: kiro-cli
// keys agents by the "name" field inside the JSON, not by the namespaced link
// filename the app bridge writes into ~/.kiro/agents/, so agent names are one
// FLAT GLOBAL namespace. 'mochi-pet' is taken on some machines by an
// unrelated standalone build; app-id-prefixed names cannot collide.
export const MOCHI_AGENT = 'mochi'

type ChunkListener = (content: string) => void
type MessageListener = (msg: Record<string, unknown>) => void

/**
 * Roles the pet's chat surface renders.
 *
 * Core appends EIGHT roles to a slot and broadcasts every one as a
 * `chat_message`: assistant, user, error, plus the internal `tool` / `done` /
 * `chunk` / `system` / `permission` / `notice`. Upstream Mochi never saw those --
 * its main process broadcast only the user/assistant pair -- so the vendored
 * renderer has no concept of them and drew each one as an ordinary pet reply
 * (the "🔧 Running: …" bubbles). Filtering here covers live frames AND history
 * with one rule.
 *
 * `error` is kept: the panel has no other surface for a failed turn. `permission`
 * is dropped because approvals arrive on their own `approval` frame and would
 * otherwise render twice.
 */
const RENDERABLE_ROLES = new Set(['user', 'assistant', 'error'])

export function isRenderableChatRole(role: unknown): boolean {
  return typeof role === 'string' && RENDERABLE_ROLES.has(role)
}

/**
 * Reconstruct an approval request from a `permission`-role chat frame.
 *
 * The pet's turn runs on this dashboard slot, and — unlike Slack/background
 * approvals, which emit a dedicated `approval` frame via `state.request_approval`
 * — `chat_runner` surfaces an interactive tool approval as a `permission`-role
 * `chat_message` whose `cls` field holds the approval metadata
 * (`request_id` / `tool_title` / `tool_input`, and `resolved` once answered).
 * The panel drops `permission` as a non-renderable role, so without this the
 * pet showed nothing while the dashboard rendered the same request as a card.
 * This rebuilds the `{id, tool, toolInput}` shape the `approval` frame delivers
 * so BOTH surfaces render the card; resolution is already cross-surface
 * (`/api/approvals/{id}` scans slot futures and broadcasts `approval_resolved`).
 *
 * `full_command` / `base_command` are carried through as well. The gateway
 * pre-computes them for the dashboard's scoped Trust menu (see chat_runner's
 * `_extract_full_command` / `_extract_base_command`), and they arrive on this
 * frame already redacted. Dropping them is what limited the pet to the single
 * BROADEST grant ("trust all tools") while the dashboard could scope a grant to
 * one command — a security-relevant gap, since the pet's one button silently did
 * the widest thing.
 *
 * Returns null when the meta is missing/unparseable, carries no `request_id`,
 * or is already `resolved` — the last so a rehydrated history frame does not
 * re-open a card the user already answered.
 */
/**
 * The agent's own one-line statement of WHY it is calling the tool.
 *
 * Every tool call carries a reserved purpose argument (see
 * `utils/toolPurpose`), so this is the one field that describes the intent
 * WITHOUT echoing the command — which is what the pet's bubble wants: "needs
 * your approval for <purpose>" stays readable at bubble size and leaks no
 * argument values onto the desktop.
 */
function purposeFromToolInput(toolInput: string): string | undefined {
  try {
    return purposeFromToolArgs(JSON.parse(toolInput)) || undefined
  } catch {
    // Not JSON (a bare shell string) — there is no declared purpose to read.
    return undefined
  }
}

export function permissionApprovalFromFrame(
  data: Record<string, unknown>,
): {
  id: string
  tool: string
  toolInput?: string
  fullCommand?: string
  baseCommand?: string
  purpose?: string
} | null {
  let meta: unknown
  try {
    meta = JSON.parse(String(data.cls ?? ''))
  } catch {
    return null
  }
  if (!meta || typeof meta !== 'object') return null
  const m = meta as Record<string, unknown>
  const id = m.request_id
  if (typeof id !== 'string' || id === '') return null
  if (m.resolved) return null
  const tool =
    typeof m.tool_title === 'string' && m.tool_title ? m.tool_title : String(data.content ?? '')
  const req: {
    id: string
    tool: string
    toolInput?: string
    fullCommand?: string
    baseCommand?: string
    purpose?: string
  } = { id, tool }
  if (typeof m.tool_input === 'string' && m.tool_input) {
    req.toolInput = m.tool_input
    const purpose = purposeFromToolInput(m.tool_input)
    if (purpose !== undefined) req.purpose = purpose
  }
  if (typeof m.full_command === 'string' && m.full_command) req.fullCommand = m.full_command
  if (typeof m.base_command === 'string' && m.base_command) req.baseCommand = m.base_command
  return req
}

/** Give a gateway chat frame the ChatMessage shape the vendored renderer needs. */
function normaliseChatMessage(data: Record<string, unknown>): Record<string, unknown> {
  const role = typeof data.role === 'string' ? data.role : 'assistant'
  return {
    ...data,
    id: typeof data.id === 'string' && data.id !== '' ? data.id : `${role}-${Date.now()}-${msgSeq++}`,
    content: typeof data.content === 'string' ? data.content : '',
    timestamp: typeof data.timestamp === 'number' ? data.timestamp : Date.now(),
  }
}

/** Two frames can share a millisecond, and React keys must stay unique. */
let msgSeq = 0
type DoneListener = () => void
type SlotsListener = (slots: unknown[]) => void

const chunkListeners = new Set<ChunkListener>()
const messageListeners = new Set<MessageListener>()
const doneListeners = new Set<DoneListener>()
const slotsListeners = new Set<SlotsListener>()

type ContextListener = (pct: number) => void
const contextListeners = new Set<ContextListener>()

type StateListener = (state: PetState) => void
// The original always passes an intensity alongside the mood; a 1-arg type
// here made every vendored subscriber unassignable.
type MoodListener = (mood: PetMood, intensity: number) => void
const stateListeners = new Set<StateListener>()
const moodListeners = new Set<MoodListener>()

// Event-type strings the backend broadcasts for the pet (EventBus payloads,
// declared in app.json permissions.events). Named so the WS handler and any
// future emitter stay in lockstep. Their payload is {args: [value]} (the
// runtime's _broadcast helper), NOT a slot-scoped chat frame.
const PET_STATE_CHANGE_TYPE = 'pet:state-change'
const PET_MOOD_TYPE = 'mochi:mood'

// The remaining app-scoped broadcasts, all carrying the same {args:[payload]}
// envelope. Every one of these was already published by the backend and already
// reaching /api/ws; the panel simply had no branch for them.
const WATCHLIST_CHANGED_TYPE = 'mochi:watchlist-changed'
const PINNED_FILES_CHANGED_TYPE = 'pinned:files-changed'
const PINNED_FILE_UPDATED_TYPE = 'pinned:file-updated'
const PINNED_FILE_DELETED_TYPE = 'pinned:file-deleted'
const NOTIFY_TYPE = 'mochi:notify'
const CHAT_PUSH_TYPE = 'mochi:chat-push'
const PEEKING_TYPE = 'mochi:peeking'
const GALLERY_PACKS_CHANGED_TYPE = 'mochi:gallery-packs-changed'
const COLOR_MAP_CHANGED_TYPE = 'mochi:color-map-changed'

/**
 * Generic app-event fan-out, keyed by frame type.
 *
 * One registry rather than a Set per event: these all share an envelope and
 * differ only in payload, and a per-event Set is what let four of them be
 * forgotten. Adding an event is now a constant plus an exported subscriber.
 */
type AppEventListener = (payload: unknown) => void
const appEventListeners = new Map<string, Set<AppEventListener>>()

export function subscribeAppEvent(type: string, cb: AppEventListener): () => void {
  connect()
  let set = appEventListeners.get(type)
  if (set === undefined) {
    set = new Set()
    appEventListeners.set(type, set)
  }
  set.add(cb)
  return () => {
    set?.delete(cb)
  }
}

type ApprovalListener = (req: Record<string, unknown>) => void
const approvalListeners = new Set<ApprovalListener>()
const approvalResolvedListeners = new Set<ApprovalListener>()

type StatusListener = (online: boolean) => void
const statusListeners = new Set<StatusListener>()
let wsOnline = false

let socket: WebSocket | null = null
let reconnectDelay = 1000
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

/** Fire status subscribers only on an actual transition. */
function setOnline(next: boolean): void {
  if (wsOnline === next) return
  wsOnline = next
  for (const cb of statusListeners) cb(next)
}

function connect(): void {
  if (socket !== null) return
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const ws = new WebSocket(`${proto}//${location.host}/api/ws`)
  socket = ws

  ws.onopen = () => {
    reconnectDelay = 1000
    setOnline(true)
  }

  ws.onmessage = (ev) => {
    let msg: { type?: string; data?: Record<string, unknown> }
    try {
      msg = JSON.parse(ev.data as string)
    } catch {
      return
    }
    // App-scoped events are namespaced on the wire under a single WS type
    // (`app_event`) so an app's event name can never collide with a core WS
    // message type; the REAL event name is in `data.event` and the actual
    // payload in `data.data`. Unwrap to the pre-namespacing shape
    // (`{type: <real>, data: <payload>}`) so every branch below keeps matching.
    // WITHOUT this, notify bubbles, appearance/colour-map, pet state/mood,
    // watchlist, pinned and chat-push all silently stopped arriving.
    if (msg.type === 'app_event' && msg.data && typeof msg.data === 'object') {
      const inner = msg.data as { event?: unknown; data?: unknown }
      msg = {
        type: String(inner.event ?? ''),
        data: (inner.data ?? {}) as Record<string, unknown>,
      }
    }
    const data = msg.data ?? {}
    // Slots is global (not slot-scoped); everything else is filtered so the
    // pet never renders another slot's turn.
    if (msg.type === 'slots') {
      const slots = Array.isArray(data.slots) ? data.slots : []
      // RE-ARM the bind if our slot is gone. Deleting the slot elsewhere (the
      // dashboard's Chat tab closes it) left this page latched, so the next send
      // posted to /api/chat with no slot -- the gateway then auto-created one
      // with the DEFAULT agent, silently taking the pet's prompt, skills, MCP
      // and its context-usage reporting with it.
      const present = slots.some(
        (s) => (s as { key?: unknown } | null)?.key === MOCHI_SLOT,
      )
      if (!present) slotEnsured = false
      for (const cb of slotsListeners) cb(slots)
      return
    }
    // Pet state/mood are app-scoped broadcasts ({args:[value]}), NOT slot-scoped
    // chat frames — dispatch them before the slot filter below, which would
    // otherwise drop them for carrying no `slot`.
    if (msg.type === PET_STATE_CHANGE_TYPE) {
      const args = Array.isArray(data.args) ? data.args : []
      const s = String(args[0] ?? '') as PetState
      for (const cb of stateListeners) cb(s)
      return
    }
    if (msg.type === PET_MOOD_TYPE) {
      const args = Array.isArray(data.args) ? data.args : []
      const m = String(args[0] ?? '') as PetMood
      // Intensity is optional upstream; 1 means "as reported".
      const intensity = typeof args[1] === 'number' ? args[1] : 1
      for (const cb of moodListeners) cb(m, intensity)
      return
    }
    // Approval frames are gateway-level, not app events. `approval` carries the
    // pending record (including its slot); `approval_resolved` carries only
    // {id, approved} with NO slot, so it must be handled before the slot filter
    // below or it would be dropped — that frame is how the panel learns the
    // dashboard (or another surface) answered the same request.
    if (msg.type === 'approval') {
      if (data.slot === MOCHI_SLOT) {
        reportPetEvent('approval_required')
        for (const cb of approvalListeners) cb(data)
      }
      return
    }
    if (msg.type === 'approval_resolved') {
      reportPetEvent(data.approved ? 'approval_granted' : 'approval_rejected')
      for (const cb of approvalResolvedListeners) cb(data)
      return
    }
    // App-scoped broadcasts (pinned:*, mochi:watchlist-changed, mochi:notify,
    // gallery packs / colour map). Same {args:[payload]} envelope as pet state.
    // WITHOUT this branch the frames arrived and were thrown away, which is why
    // the pin rail and the appearance packs only ever refreshed by polling.
    const appSet = appEventListeners.get(String(msg.type ?? ''))
    if (appSet !== undefined) {
      // TWO envelopes are in use on the backend and both must work:
      //   `_broadcast(ch, ...args)` wraps as {args:[...]}  (pinned:*, gallery,
      //       colour map, pet:state-change, mochi:mood)
      //   `publish(ch, payload)`    sends the dict DIRECTLY (mochi:notify,
      //       mochi:move, mochi:idle, mochi:watchlist-changed)
      // Reading only `args[0]` handed every publish()-style listener `undefined`
      // — which is why bubbles and pet movement never appeared and the panel's
      // notifications were dead: the frame arrived, the payload was dropped.
      const payload = Array.isArray(data.args) ? data.args[0] : data
      for (const cb of appSet) cb(payload)
      return
    }
    if (data.slot !== MOCHI_SLOT) return
    switch (msg.type) {
      case 'chat_chunk':
        for (const cb of chunkListeners) cb(String(data.content ?? ''))
        break
      case 'chat_message':
        // Tool and error frames are not rendered by the panel, but they ARE the
        // pet's `working` and `error` transitions -- report before the
        // renderable-role filter below discards them.
        if (data.role === 'tool') reportPetEvent('tool_call')
        else if (data.role === 'error') reportPetEvent('error')
        else if (data.role === 'permission') {
          // Interactive tool approval for the pet's slot. Rebuild the request
          // from the frame's `cls` meta and drive the SAME approval-card path as
          // the `approval` frame — this is the only surface that renders it, and
          // dropping it (permission is not a renderable role) is why the pet went
          // silent while the dashboard showed the card.
          const req = permissionApprovalFromFrame(data)
          if (req) {
            reportPetEvent('approval_required')
            for (const cb of approvalListeners) cb(req)
          }
          break
        }
        // NORMALISE here, not in the component. The gateway frame is
        // {slot, role, content}; upstream's IPC always carried a full
        // ChatMessage, so the vendored renderer reads `msg.id.startsWith(...)`
        // unguarded and the FIRST message you send took the whole panel down.
        // Fixing it at the seam covers every consumer (messages AND history).
        if (!isRenderableChatRole(data.role)) break
        for (const cb of messageListeners) cb(normaliseChatMessage(data))
        break
      case 'chat_done':
        reportPetEvent('task_complete')
        for (const cb of doneListeners) cb()
        break
      case 'context_usage':
        // Payload is {slot, pct, used_tokens?, window_tokens?}; the ported
        // consumer only wants the percentage.
        for (const cb of contextListeners) cb(Number(data.pct ?? 0))
        break
    }
  }

  const reopen = () => {
    socket = null
    setOnline(false)
    // Capped backoff; the panel may outlive a gateway restart. Held in a
    // handle so retryConnect() can cancel it and reconnect immediately.
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 30_000)
  }
  ws.onclose = reopen
  ws.onerror = () => ws.close()
}

function subscribe<T>(set: Set<T>, cb: T): () => void {
  connect()
  set.add(cb)
  return () => {
    set.delete(cb)
  }
}

export const onChatChunk = (cb: ChunkListener) => subscribe(chunkListeners, cb)
export const onChatMessage = (cb: MessageListener) => subscribe(messageListeners, cb)
export const onChatDone = (cb: DoneListener) => subscribe(doneListeners, cb)
export const onSlotsUpdate = (cb: SlotsListener) => subscribe(slotsListeners, cb)

// ── Connection indicator ─────────────────────────────────────────────────────
//
// DECISION: what does "online / offline" mean for a builtin?
//
// In the original standalone app these methods probed a SEPARATE gateway
// process over the preload's status/retry IPC channels, which drove the main
// process's GatewayManager.spawn(); the "Start KiroCrew" button launched it. None of that maps onto a builtin: the panel is SERVED BY the gateway, so
// if the gateway were down this page could not have loaded at all and even the
// plain-HTTP calls (the watchlist, pins, history) would fail — there is no
// separate process to probe or to "start".
//
// The one connection a builtin CAN lose at runtime is THIS WebSocket. It
// carries the live chat stream (chat_chunk/chat_done/chat_message) plus the
// slots and context side-channels, and it already reconnects with backoff. So
// the indicator tracks the socket, and retryConnect() forces an immediate
// reconnect rather than spawning anything. (The panel relabels the old
// "Start KiroCrew" button to "Reconnect" — see ChatPanel's banner.)

/** Current WebSocket liveness. Kicks off a connect so the answer is meaningful. */
export function getBackendStatus(): Promise<boolean> {
  connect()
  return Promise.resolve(wsOnline)
}

/** Subscribe to WebSocket connect/disconnect transitions. */
export function onBackendStatus(cb: StatusListener): () => void {
  connect()
  statusListeners.add(cb)
  return () => {
    statusListeners.delete(cb)
  }
}

/**
 * Force an immediate reconnect. Cancels any pending backoff timer and reopens
 * the socket now. There is no gateway process to launch (see the DECISION note
 * above); this only kicks the WebSocket.
 */
export async function retryConnect(): Promise<{ ok: boolean; message?: string }> {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  reconnectDelay = 1000
  if (socket === null) connect()
  return { ok: true }
}

/**
 * Echo the user's own message locally.
 *
 * Upstream Mochi's MAIN process did this (`broadcastToRenderers(IPC.CHAT_MESSAGE,
 * userMsg)` in ipcHandlers), so the vendored ChatPanel never appends optimistically
 * — it waits for the frame. KiroCrew core does NOT echo a normal send over the
 * socket, so without this the panel showed only the replies.
 */
function echoOwnMessage(text: string, screenshot?: string): void {
  const msg: Record<string, unknown> = {
    id: `msg-${Date.now()}-${msgSeq++}`,
    role: 'user',
    content: text,
    timestamp: Date.now(),
  }
  if (screenshot !== undefined) msg.screenshot = screenshot
  for (const cb of messageListeners) cb(msg)
}

export async function sendMessage(text: string, screenshot?: string): Promise<void> {
  echoOwnMessage(text, screenshot)
  // Bind the slot to the mochi agent before the first turn (idempotent).
  await ensureSlot()
  // The pet must react to the SEND, not to the first token: `thinking` is
  // precisely the gap between the two. Reported after the bind so a turn that
  // never gets a slot does not leave the pet thinking about nothing.
  reportPetEvent('user_input')
  // `ws=1` tells the gateway to fan the turn out over the WebSocket instead of
  // holding an SSE response open (matching how the dashboard chat works).
  await fetch('/api/chat?ws=1', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: text,
      slot: MOCHI_SLOT,
      ...(screenshot ? { meta: { screenshot } } : {}),
    }),
  })
}

// Whether the slot is currently known to be bound to the mochi agent. NOT a
// once-per-page latch: it is cleared again whenever a `slots` frame shows the
// slot is gone (see the dispatcher), because a slot recreated without the agent
// falls back to the dashboard default.
let slotEnsured = false

/**
 * Create-or-get the pet's slot bound to the mochi agent BEFORE the first
 * send. Mirrors the original adapter's ensureSlot (createSlot = get_or_create):
 * without the binding the slot has no agent, so the ambient dashboard agent
 * answers the pet's chat. Binding via /api/chat/slots (get_or_create) rather
 * than the `agent` field on /api/chat avoids the 409 'slot agent mismatch'
 * path — the create endpoint never rejects an already-bound slot.
 */
export async function ensureSlot(): Promise<void> {
  if (slotEnsured) return
  const resp = await fetch('/api/chat/slots', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: MOCHI_SLOT, agent: MOCHI_AGENT }),
  })
  // A failed bind leaves the flag false so the next send retries rather than
  // posting to an unbound slot.
  if (!resp.ok) return
  // get_or_create returns an EXISTING slot UNCHANGED — it does not rebind its
  // agent. So a `mochi`-named slot already owned by another agent comes back
  // with that foreign agent, and sending the pet's turn into it would hijack and
  // corrupt that session. Verify the returned binding and refuse otherwise; the
  // throw aborts the send (ChatPanel restores the message and shows the error).
  let bound: { agent?: unknown } = {}
  try {
    bound = await resp.json()
  } catch {
    throw new Error('mochi slot: could not verify the agent binding')
  }
  if (bound.agent !== MOCHI_AGENT) {
    throw new Error(
      `mochi slot "${MOCHI_SLOT}" is bound to another agent ` +
        `(${String(bound.agent) || 'none'}); refusing to send`,
    )
  }
  slotEnsured = true
}

/**
 * Disable the Mochi app.
 *
 * The original's equivalent menu item quit a standalone Electron app. As a
 * builtin there is nothing to quit — the autonomous core runs inside the gateway
 * — so the honest action is to disable the app: the shell's reconcile loop then
 * closes the pet and the panel on its next tick, and the runtime's on_shutdown
 * hook stops the owner loop. The App Store re-enables it.
 *
 * Deliberately NOT a confirm dialog here: this is reached from a menu item
 * already marked `danger`, and re-enabling is one click in the App Store.
 */
export async function disableApp(): Promise<void> {
  await fetch('/api/apps/mochi/disable', {
    method: 'POST',
    credentials: 'same-origin',
  })
}

export async function stopGeneration(): Promise<void> {
  await fetch(`/api/chat/slots/${MOCHI_SLOT}/stop`, {
    method: 'POST',
    credentials: 'same-origin',
  })
}

/**
 * Backfill on mount. The original read Mochi's own chatHistory.json; as a
 * builtin the gateway owns persistence, so the slot's message list IS the
 * history (it already merges on-disk older messages with in-memory ones).
 * Returns an array so the ported call site's `Array.isArray` check holds.
 */
export async function getChatHistory(): Promise<Record<string, unknown>[]> {
  try {
    const resp = await fetch(`/api/chat/slots/${MOCHI_SLOT}`, {
      credentials: 'same-origin',
    })
    if (!resp.ok) return [] // 404 = slot not created yet (first ever open)
    const data = await resp.json()
    if (!Array.isArray(data.messages)) return []
    // Same rule as the live frames: persisted tool/system rows must not come
    // back as pet replies when the panel reloads its history.
    return (data.messages as Record<string, unknown>[]).filter((m) =>
      isRenderableChatRole(m.role),
    )
  } catch {
    return []
  }
}

/**
 * `/new` — start a fresh session. Deleting the slot drops its session and
 * context; the next sendMessage re-creates it empty, which is exactly the
 * original's "fresh session, history stays on screen" semantics (the ported
 * caller keeps the rendered messages and appends a separator itself).
 */
export async function newSession(): Promise<void> {
  const resp = await fetch(`/api/chat/slots/${MOCHI_SLOT}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  // The ported caller shows an error toast if this throws.
  if (!resp.ok && resp.status !== 404) {
    throw new Error(`newSession failed: ${resp.status}`)
  }
  // The slot is gone now (just deleted, or already absent on 404). Disarm the
  // agent-binding latch directly rather than waiting for the WS slots-update
  // that normally resets it (see the dispatcher): if the socket is down that
  // event never arrives, so the next send would recreate the slot WITHOUT
  // re-running ensureSlot() and leave it bound to the default agent.
  slotEnsured = false
}

/** Clear chat — same server-side operation; the caller also empties its lists. */
export const clearChat = newSession

/**
 * Permanently delete the pet's conversation history.
 *
 * NOT `newSession`. Deleting the slot ARCHIVES it (`save_slot_off_loop(...,
 * closed=true)`), so the conversation reappears in KiroCrew's session list with
 * its preview intact — which is what "reset" and the "cannot be undone" dialog
 * both looked like they had failed to do. `DELETE /api/sessions/{key}` is the
 * real erase: it drops the stored session, removes the matching slot, and pushes
 * both a slots update and a history refresh, so every surface stops showing it
 * without waiting for the next message to force a reload.
 */
export async function deleteHistory(): Promise<void> {
  const resp = await fetch(`/api/sessions/${encodeURIComponent(`dashboard:${MOCHI_SLOT}`)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  if (!resp.ok && resp.status !== 404) {
    throw new Error(`deleteHistory failed: ${resp.status}`)
  }
}

export const onContextUsage = (cb: ContextListener) =>
  subscribe(contextListeners, cb)

// ── Pet state / mood ─────────────────────────────────────────────────────────
//
// The panel title bar and the pet overlay read the CURRENT state/mood once on
// mount (getPetState) and then track live changes over the shared dashboard WS
// (onStateChange / onMood). The backend PetStateManager is the source of truth;
// no shell IPC is involved (the original drove these from the Electron main
// process, which does not exist for a builtin — which is why the ported call
// sites' shell forwards were dead and the title bar was stuck on 'offline').

/**
 * Current behaviour state AND mood, as the backend reports them.
 *
 * The route has always answered `{state, mood}` (see routes.py
 * `_handle_pet_state_get`), but this bridge only ever returned the state — so
 * every mount-time reader threw the mood away and could only learn one from a
 * LIVE `mochi:mood` frame. Moods are mostly transient (they self-clear after a
 * few seconds), so a panel or overlay opened between two mood changes showed
 * none at all: the chat title bar had no mood essentially always.
 *
 * Both fields degrade to the cold-start values rather than throwing, because the
 * callers render them directly.
 */
export async function getPetStateInfo(): Promise<{ state: PetState; mood: string }> {
  try {
    const res = await fetch('/api/apps/mochi/pet-state', { credentials: 'same-origin' })
    if (!res.ok) return { state: 'offline', mood: 'neutral' }
    const data = await res.json()
    return {
      state: typeof data.state === 'string' ? data.state : 'offline',
      // An older backend omits the key entirely; 'neutral' is the manager's own
      // initial value, so it is the honest stand-in for "not reported".
      mood: typeof data.mood === 'string' && data.mood !== '' ? data.mood : 'neutral',
    }
  } catch {
    return { state: 'offline', mood: 'neutral' }
  }
}

/** Current behaviour state; 'offline' when unavailable (matches cold-start). */
export async function getPetState(): Promise<PetState> {
  return (await getPetStateInfo()).state
}

export const onStateChange = (cb: StateListener) => subscribe(stateListeners, cb)
export const onMood = (cb: MoodListener) => subscribe(moodListeners, cb)

// ── Shell bridge (window.mochi) ──────────────────────────────────────────
//
// The panel window's preload is pet-preload.js, so the same `window.mochi`
// the pet overlay uses is present here too — but ONLY in the Electron shell. In
// the browser dev preview it is undefined, so every call is guarded and
// degrades to a no-op (the panel still renders; only shell-only actions sleep).
//
// These back the ChatPanel context-menu actions that must reach the main
// process: Avatars (its own window) and Dashboard (opened in the system
// browser), plus the pet-initiated Memories/Settings view switches the panel
// listens for. Named methods, not a generic relay — see petBridge's DECISION.
type ShellFn = (...args: unknown[]) => unknown
const shell = (window as unknown as { mochi?: Record<string, ShellFn | undefined> }).mochi

/** Open the Avatars window (character choice / pack import). */
export function openAvatars(): void {
  shell?.openAvatars?.()
}

/** Open the KiroCrew dashboard in the system browser (main supplies the URL). */
export function openDashboard(): void {
  shell?.openDashboard?.()
}

/**
 * Hide the chat panel — the title-bar close button.
 *
 * The main process HAD the handler all along (`mochi-panel:close` → hide, since
 * the panel is a hidden singleton); no layer exposed a sender, so the button was
 * dead. Hide, not destroy: the WS session and history must survive a close.
 */
export function closeChat(): void {
  shell?.closeChat?.()
}

/**
 * Reveal a file in Finder / the OS file manager.
 *
 * Shell-only by nature. Takes a path, and the main process validates it — page
 * content must not be able to hand the shell an arbitrary target.
 */
export function revealFile(path: string): void {
  shell?.revealFile?.(path)
}

/**
 * Open an http(s) link in the system browser.
 *
 * Needed because the panel is a frameless always-on-top window: letting a link
 * navigate in place would replace the chat with the page and there is no back
 * button. The main process enforces the http(s)-only rule.
 */
export function openExternal(url: string): void {
  shell?.openExternal?.(url)
}

/**
 * Show an image full-size, by handing it to the OS image viewer.
 *
 * Was a no-op, on the reasoning that a lightbox WINDOW was mere fidelity. In use
 * that reads as a broken control: the thumbnail invites a click and nothing
 * happens. An in-page lightbox is not the fix either -- the panel is 320px wide,
 * so "full size" cannot happen inside it. Preview (or the platform equivalent)
 * enlarges properly and costs no window.
 *
 * Takes a PATH. A `data:` URL is rejected rather than silently ignored, because
 * the OS viewer has nothing to open: callers must pass the underlying file path
 * even when what they render is a data URL built from its bytes.
 */
export function openLightbox(src: string): void {
  if (typeof src !== 'string' || src === '' || src.startsWith('data:')) return
  void shell?.openImage?.(src)
}


function onShellEvent(name: string, cb: () => void): () => void {
  const off = shell?.[name]?.(cb) as (() => void) | undefined
  return typeof off === 'function' ? off : () => {}
}

/** Pet menu → "show Memories" (the pet is a separate window; see pet-preload). */
export const onOpenMemories = (cb: () => void) => onShellEvent('onOpenMemories', cb)
/** Pet menu → "show Settings". The renderer mount is being ported in parallel. */
// Settings is its own window now, so there is no onOpenSettings view signal.
// These two carry the shared menu's panel-local actions from the pet.
export const onClearScreen = (cb: () => void) => onShellEvent('onClearScreen', cb)
export const onDeleteHistory = (cb: () => void) => onShellEvent('onDeleteHistory', cb)

// ── Appearance packs ────────────────────────────────────────────────────────
// Backed by /api/apps/mochi/packs (appearance_store.py). The original reached
// these over Electron IPC (`gallery:*`); here they are same-origin HTTP, so the
// panel and the dashboard both use the same path.

/** The pack list route returns the vendored PackMeta verbatim. */
export type PackMetaWire = PackMeta

export async function galleryListPacks(): Promise<PackMetaWire[]> {
  const res = await fetch('/api/apps/mochi/packs', { credentials: 'same-origin' })
  if (!res.ok) return []
  const body = await res.json()
  return Array.isArray(body?.packs) ? body.packs : []
}

export async function galleryGetPackDetail(packId: string): Promise<PackManifest | null> {
  const res = await fetch(`/api/apps/mochi/packs/${encodeURIComponent(packId)}`, {
    credentials: 'same-origin',
  })
  return res.ok ? res.json() : null
}

/** URL of one image inside a pack — usable directly as an `<img src>`. */
export function galleryPackFileUrl(packId: string, filename: string): string {
  return `/api/apps/mochi/packs/${encodeURIComponent(packId)}/file/${encodeURIComponent(filename)}`
}

export async function gallerySaveSpritePack(
  data: Record<string, unknown>,
): Promise<{ ok: boolean; packId?: string; error?: string }> {
  const res = await fetch('/api/apps/mochi/packs', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  const body = await res.json().catch(() => ({}))
  // The error is surfaced rather than swallowed: a save that fails silently
  // leaves the user believing their pack was stored.
  if (!res.ok) return { ok: false, error: body?.error ?? `save failed (${res.status})` }
  return { ok: true, packId: body?.packId }
}

/**
 * Make `packId` the active appearance, and CONFIRM it stuck.
 *
 * The response body is the updated settings, so the write can verify itself. It
 * used to ignore the status entirely: a rejected or clobbered write resolved
 * normally, the gallery marked the pack Active from local state, and the pet went
 * on rendering the previous character — an apply that failed was indistinguishable
 * from one that worked. Reading the persisted value back turns "nothing happened"
 * into a named error, including the case where something else wins the write.
 */
export async function gallerySetActive(packId: string): Promise<void> {
  const res = await fetch('/api/apps/mochi/settings', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ activeAppearance: packId }),
  })
  if (!res.ok) {
    throw new Error(`could not apply the appearance (${res.status})`)
  }
  const saved = (await res.json().catch(() => null)) as { activeAppearance?: unknown } | null
  if (saved !== null && saved.activeAppearance !== packId) {
    throw new Error(
      `the appearance did not stick — stored value is ${String(saved.activeAppearance)}`,
    )
  }
}

export async function galleryDeletePack(packId: string): Promise<boolean> {
  const res = await fetch(`/api/apps/mochi/packs/${encodeURIComponent(packId)}`, {
    method: 'DELETE',
    credentials: 'same-origin',
  })
  return res.ok
}

// ── Live-refresh subscribers ────────────────────────────────────────────────
//
// These replace polling for the pin rail and make appearance changes take
// effect without reopening the pet. Each returns an unsubscribe, so the ported
// cleanup (`off?.()`) works unchanged.

/** Whole pin list changed — payload IS the new list. */
export function onPinnedFilesChanged(cb: (pins: PinnedFileEntry[]) => void): () => void {
  return subscribeAppEvent(PINNED_FILES_CHANGED_TYPE, (payload) => {
    cb(Array.isArray(payload) ? (payload as PinnedFileEntry[]) : [])
  })
}

/** One pinned file's contents changed on disk — {path, updatedAt}. */
export function onPinnedFileUpdated(
  cb: (info: { path: string; updatedAt: number }) => void,
): () => void {
  return subscribeAppEvent(PINNED_FILE_UPDATED_TYPE, (payload) => {
    const info = (payload ?? {}) as { path: string; updatedAt?: number }
    cb({ path: info.path, updatedAt: info.updatedAt ?? Date.now() })
  })
}

/** A pinned file disappeared — {path}. */
export function onPinnedFileDeleted(cb: (info: { path: string }) => void): () => void {
  return subscribeAppEvent(PINNED_FILE_DELETED_TYPE, (payload) => {
    cb((payload ?? {}) as { path: string })
  })
}

/** The pet tucked itself against a screen edge, or came back out. */
export function onPeeking(cb: (peeking: boolean) => void): () => void {
  return subscribeAppEvent(PEEKING_TYPE, (payload) => {
    const data = (payload ?? {}) as { peeking?: boolean }
    cb(data.peeking === true)
  })
}

/** Agent-originated notification (watch hit, reminder, degraded run). */
export function onNotification(cb: (n: NotificationPayload) => void): () => void {
  return subscribeAppEvent(NOTIFY_TYPE, (payload) => {
    cb((payload ?? {}) as unknown as NotificationPayload)
  })
}

/**
 * Agent-pushed pet message for the transcript (`notify` with pushToChat).
 *
 * The backend already applied the re-notify guard (hooks.py `_push_to_chat`),
 * so every event that arrives is an accepted push — the panel just renders
 * it. Forwarded into the SAME listener set as real chat frames so ChatPanel
 * needs no second path. Display-only: the transcript is backed by the core
 * chat slot, which has no append-without-a-turn API, so the message lives
 * until the panel reloads; the durable record is the activity-log entry the
 * backend wrote. Module-scope subscription (like the pet's bubble listener):
 * the frame must not be lost just because no panel view happens to be
 * mounted at that moment.
 */
subscribeAppEvent(CHAT_PUSH_TYPE, (payload) => {
  const data = (payload ?? {}) as { content?: string; timestamp?: number }
  if (typeof data.content !== 'string' || data.content === '') return
  const ts = typeof data.timestamp === 'number' ? data.timestamp : Date.now()
  const msg = {
    id: `push-${ts}-${msgSeq++}`,
    role: 'assistant',
    content: data.content,
    timestamp: ts,
  }
  for (const cb of messageListeners) cb(msg)
})

/** Appearance packs added/removed — consumers re-read the pack list. */
export function onGalleryPacksChanged(cb: (packId: string) => void): () => void {
  // The frame does not name the pack; consumers re-read the whole list.
  return subscribeAppEvent(GALLERY_PACKS_CHANGED_TYPE, () => cb(''))
}

/** Active pack's colour map changed — consumers re-render current art. */
export function onColorMapChanged(cb: (map: unknown) => void): () => void {
  return subscribeAppEvent(COLOR_MAP_CHANGED_TYPE, (payload) => cb(payload))
}

// ── Approvals (reuse core, do NOT re-implement) ─────────────────────────────
//
// The original did not own this logic either — it called its gateway client. The
// core gateway already exposes GET /api/approvals plus the two decision routes
// (see approvalActions.ts for why trust needs a different one), and it pushes
// `approval` / `approval_resolved` over the SAME /api/ws this panel is already
// connected to. So this is wiring, not a re-build.

export function onApprovalRequest(cb: ApprovalListener): () => void {
  connect()
  approvalListeners.add(cb)
  return () => {
    approvalListeners.delete(cb)
  }
}

/**
 * The request was answered somewhere else (dashboard, Slack, another surface).
 * The frame carries no slot, so this fires for ANY resolution; the panel's own
 * handler clears its pending cards, which is the original's behaviour.
 */
export function onApprovalResolvedExternal(cb: ApprovalListener): () => void {
  connect()
  approvalResolvedListeners.add(cb)
  return () => {
    approvalResolvedListeners.delete(cb)
  }
}

/** Pending approvals at mount time — rehydration after a panel reopen. */
export async function getPendingApprovals(): Promise<Record<string, unknown>[]> {
  try {
    const res = await fetch('/api/approvals', { credentials: 'same-origin' })
    if (!res.ok) return []
    const body = await res.json()
    const all = Array.isArray(body) ? (body as Record<string, unknown>[]) : []
    return all.filter((a) => a.slot === MOCHI_SLOT)
  } catch {
    return []
  }
}

/**
 * Answer one approval. Returns `{ok:false}` on failure so the caller can say so
 * instead of claiming the tool was approved while the agent stays blocked (that
 * exact false confirmation is why the result is checked at all).
 *
 * `pattern` scopes a trust grant (`trust_command` / `trust_base`) to one command
 * or one command family; the slot endpoint reads it as `pattern`. Omitted for
 * `approve` / `reject` and for the unscoped `trust` (trust every tool), which
 * carry no pattern.
 */
export async function respondApproval(
  id: string,
  action: string,
  pattern?: string,
): Promise<{ ok: boolean; error?: string }> {
  const route = approvalRoute(action)
  try {
    const res =
      route.kind === 'approval'
        ? await fetch(`/api/approvals/${encodeURIComponent(id)}/${route.action}`, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
          })
        : await fetch(`/api/chat/slots/${MOCHI_SLOT}/approve`, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              action: route.action,
              request_id: id,
              ...(pattern ? { pattern } : {}),
            }),
          })
    if (!res.ok) return { ok: false, error: `approval failed (${res.status})` }
    return { ok: true }
  } catch (err) {
    return { ok: false, error: String(err) }
  }
}

// ── Model selection + edit/resend (reuse core) ──────────────────────────────

/** One entry of core's `/api/models` payload (kiro-cli's own `--list-models`). */
export interface ModelChoice {
  model_name: string
  model_id?: string
  display_name?: string
  description?: string
}

/**
 * Available models, or `[]`.
 *
 * `[]` covers BOTH "no models" and core's degraded 503 (kiro-cli signed out or
 * the list spawn timed out). The caller hides the selector on an empty list, so
 * a degraded gateway shows nothing rather than an empty dropdown that silently
 * fails to switch anything — which is what the original did too.
 */
export async function getModels(): Promise<ModelChoice[]> {
  try {
    const res = await fetch('/api/models', { credentials: 'same-origin' })
    if (!res.ok) return []
    const body = await res.json()
    const list = Array.isArray(body) ? body : body?.models
    return Array.isArray(list) ? (list as ModelChoice[]) : []
  } catch {
    return []
  }
}

/**
 * The model the PET's slot is currently on.
 *
 * The original never read this — its selector opened on a blank value, so the
 * dropdown misreported the state until the user touched it. The slot list is the
 * only place core reports it (the per-slot detail payload does not carry model).
 * `''` means "gateway default", which is a real, selectable value.
 */
export async function getSlotModel(): Promise<string> {
  try {
    const res = await fetch('/api/chat/slots', { credentials: 'same-origin' })
    if (!res.ok) return ''
    const body = await res.json()
    const slots = Array.isArray(body) ? body : body?.slots
    if (!Array.isArray(slots)) return ''
    const mine = slots.find((s: Record<string, unknown>) => s.key === MOCHI_SLOT)
    return typeof mine?.model === 'string' ? mine.model : ''
  } catch {
    return ''
  }
}

/**
 * Switch the pet slot's model. Slot-scoped, so it cannot disturb the dashboard's
 * own conversations. An empty string hands the slot back to the gateway default.
 */
export async function setModel(model: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/chat/slots/${MOCHI_SLOT}/model`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    })
    return res.ok
  } catch {
    return false
  }
}

/**
 * Edit a previous user message and re-run from it.
 *
 * Core's contract is {ts, content} — the message is addressed by its TIMESTAMP,
 * not an index, which is what the ported call site already passes. Returns the
 * `{ok}` shape that call site checks so a failure falls back to a plain send
 * instead of silently dropping the edit.
 */
export async function editResend(
  text: string,
  ts: string,
): Promise<{ ok: boolean; message?: string }> {
  try {
    const res = await fetch(`/api/chat/slots/${MOCHI_SLOT}/edit-resend`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ts, content: text }),
    })
    return { ok: res.ok }
  } catch {
    return { ok: false }
  }
}

// ── Files / images (reuse core, do NOT re-implement) ────────────────────────

/**
 * URL for a local image, usable directly as an `<img src>`.
 *
 * Replaces the original's `readLocalImage`, which round-tripped file bytes
 * through base64 over IPC. Core's route streams them and adds symlink and
 * sensitive-path guards the original never had, so re-porting the old path would
 * be strictly worse.
 */
export function localFileUrl(path: string): string {
  return `/api/file-raw?path=${encodeURIComponent(path)}`
}

// ── Remote instances (replaces the original's tunnel/backend surface) ───────
//
// The original's tunnelStatus / onTunnelStatus / backend-switch methods answered
// "which gateway am I talking to, and is the tunnel up". As a same-origin builtin
// there is no separate backend to pick — but "point the pet at a REMOTE
// KiroCrew" survives as a real feature, and core owns it (/api/instances/*).
// So this is a REUSE, not a deletion: Mochi's `petInstance` setting names an
// instance, and its liveness is read from core rather than from a tunnel Mochi
// managed itself.
//
// NEVER re-introduce here: the original also exposed a method that handed the
// renderer the gateway's own shared secret, plus tunnel connect/disconnect. A
// secret readable by page content defeats the app-token boundary entirely — an
// app must never be able to read a credential it was not issued.

/** One configured instance, as `/api/instances` reports it. */
export interface CoreInstance {
  id: string
  name: string
  remote_port?: number
  /** SSH-forwarded local port. 0 when no tunnel is up, so it is also the "is it usable" test. */
  local_port?: number
  ttl?: string
  status?: {
    state?: 'disconnected' | 'connecting' | 'connected' | 'error'
    error?: string
    token_ttl_remaining?: number
  }
}

/**
 * The instance list, with the states core distinguishes preserved.
 *
 * Returning a bare array (as this did) collapsed three very different answers
 * into "no instances": the feature being OFF (`instances.enabled` defaults to
 * false, so the route 403s), the feature being on but NOT YET ACTIVE (the SSH
 * manager only exists if the flag was set at gateway startup — core says the UI
 * should tell the user to restart), and genuinely having none configured. Each
 * needs different words in front of the user, so each is its own state here.
 */
export type InstancesView =
  | { state: 'disabled' }
  | { state: 'error' }
  | { state: 'inactive'; instances: CoreInstance[] }
  | { state: 'ready'; instances: CoreInstance[] }

export async function listInstances(): Promise<InstancesView> {
  try {
    const res = await fetch('/api/instances', { credentials: 'same-origin' })
    // 403 is the documented "instances.enabled is off" answer, not a failure.
    if (res.status === 403) return { state: 'disabled' }
    if (!res.ok) return { state: 'error' }
    const body = await res.json()
    const list = Array.isArray(body) ? body : body?.instances
    const instances = Array.isArray(list) ? (list as CoreInstance[]) : []
    return { state: body?.active === false ? 'inactive' : 'ready', instances }
  } catch {
    return { state: 'error' }
  }
}

/** Liveness of one configured instance (core owns the SSH tunnel, not Mochi). */
export async function getInstanceStatus(id: string): Promise<Record<string, unknown> | undefined> {
  try {
    const res = await fetch(`/api/instances/${encodeURIComponent(id)}/status`, {
      credentials: 'same-origin',
    })
    return res.ok ? await res.json() : undefined
  } catch {
    return undefined
  }
}
//
// ── Speech-to-text (reuse core's stack) ─────────────────────────────────────
//
// Mochi's own STT plumbing is deliberately NOT ported: core owns the whole
// stack, and keeping Mochi's would mean two sources of truth for the model,
// install location and config. (The original's speechService.ts was already
// dead code there — the path it shipped never called it.)

export async function getSttConfig(): Promise<SttConfig | undefined> {
  try {
    const res = await fetch('/api/config/stt', { credentials: 'same-origin' })
    return res.ok ? await res.json() : undefined
  } catch {
    return undefined
  }
}

export async function installStt(): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch('/api/stt/install', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  // The settings UI shows the failure reason, so the status text is carried
  // through rather than collapsed into a bare boolean.
  if (res.ok) return { ok: true }
  return { ok: false, error: `install failed (${res.status})` }
}

/** Transcribe one recorded clip. `audio` is a base64 payload, as core expects. */
export async function transcribeAudio(
  audio: string,
  mime = 'audio/webm',
): Promise<string | undefined> {
  try {
    const res = await fetch('/api/stt/transcribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ audio, mime }),
    })
    if (!res.ok) return undefined
    const body = await res.json()
    return typeof body?.text === 'string' ? body.text : undefined
  } catch {
    return undefined
  }
}
