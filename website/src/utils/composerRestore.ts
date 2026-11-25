import { loadDrafts, mergeIntoDraft, saveDrafts, setDraft } from './chatDrafts'
import { hasPaneListener, mergePaneDraft } from './chatPaneDrafts'

/** A mounted main composer: takes `text` for `slot` and says whether it did.
 *  With `showingOnly`, it takes it only when that slot is the one on screen. */
type MainComposer = (slot: string, text: string, showingOnly: boolean) => boolean

const mainComposers = new Set<MainComposer>()

/** Register a main chat composer as a destination for restored text. */
export function registerMainComposer(target: MainComposer): () => void {
  mainComposers.add(target)
  return () => { mainComposers.delete(target) }
}

/**
 * Where restored text landed: `shown` (a composer on screen for `slot` holds it),
 * `kept` (a mounted composer showing another slot, or the stored drafts, hold it
 * for `slot`), or `lost` (blank text, or the browser refused to store the draft).
 */
export type ComposerRestoreResult = 'shown' | 'kept' | 'lost'

/** Put `text` into `slot`'s composer and say where it landed. */
export function keepInComposer(slot: string, text: string): ComposerRestoreResult {
  if (!text.trim()) return 'lost'
  for (const target of mainComposers) if (target(slot, text, true)) return 'shown'
  if (hasPaneListener(slot)) { mergePaneDraft(slot, text, []); return 'shown' }
  for (const target of mainComposers) if (target(slot, text, false)) return 'kept'
  const drafts = loadDrafts()
  setDraft(drafts, slot, mergeIntoDraft(drafts[slot], text))
  return saveDrafts(drafts) ? 'kept' : 'lost'
}

/** Put `text` into `slot`'s composer and report whether that composer is visible. */
export function restoreToComposer(slot: string, text: string): boolean {
  return keepInComposer(slot, text) === 'shown'
}
