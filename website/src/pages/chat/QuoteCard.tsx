import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Quote, X } from 'lucide-react'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { LinkOverrideCtx, QuotedContentCtx, type LinkOverride } from '../../components/markdown/contexts'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { MESSAGE_QUOTE_ATTR } from '../../messageActions/messageText'
import { quoteExcerpt, type MessageQuote } from '../../chat-core/composer/messageQuote'
import { fmtMessageTime, fmtMessageTimeFull } from './messageTime'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/**
 * The card a quoted message is drawn as — in two places, one shape:
 *
 * - `composer`: staged in the input box, inside the text area above the caret,
 *   with a remove control. An inset card sharing the text area's padding, no
 *   divider: the quote reads as part of what is being written, not as a
 *   separate strip of chrome (the staged-file strip is chrome; this is content).
 *   Its text is the jump control when the host can jump (`onJump`).
 * - `sent`: at the top of the user bubble in place of the raw `>` lines. The
 *   quoted text renders as markdown, clamped with a Show more toggle. The
 *   author · time header is the jump control when the host can jump
 *   (`onJump`), plain text otherwise (a row re-read from another client, a
 *   quote of a message the transcript no longer has).
 *
 * Author label is derived from `role` at render time, so it follows the UI
 * language; the record itself stays language-free.
 */
export default function QuoteCard({ quote, variant, onJump, onRemove }: {
  quote: MessageQuote
  variant: 'composer' | 'sent'
  /** Scroll the transcript to the quoted message. */
  onJump?: (quote: MessageQuote) => void
  /** Unstage the quote. `composer` only. */
  onRemove?: () => void
}) {
  useLanguageGeneration()
  // The surface's own speaker name when the record carries one (a crewmate
  // DM), else the role label -- so "Worker" is quoted as Worker, not Assistant.
  const author = quote.author ?? (quote.role === 'user' ? i18nT('pages.chat.quoteCard.you') : i18nT('pages.chat.quoteCard.assistant'))
  const excerpt = quoteExcerpt(quote.text)
  const time = quote.ts ? fmtMessageTime(quote.ts) : ''
  const timeFull = quote.ts ? fmtMessageTimeFull(quote.ts) : undefined
  const head = (
    <span className="flex items-center gap-1 text-[12px] leading-5 font-semibold text-accent min-w-0">
      <Quote size={12} className="shrink-0" aria-hidden="true" />
      <span className="truncate">
        {variant === 'composer' ? i18nT('pages.chat.quoteCard.quoting_author', { author }) : author}
      </span>
      {time && <span className="shrink-0 font-normal text-muted tabular-nums" title={timeFull}>· {time}</span>}
    </span>
  )
  // `--accent` bar + `--muted` excerpt on a `--bg-hover` / `--bg` fill: tokens
  // only, and the box carries its own edge (border or fill step) so it stays
  // visible in kiro-light where `--card` equals `--bg`.
  if (variant === 'composer') {
    // The remove control stays its own button beside the jump: a button
    // cannot hold another one. No aria-label: the visible quote stays the
    // button's name, the title its description.
    const text = (
      <>
        {head}
        <span className="block truncate text-[13px] leading-5 text-muted group-hover/quote:text-text transition-colors">{excerpt}</span>
      </>
    )
    return (
      <div data-testid="quote-card-composer" className="flex items-center gap-2.5 mx-3 mt-2.5 px-2.5 py-1.5 rounded-lg bg-bg-hover">
        <span aria-hidden="true" className="w-[3px] self-stretch rounded-full bg-accent shrink-0" />
        {onJump ? (
          <button
            type="button"
            onClick={() => onJump(quote)}
            className="group/quote min-w-0 flex-1 block text-left cursor-pointer"
            title={i18nT('pages.chat.quoteCard.jump_to_quoted_message')}
            data-testid="quote-card-jump"
          >
            {text}
          </button>
        ) : (
          <span className="min-w-0 flex-1 block">{text}</span>
        )}
        {onRemove && (
          <button
            type="button"
            onClick={onRemove}
            className="shrink-0 text-muted hover:text-text p-1 rounded-md hover:bg-bg-hover transition-colors [@media(hover:none)]:p-2.5"
            title={i18nT('pages.chat.quoteCard.remove_quote')}
            aria-label={i18nT('pages.chat.quoteCard.remove_quote')}
            data-testid="quote-card-remove"
          >
            <X size={14} />
          </button>
        )}
      </div>
    )
  }
  // `w-0 min-w-full`, not `w-full`: the bubble is `w-fit`, so a card whose
  // intrinsic width is its quoted text would inflate a short message to the
  // full column. Contributing NO intrinsic width and then filling the bubble
  // keeps the bubble sized by the message text (with the floor the bubble sets
  // for a carried quote), so a quoted "why?" stays a 16rem bubble.
  // A div, never a button: the card holds the Show more toggle and the
  // quoted markdown's own controls (copy buttons, chips), and a button cannot
  // contain them. The header is the jump control.
  // `MESSAGE_QUOTE_ATTR`: a message action reads the row's own words, not the
  // quoted text (`readMessageText`).
  return (
    <div className="flex w-0 min-w-full items-stretch gap-2.5 mb-2 px-3 py-1.5 rounded-lg border border-border bg-bg" data-testid="quote-card-sent" {...{ [MESSAGE_QUOTE_ATTR]: '' }}>
      <span aria-hidden="true" className="w-[3px] self-stretch rounded-full bg-accent shrink-0" />
      <div className="min-w-0 flex-1 text-left">
        {onJump ? (
          <button
            type="button"
            onClick={() => onJump(quote)}
            className="flex max-w-full rounded-sm cursor-pointer hover:underline underline-offset-2 decoration-accent"
            title={i18nT('pages.chat.quoteCard.jump_to_quoted_message')}
            aria-label={i18nT('pages.chat.quoteCard.jump_to_quoted_message')}
            data-testid="quote-card-sent-jump"
          >
            {head}
          </button>
        ) : head}
        <SentQuoteBody text={quote.text} softBreaks={quote.role === 'user'} />
      </div>
    </div>
  )
}

/** Height the sent card shows before Show more: about five lines of its
 *  13px/20px text. */
export const QUOTE_CLAMP_PX = 104

/** Links in a quote stay text. The quote is a copy of another row's raw text,
 *  so the server's per-row link checks (`meta.link_blocks`) do not travel with
 *  it: a link blocked on the original would turn live here. Images and
 *  widgets are held back the same way, by `QuotedContentCtx`. */
const inertLink: LinkOverride = ({ children }) => <span>{children}</span>

/** The sent card's quoted text, rendered as the markdown it is (read-only
 *  code, no widgets, images as their alt text) and clamped to
 *  `QUOTE_CLAMP_PX` with a fade. Show more grows the same box in place up to
 *  a third of the viewport, past which the box scrolls on its own, so a long
 *  quote never pushes the message under the composer. `softBreaks`: a quoted user message keeps its Shift+Enter
 *  line breaks, as its transcript row does. */
function SentQuoteBody({ text, softBreaks }: { text: string; softBreaks: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const contentRef = useRef<HTMLDivElement | null>(null)
  // Expanded, the box scrolls past a third of the viewport; the bottom fade
  // stays while more sits below, so a long table does not read as complete.
  const [attachBox, edges, , attachContent] = useScrollEdgesY<HTMLDivElement>()
  const boxRef = useRef<HTMLDivElement | null>(null)
  const boxRefCb = useCallback((node: HTMLDivElement | null) => {
    boxRef.current = node
    attachBox(node)
  }, [attachBox])
  const contentRefCb = useCallback((node: HTMLDivElement | null) => {
    contentRef.current = node
    attachContent(node)
  }, [attachContent])
  // Show less turns the scrolled box back to overflow-hidden; start it from
  // the top again, or the collapsed card shows a slice it cannot scroll off.
  const toggle = () => {
    if (expanded && boxRef.current) boxRef.current.scrollTop = 0
    setExpanded(v => !v)
  }
  const reduceMotion = useReducedMotion()
  useLayoutEffect(() => {
    const el = contentRef.current
    if (!el) return
    const measure = () => setOverflows(el.scrollHeight > QUOTE_CLAMP_PX + 1)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [text])
  const clamped = overflows && !expanded
  const maxHeight = expanded ? Math.max(QUOTE_CLAMP_PX, Math.round(window.innerHeight / 3)) : QUOTE_CLAMP_PX
  const fade = 'linear-gradient(to bottom, #000 55%, transparent)'
  return (
    <>
      <motion.div
        initial={false}
        animate={{ maxHeight }}
        transition={{ duration: reduceMotion ? 0 : 0.2, ease: 'easeOut' }}
        ref={boxRefCb}
        className={`text-[13px] leading-5 text-muted ${expanded ? `overflow-y-auto ${edges.bottom ? MORE_BELOW_MASK : ''}` : 'overflow-hidden'}`}
        style={clamped ? { maskImage: fade, WebkitMaskImage: fade } : undefined}
        data-testid="quote-card-body"
        data-clamped={clamped ? '' : undefined}
      >
        <div ref={contentRefCb} className="[&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
          <LinkOverrideCtx.Provider value={inertLink}>
            <QuotedContentCtx.Provider value>
              <MarkdownRenderer content={text} readOnlyCode compactImages softBreaks={softBreaks} />
            </QuotedContentCtx.Provider>
          </LinkOverrideCtx.Provider>
        </div>
      </motion.div>
      {overflows && (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={expanded}
          className="mt-1.5 text-[12px] leading-5 text-accent hover:underline underline-offset-2"
          data-testid="quote-card-toggle"
        >
          {expanded ? i18nT('pages.chat.quoteCard.show_less') : i18nT('pages.chat.quoteCard.show_more')}
        </button>
      )}
    </>
  )
}
