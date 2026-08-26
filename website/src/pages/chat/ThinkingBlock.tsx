import { memo, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight, Sparkles } from 'lucide-react'
import { useRowDisclosure } from './rowDisclosure'
import { ROW_PILL_BUTTON_CLASS, ROW_PILL_WRAPPER_CLASS, ROW_RAIL_CLASS } from './rowPill'
import { useStreamIdle } from './ChatFooter'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
    <div className="self-start w-full">
      {/* The -ml-2 inside ROW_PILL_WRAPPER_CLASS cancels the button's px-2 so
          the leading icon lands on the message column's text edge (x=0),
          exactly like the tool pill's wrapper in ToolCallLine. */}
      <div className={`${tail ? 'flex w-full' : 'inline-flex'} ${ROW_PILL_WRAPPER_CLASS}`}>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        // Geometry comes from ROW_PILL_BUTTON_CLASS, shared with ToolCallLine,
        // so a reasoning row and a tool row read as one component family and a
        // later pill restyle moves both rows together. The preview needs the
        // full row to scroll in, but a row WITHOUT one keeps its content-sized
        // hit area: widening it unconditionally would make empty space beside
        // the label toggle every settled block.
        className={`${tail ? 'flex w-full min-w-0' : 'inline-flex'} ${ROW_PILL_BUTTON_CLASS} text-muted hover:text-text cursor-pointer bg-transparent border-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-none`}
        aria-expanded={expanded}
        aria-label={expanded ? i18nT('pages.chat.thinkingBlock.collapse_model_reasoning') : i18nT('pages.chat.thinkingBlock.expand_model_reasoning')}
        title={expanded ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.thinkingBlock.show_reasoning')}
      >
        {/* Same deterministic centering as the tool pill's status icon: the
            label spans pin leading-5 (20px), so the 12px icon centers on the
            first line at (20 − 12) / 2 = 4px. While reasoning is live the icon
            gently pulses, reinforcing the label's streaming shimmer so a folded
            row still reads as "in progress" even when the one-line tail is
            empty between two bursts. */}
        <Sparkles size={12} className={`shrink-0 text-accent${streaming ? ' animate-pulse' : ''}`} style={{ marginTop: '4px' }} />
        {/* The label is tense-aware: several locales render `thinking` as an
            explicitly in-progress form ("思考中", "考え中"), which reads wrong
            once the burst has settled. It rides the same growth-derived
            liveness as the preview line, so the row's whole header flips to
            the finished form the moment the preview disappears — and a block
            restored from history starts on the finished form. While live it
            also carries `.streaming-glow` — the same accent shimmer-sweep the
            streaming assistant answer uses — so a folded turn's single row
            actively signals the model is still thinking. */}
        <span className={`shrink-0 leading-5${streaming ? ' streaming-glow' : ''}`}>{streaming ? i18nT('pages.chat.thinkingBlock.thinking') : i18nT('pages.chat.thinkingBlock.thought_process')}</span>
        <ChevronRight
          size={13}
          className="shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(90deg)' : 'none', marginTop: '3.5px' }}
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
            className="flex-1 min-w-0 overflow-hidden whitespace-nowrap opacity-70 leading-5 text-[12px]"
            style={clipped ? { maskImage: fade, WebkitMaskImage: fade } : undefined}
          >{tail}</span>
        )}
      </button>
      </div>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="reasoning"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.8 }}
            style={{ overflow: 'hidden' }}
          >
            {/* The rail mirrors ToolDetails' spec exactly (2px solid, flush
                form, pl-3 content inset) so the two collapsible blocks of a
                turn share one left-edge geometry; only the colour differs —
                reasoning keeps the accent identity where a tool rail carries
                its status colour. Arbitrary-value class rather than a `/N`
                opacity modifier: the theme colours are raw var() references,
                so Tailwind opacity variants silently generate nothing. */}
            <div className={`${ROW_RAIL_CLASS} border-l-[color-mix(in_srgb,var(--accent)_70%,transparent)]`}>
              <div className="max-h-[360px] overflow-auto">
                {/* The RAIL spans the row so its edge aligns with the tool
                    payload box, but the prose inside is capped at a readable
                    measure — reasoning is sentences, not code, and ~140-char
                    lines across the full column are harder to read than the
                    alignment is worth. */}
                <div
                  className="py-1 max-w-[65ch] text-[12px] text-muted leading-5 whitespace-pre-wrap"
                  style={{ wordBreak: 'break-word' }}
                >
                  {content}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default memo(ThinkingBlock)
