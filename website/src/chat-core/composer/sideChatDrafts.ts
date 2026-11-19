import { useCallback, useSyncExternalStore } from 'react'
import { quoteIntoDraft } from './quoteDraft'
import { carryPastes, mergeCarriedDraft, pruneBlocks, type PasteBlock } from '../../utils/pasteTokens'

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
 * The collapsed paste blocks behind any `[ Paste #N · M lines ]` token in the
 * text live in the SAME entry. The token means nothing without its block —
 * restored alone it shows a chip that expands to nothing and sends the
 * literal token string — so blocks take exactly the text's lifetime: they
 * survive the remounts the text survives and go when the text goes. Writing
 * the text prunes blocks whose token is no longer in it.
 *
 * The attachment paths staged for the question (uploaded images and files,
 * shown as chips) live in the entry too, beside the text rather than in it:
 * they are typed work the same way, so a remount keeps them and a refused send
 * hands them back. Writing the text never touches them; only a send, a chip's
 * remove or a hand-back does.
 *
 * In-memory on purpose: a Side Chat is a scratch conversation that is never
 * persisted ("Nothing here is saved to the chat"), so its draft should not
 * outlive the page either — unlike the main composer's per-slot draft store,
 * which survives a reload. That holds for the blocks too: a pasted secret in
 * an off-record composer does not land in localStorage. Keyed by slot so two
 * panels (the split view's re-bound panel, a member thread) never see each
 * other's text.
 */

interface SideChatDraft {
  text: string
  /** Bumped by every seed, reset to 0 once the panel has acted on it: non-zero
   *  means "a seed is waiting for the caret", never "was seeded once". */
  seedTick: number
  /** Blocks behind the `[ Paste #N · M lines ]` tokens in `text`. */
  pastes: PasteBlock[]
  /** Uploaded attachment paths staged for the question, in staging order. */
  files: string[]
}

const NO_PASTES: PasteBlock[] = []
const NO_FILES: string[] = []
const EMPTY: SideChatDraft = { text: '', seedTick: 0, pastes: NO_PASTES, files: NO_FILES }
const drafts = new Map<string, SideChatDraft>()
const listeners = new Set<() => void>()

function notify(): void {
  for (const cb of listeners) cb()
}

function entry(slot: string): SideChatDraft {
  return drafts.get(slot) ?? EMPTY
}

function set(slot: string, next: SideChatDraft): void {
  if (!next.text && next.seedTick === 0 && !next.pastes.length && !next.files.length) drafts.delete(slot)
  else drafts.set(slot, next)
  notify()
}

export function readSideChatDraft(slot: string): string {
  return entry(slot).text
}

/** Write the slot's text. Blocks whose token the new text no longer contains
 *  are dropped with it, so a token deleted, cut or overwritten as text cannot
 *  leave a block behind. */
export function writeSideChatDraft(slot: string, text: string): void {
  const cur = entry(slot)
  if (cur.text === text) return
  set(slot, { ...cur, text, pastes: pruneBlocks(text, cur.pastes) })
}

/** Replace the slot's paste blocks — the composer's `onPasteBlocksChange`. */
export function writeSideChatPastes(slot: string, pastes: PasteBlock[]): void {
  const cur = entry(slot)
  if (cur.pastes === pastes) return
  set(slot, { ...cur, pastes: pastes.length ? pastes : NO_PASTES })
}

/** Replace the slot's staged attachment paths (a chip removed, a send). */
export function writeSideChatFiles(slot: string, files: readonly string[]): void {
  const cur = entry(slot)
  if (cur.files === files || (!cur.files.length && !files.length)) return
  set(slot, { ...cur, files: files.length ? [...files] : NO_FILES })
}

/** Stage `paths` after the slot's current ones, skipping any already staged. */
export function addSideChatFiles(slot: string, paths: readonly string[]): void {
  const cur = entry(slot)
  writeSideChatFiles(slot, unionFiles(cur.files, paths))
}

function unionFiles(cur: readonly string[], more: readonly string[]): string[] {
  return [...cur, ...more.filter((path, i) => !cur.includes(path) && more.indexOf(path) === i)]
}

/** Hand a payload the server never accepted back to the slot's draft: the
 *  TOKEN text (not the expanded lines) appended under the shared recovery rule,
 *  with its blocks carried in past whatever the slot holds now (`carryPastes`
 *  re-numbers a colliding one and rewrites its token). Restoring the expanded
 *  text instead would lose the tokens' identity: a paste whose content happens
 *  to contain a token-shaped string would then be re-claimed by the next
 *  paste's block on retry. */
export function restoreSideChatDraft(slot: string, text: string, pastes: PasteBlock[] = [], files: readonly string[] = []): void {
  const cur = entry(slot)
  const carried = carryPastes(text, pastes, cur.pastes)
  // The recovery merge, not the plain append: a payload the composer already
  // holds whole (an undo put it back before the refusal landed) is not added.
  // Its attachments join as a set union, so a file staged again meanwhile is
  // not staged twice.
  const staged = unionFiles(cur.files, files)
  set(slot, { ...cur, text: mergeCarriedDraft(cur.text, carried), pastes: carried.pastes.length ? carried.pastes : NO_PASTES, files: staged.length ? staged : NO_FILES })
}

/** Select-to-Ask: append `selection` to the slot's draft as a blockquote and
 *  mark it as a seed. Works whether or not that slot's panel is mounted. */
export function seedSideChatDraft(slot: string, selection: string): void {
  const sel = selection.trim()
  if (!sel) return
  const cur = entry(slot)
  set(slot, { ...cur, text: quoteIntoDraft(cur.text, sel), seedTick: cur.seedTick + 1 })
}

/** The panel has acted on the pending seed (caret placed). Clears the seed
 *  mark so a later remount of the same slot — reopening the Side tab, a
 *  member switch and back — does not pull focus into the composer again. */
export function consumeSideChatSeed(slot: string): void {
  const cur = entry(slot)
  if (cur.seedTick === 0) return
  set(slot, { ...cur, seedTick: 0 })
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
