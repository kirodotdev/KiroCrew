import { useState, memo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'
import type { PasteBlock } from '../utils/pasteTokens'

import { i18nT } from '../i18n/t'
/**
 * Inline chip shown in a sent user bubble in place of a collapsed-paste token.
 *
 * Visual: tight accent-colored inline text matching body font. Click toggles an
 * animated inline reveal of the full content with a rounded accent bar gutter.
 * Text inside the expanded pre is lighter than surrounding bubble text so it
 * reads as a quote. No popups, no overlays.
 */
function PastedChip({ block }: { block: PasteBlock }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <span style={{ display: 'block' }} data-paste-seq={block.seq}>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-center gap-0.5 align-baseline p-0 bg-transparent border-none text-accent text-[12px] cursor-pointer hover:text-accent-hover transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm"
        aria-expanded={expanded}
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
