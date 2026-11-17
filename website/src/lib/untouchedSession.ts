import type { RootState } from '../store'
import { draftsSaveFailed, loadDrafts } from '../utils/chatDrafts'
import { fileDraftsSaveFailed, loadFileDrafts } from '../utils/chatFileDrafts'
import { loadSessionRefDrafts, sessionRefDraftsSaveFailed } from '../utils/chatSessionRefDrafts'
import { isComposerSendHeld } from '../utils/composerSendHolds'
import { closeBusyReasons } from './closeBusyGate'

/**
 * True while the session holds nothing the user wrote and no live work: no
 * transcript row, nothing a sidebar close would ask about (a turn, a queue, an
 * approval, sub-agents, a workflow, a goal loop or monitor), and no unsent
 * composer text, staged file or session reference. Files and session references
 * are stored apart from the text. It reads the stored drafts, so it answers false
 * while the last write of any of them failed.
 *
 * `seededDraft` is text the host itself put in the composer when it created the
 * session (an app launch's message). The draft must still read exactly that, up
 * to surrounding whitespace: a cleared composer is an edit too.
 *
 * A surface that creates a session before the user types (the artifact companion
 * chat, an app's draft chat launch) reads this to decide whether the session it
 * leaves behind is worth keeping. The server re-checks on the discard itself.
 */
export function isUntouchedSession(state: RootState, slotKey: string, seededDraft = ''): boolean {
  const slot = state.dashboard.slots.find((x) => x.key === slotKey)
  if (!slot || slot.messages > 0 || closeBusyReasons(state, slotKey).length > 0) return false
  // A send shows here as an optimistic row before the slots snapshot counts it.
  const rows = state.chat.activeSlot === slotKey ? state.chat.messages : state.chat.slotMessages[slotKey]
  if (rows?.length) return false
  // The composer keeps what it holds in memory and writes it to storage, which
  // is what this reads. A write that did not stick (storage full or disabled)
  // leaves storage older than the composer, so it cannot show the session empty.
  if (draftsSaveFailed() || fileDraftsSaveFailed() || sessionRefDraftsSaveFailed()) return false
  if (loadFileDrafts()[slotKey]?.length || loadSessionRefDrafts()[slotKey]?.length) return false
  // An upload still in flight has staged no path yet, but it holds the composer.
  if (isComposerSendHeld(slotKey)) return false
  const draft = loadDrafts()[slotKey]?.trim() ?? ''
  return draft === seededDraft.trim()
}
