import { useEffect, useRef, useState, memo, useId } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'
import type { PasteBlock } from '../utils/pasteTokens'
import { isTouchDevice } from '../utils/isTouchDevice'

import { i18nT } from '../i18n/t'

/** Lines shown in the hover preview before truncating with a "+N more" footer. */
export const PREVIEW_MAX_LINES = 12
/** Hover dwell (ms) before the preview opens — avoids flicker on cursor pass-through. */
const PREVIEW_OPEN_DELAY_MS = 300
/** Max height of the preview panel (px); used for viewport flip positioning too. */
const PREVIEW_MAX_HEIGHT = 240

/**
 * Inline chip shown in a sent user bubble in place of a collapsed-paste token.
 *
 * Visual: tight accent-colored inline text matching body font. Click toggles an
 * animated inline reveal of the full content with a rounded accent bar gutter.
 * Text inside the expanded pre is lighter than surrounding bubble text so it
 * reads as a quote. No popups, no overlays.
 *
 * Hovering (or keyboard-focusing) the collapsed chip shows a small floating
 * preview of the first PREVIEW_MAX_LINES lines, so a paste can be identified
 * without expanding it. The preview renders through a portal to document.body,
 * anchored to the chip's viewport rect — the message bubble ancestors clip
 * overflow, so an in-tree absolutely-positioned panel would be invisible. It
 * is suppressed while the block is expanded (the full content is already
 * visible) and dismisses on mouse-out/blur/scroll.
 */
function PastedChip({ block }: { block: PasteBlock }) {
  const [expanded, setExpanded] = useState(false)
  const [anchor, setAnchor] = useState<{ left: number; top: number; below: boolean } | null>(null)
  const btnRef = useRef<HTMLButtonElement | null>(null)
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const panelId = useId()

  // Clear any pending open-timer on unmount so a hover right before unmount
  // can't fire setState afterwards.
  useEffect(() => () => { if (openTimer.current) clearTimeout(openTimer.current) }, [])

  // The portal panel is position:fixed against the viewport, so any scroll
  // would leave it floating detached from the chip — dismiss instead.
  useEffect(() => {
    if (!anchor) return
    const close = () => setAnchor(null)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close) }
  }, [anchor])

  // Escape keydown + pointerdown-outside dismiss the preview (mirrors McpInfoButton).
  useEffect(() => {
    if (!anchor) return
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') { setAnchor(null); if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null } } }
    const onPointerDown = (e: PointerEvent) => {
      if (btnRef.current?.contains(e.target as Node)) return
      setAnchor(null)
      if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null }
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => { document.removeEventListener('keydown', onKeyDown); document.removeEventListener('pointerdown', onPointerDown) }
  }, [anchor])

  const scheduleOpen = () => {
    if (expanded) return
    if (isTouchDevice()) return
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = setTimeout(() => {
      const r = btnRef.current?.getBoundingClientRect()
      if (!r) return
      // Flip above the chip when there's not enough viewport room below.
      const below = window.innerHeight - r.bottom >= PREVIEW_MAX_HEIGHT + 24
      setAnchor({ left: r.left, top: below ? r.bottom + 4 : r.top - 4, below })
    }, PREVIEW_OPEN_DELAY_MS)
  }
  const cancelOpen = () => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = null
    setAnchor(null)
  }

  const previewLines = block.content.split('\n')
  const truncated = previewLines.length > PREVIEW_MAX_LINES
  const previewText = previewLines.slice(0, PREVIEW_MAX_LINES).join('\n')
  const moreCount = previewLines.length - PREVIEW_MAX_LINES

  const previewOpen = !!anchor && !expanded

  return (
    <span style={{ display: 'block' }} data-paste-seq={block.seq}>
      <button
        ref={btnRef}
        type="button"
        onClick={() => { cancelOpen(); setExpanded(v => !v) }}
        onMouseEnter={scheduleOpen}
        onMouseLeave={cancelOpen}
        onFocus={scheduleOpen}
        onBlur={cancelOpen}
        className="inline-flex items-center gap-0.5 align-baseline p-0 bg-transparent border-none text-accent text-[12px] cursor-pointer hover:text-accent-hover transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm"
        aria-expanded={expanded}
        aria-describedby={previewOpen ? panelId : undefined}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} pasted ${block.lines} ${block.lines === 1 ? 'line' : 'lines'}`}
        title={expanded ? i18nT('components.pastedChip.collapse_paste') : i18nT('components.pastedChip.expand_paste')}
      >
        <ChevronRight
          size={12}
          aria-hidden
          className={`shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
        />
        {i18nT('components.pastedChip.paste_lines', { seq: block.seq, count: block.lines })}
      </button>
      {createPortal(
        <AnimatePresence>
          {previewOpen && (
            <motion.div
              key="preview"
              id={panelId}
              role="tooltip"
              data-testid={`paste-preview-${block.seq}`}
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
      )}
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="expanded"
            initial={{ height: 0, width: 0, opacity: 0, marginTop: 0, marginBottom: 0 }}
            animate={{ height: 'auto', width: 'auto', opacity: 1, marginTop: 6, marginBottom: 4 }}
            exit={{ height: 0, width: 0, opacity: 0, marginTop: 0, marginBottom: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.8 }}
            style={{ overflow: 'hidden' }}
          >
            <div className="max-h-[280px] overflow-auto">
              <div className="flex gap-3 py-1 text-[12px] font-mono text-muted leading-[1.55] whitespace-pre-wrap" style={{ wordBreak: 'break-word' }}>
                <span aria-hidden className="w-[3px] shrink-0 self-stretch rounded-full bg-accent" />
                <span className="flex-1 min-w-0">{block.content}</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </span>
  )
}

export default memo(PastedChip)
