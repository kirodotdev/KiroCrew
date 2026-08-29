/**
 * Which mounted composers hold unsent work for a given slot?
 *
 * The close option action tears a slot down, and its gate used to read ONE
 * composer: the host that owned the click. That is a different question from "is
 * it safe to close this slot", because a slot can be displayed by more than one
 * mounted host and each keeps its draft in its OWN `useState` — so a draft typed
 * in pane B is invisible to the gate running in pane A, and the delete strands it.
 *
 * Nothing in the pane tree forbids that: `useSessionGrid.fillLeaf` applies a slot
 * with no duplicate check, and only a `.filter()` over `occupiedSlots` in the
 * placeholder picker's render keeps one slot out of two panes. A UI-side filter is
 * not an invariant, so the gate must not depend on it.
 */

import { loadDrafts } from './chatDrafts'
import { loadFileDrafts } from './chatFileDrafts'
import { loadPasteDrafts } from './chatPasteDrafts'
import { loadSessionRefDrafts } from './chatSessionRefDrafts'
import { loadSideDrafts } from './sideComposerDrafts'
import { anyWindowSlotDirty } from './slotDirtyBeacon'

// A slot GETTER, not a slot string: a host's slot changes while it stays mounted, so a
// registry keyed on the value at mount time would answer for the wrong slot.
interface ComposerEntry {
  getSlot: () => string | null
  hasWork: () => boolean
}

const entries = new Map<string, ComposerEntry>()

let nextId = 0

/**
 * A per-WINDOW prefix, so ids are unique in the cross-window namespace.
 *
 * The counter alone is only process-unique, and the beacon it feeds is shared
 * `localStorage`: two windows each minted `composer-1`, so one window publishing a clean
 * claim retracted the other's live one and a close then deleted a slot whose draft had
 * not yet been persisted. `randomUUID` needs a secure context, so the fallback keeps the
 * id unique wherever it is missing rather than silently reusing a colliding one.
 */
const WINDOW_TAG: string = (() => {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    /* fall through to the non-crypto path */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
})()

/** An id unique across every same-origin window, for one mounted composer. */
export function nextComposerId(): string {
  nextId += 1
  return `composer-${WINDOW_TAG}-${nextId}`
}

/** Register a mounted composer; returns the deregister function. */
export function registerSlotComposer(id: string, entry: ComposerEntry): () => void {
  entries.set(id, entry)
  return () => {
    entries.delete(id)
  }
}

/**
 * Does ANY composer bound to *slot* hold unsent work?
 *
 * Includes the caller's own registration, so a host does not need to check its
 * own composer separately — one question, one answer, for every host at once.
 *
 * The PERSISTED drafts are consulted as well, always, because a registry-only
 * answer made this guard half-true: it warned on the surface the user was looking
 * at and destroyed the draft silently everywhere else — a background tab, or a
 * popout window. That teaches the user the warning is reliable from the one case
 * that works, which is worse than not warning at all.
 */
/**
 * WHERE the unsent work on *slot* is, as precisely as the layers can actually say.
 *
 * `here` — a composer mounted in THIS window answers, so the draft is on this screen,
 * typically the side panel. `other-window` — no local composer holds work, but a live
 * cross-window claim does; a composer retracts its claim on unmount, so a claim this
 * window did not answer for is not this window's. `elsewhere` — only a persisted store
 * answers, which is an unmounted or frozen surface whose window cannot be identified.
 *
 * The third case is kept rather than folded into one of the others because it is
 * genuinely unknown, and a notice that sends the user to the wrong window is worse than
 * one that admits it does not know.
 */
export type UnsentWorkSource = 'here' | 'other-window' | 'elsewhere'

export function slotUnsentWorkSource(slot: string): UnsentWorkSource | null {
  if (slotHasUnsentWorkHere(slot)) return 'here'
  if (anyWindowSlotDirty(slot)) return 'other-window'
  return slotHasPersistedDraft(slot) ? 'elsewhere' : null
}

/**
 * Does a composer registered in THIS window hold unsent work for *slot*?
 *
 * Reads only the in-process registry, which is what makes it safe on a render path: the
 * cross-window tiers have to enumerate storage keys, and a render that does so on every
 * pass both costs a full scan per paint and reads as a storage side effect to callers
 * that have none.
 */
export function slotHasUnsentWorkHere(slot: string): boolean {
  for (const entry of entries.values()) {
    if (entry.getSlot() !== slot) continue
    if (entry.hasWork()) return true
  }
  return false
}

export function slotHasUnsentWork(slot: string): boolean {
  return slotUnsentWorkSource(slot) !== null
}

/**
 * Does any persisted store hold a draft for *slot*?
 *
 * Consulted AFTER the registry and the claim, and the reason a claim may expire at all: a
 * window frozen in the background stops refreshing, so the persisted copy is the only
 * record. The cost is the <=300ms DRAFT_SAVE_DEBOUNCE_MS window, where a just-emptied box
 * still reads dirty; that refusal is recoverable by clicking again, the loss is not.
 */
function slotHasPersistedDraft(slot: string): boolean {
  return Boolean(
    loadDrafts()[slot] ||
    loadSideDrafts()[slot]?.length ||
    loadFileDrafts()[slot]?.length ||
    loadPasteDrafts()[slot]?.length ||
    loadSessionRefDrafts()[slot]?.length,
  )
}
