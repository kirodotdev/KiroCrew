import { useCallback, useMemo, useRef } from 'react'
import { livePromptHistoryCursor, promptHistoryKeyEngages, stepPromptHistory, type PromptHistoryCursor, type PromptHistoryItem } from '../composerPromptHistory'

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
    // A recalled prompt leaves the caret at its end, as readline does; the
    // first-line ↑ gate lets the next ↑ keep walking older from there.
    // Deferred to next frame so the controlled textarea has re-rendered with
    // the new value first.
    const moveCaretToEndAfterRecall = () => {
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (!el) return
        const p = el.value.length
        el.setSelectionRange(p, p)
      })
    }
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    const cursor = livePromptHistoryCursor(historyCursorRef.current, cur)
    historyCursorRef.current = cursor
    const direction = e.key === 'ArrowUp' ? 'older' : 'newer'
    // A selection is never a history step: the key collapses it natively.
    if (ta.selectionStart !== ta.selectionEnd) return
    if (!promptHistoryKeyEngages(direction, cur, ta.selectionStart, cursor !== null)) return
    // A held ↑ scrolling up through the user's own draft stops at its start;
    // a fresh press recalls, and a held ↑ keeps walking recalled prompts.
    if (direction === 'older' && e.repeat && !cursor && cur !== '') return
    const step = stepPromptHistory(sentMessages, cursor, direction, cur)
    if (!step) return
    historyCursorRef.current = step.cursor
    // ↑ on the oldest entry resolves to the text already shown: consume the
    // key so the caret does not jump, but leave the value alone.
    if (step.cursor === null || step.text !== cur || cursor === null) {
      onChange(step.text)
      moveCaretToEndAfterRecall()
    }
    e.preventDefault()
  }, [])
  return useMemo(() => ({ exitIfDiverged, endBrowsing, recall }), [exitIfDiverged, endBrowsing, recall])
}
