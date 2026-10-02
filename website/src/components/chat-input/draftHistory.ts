import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { PasteBlock } from '../../utils/pasteTokens'
import type { ComposerControl } from '../composerControl'
import { livePromptHistoryCursor, stepPromptHistory, type PromptHistoryCursor, type PromptHistoryItem } from '../composerPromptHistory'

/* The draft's own history: ↑/↓ recall of the prompts this slot sent, and the
   explicit undo/redo snapshots a controlled editor needs because every
   programmatic value reset wipes the browser's native undo stack. */
// Prompt undo/redo tuning. The chat textarea is a controlled component, so any
// programmatic value reset (send-clear, ↑/↓ history recall, prompt optimize)
// wipes the browser's native undo stack. We keep an explicit snapshot history
// so Ctrl/Cmd+Z can always restore prior text — including after an accidental
// full erase.
const UNDO_COALESCE_MS = 400 // merge keystrokes within this window into one undo step
const UNDO_BULK_DELTA = 8 // an insert/delete of >= this many chars is its own boundary
const UNDO_MAX_HISTORY = 200 // cap snapshots to bound memory

// `blocks` rides with each snapshot so undo/redo restores the paste content
// backing any `[ Paste #N ]` token in `value` — deleting or expanding a token
// drops its PasteBlock, and without this an undo would resurrect the token text
// as a dead literal with no recoverable content.
type UndoSnap = { value: string; selStart: number; selEnd: number; blocks: PasteBlock[] }

/** True when two block lists hold the same blocks by id (order-independent).
 *  Lets undo/redo skip a redundant onPasteBlocksChange when the paste set is
 *  unchanged (e.g. plain-text undo, where both sides are empty). */
function sameBlocks(a: PasteBlock[], b: PasteBlock[]): boolean {
  if (a === b) return true
  if (a.length !== b.length) return false
  const ids = new Set(a.map(x => x.id))
  return b.every(x => ids.has(x.id))
}

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
    // After recall, place the caret where the next arrow press will re-engage
    // history immediately (↑ → start, ↓ → end). Deferred to next frame so the
    // controlled textarea has re-rendered with the new value first.
    const moveCaretAfterRecall = (pos: 'start' | 'end') => {
      requestAnimationFrame(() => {
        const el = inputRef.current
        if (!el) return
        const p = pos === 'start' ? 0 : el.value.length
        el.setSelectionRange(p, p)
      })
    }
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    const cursor = livePromptHistoryCursor(historyCursorRef.current, cur)
    historyCursorRef.current = cursor
    if (e.key === 'ArrowUp') {
      // Only intercept when input is empty OR caret is collapsed at position 0.
      const atStart = ta.selectionStart === 0 && ta.selectionEnd === 0
      if (!atStart && cur !== '') return
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
      moveCaretAfterRecall(e.key === 'ArrowUp' ? 'start' : 'end')
    }
    e.preventDefault()
  }, [])
  return useMemo(() => ({ exitIfDiverged, endBrowsing, recall }), [exitIfDiverged, endBrowsing, recall])
}

/** Per-slot undo/redo over the controlled value. `valueFromUserRef` says
 *  whether the change being recorded came from a real DOM edit rather than a
 *  parent-driven draft restore; `optimizingRef` pauses recording while the
 *  optimizer owns the draft. */
export function useUndoHistory({ value, pasteBlocks, autoFocusKey, composerControl, pasteBlocksRef, valueFromUserRef, optimizingRef, onChange, onPasteBlocksChange, onRemoveFile, onRemoveDir, inputRef, ime }: {
  value: string
  pasteBlocks: PasteBlock[]
  autoFocusKey?: string | null
  composerControl: () => ComposerControl | null
  pasteBlocksRef: React.MutableRefObject<PasteBlock[]>
  valueFromUserRef: React.MutableRefObject<boolean>
  optimizingRef: React.MutableRefObject<boolean>
  onChange: (v: string) => void
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  onRemoveFile?: (path: string) => void
  onRemoveDir?: (path: string) => void
  inputRef: React.RefObject<HTMLTextAreaElement>
  ime: ReturnType<typeof useImeGuard>
}) {
  // --- Prompt undo/redo history (per slot) ---
  // Explicit snapshot stack: undoHistoryRef[undoPointerRef] always mirrors the
  // live value. Rapid keystrokes coalesce into one entry; bulk deletes and
  // programmatic resets become their own restorable boundary. applyingUndoRef
  // suppresses re-recording the value we set during an undo/redo.
  const undoHistoryRef = useRef<UndoSnap[]>([{ value, selStart: value.length, selEnd: value.length, blocks: pasteBlocks }])
  const undoPointerRef = useRef(0)
  const undoLastEditRef = useRef(0)
  const applyingUndoRef = useRef(false)
  const prevUndoAfkRef = useRef(autoFocusKey)
  const slotSettlingRef = useRef(false)

  // Record undo snapshots as the controlled value changes.
  useEffect(() => {
    const selection = composerControl()?.getSelection()
    // Consume the "this change came from a DOM edit" flag exactly once per run.
    const fromUser = valueFromUserRef.current
    valueFromUserRef.current = false
    const seed = () => {
      undoHistoryRef.current = [{
        value,
        selStart: selection?.start ?? value.length,
        selEnd: selection?.end ?? value.length,
        blocks: pasteBlocksRef.current,
      }]
      undoPointerRef.current = 0
      undoLastEditRef.current = 0
    }
    // Skip the change we just made via undo/redo — the pointer is already
    // correct. Keep slot tracking in sync so a coincident switch can't trigger
    // a spurious reset on a later pass.
    if (applyingUndoRef.current) {
      applyingUndoRef.current = false
      prevUndoAfkRef.current = autoFocusKey
      return
    }
    // Slot/session switch. ChatPage restores a slot's draft via the
    // `[activeSlot]` effect in ChatPage.tsx, which calls `setInput` in a
    // *separate* commit after `activeSlot` (`autoFocusKey`) changes — so on this
    // pass `value` may still be the previous slot's text. Reseed now and mark
    // the next value change as "settling" so the draft restore reseeds the base
    // rather than being recorded as an undoable transition from the prior slot's
    // stale text — otherwise Ctrl+Z in the new slot would restore the old draft.
    if (autoFocusKey !== prevUndoAfkRef.current) {
      prevUndoAfkRef.current = autoFocusKey
      seed()
      slotSettlingRef.current = true
      return
    }
    if (slotSettlingRef.current) {
      slotSettlingRef.current = false
      // The first value change after a switch. A parent-driven prop change is
      // the draft restore (reseed the base at it). A real DOM edit means the
      // user typed before/without a separate restore commit — i.e. ChatPage
      // restored synchronously, the base was already seeded at the switch — so
      // fall through and record the keystroke as a normal edit instead of
      // folding it into the base. Keeps undo correct for sync and async restore.
      if (!fromUser) {
        if (undoHistoryRef.current[undoPointerRef.current]?.value !== value) seed()
        return
      }
    }
    // While the optimizer owns the textarea, skip per-keystroke recording. A
    // single-shot optimize (one execCommand) lands after `optimizing` clears and
    // records normally; a streaming optimize is captured as one boundary by the
    // optimizer's completion effect (`appendBoundary`). Either way one Ctrl+Z
    // reverses a whole optimize.
    if (optimizingRef.current) return
    const hist = undoHistoryRef.current
    const ptr = undoPointerRef.current
    const prev = hist[ptr]?.value
    if (prev === value) return // selection-only re-render, no text change
    const snap: UndoSnap = {
      value,
      selStart: selection?.start ?? value.length,
      selEnd: selection?.end ?? value.length,
      blocks: pasteBlocksRef.current,
    }
    const now = Date.now()
    // Coalesce only small, incremental, recent edits at the tip of the history.
    // A bulk change (clear, recall, optimize, select-all-delete) or a pause
    // starts a new boundary so it can be undone on its own. The `prev !== ''`
    // guard also makes the first char typed from empty its own boundary.
    // One exception to the timing rule: a file/folder chip remove clears
    // undoLastEditRef before calling the parent (see removeFileEndingUndoBurst),
    // so a short mention strip right after a keystroke is never merged into the
    // typing burst that holds the pre-remove text.
    const incremental =
      prev !== undefined && prev !== '' && value !== '' &&
      Math.abs(value.length - prev.length) < UNDO_BULK_DELTA
    const recent = now - undoLastEditRef.current < UNDO_COALESCE_MS
    const atTip = ptr === hist.length - 1
    if (atTip && incremental && recent) {
      hist[ptr] = snap // merge typing burst into the current entry
    } else {
      hist.splice(ptr + 1) // editing discards any redo branch
      hist.push(snap)
      if (hist.length > UNDO_MAX_HISTORY) hist.shift()
      undoPointerRef.current = hist.length - 1
    }
    undoLastEditRef.current = now
  }, [value, autoFocusKey, composerControl, pasteBlocksRef, valueFromUserRef, optimizingRef])

  // A chip remove strips the chip's mention from `value` in the parent. Ending
  // the burst first gives that change its own undo entry, so Ctrl/Cmd+Z brings
  // the mention (and through it the chip) back instead of skipping past it.
  const removeFileEndingUndoBurst = useMemo(() => onRemoveFile && ((path: string) => {
    undoLastEditRef.current = 0
    onRemoveFile(path)
  }), [onRemoveFile])
  const removeDirEndingUndoBurst = useMemo(() => onRemoveDir && ((path: string) => {
    undoLastEditRef.current = 0
    onRemoveDir(path)
  }), [onRemoveDir])

  /** Walk the explicit snapshot history one step. Shared by the textarea's
   *  keydown and the Lexical composer's UNDO/REDO commands: both composers are
   *  controlled, so a programmatic value reset (send-clear, ↑/↓ recall,
   *  optimize) wipes the editor's own history and only this one survives it.
   *  Returns false when there is nothing to undo/redo. */
  const stepUndoHistory = useCallback((direction: 'undo' | 'redo'): boolean => {
    if (optimizingRef.current) return false
    const hist = undoHistoryRef.current
    let ptr = undoPointerRef.current
    if (direction === 'undo' && ptr > 0) ptr -= 1
    else if (direction === 'redo' && ptr < hist.length - 1) ptr += 1
    else return false // nothing to undo/redo
    undoPointerRef.current = ptr
    const snap = hist[ptr]
    applyingUndoRef.current = true
    onChange(snap.value)
    // Restore the paste blocks captured in this snapshot so a `[ Paste #N ]`
    // token brought back by the undo has its backing content again. Only
    // emit when the set actually differs (identity or membership) to avoid a
    // redundant parent render on plain-text undo. The pruneBlocks effect
    // would otherwise strip a block whose token the undo just restored.
    if (onPasteBlocksChange && !sameBlocks(pasteBlocksRef.current, snap.blocks)) {
      onPasteBlocksChange(snap.blocks)
    }
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (el) {
        el.focus()
        el.setSelectionRange(snap.selStart, snap.selEnd)
        return
      }
      // No textarea mounted: the Lexical composer is the input.
      composerControl()?.setSelection(snap.selStart, snap.selEnd, { focus: true })
    })
    return true
  }, [optimizingRef, onChange, onPasteBlocksChange, pasteBlocksRef, inputRef, composerControl])

  /** Cmd/Ctrl+Z undoes, Cmd/Ctrl+Shift+Z or Ctrl+Y redoes. True when the key
   *  was one of those gestures, whether or not there was a step to take. */
  const handleUndoKey = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Undo / redo — drive the explicit per-slot history so Ctrl/Cmd+Z restores
    // text even after a programmatic reset (send-clear, ↑/↓ recall, optimize)
    // wiped the browser's native undo stack. We own the gesture and
    // preventDefault native undo so behaviour is deterministic regardless of how
    // `value` changed. Cmd/Ctrl+Z = undo, Cmd/Ctrl+Shift+Z or Ctrl+Y = redo.
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !ime.isComposing(e) && !optimizingRef.current) {
      const k = e.key.toLowerCase()
      const isUndo = k === 'z' && !e.shiftKey
      const isRedo = (k === 'z' && e.shiftKey) || k === 'y'
      if (isUndo || isRedo) {
        e.preventDefault()
        stepUndoHistory(isRedo ? 'redo' : 'undo')
        return true
      }
    }
    return false
  }, [ime, optimizingRef, stepUndoHistory])

  /** One undo boundary at `v` unless the tip already holds it: what makes a
   *  finished optimize a single restorable step. */
  const appendBoundary = useCallback((v: string) => {
    const hist = undoHistoryRef.current
    const ptr = undoPointerRef.current
    if (hist[ptr]?.value !== v) {
      const selection = composerControl()?.getSelection()
      hist.splice(ptr + 1)
      hist.push({ value: v, selStart: selection?.start ?? v.length, selEnd: selection?.end ?? v.length, blocks: pasteBlocksRef.current })
      if (hist.length > UNDO_MAX_HISTORY) hist.shift()
      undoPointerRef.current = hist.length - 1
      undoLastEditRef.current = Date.now()
    }
  }, [composerControl, pasteBlocksRef])

  const endUndoBurst = useCallback(() => { undoLastEditRef.current = 0 }, [])

  return { handleUndoKey, stepUndoHistory, appendBoundary, endUndoBurst, removeFileEndingUndoBurst, removeDirEndingUndoBurst }
}
