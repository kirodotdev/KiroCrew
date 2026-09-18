/** Cross-window unread-badge sync: the read side of the relay.
 *
 * `markSlotUnread` reaches every window on its own (each holds a live WS and
 * badges any slot that is not ITS active slot), but `markSlotRead` was purely
 * window-local Redux + localStorage — reading a session in one dashboard
 * window left the bubble lit in every other one. This module carries the read
 * gesture to the gateway (`{type: 'slot_read', slot}`), which rebroadcasts it
 * to every owner window; each dispatches plain `markSlotRead` on receipt.
 *
 * The sender is a module-level indirection bound by `useWebSocket` while
 * mounted, for the same reason `emitSlotFocused` is one: read gestures happen
 * in code (chatSlice's `switchSlot`, the sidebar mark-as-read toggle, the
 * members surface) that has no access to the hook's socket. Before the hook
 * binds (or after it unmounts) the emitter is a no-op — the relay is a
 * best-effort optimization, never load-bearing: the local dispatch it rides
 * beside has already cleared THIS window.
 *
 * Emits are throttled per slot (leading + trailing edge). The arrival-branch
 * caller fires once per streamed row of a watched turn — tool events land
 * several per second — and dropping instead of coalescing would let a
 * final-row emit vanish inside the quiet window, leaving another window's
 * bubble lit until the next event. The trailing send makes the last read of
 * a burst always reach the wire.
 */

const READ_RELAY_QUIET_MS = 1_000

type PendingEntry = { timer: ReturnType<typeof setTimeout>; again: boolean; ts?: string }

let sendSlotReadImpl: (slot: string, readTs?: string) => void = () => {}
const pending = new Map<string, PendingEntry>()

/** Bind (or unbind, by passing a no-op) the wire sender. useWebSocket only. */
export function bindSlotReadSender(impl: (slot: string, readTs?: string) => void): void {
  sendSlotReadImpl = impl
}

/** Chronologically newer of two timestamps, parsed as instants — lexical
 *  order lies for mixed-offset strings. A side that does not parse loses to
 *  one that does; undefined loses to anything. */
/** Chronologically newer of two parseable instants; a side that does not
 *  parse loses to one that does, and `undefined` loses to anything. Shared by
 *  the relay's coalescing buffer and the store's watermark retention. */
export const newerTs = (a?: string, b?: string): string | undefined => {
  if (a === undefined) return b
  if (b === undefined) return a
  const pa = Date.parse(a)
  const pb = Date.parse(b)
  if (!Number.isFinite(pa)) return Number.isFinite(pb) ? b : a
  if (!Number.isFinite(pb)) return a
  return pb > pa ? b : a
}

/** Relay "this slot was read here" to every other open dashboard window.
 *
 *  `readTs` is the read WATERMARK: the newest message timestamp this window
 *  knows for the slot at the moment of the read. Receivers keep any badge
 *  their window recorded for a NEWER message, so an in-flight relay can never
 *  erase a message the reader had not seen (the classic race: A reads N and
 *  switches away, N+1 badges B, A's relay lands after). A coalesced burst
 *  carries the newest watermark seen during the window. */
export function emitSlotRead(slot: string, readTs?: string): void {
  if (!slot) return
  const entry = pending.get(slot)
  if (entry) {
    entry.again = true  // coalesce into one trailing send at quiet-window end
    entry.ts = newerTs(entry.ts, readTs)
    return
  }
  sendSlotReadImpl(slot, readTs)
  pending.set(slot, {
    again: false,
    ts: readTs,
    timer: setTimeout(() => {
      const e = pending.get(slot)
      pending.delete(slot)
      // Trailing edge: a read arrived mid-window; send the coalesced one so
      // the LAST read of a burst is never dropped.
      if (e?.again) sendSlotReadImpl(slot, e.ts)
    }, READ_RELAY_QUIET_MS),
  })
}

/** Flush a slot's pending trailing relay NOW (every slot when omitted).
 *
 * Called when a slot stops being the visible active one (active-slot switch,
 * document hidden). A trailing timer that outlived that status could fire
 * AFTER a newer message re-badged the slot in other windows and wipe a badge
 * nobody read; flushing at the boundary sends "read up to the moment I left"
 * and can never cover anything that arrives after it. */
export function flushSlotRead(slot?: string): void {
  for (const [key, e] of [...pending]) {
    if (slot !== undefined && key !== slot) continue
    clearTimeout(e.timer)
    pending.delete(key)
    if (e.again) sendSlotReadImpl(key, e.ts)
  }
}

/** Test hook: clear throttle state so cases don't leak windows into each other. */
export function _resetSlotReadRelayForTest(): void {
  for (const e of pending.values()) clearTimeout(e.timer)
  pending.clear()
  sendSlotReadImpl = () => {}
}
