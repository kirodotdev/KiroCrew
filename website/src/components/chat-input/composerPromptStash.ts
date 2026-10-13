import { useCallback, type MutableRefObject } from 'react'
import { usePromptStash, type PromptStashSendResult, type PromptStashState } from '../../hooks/usePromptStash'
import type { PasteBlock } from '../../utils/pasteTokens'
import { isTouchDevice } from '../../utils/isTouchDevice'
import { isMac } from '../../utils/platform'

/** The composer's prompt-stash wiring: the chord label, the undo reseed, the
 *  hook itself and where its rows mount. ChatInput is the only caller. */
export function useComposerPromptStash({
  enabled, slotId, value, blocks, onChange, onBlocksChange, reseedUndoHistory, inert, attachmentCount,
  rowsInOverflow, menuGlyph, trackSendRef,
}: {
  enabled: boolean
  slotId: string | null | undefined
  value: string
  blocks: PasteBlock[]
  onChange: (value: string) => void
  onBlocksChange?: (blocks: PasteBlock[]) => void
  reseedUndoHistory: (text: string, blocks: PasteBlock[]) => void
  /** Live dictation rebuilds the text from a snapshot taken when capture
   *  began, so a stash or restore mid-dictation would be overwritten, and a
   *  stash under it would bring back paste tokens whose blocks were cleared.
   *  Both actions wait until dictation settles, and while the composer is
   *  disabled or optimizing. */
  inert: boolean
  /** Attachments are not stashed (follow-up under #14766); any pending makes
   *  both actions refuse rather than restore text over files or leave them
   *  behind. Folder references are `@dir/` tokens in the text, so they travel
   *  with it. A staged quote, an upload still in flight and a selected
   *  knowledge chip count too: the stash keeps none of them, so stashing would
   *  leave them for the next send and restoring would not bring them back. */
  attachmentCount: number
  /** The rows live in the ⋯ overflow instead of "+" (a bare file picker on the
   *  collapsible touch host, or no "+" at all). */
  rowsInOverflow: boolean
  menuGlyph: string
  trackSendRef: MutableRefObject<((send: () => PromptStashSendResult) => void) | null>
}): {
  promptStash: PromptStashState
  promptStashChord: string
  stashTouchHost: boolean
  stashInOverflow: boolean
} {
  // From the platform, not the `isMac` prop: only one host passes that prop,
  // and the chord label must be right in split panes and the side chat too.
  const promptStashChord = isMac ? '⌘S' : 'Ctrl+S'
  // The stash's composer writes (clear on stash, restore, swap) are programmatic
  // replacements of text whose only other copy is the stash entry just written
  // or deleted, so they reseed the undo history instead of recording a step.
  // Both composers use this host history: Lexical routes UNDO_COMMAND to it
  // through `onHistoryStep`, so the reseed must run for Lexical too.
  const onReseed = useCallback((text: string, b: PasteBlock[]) => {
    reseedUndoHistory(text, b)
  }, [reseedUndoHistory])
  const promptStash = usePromptStash({
    slotKey: enabled ? slotId : null, value, blocks, onChange, onBlocksChange, onReseed,
    inert, chordLabel: promptStashChord, attachmentCount,
    // Copy follows the input method: a keyboard user in a narrow window
    // still has the chord, even though the rows mount in the overflow.
    touch: isTouchDevice(),
    menuGlyph,
  })
  trackSendRef.current = promptStash.trackSend
  // The stash rows need a pointer/touch host wherever there is a session to
  // stash for. On touch that host is the overflow behind `composer-more-trigger`,
  // which `stashInOverflow` mounts (see ChatInput's bottom icon row).
  const stashTouchHost = enabled && !!slotId
  return { promptStash, promptStashChord, stashTouchHost, stashInOverflow: stashTouchHost && rowsInOverflow }
}
