/** The dashboard socket's per-slot change signal for app embeds.
 *
 *  `useWebSocket` reports each frame here (`noteAppSlotFrame`) and the
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
])

interface Entry { app: string; watcher: SlotPushWatcher }

const watchers = new Map<string, Set<Entry>>()
let connected = false

/** The owning app of a listed slot, or null when unowned or not listed. */
type OwnerOf = (slotKey: string) => string | null

const storeOwnerOf: OwnerOf = (slotKey) =>
  store.getState().dashboard.slots.find(s => s.key === slotKey)?.app || null

let ownerOf: OwnerOf = storeOwnerOf

/** Signal the watchers of the frame's slot, if the frame changes it. */
export function noteAppSlotFrame(type: string, data: unknown): void {
  if (!SLOT_CHANGE_FRAMES.has(type) || typeof data !== 'object' || data == null) return
  const slot = (data as { slot?: unknown }).slot
  if (typeof slot !== 'string' || !slot) return
  const set = watchers.get(slot)
  if (!set || set.size === 0) return
  const owner = ownerOf(slot)
  if (!owner) return
  for (const e of [...set]) {
    if (e.app === owner) e.watcher.onChange()
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
      if (!set) { set = new Set(); watchers.set(slotKey, set) }
      set.add(entry)
      return () => {
        const cur = watchers.get(slotKey)
        if (!cur) return
        cur.delete(entry)
        if (cur.size === 0) watchers.delete(slotKey)
      }
    },
  }
}

/** One stable `SlotPush` per app name, for a host to pass to its provider. */
export function useAppSlotPush(app: string): SlotPush {
  return useMemo(() => createAppSlotPush(app), [app])
}

/** Forget every watcher and the connection flag; optionally replace the owner
 *  lookup (the store by default). For tests. */
export function resetAppSlotPushForTests(owner?: OwnerOf): void {
  watchers.clear()
  connected = false
  ownerOf = owner ?? storeOwnerOf
}
