/** The one client-side reading of a gateway slot list's drain hold.
 *
 *  While a restart is still restoring sessions, the gateway labels the lists it
 *  pushes (`slotsPartial`) and names the absent keys it still holds
 *  (`slotsHeldKeys`, plus every channel key under `slotsHoldChannels`). A key
 *  absent from such a list was not deleted, so nothing keyed by it may be
 *  destructively drained: not its unread entry, not its per-slot chat state, and
 *  not its contribution to a badge. Every site that applies a list asks this
 *  helper, so the hold rules cannot drift between them. */
import { slotChannelNamespace } from './channelOrigin'
import { isUnsafeKey } from './sanitize'

export interface SlotListHold {
  /** The list is labelled partial: every absent key is held. */
  partial: boolean
  /** Known absent keys held even once the list is labelled complete. */
  heldKeys: readonly string[]
  /** Every channel-origin key is held. */
  holdChannels: boolean
}

/** A test for whether the hold protects *key* from a destructive drain. It says
 *  nothing about liveness: callers check list membership first. Both spellings
 *  of an unsafe key are held, since some writers store it rerouted to
 *  `unsafe-key:<key>`. */
export const slotListHoldTest = ({ partial, heldKeys, holdChannels }: SlotListHold): ((key: string) => boolean) => {
  if (partial) return () => true
  const held = new Set(heldKeys.flatMap(key => (isUnsafeKey(key) ? [key, `unsafe-key:${key}`] : [key])))
  return key => held.has(key) || (holdChannels && !!slotChannelNamespace(key))
}
