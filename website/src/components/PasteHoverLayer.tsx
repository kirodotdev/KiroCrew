import { forwardRef, useCallback, useEffect, useId, useImperativeHandle, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { findTokenRanges, type PasteBlock } from '../utils/pasteTokens'
import { isTouchDevice } from '../utils/isTouchDevice'
import { PREVIEW_MAX_LINES } from './PastedChip'

import { i18nT } from '../i18n/t'

/** Hover dwell (ms) before the preview opens — matches PastedChip. */
const PREVIEW_OPEN_DELAY_MS = 300
/** Max height of the preview panel (px). */
const PREVIEW_MAX_HEIGHT = 240

export interface PasteHoverHandle {
  /** Call on textarea mousemove to check if a paste token is under the cursor. */
  handleMouseMove: (e: MouseEvent | React.MouseEvent) => void
  /** Call on textarea mouseleave to dismiss any pending/open preview. */
  handleMouseLeave: () => void
}

interface Props {
  value: string
  blocks: PasteBlock[]
  /** The mirror div (PasteHighlightLayer) whose chip-background spans are used
   *  for hit-testing. We read their bounding rects to determine which token
   *  the cursor is over without placing anything above the textarea. */
  mirrorRef: React.RefObject<HTMLDivElement | null>
}

/**
 * Hover preview controller for collapsed paste tokens in the chat composer.
 *
 * Architecture: rather than placing an interactive layer above the textarea
 * (which would intercept clicks and break double-click-to-expand), this
 * component exposes an imperative handle. The textarea's onMouseMove calls
 * handle.handleMouseMove, which reads the bounding rects of the chip spans in
 * the existing PasteHighlightLayer (the mirror div) to determine if the cursor
 * is over a token. If so, it shows the same floating preview tooltip that
 * PastedChip uses in message bubbles.
 *
 * No DOM is rendered above the textarea — zero interference with native
 * textarea interactions (clicking, selecting, double-click expand, drag).
 */
const PasteHoverLayer = forwardRef<PasteHoverHandle, Props>(function PasteHoverLayer({ value, blocks, mirrorRef }, ref) {
  const [hovered, setHovered] = useState<PasteBlock | null>(null)
  const [anchor, setAnchor] = useState<{ left: number; top: number; below: boolean } | null>(null)
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const panelId = useId()
  const ranges = findTokenRanges(value, blocks)

  // Clear pending timer on unmount.
  useEffect(() => () => { if (openTimer.current) clearTimeout(openTimer.current) }, [])

  // Dismiss on scroll/resize (preview is position:fixed).
  useEffect(() => {
    if (!anchor) return
    const close = () => { setAnchor(null); setHovered(null) }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close) }
  }, [anchor])

  const cancelOpen = useCallback(() => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = null
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
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') { setAnchor(null); setHovered(null); if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null } } }
    const onPointerDown = () => { setAnchor(null); setHovered(null); if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null } }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => { document.removeEventListener('keydown', onKeyDown); document.removeEventListener('pointerdown', onPointerDown) }
  }, [anchor])

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

    // Already showing preview for this block — no need to restart timer.
    if (hovered?.id === matchedBlock.id && anchor) return

    // Schedule opening the preview.
    if (openTimer.current) clearTimeout(openTimer.current)
    const block = matchedBlock
    const rect = matchedRect
    openTimer.current = setTimeout(() => {
      const below = window.innerHeight - rect.bottom >= PREVIEW_MAX_HEIGHT + 24
      setHovered(block)
      setAnchor({ left: rect.left, top: below ? rect.bottom + 4 : rect.top - 4, below })
    }, PREVIEW_OPEN_DELAY_MS)
  }, [ranges, mirrorRef, cancelOpen, hovered, anchor])

  const handleMouseLeave = useCallback(() => {
    cancelOpen()
  }, [cancelOpen])

  // Expose the imperative handle for the textarea to call.
  useImperativeHandle(ref, () => ({ handleMouseMove, handleMouseLeave }), [handleMouseMove, handleMouseLeave])

  const previewOpen = !!anchor && !!hovered
  const previewLines = hovered ? hovered.content.split('\n') : []
  const truncated = previewLines.length > PREVIEW_MAX_LINES
  const previewText = previewLines.slice(0, PREVIEW_MAX_LINES).join('\n')
  const moreCount = previewLines.length - PREVIEW_MAX_LINES

  return createPortal(
    <AnimatePresence>
      {previewOpen && (
        <motion.div
          key="composer-paste-preview"
          id={panelId}
          role="tooltip"
          data-testid={`composer-paste-preview-${hovered!.seq}`}
          initial={{ opacity: 0, y: anchor!.below ? 2 : -2 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed z-[130] max-w-[min(420px,calc(100vw-16px))] w-max rounded-md border border-border bg-bg-elevated shadow-lg px-3 py-2 pointer-events-none"
          style={{
            left: Math.max(8, Math.min(anchor!.left, window.innerWidth - 436)),
            top: anchor!.below ? anchor!.top : undefined,
            bottom: anchor!.below ? undefined : window.innerHeight - anchor!.top,
          }}
        >
          <pre className="m-0 overflow-hidden text-[11px] font-mono text-muted leading-[1.5] whitespace-pre-wrap" style={{ maxHeight: PREVIEW_MAX_HEIGHT - 40, wordBreak: 'break-word' }}>{previewText}</pre>
          {truncated && (
            <div className="pt-1 text-[10px] text-muted-strong border-t border-border mt-1.5">
              {i18nT('components.pastedChip.preview_more_lines', { count: moreCount })}
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  )
})

export default PasteHoverLayer
