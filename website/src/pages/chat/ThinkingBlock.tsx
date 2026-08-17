import { memo, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'
import { useRowDisclosure } from './rowDisclosure'
import { useStreamIdle } from './ChatFooter'

import { i18nT } from '../../i18n/t'

/** Newest slice of the reasoning kept for the one-line live preview. Bounded so
 *  a long trace does not put tens of kB of nowrap text in the DOM on every
 *  chunk; the row can only ever show a line's worth anyway. */
const LIVE_TAIL_CHARS = 240

/** Idle gap after which the preview stops counting as actively streaming.
 *  Longer than the footer's own window because the cost of being wrong differs:
 *  there a late hand-off shows a redundant indicator, here a short window makes
 *  the line appear and vanish across the gap between two reasoning bursts. */
const PREVIEW_IDLE_MS = 1200

/** The tail of the trace as a single line: whatever the model just wrote, with
 *  newlines collapsed so a multi-line thought still reads as one line. */
function liveTail(content: string): string {
  return content.slice(-LIVE_TAIL_CHARS).replace(/\s+/g, ' ').trimStart()
}

/**
 * Collapsible reasoning trace shown above an assistant answer.
 *
 * kiro-cli/ACP streams the model's chain-of-thought as `agent_thought_chunk`
 * updates; the backend broadcasts them as `chat_thinking` WS events, which the
 * chatSlice accumulates into a content-bearing `thinking`-role message. This
 * component renders that text as a collapsed-by-default disclosure so the
 * reasoning is available without cluttering the conversation.
 *
 * While chunks are still arriving the collapsed row also shows the tail of the
 * trace on one line, right-aligned with a left fade, so the reasoning is
 * legible as it happens without expanding anything. Reasoning is rendered as
 * dim pre-wrapped text rather than markdown -- thought streams are often
 * partial/ill-formed and shouldn't run through the markdown renderer.
 */
function ThinkingBlock({ content, disclosureKey }: { content: string; disclosureKey?: string }) {
  // Held outside the row: the transcript is virtualised, so this block is
  // unmounted whenever its row leaves the mounted window.
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const [changeTick, setChangeTick] = useState(0)
  const [clipped, setClipped] = useState(false)
  const lastContent = useRef<string | null>(null)
  const ticker = useRef<HTMLSpanElement | null>(null)

  // Liveness is derived from the content GROWING rather than from the slot's
  // running flag: one turn keeps a single reasoning block, so a burst that
  // arrives after a tool call appends to a block that is no longer the trailing
  // message and a position-based test would miss it.
  useEffect(() => {
    // A mount is not a stream event. The transcript is virtualised, so a
    // finished block scrolled back into view must not replay the preview, which
    // is why the tick stays at 0 until the content is seen to change.
    if (lastContent.current === null) { lastContent.current = content; return }
    if (lastContent.current === content) return
    lastContent.current = content
    setChangeTick(t => t + 1)
  }, [content])

  // The idle window itself is the shared one-timer hook, so this row cannot
  // drift from the other stream-quiet consumers.
  const quiet = useStreamIdle(changeTick, changeTick > 0, PREVIEW_IDLE_MS)
  const streaming = changeTick > 0 && !quiet

  const tail = content && streaming && !expanded ? liveTail(content) : ''

  // The newest words must be the ones on screen, so the row is kept scrolled to
  // its end. `text-align: right` is NOT enough: Chrome leaves scrollLeft at 0 on
  // an overflowing LTR box, which shows the OLDEST words and clips the newest.
  // The same measurement says whether anything is clipped at all, which gates
  // the fade -- a preview that fits must not have its first glyphs faded out.
  useEffect(() => {
    const el = ticker.current
    if (!el) { setClipped(false); return }
    el.scrollLeft = el.scrollWidth
    const overflowing = el.scrollWidth > el.clientWidth
    setClipped(c => (c === overflowing ? c : overflowing))
  }, [tail])

  if (!content) return null

  const fade = 'linear-gradient(to right, transparent 0, #000 36px)'

  return (
    <div className="self-start max-w-[550px] w-full">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        // The preview needs the full row to scroll in, but a row WITHOUT one
        // keeps its content-sized hit area: widening it unconditionally would
        // make empty space beside the label toggle every settled block.
        className={`${tail ? 'flex w-full min-w-0 text-left' : 'inline-flex'} items-center gap-1.5 text-[12px] text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none p-0 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm`}
        aria-expanded={expanded}
        aria-label={expanded ? i18nT('pages.chat.thinkingBlock.collapse_model_reasoning') : i18nT('pages.chat.thinkingBlock.expand_model_reasoning')}
        title={expanded ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.thinkingBlock.show_reasoning')}
      >
        <span className="shrink-0">{i18nT('pages.chat.thinkingBlock.thinking')}</span>
        <ChevronRight
          size={13}
          className="shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(90deg)' : 'none' }}
        />
        {tail && (
          // Held scrolled to its end (see the effect above), so the words the
          // model just wrote sit against the right edge and the older ones run
          // off the left; the fade marks that clipped edge instead of cutting a
          // glyph in half, and is applied ONLY when something is actually
          // clipped. aria-hidden: the text is replaced several times a second
          // and the button already carries a stable label.
          <span
            ref={ticker}
            aria-hidden
            data-testid="thinking-live-line"
            // The fade lives in an inline mask, which jsdom's style
            // implementation drops -- this mirrors the same state so the gate is
            // observable in a unit test as well as in a real browser.
            data-clipped={clipped ? 'true' : 'false'}
            className="flex-1 min-w-0 overflow-hidden whitespace-nowrap opacity-70"
            style={clipped ? { maskImage: fade, WebkitMaskImage: fade } : undefined}
          >{tail}</span>
        )}
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="reasoning"
            initial={{ height: 0, opacity: 0, marginTop: 0 }}
            animate={{ height: 'auto', opacity: 1, marginTop: 6 }}
            exit={{ height: 0, opacity: 0, marginTop: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.8 }}
            style={{ overflow: 'hidden' }}
          >
            <div className="max-h-[360px] overflow-auto">
              <div
                className="flex gap-3 py-1 text-[12px] text-muted leading-[1.6] whitespace-pre-wrap"
                style={{ wordBreak: 'break-word' }}
              >
                <span aria-hidden className="w-[3px] shrink-0 self-stretch rounded-full bg-accent opacity-40" />
                <span className="flex-1 min-w-0">{content}</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default memo(ThinkingBlock)
