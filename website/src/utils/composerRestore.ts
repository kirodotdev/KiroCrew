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

/** Put `text` into `slot`'s composer and report whether that composer is visible. */
export function restoreToComposer(slot: string, text: string): boolean {
  if (!text.trim()) return false
  for (const target of mainComposers) if (target(slot, text, true)) return true
  if (hasPaneListener(slot)) { mergePaneDraft(slot, text, []); return true }
  for (const target of mainComposers) if (target(slot, text, false)) return false
  const drafts = loadDrafts()
  setDraft(drafts, slot, mergeIntoDraft(drafts[slot], text))
  saveDrafts(drafts)
  return false
}
