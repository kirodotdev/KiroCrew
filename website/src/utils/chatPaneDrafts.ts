/**
 * Per-slot composer drafts for ChatPane — the pane's instance of the repo's
 * slot-draft store (`createSlotDraftStore`), alongside `chatDrafts` (ChatPage
 * text) and `chatFileDrafts` (ChatPage attachments).
 *
 * Why a separate KEY rather than ChatPage's stores: ChatPage holds its draft
 * maps in memory and persists them wholesale on its own schedule, so a second
 * writer on the same key would be overwritten by ChatPage's next save (and
 * ChatPage would never see the pane's write until a reload). The pane instead
 * does read-modify-write against its own key on every access, so several
 * panes — split view — can share it safely.
 *
 * What it holds: the composer of every slot a pane is NOT currently showing.
 * A pane can be rebound to another slot without remounting (the Members page
 * switches `slotKey` on one instance), and a send's recovery can land after
 * that switch; both park here. The on-screen slot's composer is the live
 * React state — the store is authoritative only for off-screen slots, which
 * is why the pane writes on rebind and unmount rather than per keystroke.
 *
 * A parked draft is ONE value: text, attachment paths and the collapsed paste
 * blocks behind any `[ Paste #N · M lines ]` token in that text, stored
 * together under one key so they are written, evicted and read as a unit. The
 * token is meaningless without its block — text restored without blocks would
 * show a chip that expands to nothing and send the literal token string — so
 * no eviction, quota refusal or corruption can separate the two: a slot's
 * draft is either whole or gone. Blocks therefore have exactly the text's
 * lifetime here — never longer (no localStorage copy outliving the tab, unlike
 * the main chat's `chatPasteDrafts`, whose text draft DOES survive a reload).
 *
 * Storage: sessionStorage, with a small byte cap. The pane's parking is a
 * within-session hand-off (a rebind, a page change), so it does not need to
 * outlive the tab; keeping it out of localStorage means ChatPage's two 2 MiB
 * stores can never crowd a parked pane draft out of a shared quota. Every
 * entry is ALSO mirrored in memory: reads prefer the mirror (write-through,
 * so within this tab it is the latest write even when storage refused it) and
 * fall back to storage, which is what a reloaded tab has.
 */
import { createSlotDraftStore } from './slotDraftStore'
import { DRAFT_MAX_ENTRIES } from './draftConstants'
import { sanitizePasteBlocks } from './chatPasteDrafts'
import { carryPastes, mergeCarriedDraft, type PasteBlock } from './pasteTokens'

export const PANE_DRAFTS_KEY = 'mc-pane-drafts'
/** The key attachment paths were parked under while text and paths were two
 *  stores; read once to fold its entries into the unified draft, then removed. */
export const LEGACY_PANE_FILE_DRAFTS_KEY = 'mc-pane-file-drafts'
/** Byte budget for parked pane drafts — a fraction of ChatPage's 2 MiB: parked
 *  drafts are a hand-off, not an archive, and the in-memory mirror covers the
 *  eviction / persist-failure cases for the live session. A paste block IS the
 *  text its token stands in for, so a parked paste costs what the same paste
 *  typed out would; the oldest slots are evicted whole until the blob fits. */
export const PANE_DRAFTS_MAX_BYTES = 256 * 1024

export interface PaneDraft {
  text: string
  files: string[]
  /** Blocks behind the `[ Paste #N · M lines ]` tokens in `text`. */
  pastes: PasteBlock[]
}

/** Corruption guard, emptiness predicate and defensive copier for one parked
 *  draft. Anything that is not an object with a string `text` is dropped; a
 *  non-array `files` / `pastes` reads as none; an invalid block is dropped by
 *  the shared block sanitizer. A draft with nothing in it is `null`, which the
 *  store treats as "delete the slot". */
function sanitizeDraft(v: unknown): PaneDraft | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.text !== 'string') return null
  const files = Array.isArray(d.files) ? d.files.filter((x): x is string => typeof x === 'string') : []
  const pastes = sanitizePasteBlocks(d.pastes) ?? []
  if (!d.text && !files.length && !pastes.length) return null
  return { text: d.text, files, pastes }
}

const store = createSlotDraftStore<PaneDraft>({
  key: PANE_DRAFTS_KEY,
  storage: 'session',
  maxEntries: DRAFT_MAX_ENTRIES,
  maxStoreBytes: PANE_DRAFTS_MAX_BYTES,
  sanitize: sanitizeDraft,
})

const EMPTY: PaneDraft = { text: '', files: [], pastes: [] }

/** Fold drafts parked by the two-store layout — a bare text string per slot
 *  under PANE_DRAFTS_KEY, a path list per slot under the legacy file key —
 *  into the unified shape. sessionStorage outlives a reload of the same tab,
 *  so a tab that parked a draft, then reloaded into this code, must still get
 *  it back rather than find the sanitizer dropping the string. Runs once per
 *  module load, before the first read; a slot that already has a unified
 *  entry keeps it. Folded drafts also go into the mirror, so this tab hands
 *  them back even if the unified write is refused.
 *
 *  The fold is DONE only once the unified blob is on disk: `legacyFolded`
 *  latches, and the legacy path key is removed, only after the write is seen
 *  to have stuck. A refused write leaves both legacy blobs untouched and the
 *  latch open, so the next access folds again instead of a later park
 *  overwriting the still-legacy text blob with a unified one that never held
 *  those drafts. */
let legacyFolded = false
/** Slots this tab has written since load. A fold that runs (or re-runs, after
 *  a refused write) must not put a legacy copy back under a slot the tab has
 *  since parked over or consumed with `takePaneDraft` — the legacy blob can
 *  outlive that write only while the unified one keeps being refused. */
const writtenSinceLoad = new Set<string>()

/** A stored `Record<slot, value>` blob, or `{}` when absent or unreadable. */
function parseSlotMap(raw: string | null): Record<string, unknown> {
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {}
  } catch { return {} }
}

function foldLegacyDrafts(): void {
  if (legacyFolded) return
  try {
    const rawTexts = sessionStorage.getItem(PANE_DRAFTS_KEY)
    const rawFiles = sessionStorage.getItem(LEGACY_PANE_FILE_DRAFTS_KEY)
    if (!rawTexts && !rawFiles) { legacyFolded = true; return }
    // Each blob parses on its own: a malformed path blob must not cost the
    // text drafts beside it (or the other way round) — only the unreadable
    // one reads as empty.
    const texts = parseSlotMap(rawTexts)
    const files = parseSlotMap(rawFiles)
    // Nothing legacy on disk (the text blob is already unified, no path blob):
    // there is nothing to fold and nothing to retire.
    if (rawFiles === null && !Object.values(texts).some((v) => typeof v === 'string')) { legacyFolded = true; return }
    const drafts = store.load()
    const slots = new Set([...Object.keys(texts), ...Object.keys(files)])
    for (const slot of slots) {
      if (slot in drafts || writtenSinceLoad.has(slot)) continue
      const text = typeof texts[slot] === 'string' ? texts[slot] as string : ''
      const paths = Array.isArray(files[slot]) ? (files[slot] as unknown[]).filter((x): x is string => typeof x === 'string') : []
      if (!text && !paths.length) continue
      const draft: PaneDraft = { text, files: paths, pastes: [] }
      store.set(drafts, slot, draft)
      if (!mirror.has(slot)) mirror.set(slot, copy(draft))
    }
    // This tab's parked drafts ride along (a slot parked over its legacy copy,
    // or refused earlier), so the blob that replaces the legacy one is whole.
    carryTabState(drafts)
    // Written even when nothing was folded (every legacy slot already parked
    // over or taken): the legacy blobs are retired ONLY by a unified write
    // that is seen to stick, never by the fold deciding it has nothing to add.
    store.save(drafts)
    // `save` caps `drafts` in place and writes exactly its serialization, so
    // the write stuck iff the blob on disk is that serialization. A slot the
    // cap evicted is gone from both — that is the byte budget, not a failure —
    // and this tab still has it from the mirror. Only a stuck write closes the
    // fold; a refused one leaves the legacy blobs as the copy on disk.
    const stuck = sessionStorage.getItem(PANE_DRAFTS_KEY) === JSON.stringify(drafts)
    if (!stuck) return
    legacyFolded = true
    if (rawFiles) sessionStorage.removeItem(LEGACY_PANE_FILE_DRAFTS_KEY)
  } catch { legacyFolded = true /* unreadable legacy blob: nothing to fold */ }
}

/** In-memory mirror of every parked draft: the fallback when storage refused
 *  the write (quota, disabled) or evicted the entry. Lives as long as the tab. */
const mirror = new Map<string, PaneDraft>()

/** Make `drafts` (a freshly loaded store map about to be saved) agree with
 *  this tab's own state before the write: every mirrored draft OVERWRITES the
 *  stored one — a write storage refused earlier may have left a stale entry
 *  on disk, and the mirror is the newer truth — and every slot the tab has
 *  cleared since load is deleted, so a stale entry does not come back on
 *  reload as if the recovery or the take never happened. */
function carryTabState(drafts: Record<string, PaneDraft>): void {
  for (const [s, d] of mirror) store.set(drafts, s, d)
  for (const s of writtenSinceLoad) if (!mirror.has(s)) delete drafts[s]
}

function copy(d: PaneDraft): PaneDraft {
  return { text: d.text, files: d.files.slice(), pastes: d.pastes.slice() }
}

/** The parked composer for `slot`, or empty. The mirror first — it is
 *  write-through, so within this tab it is always the latest write even when
 *  storage refused it — then storage, which is what a reloaded tab has. */
export function readPaneDraft(slot: string): PaneDraft {
  foldLegacyDrafts()
  const m = mirror.get(slot)
  if (m) return copy(m)
  return copy(store.load()[slot] ?? EMPTY)
}

/** Read AND clear `slot`'s parked composer — for a pane that is about to show
 *  it live. Once live, the composer is the single copy; leaving the store's
 *  entry in place would let a later park overwrite what arrived in between. */
export function takePaneDraft(slot: string): PaneDraft {
  const draft = readPaneDraft(slot)
  if (draft.text || draft.files.length || draft.pastes.length) writePaneDraft(slot, EMPTY)
  return draft
}

/** Park `slot`'s composer verbatim, as one unit. An empty draft deletes the
 *  entry. */
export function writePaneDraft(slot: string, draft: PaneDraft): void {
  // Fold FIRST: the fold may put a legacy copy of this very slot into the
  // mirror, and this write — a park or a take's clear — must be the last word.
  foldLegacyDrafts()
  writtenSinceLoad.add(slot)
  // Delete-then-set: the mirror's insertion order is the recency the store's
  // byte cap evicts by (carryTabState replays it oldest-first), so a slot
  // written again must move to the newest position, not keep its old one and
  // be the first casualty of the next over-budget park.
  mirror.delete(slot)
  if (draft.text || draft.files.length || draft.pastes.length) mirror.set(slot, copy(draft))
  const drafts = store.load()
  carryTabState(drafts)
  store.set(drafts, slot, draft)
  store.save(drafts)
}

/** Panes currently SHOWING a slot, so a late arrival for that slot can be
 *  handed to the live composer instead of sitting in the store until a park
 *  from that very pane overwrites it. Module-level: the arrival comes from a
 *  closure of a pane instance that may be long gone (unmounted, remounted). */
const listeners = new Map<string, Set<() => void>>()

/** Be told when something merges into `slot`'s parked draft while the caller
 *  shows that slot. The callback should `takePaneDraft` and merge. */
export function subscribePaneDraft(slot: string, onArrival: () => void): () => void {
  let subs = listeners.get(slot)
  if (!subs) { subs = new Set(); listeners.set(slot, subs) }
  subs.add(onArrival)
  return () => {
    subs!.delete(onArrival)
    if (subs!.size === 0) listeners.delete(slot)
  }
}

/** Merge a late recovery (or a late upload) into `slot`'s parked composer:
 *  text appends under the shared recovery rule, paths union, paste blocks
 *  carry over with their tokens re-numbered past the parked ones
 *  (`carryPastes`). Any pane showing the slot is notified so it can take the
 *  merge into its live composer. */
export function mergePaneDraft(slot: string, text: string, files: string[], pastes: PasteBlock[] = []): void {
  const cur = readPaneDraft(slot)
  const carried = carryPastes(text, pastes, cur.pastes)
  writePaneDraft(slot, {
    text: text ? mergeCarriedDraft(cur.text, carried) : cur.text,
    files: [...cur.files, ...files.filter((f) => !cur.files.includes(f))],
    pastes: carried.pastes,
  })
  const subs = listeners.get(slot)
  if (subs) for (const fn of Array.from(subs)) fn()
}

/** Test-only: drop the in-memory mirror and every subscriber. The mirror is
 *  read BEFORE storage by design, so `sessionStorage.clear()` between tests
 *  does not reset it — a pane unmounted at the end of one test parks its
 *  composer here and the next test's rebind to that slot would take it. */
export function __resetPaneDraftsForTests(): void {
  mirror.clear()
  listeners.clear()
  // The store is the fallback read when the mirror is empty, so a reset that
  // left the persisted entries behind would hand the previous test's park
  // straight back on the next mount. Best-effort: a suite that stubs storage
  // to throw is exercising exactly that refusal.
  try {
    sessionStorage.removeItem(PANE_DRAFTS_KEY)
    sessionStorage.removeItem(LEGACY_PANE_FILE_DRAFTS_KEY)
  } catch { /* storage unavailable or stubbed to refuse */ }
  legacyFolded = false
  writtenSinceLoad.clear()
}
