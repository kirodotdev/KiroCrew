import { useCallback, useMemo, useRef } from 'react'
import { livePromptHistoryCursor, stepPromptHistory, type PromptHistoryCursor, type PromptHistoryItem } from '../composerPromptHistory'

/* The draft's own prompt history: ↑/↓ recall of the prompts this slot sent.
   The explicit undo/redo snapshots live in chat-core
   (`chat-core/composer/undoHistory.ts`), shared with the app-SDK embed. */
/** ↑/↓ recall of the prompts this slot sent (oldest → newest). The keydown
 *  owner decides WHETHER a key may recall (no menu open, no IME, no
 *  modifier); this owns what recall does, and when history mode ends. */
export function usePromptHistory() {
  // Prompt history navigation: null = not browsing. The cursor names its entry
  // (see composerPromptHistory.ts) so it survives `sentMessages` changing
  // underneath it; a ref keeps it across re-renders between keystrokes.
  const historyCursorRef = useRef<PromptHistoryCursor | null>(null)
  /** Exit history mode when `value` diverges from the recalled message (the
   *  user edited it, or the send pipeline cleared it). */
  const exitIfDiverged = useCallback((value: string) => {
    historyCursorRef.current = livePromptHistoryCursor(historyCursorRef.current, value)
  }, [])
  /** Prompt-history browsing belongs to the slot it started in. */
  const endBrowsing = useCallback(() => {
    historyCursorRef.current = null
  }, [])
  const recall = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>, { sentMessages, current, onChange, inputRef }: {
    sentMessages: PromptHistoryItem[]
    current: string
    onChange: (v: string) => void
    inputRef: React.RefObject<HTMLTextAreaElement>
  }) => {
    const ta = e.currentTarget
    const cur = current
    // After recall, place the caret at the END of the recalled prompt, in
    // either direction: a recalled prompt is usually there to be edited or
    // re-sent, and every shell history (and the browser console) leaves the
    // caret after the text. The ↑ gate below is what keeps a further ↑ walking
    // to the next older entry from there. Deferred to next frame so the
    // controlled textarea has re-rendered with the new value first.
    const moveCaretAfterRecall = () => {
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (!el) return
        el.setSelectionRange(el.value.length, el.value.length)
      })
    }
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    const cursor = livePromptHistoryCursor(historyCursorRef.current, cur)
    historyCursorRef.current = cursor
    if (e.key === 'ArrowUp') {
      // Entering history: only intercept when the input is empty OR the caret
      // is collapsed at position 0, so ↑ never shadows caret movement through
      // the user's own typed draft.
      const collapsed = ta.selectionStart === ta.selectionEnd
      const atStart = collapsed && ta.selectionStart === 0
      // Already browsing: recall parks the caret at the END of the entry, and
      // the end is the position ↑/↓ step from. A caret the user moved INSIDE
      // the text keeps its native movement — this textarea is one auto-grown
      // row (`rows={1}`, `resize-none`), so a prompt with no newline in it
      // still wraps across several rows, and ↑ there must move up a row rather
      // than swap the prompt out from under the caret. Offset 0 keeps stepping
      // too: it is where recall used to park, and ↑ has nowhere above it to go.
      const atEnd = collapsed && ta.selectionStart === cur.length
      if (cur !== '' && !(atStart || (cursor && atEnd))) return
      // A held ↑ scrolling up through the user's own draft stops at its start;
      // a fresh press recalls, and a held ↑ keeps walking recalled prompts.
      if (e.repeat && !cursor && cur !== '') return
    } else {
      if (!cursor) return // not in history mode — let textarea handle
      // Only intercept when caret is at end (so multi-line edits still navigate within).
      const atEnd = ta.selectionStart === cur.length && ta.selectionEnd === cur.length
      if (!atEnd) return
    }
    const step = stepPromptHistory(sentMessages, cursor, e.key === 'ArrowUp' ? 'older' : 'newer', cur)
    if (!step) return
    historyCursorRef.current = step.cursor
    // ↑ on the oldest entry resolves to the text already shown: consume the
    // key so the caret does not jump, but leave the value alone.
    if (step.cursor === null || step.text !== cur || cursor === null) {
      onChange(step.text)
      moveCaretAfterRecall()
    }
    e.preventDefault()
  }, [])
  return useMemo(() => ({ exitIfDiverged, endBrowsing, recall }), [exitIfDiverged, endBrowsing, recall])
}
