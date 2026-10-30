import { useLayoutEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { PREVIEW_MAX_LINES, PREVIEW_MAX_HEIGHT } from './pastePreviewConstants'

import { i18nT } from '../i18n/t'

interface Props {
  /** Whether the tooltip is currently open. */
  open: boolean
  /** Stable element id, wired to the trigger's aria-describedby. */
  panelId: string
  /** Viewport anchor: chip/token rect edge + flip direction. */
  anchor: { left: number; top: number; below: boolean } | null
  /** Full paste content; first PREVIEW_MAX_LINES lines are shown. */
  content: string
  /** A stable identity for the previewed token (used for animation key + test id). */
  seq: number
  /** Test-id prefix; defaults to 'paste-preview'. The composer passes a distinct
   *  prefix so a sent chip and a composer token with the same seq never collide
   *  under one test id. */
  testIdPrefix?: string
  /** Mark the open tooltip `data-escape-owner`. Set by callers whose own Escape
   *  handler dismisses this preview (PasteHoverLayer, PastedChip) so the
   *  composer's Escape-to-stop gate yields and one Escape does not both close
   *  the preview AND stop the running turn (chat-input/escapeOwners.ts, #22315). */
  escapeOwner?: boolean
  /** One short line naming what the user can do with the token (the composer
   *  passes its expand/remove gestures). Omitted, nothing is shown. */
  hint?: string
}

/** Height of one preview row in px. Mirrors the `<pre>`'s `text-[11px]
 *  leading-[1.5]` classes below; change both together. */
const PREVIEW_ROW_PX = 16.5 // text-[11px] at leading-[1.5]

/**
 * Count the preview lines the reader can begin to read inside the
 * height-capped `<pre>`. Each line is its own inline span, so a wrapped
 * line's rect spans all its rows; a line counts as shown once at least half
 * of its first row is visible, even when its later rows are clipped. The
 * test is the row's midpoint, not its bottom, because an inline span's rect
 * starts at the font's content area, a few px below the row's top, so
 * "row bottom" read from it overshoots and would hide a fully visible last
 * line. A rect whose top is at or above the box's own top always counts;
 * that only matters for an unlaid-out (all-zero) rect, which would
 * otherwise count every line as hidden.
 */
function countShownLines(pre: HTMLElement): number {
  const { top, bottom } = pre.getBoundingClientRect()
  let shown = 0
  for (const line of pre.querySelectorAll('[data-preview-line]')) {
    const lineTop = line.getBoundingClientRect().top
    const firstRowVisible = lineTop + PREVIEW_ROW_PX / 2 < bottom
    if (lineTop > top && !firstRowVisible) break
    shown++
  }
  return shown
}

/**
 * Floating preview of a collapsed paste's first lines, rendered by the caller
 * through a portal to the body-level overlay root and anchored to the token's viewport rect
 * (ancestors clip overflow, so an in-tree panel would be invisible).
 *
 * Shared by PastedChip (sent bubbles) and PasteHoverLayer (composer) so the two
 * previews cannot drift. It is presentational only: open/close, timing, and
 * anchor computation stay with each caller, whose trigger semantics differ (a
 * real <button> chip vs. the textarea's caret/hover).
 *
 * The `<pre>` is capped both by line count and by height, so wrapped lines can
 * push later lines out of view. The footer counts the lines the reader cannot
 * begin to read (less than half of the first row inside the box), measured before paint,
 * so shown + footer = total.
 */
export default function PastePreviewTooltip({ open, panelId, anchor, content, seq, testIdPrefix = 'paste-preview', escapeOwner = false, hint }: Props) {
  const preRef = useRef<HTMLPreElement>(null)
  const [measured, setMeasured] = useState<{ content: string; shown: number } | null>(null)
  const allLines = content.split('\n')
  const previewLines = allLines.slice(0, PREVIEW_MAX_LINES)
  const total = allLines.length
  const shown = measured && measured.content === content ? measured.shown : previewLines.length
  const moreCount = total - shown

  useLayoutEffect(() => {
    const pre = preRef.current
    if (!open || !anchor || !pre) return
    const next = countShownLines(pre)
    setMeasured(prev => (prev && prev.content === content && prev.shown === next ? prev : { content, shown: next }))
  }, [open, anchor, content])

  return (
    <AnimatePresence>
      {open && anchor && (
        <motion.div
          key={`${testIdPrefix}-${seq}`}
          id={panelId}
          role="tooltip"
          data-testid={`${testIdPrefix}-${seq}`}
          {...(escapeOwner ? { 'data-escape-owner': '' } : {})}
          initial={{ opacity: 0, y: anchor.below ? 2 : -2 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed z-[130] max-w-[min(420px,calc(100vw-16px))] w-max rounded-md border border-border bg-bg-elevated shadow-lg px-3 py-2 pointer-events-none"
          style={{
            left: Math.max(8, Math.min(anchor.left, window.innerWidth - 436)),
            top: anchor.below ? anchor.top : undefined,
            bottom: anchor.below ? undefined : window.innerHeight - anchor.top,
          }}
        >
          <pre ref={preRef} className="m-0 overflow-hidden text-[11px] font-mono text-muted leading-[1.5] whitespace-pre-wrap" style={{ maxHeight: PREVIEW_MAX_HEIGHT - 40, wordBreak: 'break-word' }}>
            {previewLines.map((line, i) => (
              <span key={i} data-preview-line="">{line}{i < previewLines.length - 1 ? '\n' : ''}</span>
            ))}
          </pre>
          {moreCount > 0 && (
            <div data-testid={`${testIdPrefix}-${seq}-more`} className="pt-1 text-[10px] text-muted-strong border-t border-border mt-1.5">
              {i18nT('components.pastedChip.preview_more_lines', { count: moreCount })}
            </div>
          )}
          {hint && (
            <div data-testid={`${testIdPrefix}-${seq}-hint`} className={`text-[10px] text-muted ${moreCount > 0 ? 'pt-0.5' : 'pt-1 border-t border-border mt-1.5'}`}>
              {hint}
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
