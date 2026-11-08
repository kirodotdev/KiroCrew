/**
 * The host's realtime seam for one slot: a content-free "this slot changed"
 * signal plus the state of the channel that carries it.
 *
 * A host that holds a live event stream (the dashboard's `/api/ws`) passes one
 * to `AppScopedApiProvider` as `slotPush`. `ChatEmbed` then re-reads its slot
 * the moment the slot changes instead of waiting for the next poll, and slows
 * its poll to a backstop while the channel is up. A host without one (the
 * vendored iframe copy, a test) leaves it out and the embed polls as before.
 *
 * The signal carries no rows, no text and no slot metadata: the embed's own
 * scoped read stays the only way content reaches app code, so the read's
 * permission checks (the declared `permissions.api`, the gateway's slot
 * ownership check) still decide what an app sees. Which slots a host signals
 * for is the host's decision; the dashboard signals only for slots the app
 * owns, the same set an app token receives slot events for without declaring
 * any `slots:*` scope.
 */
import { useContext } from 'react'
import { AppSdkContext } from './scopedApi'

export interface SlotPushWatcher {
  /** The slot may have new rows: re-read it. */
  onChange(): void
  /**
   * The channel went up or down for this slot (see `SlotPush.connected`).
   * `true` is sent again on every reconnect, even when the previous call also
   * said `true`: a change made while the channel was down was never signalled,
   * so a watcher re-reads on each `true`.
   */
  onConnection(connected: boolean): void
}

export interface SlotPush {
  /** Whether this slot's changes are being signalled right now: the channel
   *  is up AND the host signals for this slot. A slot the host does not signal
   *  for reads `false`, so its embed keeps the full poll cadence. */
  connected(slotKey: string): boolean
  /** Watch one slot until the returned function is called. */
  watch(slotKey: string, watcher: SlotPushWatcher): () => void
}

/** The provider's slot push, or null when the host gave none. Null outside a
 *  provider too, so an embed rendered without one keeps polling. */
export function useSlotPush(): SlotPush | null {
  return useContext(AppSdkContext)?.slotPush ?? null
}
