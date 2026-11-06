/** A close tombstone entry (see `applySlots`). */
export interface CloseHold {
  /** The `deleteSlot` attempt that owns this hold. A stale attempt's lifecycle
   *  actions (a `rejected` trailing a slow peer navigation) must not touch a
   *  newer attempt's hold on the same key. */
  requestId: string
  /** Deadline while the DELETE is in flight; `null` once the server has answered. */
  inFlightUntil: number | null
  /** This attempt received 404 because another close popped the key; wait for
   *  that close's durable removal or rollback signal. */
  awaitingOutcome?: true
  /** Straggler lists an awaiting or confirmed hold still survives. The same
   *  counter carries across a durable `removed` outcome. */
  graceFrames: number
  /** `fetchSlots` requests that were already in flight when the server
   *  confirmed the close. Their replies may predate the pop, so the hold
   *  outlives them however many frames arrive first. */
  awaitingFetches: string[]
  /** Wall-clock deadline for the confirmed phase. `chatSlots()` carries no
   *  deadline either, so an `awaitingFetches` entry that never settles would
   *  otherwise pin the tombstone until a reload. Set at confirmation. */
  confirmedUntil: number | null
}
