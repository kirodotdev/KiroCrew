import { useCallback, useSyncExternalStore } from 'react'
import { quoteIntoDraft } from './quoteDraft'

/**
 * Side Chat drafts, kept per slot for the life of the page — the ONE place a
 * Side Chat's unsent text lives.
 *
 * `SideChat` is mounted only while its host shows it — the activity panel's
 * Side tab, the Members drawer's Side Chat view — and every host has an
 * ordinary control right beside the composer that unmounts it (another tab,
 * "Details", closing the drawer, switching members). A draft held only in the
 * component's own state died with each of those, silently. This store is the
 * memory that outlives the mount, and it is SUBSCRIBABLE so the mounted panel
 * re-renders on every write, whoever wrote: the user typing, a failed request
 * handing its text back, or the selection toolbar's Ask seeding a quote.
 *
 * Seeding writes here too, rather than firing an event at the panel. An event
 * needs a listener already mounted — a panel that came up a frame late missed
 * it and the selection vanished — while a store entry simply waits for the
 * panel to mount and read it. `seedTick` tells the panel a seed (as opposed
 * to typing) just landed, so it can put the caret after the quote.
 *
 * In-memory on purpose: a Side Chat is a scratch conversation that is never
 * persisted ("Nothing here is saved to the chat"), so its draft should not
 * outlive the page either — unlike the main composer's per-slot draft store,
 * which survives a reload. Keyed by slot so two panels (the split view's
 * re-bound panel, a member thread) never see each other's text.
 */

interface SideChatDraft {
  text: string
  /** Bumped by every seed, reset to 0 once the panel has acted on it: non-zero
   *  means "a seed is waiting for the caret", never "was seeded once". */
  seedTick: number
}

const EMPTY: SideChatDraft = { text: '', seedTick: 0 }
const drafts = new Map<string, SideChatDraft>()
const listeners = new Set<() => void>()

function notify(): void {
  for (const cb of listeners) cb()
}

function entry(slot: string): SideChatDraft {
  return drafts.get(slot) ?? EMPTY
}

function set(slot: string, next: SideChatDraft): void {
  if (!next.text && next.seedTick === 0) drafts.delete(slot)
  else drafts.set(slot, next)
  notify()
}

export function readSideChatDraft(slot: string): string {
  return entry(slot).text
}

export function writeSideChatDraft(slot: string, text: string): void {
  const cur = entry(slot)
  if (cur.text === text) return
  set(slot, { text, seedTick: cur.seedTick })
}

/** Select-to-Ask: append `selection` to the slot's draft as a blockquote and
 *  mark it as a seed. Works whether or not that slot's panel is mounted. */
export function seedSideChatDraft(slot: string, selection: string): void {
  const sel = selection.trim()
  if (!sel) return
  const cur = entry(slot)
  set(slot, { text: quoteIntoDraft(cur.text, sel), seedTick: cur.seedTick + 1 })
}

/** The panel has acted on the pending seed (caret placed). Clears the seed
 *  mark so a later remount of the same slot — reopening the Side tab, a
 *  member switch and back — does not pull focus into the composer again. */
export function consumeSideChatSeed(slot: string): void {
  const cur = entry(slot)
  if (cur.seedTick === 0) return
  set(slot, { text: cur.text, seedTick: 0 })
}

// Module-private: `useSideChatDraft` is the one subscription surface.
function subscribeSideChatDrafts(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** The live draft for `slot`: re-renders on every write to that slot. */
export function useSideChatDraft(slot: string): SideChatDraft {
  const get = useCallback(() => entry(slot), [slot])
  return useSyncExternalStore(subscribeSideChatDrafts, get, get)
}

/** Test seam: forget every draft. */
export function clearSideChatDrafts(): void {
  drafts.clear()
  notify()
}
