import { Quote, X } from 'lucide-react'
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
 * - `sent`: at the top of the user bubble in place of the raw `>` lines. The
 *   whole card is the jump control when the host can jump (`onJump`), a plain
 *   box otherwise (a row re-read from another client, a quote of a message the
 *   transcript no longer has).
 *
 * Author label is derived from `role` at render time, so it follows the UI
 * language; the record itself stays language-free.
 */
export default function QuoteCard({ quote, variant, onJump, onRemove }: {
  quote: MessageQuote
  variant: 'composer' | 'sent'
  /** Scroll the transcript to the quoted message. `sent` only. */
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
    return (
      <div data-testid="quote-card-composer" className="flex items-center gap-2.5 mx-3 mt-2.5 px-2.5 py-1.5 rounded-lg bg-bg-hover">
        <span aria-hidden="true" className="w-[3px] self-stretch rounded-full bg-accent shrink-0" />
        <span className="min-w-0 flex-1 block">
          {head}
          <span className="block truncate text-[13px] leading-5 text-muted">{excerpt}</span>
        </span>
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
  const body = (
    <>
      <span aria-hidden="true" className="w-[3px] self-stretch rounded-full bg-accent shrink-0" />
      <span className="min-w-0 block text-left">
        {head}
        <span className="block text-[13px] leading-5 text-muted line-clamp-2">{excerpt}</span>
      </span>
    </>
  )
  // `w-0 min-w-full`, not `w-full`: the bubble is `w-fit`, so a card whose
  // intrinsic width is its one-line excerpt would inflate a short message to
  // the full column. Contributing NO intrinsic width and then filling the
  // bubble keeps the bubble sized by the message text (with the floor the
  // bubble sets for a carried quote), so a quoted "why?" stays a 16rem bubble
  // and the excerpt truncates to whatever width the text earned.
  const cls = 'flex w-0 min-w-full items-stretch gap-2.5 mb-2 px-3 py-1.5 rounded-lg border border-border bg-bg'
  return onJump ? (
    <button
      type="button"
      onClick={() => onJump(quote)}
      className={`${cls} cursor-pointer hover:bg-bg-hover transition-colors`}
      title={i18nT('pages.chat.quoteCard.jump_to_quoted_message')}
      aria-label={i18nT('pages.chat.quoteCard.jump_to_quoted_message')}
      data-testid="quote-card-sent"
    >
      {body}
    </button>
  ) : (
    <div className={cls} data-testid="quote-card-sent">{body}</div>
  )
}
