import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ComposerBox } from './SelectionToolbar'
import { useAnchorRemeasure } from '../hooks/useAnchorRemeasure'
import { i18nT } from '../i18n/t'

/** Gap between the point and the box, and the minimum margin to the viewport edge. */
const GAP = 12
const EDGE = 8

/**
 * Where a box hung off a point goes: below the point, flipped above it when the
 * viewport runs out, and clamped horizontally so it never leaves the screen.
 * Measured before paint, so a box near an edge never flashes at the unclamped
 * spot. `tick` re-runs it when the box changes size without the point moving.
 */
function useAnchoredPosition(rootRef: React.RefObject<HTMLDivElement | null>, x: number, y: number, tick: unknown) {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  useLayoutEffect(() => {
    const el = rootRef.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(EDGE, Math.min(x - w / 2, window.innerWidth - w - EDGE))
    const below = y + GAP
    const top = below + h > window.innerHeight - EDGE ? Math.max(EDGE, y - GAP - h) : below
    setPos(prev => (prev && prev.left === left && prev.top === top ? prev : { left, top }))
  }, [rootRef, x, y, tick])
  // Hidden, not absent, for the one measuring pass: the box must lay out to be
  // measured, and must not flash at the unclamped spot.
  return pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: 'hidden' as const }
}

/**
 * The composer for a comment on a box drawn over an image. It renders the same
 * box a text selection opens (`ComposerBox`): the input, the labelled "Add
 * comment" button and the close control, so the two ways of commenting look and
 * behave alike. A box has no selected text, so the composer carries no Copy
 * action.
 *
 * It hangs below the point the drag ended at and flips above it when the
 * viewport runs out, clamped horizontally so it never leaves the screen. Typed
 * text is guarded the way the text composer guards it: a press outside closes
 * an empty box but never one holding text, a scroll of `scrollRef` moves the
 * box with the image instead of closing it, and Escape or the close button
 * over typed text asks `confirmDiscard` first. The text is mirrored to the host
 * through `onTextChange` and comes back as `initialText`, so a remount (the tab
 * suspended, a view switch and back) does not lose it.
 */
export function ImageCommentComposer({ x, y, onSubmit, onCancel, scrollRef, initialText = '', onTextChange, confirmDiscard }: {
  /** Viewport coordinates the composer hangs off. */
  x: number
  y: number
  onSubmit: (text: string) => void
  onCancel: () => void
  scrollRef?: React.RefObject<HTMLElement | null>
  /** The draft the host kept for this box, restored on mount. */
  initialText?: string
  /** Every change to the typed text, so the host can keep it and guard it. */
  onTextChange?: (text: string) => void
  /** Asked before Escape / the close button discards typed text; resolve `true`
   *  to discard. Omit to discard without asking. */
  confirmDiscard?: () => Promise<boolean>
}) {
  const [text, setText] = useState(initialText)
  const [growTick, setGrowTick] = useState(0)
  // How far `scrollRef` has scrolled since the box opened, so the box follows
  // the image it names rather than staying fixed over other content.
  const [scrolled, setScrolled] = useState({ dx: 0, dy: 0 })
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const hintIdBase = useId()
  const onCancelRef = useRef(onCancel)
  useEffect(() => { onCancelRef.current = onCancel }, [onCancel])
  const textRef = useRef(initialText)
  const onTextChangeRef = useRef(onTextChange)
  useEffect(() => { onTextChangeRef.current = onTextChange }, [onTextChange])
  const changeText = useCallback((next: string) => {
    textRef.current = next
    setText(next)
    onTextChangeRef.current?.(next)
  }, [])

  // Re-placed as the input grows, and as the image scrolls.
  const style = useAnchoredPosition(rootRef, x - scrolled.dx, y - scrolled.dy, growTick)

  // Glued to its image the way the composer's other popovers stay glued to
  // their triggers: on a resize (window, rotation, on-screen keyboard) it is
  // clamped again, and on a scroll it moves by how far `scrollRef` has
  // scrolled since the box opened.
  const scrollOrigin = useRef<{ left: number; top: number } | null>(null)
  useLayoutEffect(() => {
    const t = scrollRef?.current
    scrollOrigin.current = t ? { left: t.scrollLeft, top: t.scrollTop } : null
  }, [scrollRef])
  const remeasure = useCallback(() => {
    const t = scrollRef?.current
    const o = scrollOrigin.current
    if (t && o) {
      const dx = t.scrollLeft - o.left
      const dy = t.scrollTop - o.top
      setScrolled(prev => (prev.dx === dx && prev.dy === dy ? prev : { dx, dy }))
    }
    setGrowTick(n => n + 1)
  }, [scrollRef])
  useAnchorRemeasure(true, remeasure)

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current || rootRef.current.contains(e.target as Node)) return
      // A typed comment is not discarded by a stray click: the box stays until
      // it is submitted or closed deliberately (Escape, the close button).
      if (textRef.current.trim()) return
      onCancelRef.current()
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  // Escape and the close button: a deliberate close, confirmed over typed text.
  const requestCancel = useCallback(async () => {
    if (textRef.current.trim() && confirmDiscard && !(await confirmDiscard())) {
      inputRef.current?.focus()
      return
    }
    onCancelRef.current()
  }, [confirmDiscard])

  const submit = useCallback((comment: string) => {
    const trimmed = comment.trim()
    if (trimmed) onSubmit(trimmed)
  }, [onSubmit])
  const onGrow = useCallback(() => setGrowTick(t => t + 1), [])

  return (
    <div
      ref={rootRef}
      data-image-comment-composer=""
      className="fixed z-50 pointer-events-auto"
      style={style}
    >
      <ComposerBox
        inputRef={inputRef}
        autoFocus
        actions={[]}
        copiedId={null}
        hintIdBase={hintIdBase}
        onAction={() => {}}
        onSubmit={submit}
        onEscape={requestCancel}
        // Nothing selected to copy: the shortcut stays the textarea's own.
        onCopyShortcut={async () => false}
        onGrow={onGrow}
        text={text}
        onTextChange={changeText}
        copyFailed={false}
        onDismissCopyFailed={() => {}}
        // The comment lands in the pending list synchronously, so there is
        // never a host round trip to wait on or a refusal to report.
        submitting={false}
        submitFailed={false}
        onDismissSubmitFailed={() => {}}
        // Not the text composer's "Comment on the selected text": nothing is selected.
        inputLabel={i18nT('components.commentOverlay.comment_on_the_boxed_area')}
      />
    </div>
  )
}
