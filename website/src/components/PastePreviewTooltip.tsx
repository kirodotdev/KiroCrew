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
}

/**
 * Floating preview of a collapsed paste's first lines, rendered by the caller
 * through a portal to document.body and anchored to the token's viewport rect
 * (ancestors clip overflow, so an in-tree panel would be invisible).
 *
 * Shared by PastedChip (sent bubbles) and PasteHoverLayer (composer) so the two
 * previews cannot drift. It is presentational only: open/close, timing, and
 * anchor computation stay with each caller, whose trigger semantics differ (a
 * real <button> chip vs. the textarea's caret/hover).
 */
export default function PastePreviewTooltip({ open, panelId, anchor, content, seq, testIdPrefix = 'paste-preview' }: Props) {
  const previewLines = content.split('\n')
  const truncated = previewLines.length > PREVIEW_MAX_LINES
  const previewText = previewLines.slice(0, PREVIEW_MAX_LINES).join('\n')
  const moreCount = previewLines.length - PREVIEW_MAX_LINES

  return (
    <AnimatePresence>
      {open && anchor && (
        <motion.div
          key={`${testIdPrefix}-${seq}`}
          id={panelId}
          role="tooltip"
          data-testid={`${testIdPrefix}-${seq}`}
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
          <pre className="m-0 overflow-hidden text-[11px] font-mono text-muted leading-[1.5] whitespace-pre-wrap" style={{ maxHeight: PREVIEW_MAX_HEIGHT - 40, wordBreak: 'break-word' }}>{previewText}</pre>
          {truncated && (
            <div className="pt-1 text-[10px] text-muted-strong border-t border-border mt-1.5">
              {i18nT('components.pastedChip.preview_more_lines', { count: moreCount })}
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  )
}
