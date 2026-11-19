/** Notes typed into an action card, kept outside any one component.
 *
 *  The stack unmounts on a session switch and the sidebar card on pointer
 *  leave, and a half-typed note must survive both. A draft also holds a copy of
 *  its ask, so when the agent clears the ask or the person's own message
 *  retires it, the window can keep the card on screen as closed and offer the
 *  note back for the composer instead of dropping it. */
import { useSyncExternalStore } from 'react'
import type { WaitingOnYouAsk } from '../../types'

export interface WaitingDraft {
  slot: string
  ask: WaitingOnYouAsk
  text: string
}

const drafts = new Map<string, WaitingDraft>()
const listeners = new Set<() => void>()
const NO_DRAFTS: readonly WaitingDraft[] = Object.freeze([])
// Per-slot snapshots, rebuilt only after a write, so `useSyncExternalStore`
// sees a stable value between writes.
let snapshots = new Map<string, readonly WaitingDraft[]>()

const keyOf = (slot: string, askId: string) => [slot, askId].join('\u0000')

function emit(): void {
  snapshots = new Map()
  for (const l of [...listeners]) l()
}

export function setWaitingDraft(slot: string, ask: WaitingOnYouAsk, text: string): void {
  const k = keyOf(slot, ask.ask_id)
  if (text) drafts.set(k, { slot, ask, text })
  else if (!drafts.delete(k)) return
  emit()
}

export function deleteWaitingDraft(slot: string, askId: string): void {
  if (drafts.delete(keyOf(slot, askId))) emit()
}

export function waitingDraftText(slot: string, askId: string): string {
  return drafts.get(keyOf(slot, askId))?.text ?? ''
}

function slotDrafts(slot: string): readonly WaitingDraft[] {
  const cached = snapshots.get(slot)
  if (cached) return cached
  const list = [...drafts.values()].filter(d => d.slot === slot)
  const value = list.length ? list : NO_DRAFTS
  snapshots.set(slot, value)
  return value
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Every draft typed for one slot's asks, live or since closed. */
export function useWaitingDrafts(slot: string | null): readonly WaitingDraft[] {
  return useSyncExternalStore(subscribe, () => (slot ? slotDrafts(slot) : NO_DRAFTS))
}

export function __resetWaitingDraftsForTests(): void {
  drafts.clear()
  emit()
}
