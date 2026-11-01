/** The sends each chat surface is warning about above its composer.
 *
 *  Separate from `meta.deliveryUnconfirmed` on purpose: that mark is cleared by the same
 *  confirmation doors that decide this, and the record must outlive the send path that raised it,
 *  since the paths that learn a receipt never came (`useChatPaneSend`, `composerSend`) are not the
 *  components that draw the strip.
 *
 *  Module-level and slot-keyed, like `queuedSendStash`: MembersPage reuses one unkeyed pane, so
 *  unscoped state captions the wrong member. Every warned send in a slot is kept, so a second
 *  doubtful send cannot overwrite the only copy of the first.
 *
 *  A record holds the payload its raising surface's own send-failure restore takes, and only that
 *  surface offers the exit: the two surfaces stage different context, so one cannot hand back what
 *  the other consumed.
 */

import { useSyncExternalStore } from 'react'

export type DeliveryWarningSurface = 'page' | 'pane'

export interface DeliveryWarningRecord<P = unknown> {
  sendId: string
  surface: DeliveryWarningSurface
  /** The slot's run epoch when the warning was raised: Remove ends only that turn. */
  epoch: number
  sent: P
}

const warnings = new Map<string, DeliveryWarningRecord[]>()
/** Sends the user removed. A receipt that still arrives for one means the composer holds a copy of
 *  a delivered message, which the user must be told before the next Enter sends it twice. */
const removed = new Map<string, Set<string>>()
const listeners = new Set<() => void>()
let version = 0
const NONE: readonly DeliveryWarningRecord[] = []

function announce(): void {
  version += 1
  for (const fn of listeners) fn()
}

export function warnDeliveryUnconfirmed<P>(slot: string, record: DeliveryWarningRecord<P>): void {
  if (!slot || !record.sendId) return
  const held = warnings.get(slot) ?? []
  warnings.set(slot, [...held.filter(r => r.sendId !== record.sendId), record])
  announce()
}

export function deliveryWarningsFor(slot: string | null | undefined): readonly DeliveryWarningRecord[] {
  return (slot && warnings.get(slot)) || NONE
}

/** Drop records whose row is no longer pending in the transcript: confirmed, queued, cleared or
 *  refetched away. Nothing is left for those to hand back without duplicating or misplacing it. */
export function pruneDeliveryWarnings(slot: string, stillPending: (sendId: string) => boolean): void {
  const held = warnings.get(slot)
  if (!held) return
  const kept = held.filter(r => stillPending(r.sendId))
  if (kept.length === held.length) return
  if (kept.length) warnings.set(slot, kept)
  else warnings.delete(slot)
  announce()
}

/** True once, for a removed send whose acceptance has now been observed. */
export function takeRemovedSend(slot: string, sendId: string): boolean {
  const ids = removed.get(slot)
  if (!ids?.delete(sendId)) return false
  if (!ids.size) removed.delete(slot)
  return true
}

export function removedSendsFor(slot: string | null | undefined): readonly string[] {
  return slot ? [...(removed.get(slot) ?? [])] : []
}

export function subscribeDeliveryWarnings(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => { listeners.delete(onChange) }
}

/** The `useSyncExternalStore` snapshot. A counter rather than the records: mutation keeps
 *  identities, so it would not re-render on its own. */
export function deliveryWarningVersion(): number {
  return version
}

export function useDeliveryWarnings(slot: string | null | undefined): readonly DeliveryWarningRecord[] {
  useSyncExternalStore(subscribeDeliveryWarnings, deliveryWarningVersion, deliveryWarningVersion)
  return deliveryWarningsFor(slot)
}

export interface DismissDeliveryWarningDeps<P> {
  /** Is this send's row still pending in the transcript, re-read at click time. */
  stillPending: (sendId: string) => boolean
  dropRow: (sendId: string, epoch: number) => void
  /** The surface's own send-failure restore. */
  restore: (sent: P) => void
  /** Folds a later payload into an earlier one. */
  merge: (earlier: P, later: P) => P
}

/** The strip's one exit, for the records `surface` raised. Each send is re-checked at click, not
 *  taken from the last render: one that confirmed or was queued in between is already on its way,
 *  and handing its text back would make the next Enter send it twice. The payloads are folded into
 *  one restore, because the composer state a restore reads only advances at render. */
export function dismissDeliveryWarnings<P>(
  slot: string | null | undefined,
  surface: DeliveryWarningSurface,
  deps: DismissDeliveryWarningDeps<P>,
): void {
  const held = deliveryWarningsFor(slot)
  const mine = held.filter(r => r.surface === surface)
  if (!slot || !mine.length) return
  const others = held.filter(r => r.surface !== surface)
  if (others.length) warnings.set(slot, others)
  else warnings.delete(slot)
  const live = mine.filter(r => deps.stillPending(r.sendId))
  if (live.length) {
    deps.restore(live.map(r => r.sent as P).reduce(deps.merge))
    const ids = removed.get(slot) ?? new Set<string>()
    for (const warned of live) {
      deps.dropRow(warned.sendId, warned.epoch)
      ids.add(warned.sendId)
    }
    removed.set(slot, ids)
  }
  announce()
}
