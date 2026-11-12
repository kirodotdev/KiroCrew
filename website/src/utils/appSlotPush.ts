/** The dashboard socket's per-slot change signal for app embeds.
 *
 *  `useWebSocket` reports each frame here (`noteAppSlotFrame`), the slot-list
 *  handlers report the rows of each list they apply (`noteAppSlotRows`), and the
 *  connectivity helpers report the socket going up or down
 *  (`setAppSlotPushConnected`). A host hands an app `useAppSlotPush(app)`, the
 *  app-sdk `SlotPush` (app-sdk/slotPush.ts) through which `ChatEmbed` learns
 *  that its slot changed.
 *
 *  The permission boundary lives here. This socket is the dashboard user's, so
 *  it carries every slot's frames; an app is signalled only for a slot whose
 *  owning app (`ChatSlot.app`) is that app -- the set the gateway's own event
 *  gate (`ws_event_scope._slot_visible`) admits for an app token with no
 *  `slots:*` declaration. Ownership is read again at every delivery, so a slot
 *  that changes hands stops signalling at once. The signal carries nothing: no
 *  frame payload, no slot key beyond the one the app asked to watch. The app
 *  re-reads through its own scoped client, as `mc:app:slots` already does for
 *  the session list (hooks/websocket/slotList.ts). It sits outside
 *  hooks/websocket/ so app hosts can import it: that directory is reached only
 *  through the useWebSocket facade. */
import { useMemo } from 'react'
import { store } from '../store'
import type { SlotPush, SlotPushWatcher } from '../app-sdk/slotPush'
import type { ChatSlot } from '../types'

/** Frames that change what the slot-detail read returns: its rows, its queue,
 *  its running and stop state, a pending approval or question. Other
 *  slot-scoped frames (voice, subagent lifecycle, todo and report panels) leave
 *  that read as it was, and the embed's backstop poll covers anything missed. */
const SLOT_CHANGE_FRAMES: ReadonlySet<string> = new Set([
  'chat_chunk', 'chat_thinking', 'chat_status', 'chat_message', 'chat_done',
  'chat_segment', 'chat_append', 'chat_message_update', 'chat_variant_switch',
  'tool_call', 'tool_result',
  'queue_push', 'queue_cancel', 'queue_edit', 'queue_pop', 'queue_reorder',
  'steer_push', 'slot_clear',
  'approval', 'approval_resolved', 'question_card',
  // Stamps the elicitation card row resolved in the transcript.
  'elicitation_resolved',
  // Changes the title of the read, which the embed header shows.
  'slot_title',
])

/** Frames that name their slot in `key` rather than `slot`, as the gateway's
 *  gate reads them (`ws_event_scope.ws_event_allowed`). */
const KEY_SLOT_FRAMES: ReadonlySet<string> = new Set(['slot_title'])

/** Slot-list row fields that move with what the slot-detail read returns: a
 *  new row, a turn that starts, stops or ends, the queue, a pending approval.
 *  A row can enter a slot with no slot-scoped frame (a send whose sender drew
 *  it, as the gateway assumes of a `?ws=1` send without `meta.sendId`), but the
 *  slot list still restates these fields. An app token receives the same row
 *  for its own slot (`ws_event_scope.filter_slots_for_app`). */
const SLOT_ROW_FIELDS = ['messages', 'running', 'stopping', 'queue_depth', 'pending_approval'] as const

type RowFacts = Partial<Pick<ChatSlot, typeof SLOT_ROW_FIELDS[number]>>

interface Entry { app: string; watcher: SlotPushWatcher }

const watchers = new Map<string, Set<Entry>>()
/** The watched fields last seen for each watched slot. */
const lastRows = new Map<string, RowFacts>()
let connected = false
const watchListeners = new Set<() => void>()

/** The slots some app embed watches: they render a live transcript, so the
 *  dashboard socket names them in its `chunk_watch` set (websocket/chunkWatch). */
export function appWatchedSlots(): string[] {
  return [...watchers.keys()]
}

/** Call *listener* whenever the set `appWatchedSlots` returns may have changed. */
export function onAppWatchedSlotsChange(listener: () => void): () => void {
  watchListeners.add(listener)
  return () => { watchListeners.delete(listener) }
}

function watchedSlotsChanged(): void {
  for (const listener of [...watchListeners]) listener()
}

/** The owning app of a listed slot, or null when unowned or not listed. */
type OwnerOf = (slotKey: string) => string | null

/** This tab's slot-list row for a slot, or null when it is not listed. */
type ListedRowOf = (slotKey: string) => RowFacts | null

const storeRowOf = (slotKey: string): ChatSlot | null =>
  store.getState().dashboard.slots.find(s => s.key === slotKey) ?? null

const storeOwnerOf: OwnerOf = (slotKey) => storeRowOf(slotKey)?.app || null

let ownerOf: OwnerOf = storeOwnerOf
let listedRowOf: ListedRowOf = storeRowOf

/** Tell the app embeds that own and watch *slot* to re-read it. Content-free
 *  and gated on ownership; the `chunk_watch_ack` catch-up calls it too. */
export function noteAppSlotChanged(slot: string): void {
  const set = watchers.get(slot)
  if (!set || set.size === 0) return
  const owner = ownerOf(slot)
  if (!owner) return
  for (const e of [...set]) {
    if (e.app === owner) e.watcher.onChange()
  }
}

/** Signal the watchers of the frame's slot, if the frame changes it. */
export function noteAppSlotFrame(type: string, data: unknown): void {
  if (!SLOT_CHANGE_FRAMES.has(type) || typeof data !== 'object' || data == null) return
  const fields = data as { slot?: unknown; key?: unknown }
  const slot = fields.slot == null && KEY_SLOT_FRAMES.has(type) ? fields.key : fields.slot
  if (typeof slot !== 'string' || !slot) return
  noteAppSlotChanged(slot)
}

/** Record a watched slot's row and say whether a field moved. A field the row
 *  leaves out (a `slot_patch` row states only what it patches) keeps its last
 *  value. A field with no recorded value counts as moved: a slot first listed
 *  after the watch began can already carry the change the embed's mount read
 *  missed, and a spurious signal costs one read where a missed one costs a poll. */
function rowMoved(slotKey: string, row: Partial<ChatSlot>): boolean {
  const prev: Record<string, unknown> | undefined = lastRows.get(slotKey)
  const next: Record<string, unknown> = { ...prev }
  let moved = false
  for (const field of SLOT_ROW_FIELDS) {
    if (!(field in row)) continue
    if (!prev || !(field in prev) || prev[field] !== row[field]) moved = true
    next[field] = row[field]
  }
  lastRows.set(slotKey, next as RowFacts)
  return moved
}

/** Signal the watchers of each watched slot whose row in this list moved. The
 *  slot-list handlers call it with every list they apply: the rows of a full
 *  `slots` frame, or the partial rows of a `slot_patch`. */
export function noteAppSlotRows(rows: ReadonlyArray<Partial<ChatSlot>> | undefined): void {
  if (watchers.size === 0 || !Array.isArray(rows)) return
  for (const row of rows) {
    const key = row?.key
    if (typeof key !== 'string' || !watchers.has(key)) continue
    if (rowMoved(key, row)) noteAppSlotChanged(key)
  }
}

/** The socket went up (every successful connect, reconnects included) or down. */
export function setAppSlotPushConnected(value: boolean): void {
  connected = value
  for (const [slotKey, set] of [...watchers.entries()]) {
    for (const e of [...set]) e.watcher.onConnection(liveFor(e.app, slotKey))
  }
}

/** The channel is live for one app's watch on one slot only while the socket
 *  is up AND the slot is that app's: a slot it does not own is never signalled,
 *  so its embed must keep the full poll cadence instead of the backstop. */
function liveFor(app: string, slotKey: string): boolean {
  return connected && ownerOf(slotKey) === app
}

/** The `SlotPush` one app receives. */
export function createAppSlotPush(app: string): SlotPush {
  return {
    connected: (slotKey) => liveFor(app, slotKey),
    watch(slotKey, watcher) {
      const entry: Entry = { app, watcher }
      let set = watchers.get(slotKey)
      if (!set) {
        set = new Set()
        watchers.set(slotKey, set)
        // The embed reads its slot when it mounts, so the row this tab already
        // lists is the baseline: the next list that moves a field signals.
        const listed = listedRowOf(slotKey)
        if (listed) rowMoved(slotKey, listed)
        watchedSlotsChanged()
      }
      set.add(entry)
      return () => {
        const cur = watchers.get(slotKey)
        if (!cur) return
        cur.delete(entry)
        if (cur.size === 0) {
          watchers.delete(slotKey)
          lastRows.delete(slotKey)
          watchedSlotsChanged()
        }
      }
    },
  }
}

/** One stable `SlotPush` per app name, for a host to pass to its provider. */
export function useAppSlotPush(app: string): SlotPush {
  return useMemo(() => createAppSlotPush(app), [app])
}

/** Forget every watcher, every baseline row and the connection flag; optionally
 *  replace the owner and listed-row lookups (the store by default). For tests. */
export function resetAppSlotPushForTests(owner?: OwnerOf, listedRow?: ListedRowOf): void {
  watchers.clear()
  lastRows.clear()
  connected = false
  ownerOf = owner ?? storeOwnerOf
  listedRowOf = listedRow ?? storeRowOf
}
