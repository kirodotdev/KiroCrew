import { forwardRef, useCallback, useEffect, useId, useImperativeHandle, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { findTokenRanges, type PasteBlock } from '../utils/pasteTokens'
import { isTouchDevice } from '../utils/isTouchDevice'
import { PREVIEW_OPEN_DELAY_MS, PREVIEW_MAX_HEIGHT } from './pastePreviewConstants'
import PastePreviewTooltip from './PastePreviewTooltip'

export interface PasteHoverHandle {
  /** Call on textarea mousemove to check if a paste token is under the cursor. */
  handleMouseMove: (e: MouseEvent | React.MouseEvent) => void
  /** Call on textarea mouseleave to dismiss any pending/open preview. */
  handleMouseLeave: () => void
  /**
   * Call on textarea selection change (caret move / focus) to open the preview
   * when the collapsed caret lands inside a token — the keyboard/AT peek path
   * that mirrors PastedChip's onFocus behavior. Reads the token's chip-span
   * rect from the mirror div for anchoring, same as the hover path.
   */
  handleCaret: (selectionStart: number, selectionEnd: number) => void
}

interface Props {
  value: string
  blocks: PasteBlock[]
  /** The mirror div (PasteHighlightLayer) whose chip-background spans are used
   *  for hit-testing. We read their bounding rects to determine which token
   *  the cursor is over without placing anything above the textarea. */
  mirrorRef: React.RefObject<HTMLDivElement | null>
  /**
   * Reports the id of the currently-open preview panel (or null when closed),
   * so the composer can wire the textarea's aria-describedby to it. This is the
   * screen-reader association: when the caret enters a token and the preview
   * opens, AT announces the tooltip content as the textarea's description.
   */
  onActivePanelChange?: (panelId: string | null) => void
}

/**
 * Hover + caret preview controller for collapsed paste tokens in the chat
 * composer.
 *
 * Architecture: rather than placing an interactive layer above the textarea
 * (which would intercept clicks and break double-click-to-expand), this
 * component exposes an imperative handle. The textarea's onMouseMove /
 * onSelect / onBlur call into it; it reads the bounding rects of the chip spans
 * in the existing PasteHighlightLayer (the mirror div) to anchor the same
 * floating preview tooltip that PastedChip uses in message bubbles.
 *
 * No DOM is rendered above the textarea — zero interference with native
 * textarea interactions (clicking, selecting, double-click expand, drag).
 *
 * Accessibility: a collapsed caret inside a token (moved there by keyboard or
 * focus) opens the preview and reports its panel id via onActivePanelChange so
 * the composer sets aria-describedby — giving keyboard and screen-reader users
 * the same peek mouse users get, matching PastedChip's onFocus + aria pattern.
 */
const PasteHoverLayer = forwardRef<PasteHoverHandle, Props>(function PasteHoverLayer({ value, blocks, mirrorRef, onActivePanelChange }, ref) {
  const [hovered, setHovered] = useState<PasteBlock | null>(null)
  const [anchor, setAnchor] = useState<{ left: number; top: number; below: boolean } | null>(null)
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The block the preview is currently OPEN for or SCHEDULED to open for.
  // Enter-once timing keys off this (not `hovered`, which is only set once the
  // dwell timer fires) so a bare mousemove over the same token during the dwell
  // window doesn't restart the timer.
  const activeBlockId = useRef<string | null>(null)
  const panelId = useId()
  const ranges = findTokenRanges(value, blocks)

  const previewOpen = !!anchor && !!hovered

  // Report the open panel id (for the composer's aria-describedby) whenever the
  // open state flips. Cleared to null on close so a stale association can't
  // linger on the textarea.
  useEffect(() => {
    onActivePanelChange?.(previewOpen ? panelId : null)
  }, [previewOpen, panelId, onActivePanelChange])

  // Clear pending timer on unmount.
  useEffect(() => () => { if (openTimer.current) clearTimeout(openTimer.current) }, [])

  // Dismiss on scroll/resize (preview is position:fixed).
  useEffect(() => {
    if (!anchor) return
    const close = () => { activeBlockId.current = null; setAnchor(null); setHovered(null) }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close) }
  }, [anchor])

  const cancelOpen = useCallback(() => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = null
    activeBlockId.current = null
    setAnchor(null)
    setHovered(null)
  }, [])

  // Dismiss on value change (typing) — the tooltip is anchored to a rect that
  // reflows when the text changes, so it would float stale over the line being
  // edited. This addresses the UX concern about hover+typing overlap.
  useEffect(() => { cancelOpen() }, [value, cancelOpen])

  // Dismiss on Escape or outside pointerdown.
  useEffect(() => {
    if (!anchor) return
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') { activeBlockId.current = null; setAnchor(null); setHovered(null); if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null } } }
    const onPointerDown = () => { activeBlockId.current = null; setAnchor(null); setHovered(null); if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null } }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => { document.removeEventListener('keydown', onKeyDown); document.removeEventListener('pointerdown', onPointerDown) }
  }, [anchor])

  /** Resolve the chip-span rect for a paste block from the mirror div. */
  const rectForBlock = useCallback((block: PasteBlock): DOMRect | null => {
    if (!mirrorRef.current) return null
    const span = mirrorRef.current.querySelector<HTMLElement>(`[data-paste-seq="${block.seq}"]`)
    return span ? span.getBoundingClientRect() : null
  }, [mirrorRef])

  /** Open the preview for a block after the dwell delay, anchored to its rect.
   *  Enter-once timing: callers only invoke this when the target block CHANGES,
   *  so trackpad micro-jitter over the same token can't restart the timer. */
  const scheduleOpen = useCallback((block: PasteBlock, rect: DOMRect) => {
    if (openTimer.current) clearTimeout(openTimer.current)
    activeBlockId.current = block.id
    openTimer.current = setTimeout(() => {
      const below = window.innerHeight - rect.bottom >= PREVIEW_MAX_HEIGHT + 24
      setHovered(block)
      setAnchor({ left: rect.left, top: below ? rect.bottom + 4 : rect.top - 4, below })
    }, PREVIEW_OPEN_DELAY_MS)
  }, [])

  /** Find which paste block (if any) the cursor is over by hit-testing
   *  the chip spans in the PasteHighlightLayer mirror div via data-paste-seq. */
  const handleMouseMove = useCallback((e: MouseEvent | React.MouseEvent) => {
    if (isTouchDevice()) return
    // Bail when a mouse button is pressed (e.g. during text-selection drag)
    // so the tooltip doesn't pop mid-selection.
    if ('buttons' in e && e.buttons !== 0) { cancelOpen(); return }
    if (!mirrorRef.current || !ranges.length) { cancelOpen(); return }

    const x = e.clientX
    const y = e.clientY

    // Hit-test against chip spans identified by data-paste-seq attribute
    // (a structural contract, not a styling class that could change).
    const chipSpans = mirrorRef.current.querySelectorAll<HTMLElement>('[data-paste-seq]')
    let matchedBlock: PasteBlock | null = null
    let matchedRect: DOMRect | null = null

    for (const span of chipSpans) {
      const seq = Number(span.dataset.pasteSeq)
      const rect = span.getBoundingClientRect()
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        matchedBlock = ranges.find(r => r.block.seq === seq)?.block ?? null
        matchedRect = rect
        break
      }
    }

    if (!matchedBlock || !matchedRect) {
      cancelOpen()
      return
    }

    // Enter-once timing: if the preview is already open OR already scheduled
    // for this exact block, do nothing — a bare mousemove over the same token
    // must not restart the dwell timer (trackpad jitter would otherwise
    // suppress it indefinitely).
    if (activeBlockId.current === matchedBlock.id) return

    // The target block changed to a different token: dismiss the stale tooltip
    // immediately, then schedule the new one (no lingering anchor on block B).
    if (anchor) { setAnchor(null); setHovered(null) }
    scheduleOpen(matchedBlock, matchedRect)
  }, [ranges, mirrorRef, cancelOpen, anchor, scheduleOpen])

  const handleMouseLeave = useCallback(() => {
    cancelOpen()
  }, [cancelOpen])

  /** Keyboard/AT peek: open the preview when the COLLAPSED caret lands inside a
   *  token range. A non-empty selection is a drag/shift-select, not a peek, so
   *  it's ignored (and dismisses any open preview). */
  const handleCaret = useCallback((selectionStart: number, selectionEnd: number) => {
    // Mirror the hover path's touch suppression: on a touch device a tap/caret
    // must not pop the pointer-events-none tooltip over the on-screen keyboard.
    if (isTouchDevice()) { cancelOpen(); return }
    if (!ranges.length) { cancelOpen(); return }
    if (selectionStart !== selectionEnd) { cancelOpen(); return }
    const caret = selectionStart
    // Strict interior: the caret must be truly WITHIN the token, not resting on
    // an edge. A big paste parks the caret at the token's end (r.end); matching
    // that boundary would auto-open the preview unrequested right after pasting.
    // Arrow-key navigation still lands inside the range, so the peek works.
    const match = ranges.find(r => caret > r.start && caret < r.end)
    if (!match) { cancelOpen(); return }
    if (activeBlockId.current === match.block.id) return
    const rect = rectForBlock(match.block)
    if (!rect) { cancelOpen(); return }
    if (anchor) { setAnchor(null); setHovered(null) }
    scheduleOpen(match.block, rect)
  }, [ranges, cancelOpen, anchor, rectForBlock, scheduleOpen])

  // Expose the imperative handle for the textarea to call. Blur reuses
  // handleMouseLeave — both simply dismiss any pending/open preview.
  useImperativeHandle(ref, () => ({ handleMouseMove, handleMouseLeave, handleCaret }), [handleMouseMove, handleMouseLeave, handleCaret])

  return createPortal(
    <PastePreviewTooltip
      open={previewOpen}
      panelId={panelId}
      anchor={anchor}
      content={hovered ? hovered.content : ''}
      seq={hovered ? hovered.seq : 0}
      testIdPrefix="composer-paste-preview"
    />,
    document.body,
  )
})

export default PasteHoverLayer
