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
 * It stays inside `columnRef`'s column when it fits there, as the text
 * composer does, so a box drawn near the image's edge doesn't spill over
 * whatever sits beside the panel. A column narrower than the box gets the
 * viewport clamp alone. Measured before paint, so a box near an edge never
 * flashes at the unclamped spot. `tick` re-runs it when the box changes size
 * without the point moving.
 */
function useAnchoredPosition(rootRef: React.RefObject<HTMLDivElement | null>, x: number, y: number, tick: unknown, columnRef?: React.RefObject<HTMLElement | null>) {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  useLayoutEffect(() => {
    const el = rootRef.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    let minLeft = EDGE
    let maxLeft = window.innerWidth - w - EDGE
    const col = columnRef?.current?.getBoundingClientRect()
    if (col && col.right - col.left - 2 * EDGE >= w) {
      minLeft = Math.max(minLeft, col.left + EDGE)
      maxLeft = Math.min(maxLeft, col.right - w - EDGE)
    }
    const left = Math.max(minLeft, Math.min(x - w / 2, maxLeft))
    const below = y + GAP
    const top = below + h > window.innerHeight - EDGE ? Math.max(EDGE, y - GAP - h) : below
    setPos(prev => (prev && prev.left === left && prev.top === top ? prev : { left, top }))
  }, [rootRef, x, y, tick, columnRef])
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
 *
 * Each change of `reveal` brings it back to the reader: a drag on another image
 * draws nothing while this box holds text, so the box that kept the drag is
 * scrolled on screen, if it isn't, and takes focus.
 */
export function ImageCommentComposer({ x, y, onSubmit, onCancel, scrollRef, initialText = '', onTextChange, confirmDiscard, reveal = 0 }: {
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
  /** Bumped to scroll the box on screen and focus it. */
  reveal?: number
}) {
  const [text, setText] = useState(initialText)
  const [growTick, setGrowTick] = useState(0)
  // How far the point has moved since the box opened (a scroll, or a resize
  // that rescales the image), so the box follows the image it names rather
  // than staying fixed over other content.
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

  // Re-placed as the input grows, and as the image scrolls or resizes.
  const style = useAnchoredPosition(rootRef, x - scrolled.dx, y - scrolled.dy, growTick, scrollRef)

  // Glued to the box it names, the way the text composer re-reads its
  // selection's rect on every scroll and resize. The point is kept as a
  // fraction of the drawn box (`[data-image-box-draft]`, which the layer
  // sizes by percentage of the picture), so a resize that rescales or reflows
  // the image carries the composer with it. Without that box on screen it
  // falls back to following `scrollRef`'s scroll since the box opened.
  const draftBox = useCallback((): HTMLElement | null =>
    scrollRef?.current?.querySelector<HTMLElement>('[data-image-box-draft]') ?? null, [scrollRef])
  const anchor = useRef<{ fx: number; fy: number } | null>(null)
  const scrollOrigin = useRef<{ left: number; top: number } | null>(null)
  useLayoutEffect(() => {
    const t = scrollRef?.current
    scrollOrigin.current = t ? { left: t.scrollLeft, top: t.scrollTop } : null
    const r = draftBox()?.getBoundingClientRect()
    anchor.current = r && r.width > 0 && r.height > 0 ? { fx: (x - r.left) / r.width, fy: (y - r.top) / r.height } : null
  }, [scrollRef, draftBox, x, y])
  const remeasure = useCallback(() => {
    const a = anchor.current
    const r = a ? draftBox()?.getBoundingClientRect() : undefined
    if (a && r && r.width > 0 && r.height > 0) {
      const dx = x - (r.left + a.fx * r.width)
      const dy = y - (r.top + a.fy * r.height)
      setScrolled(prev => (prev.dx === dx && prev.dy === dy ? prev : { dx, dy }))
    } else {
      const t = scrollRef?.current
      const o = scrollOrigin.current
      if (t && o) {
        const dx = t.scrollLeft - o.left
        const dy = t.scrollTop - o.top
        setScrolled(prev => (prev.dx === dx && prev.dy === dy ? prev : { dx, dy }))
      }
    }
    setGrowTick(n => n + 1)
  }, [scrollRef, draftBox, x, y])
  useAnchorRemeasure(true, remeasure)
  // The layer resizes the picture's box from a ResizeObserver, which reports
  // after the window resize's frame has measured, so the last measure of a
  // resize could read the box at its old size. Observing the box catches it.
  // ceiling: observes the box element found at mount; if the layer remounts it, scroll and window resize still re-query, observe the replacement if a late frame matters
  useEffect(() => {
    const el = draftBox()
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => remeasure())
    ro.observe(el)
    return () => ro.disconnect()
  }, [draftBox, remeasure])

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

  // Only a change after mount: a remount (the tab back from the background)
  // carries the last count and must not scroll the panel by itself.
  const revealSeen = useRef(reveal)
  useEffect(() => {
    if (reveal === revealSeen.current) return
    revealSeen.current = reveal
    const r = rootRef.current?.getBoundingClientRect()
    if (r && (r.top < EDGE || r.bottom > window.innerHeight - EDGE)) {
      // The box moves with `scrollRef`, so scrolling it brings the box along.
      const dy = r.top < EDGE ? r.top - EDGE * 4 : r.bottom - window.innerHeight + EDGE * 4
      scrollRef?.current?.scrollBy?.({ top: dy, behavior: 'smooth' })
    }
    inputRef.current?.focus({ preventScroll: true })
  }, [reveal, scrollRef])

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
