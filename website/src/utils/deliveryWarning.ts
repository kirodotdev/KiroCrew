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
 *  the other consumed. Each record is mirrored into `sendRecoveryStore` for the next page load.
 */

import { useSyncExternalStore } from 'react'
import { forgetSendRecovery, loadSendRecoveries, parkSendRecovery, type LoadedSendRecovery } from './sendRecoveryStore'

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
/** The removed sends whose text an earlier page load's record handed back, not a Remove click. */
const recovered = new Set<string>()
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
  parkSendRecovery(slot, { sendId: record.sendId, surface: record.surface, sent: record.sent })
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
  for (const r of held) if (!kept.includes(r)) forgetSendRecovery(slot, r.sendId)
  if (kept.length) warnings.set(slot, kept)
  else warnings.delete(slot)
  announce()
}

/** Records this page load is checking or handed back, so a second mount cannot hand one back twice. */
const claimed = new Set<string>()
const claimKey = (slot: string, sendId: string): string => `${slot}|${sendId}`

/** `surface`'s persisted warnings for `slot` from an earlier page load, claimed for one check. Nothing
 *  retires here: the record stays the only copy until the handed-back text is durable. */
export function claimRecoveredSends(slot: string, surface: DeliveryWarningSurface): LoadedSendRecovery[] {
  const live = new Set(deliveryWarningsFor(slot).map(r => r.sendId))
  const gone = removed.get(slot)
  const out: LoadedSendRecovery[] = []
  for (const rec of loadSendRecoveries(slot)) {
    if (rec.surface !== surface || live.has(rec.sendId) || gone?.has(rec.sendId)) continue
    const key = claimKey(slot, rec.sendId)
    if (claimed.has(key)) continue
    claimed.add(key)
    out.push(rec)
  }
  return out
}

export function releaseRecoveredSends(slot: string, sendIds: readonly string[]): void {
  for (const id of sendIds) claimed.delete(claimKey(slot, id))
}

/** Count handed-back sends as removed, so a receipt that still lands says so as after Remove. */
export function adoptRecoveredSends(slot: string, sendIds: readonly string[]): void {
  if (!sendIds.length) return
  const gone = removed.get(slot) ?? new Set<string>()
  for (const id of sendIds) { gone.add(id); recovered.add(id) }
  removed.set(slot, gone)
  announce()
}

/** Once, for a removed send whose acceptance has now been observed: how its text came back. */
export function takeRemovedSend(slot: string, sendId: string): 'removed' | 'recovered' | null {
  const ids = removed.get(slot)
  if (!ids?.delete(sendId)) return null
  if (!ids.size) removed.delete(slot)
  return recovered.delete(sendId) ? 'recovered' : 'removed'
}

/** One composer instance's draft store: the only writer whose save can hold that instance's text. */
export interface DraftOwner { readonly draftOwner: true }

export function createDraftOwner(): DraftOwner {
  return { draftOwner: true }
}

/** Handed-back sends held only in one instance's memory, by `surface|slot`, until its own store writes. */
const awaitingDraft = new Map<string, Map<string, DraftOwner>>()
const draftKey = (surface: DeliveryWarningSurface, slot: string): string => `${surface}|${slot}`

export function awaitDraftSave(slot: string, surface: DeliveryWarningSurface, sendIds: readonly string[], owner: DraftOwner): void {
  if (!sendIds.length) return
  const key = draftKey(surface, slot)
  const ids = awaitingDraft.get(key) ?? new Map<string, DraftOwner>()
  for (const id of sendIds) ids.set(id, owner)
  awaitingDraft.set(key, ids)
}

function settleOwned(owner: DraftOwner, surface: DeliveryWarningSurface | null, inScope: (slot: string) => boolean, retire: boolean): void {
  for (const [key, ids] of awaitingDraft) {
    const bar = key.indexOf('|')
    const at = key.slice(bar + 1)
    if ((surface !== null && key.slice(0, bar) !== surface) || !inScope(at)) continue
    for (const [id, holder] of ids) {
      if (holder !== owner) continue
      if (retire) forgetSendRecovery(at, id)
      ids.delete(id)
    }
    if (!ids.size) awaitingDraft.delete(key)
  }
}

/** `owner`'s store wrote and kept the `survived` slots' complete drafts, so their records retire. An
 *  evicted slot stays awaiting: a later save of the same instance may keep it. */
export function draftSaved(surface: DeliveryWarningSurface, owner: DraftOwner, survived: (slot: string) => boolean): void {
  settleOwned(owner, surface, survived, true)
}

/** `owner` no longer holds the text: its records stay for the next page load, never another instance's save. */
export function dropDraftOwner(owner: DraftOwner): void {
  settleOwned(owner, null, () => true, false)
}

export function __resetDeliveryWarningsForTests(): void {
  warnings.clear(); removed.clear(); recovered.clear(); claimed.clear(); awaitingDraft.clear()
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
  for (const r of mine) forgetSendRecovery(slot, r.sendId)
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
