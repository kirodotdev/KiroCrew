/** What this window shows, and the attention signals the socket derives from it.
 *
 *  Owns the on-screen predicates the unread badge and the read relay share,
 *  the arrival rule that picks between them, and the slot-focus and
 *  read-relay senders with the focus, visibility and active-slot listeners
 *  that drive them. */
import { store } from '../../store'
import { isChatPath } from '../notificationBanner'
import { bindSlotReadSender, emitSlotRead, flushSlotRead } from '../../lib/slotReadRelay'
import { getViewedThreadSlot } from '../../lib/viewedThread'
import type { SocketConnection } from './connection'

/** True when this window is rendering the active slot's transcript: the chat
 *  routes (the root path serves ChatPage too) plus the popout and embed chat
 *  frames. Passive read relays (arrival, completion, tab reveal) must check
 *  this: `chat.activeSlot` is RETAINED across navigation, so on Settings or
 *  any other route "slot === activeSlot" says nothing about what the user can
 *  see, and relaying there would erase sibling windows' badges for messages
 *  nobody displayed. Passive relays must ALSO require document.hasFocus():
 *  Page Visibility reports occluded or unfocused windows as "visible"
 *  (Firefox tracks no occlusion; side-by-side windows always report
 *  visible), so a window parked behind other apps would otherwise mark
 *  every arrival read within ~1s — unseen work looking read, the inverse
 *  of the stale-badge defect. Deliberate gestures (switchSlot,
 *  mark-as-read) need no gate — they only occur on surfaces that show the
 *  slot, under real focus. */
const isChatSurfaceVisible = (): boolean =>
  typeof window !== 'undefined' && isChatPath(window.location.pathname)
/** True when *slot* is the thread this window is displaying: the chat
 *  surfaces' `chat.activeSlot`, or the thread a non-chat surface (the Crew
 *  Members page) registered in `viewedThread`. The unread-marker's gate: a
 *  message landing in a thread the user is watching is not unread. Without
 *  the second source every message in an open member thread was flagged and
 *  then cleared by the page's read effect one render later -- a badge that
 *  lit and vanished on the parent dashboard's crew tab for each message. */
export const isSlotOnScreen = (slot: string): boolean =>
  slot === store.getState().chat.activeSlot || slot === getViewedThreadSlot()
/** The read-relay twin of `isSlotOnScreen`: true when THIS window is rendering
 *  *slot* on a surface the user can see -- a chat route showing the active
 *  slot, or the registered viewed thread (registration already implies a
 *  visible, focused window). Used by the passive relays beside the focus
 *  gate: a message the marker declined to flag because the user was watching
 *  it arrive must still be relayed as read, or a sibling window's badge for
 *  it stays lit -- and `chat_done` moves no `last_ts`, so nothing else on the
 *  Members page would ever relay that completion. */
const isSlotRenderedVisibly = (slot: string): boolean =>
  isChatSurfaceVisible() || slot === getViewedThreadSlot()

/** The arrival rule `chat_message` and `chat_done` share: a transcript event
 *  in a slot the user is not looking at goes to `onOffScreen` (the badge);
 *  one landing in the slot this window visibly shows, while the tab is
 *  visible and focused, is relayed as already read so the fresh bubble other
 *  windows just lit for it retires. Reconnect catch-up replays are neither.
 *
 *  A hidden window relays nothing here: the visibilitychange handler relays
 *  the active slot's read on reveal, watermarked at its post-flush last_ts,
 *  which covers every arrival buffered while hidden. The watermark is the
 *  event's own server ts, else the slot's last_ts (also server-minted); never
 *  client time — windows minting their own clocks disagree about the same
 *  message. No per-arrival state is kept, so a later timestamp-less frame
 *  cannot regress the reveal watermark. */
export function attendArrival(
  slot: string | undefined,
  ts: string | undefined,
  reconnecting: boolean,
  onOffScreen: (slot: string) => void,
): void {
  if (slot && !isSlotOnScreen(slot) && !reconnecting) {
    onOffScreen(slot)
  }
  else if (slot && !reconnecting && !document.hidden && document.hasFocus() && isSlotRenderedVisibly(slot)) {
    const readTs = ts || store.getState().dashboard.slots?.find(s => s.key === slot)?.last_ts
    emitSlotRead(slot, readTs)
  }
}

/* Slot-focus intent signal (resume prefetch). The hook instance owns the
   live socket, but split view needs to report pane focus from a component
   tree that has no access to the hook's return value — so the sender is a
   module-level indirection the hook binds while mounted. Before the hook
   mounts (or after it unmounts) the emitter is a no-op: focus frames are a
   best-effort optimization, never load-bearing. */
let sendSlotFocusedImpl: (slot: string | null) => void = () => {}

export function emitSlotFocused(slot: string | null): void {
  sendSlotFocusedImpl(slot)
}

export interface FocusRelayDeps {
  socket: SocketConnection
  /** The buffered sidebar-recency bumps, flushed before a reveal's read relay. */
  flushSlotActivity: () => void
  /** A new active slot starts a new playback context. */
  onActiveSlotChange: () => void
}

/** Bind the focus and read-relay senders to the socket and start the
 *  listeners that drive them; returns the teardown, which also unbinds the
 *  focus emitter. */
export function attachFocusRelay({ socket, flushSlotActivity, onActiveSlotChange }: FocusRelayDeps): () => void {
  // Slot-focus intent signal (resume prefetch). One shared sender for
  // every focus source — Redux activeSlot changes (sidebar, keyboard,
  // deep links, history), tab visibility, and split-view pane focus via
  // emitSlotFocused — so the HTTP and WS notions of "focused" cannot
  // drift. Best-effort: dropped silently while the socket is not OPEN.
  const sendFocus = (slot: string | null) => {
    socket.sendIfOpen({ type: 'slot_focused', slot })
  }
  sendSlotFocusedImpl = sendFocus
  // Read-relay sender rides the same socket with the same best-effort
  // contract; slotReadRelay owns the per-slot throttle and the watermark.
  bindSlotReadSender((slot: string, readTs?: string) => {
    socket.sendIfOpen(readTs ? { type: 'slot_read', slot, read_ts: readTs } : { type: 'slot_read', slot })
  })
  let lastFocusSent: string | null = store.getState().chat.activeSlot
  const unsubFocus = store.subscribe(() => {
    const active = store.getState().chat.activeSlot
    if (active === lastFocusSent) return  // store.subscribe fires on EVERY action
    // The outgoing slot stops being visible-active NOW: flush its pending
    // trailing read-relay so the timer can't fire after a newer message
    // re-badges the slot and wipe a bubble nobody read.
    if (lastFocusSent) flushSlotRead(lastFocusSent)
    lastFocusSent = active
    onActiveSlotChange()
    sendFocus(active)
  })
  const onVisibility = () => {
    // Hidden → blur (cancels a pending prefetch server-side); visible →
    // re-announce the active slot even if unchanged, since the server may
    // have expired the previous prefetch while the tab was away.
    if (document.hidden) {
      // Going hidden: no pending trailing read-relay may outlive visibility
      // (a newer message could re-badge the slot before the timer fired).
      flushSlotRead()
    }
    sendFocus(document.hidden ? null : store.getState().chat.activeSlot)
    if (!document.hidden) {
      // Returning to the tab IS the read of whatever the active slot shows.
      // Flush buffered recency bumps first — rAF doesn't fire in hidden
      // tabs, so arrivals from the hidden stretch are still buffered — then
      // relay the active slot's read at its post-flush last_ts (server-
      // minted, monotonic in the reducer). Receivers keep badges lit by
      // anything newer (readCovers), so an idle reveal clears nothing it
      // shouldn't. Dropped during reconnect catch-up, mirroring the
      // arrival branches.
      flushSlotActivity()
      const active = store.getState().chat.activeSlot
      if (active && !socket.reconnectingRef.current && document.hasFocus() && isChatSurfaceVisible()) {
        emitSlotRead(active, store.getState().dashboard.slots?.find(s => s.key === active)?.last_ts)
      }
    }
  }
  document.addEventListener('visibilitychange', onVisibility)
  // Also on window focus: the passive-relay gate requires hasFocus(), and
  // a focus-only change (window occluded -> foreground) fires NO
  // visibilitychange — without this listener the relay suppressed while
  // unfocused never re-fires and sibling badges stay stale until the next
  // gesture. onVisibility's hidden branch is unreachable here (a focused
  // document is never hidden), so the focus path re-announces the slot,
  // flushes buffered activity, and relays the read under the same
  // visible+focused gate.
  window.addEventListener('focus', onVisibility)
  return () => {
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('focus', onVisibility)
    unsubFocus()
    sendSlotFocusedImpl = () => {}
  }
}
