import { forwardRef } from 'react'
import { findTokenRanges, type PasteBlock } from '../utils/pasteTokens'
import { findMentionRanges } from '../utils/fileTokens'

/** Shared typography between the chat textarea and this highlight mirror. MUST
 *  stay identical to the textarea's box/font classes or the chip backgrounds
 *  drift off the token text. The size is the message font setting
 *  (`mc-message-font-text`, styles/message-font-size.css): what the user types
 *  is sized like what they read. Every composer surface — textarea, this
 *  mirror, the Lexical editor and its placeholder, the Suspense fallback —
 *  shares this one constant, which is what keeps their metrics identical. */
export const INPUT_TYPO = 'px-4 pt-3 pb-1 mc-message-font-text font-body leading-normal'

interface Props {
  value: string
  blocks: PasteBlock[]
  /** Exact recorded `@rel` alias strings for staged picker files. Each
   *  occurrence is painted with the same pill treatment as a paste token,
   *  so the atomic region READS as a token before it behaves like one
   *  (fork UX review): the keyboard already treats these spans as single
   *  units, and undifferentiated body text gave no hint of that. */
  mentionTokens?: string[]
}

/**
 * Backdrop mirror that paints a chip-style background behind each collapsed
 * paste token and each picked-mention token in the chat textarea, so
 * `[ Paste #N · M lines ]` reads as a clickable pill and `@src/main.ts`
 * reads as the atomic token it is, instead of bare literal text.
 *
 * Why a mirror: a <textarea> can't render styled inline spans. We render an
 * aria-hidden div with the EXACT same text/typography sitting directly behind
 * the transparent-background textarea; its text is transparent so only the
 * token chips' backgrounds show, while the real textarea text + caret + native
 * selection stay on top and fully interactive. Vertical scroll is synced by the
 * textarea's onScroll handler (see ChatInput).
 */
const PasteHighlightLayer = forwardRef<HTMLDivElement, Props>(function PasteHighlightLayer({ value, blocks, mentionTokens = [] }, ref) {
  // Merge both token kinds into one non-overlapping, document-ordered list.
  // Overlap cannot happen for well-formed content (a paste literal and an
  // `@rel` mention never share bytes) but pathological text must not paint
  // a double background, so later overlapping ranges are dropped.
  const merged: Array<{ start: number; end: number; seq?: number }> = [
    ...findTokenRanges(value, blocks).map(r => ({ start: r.start, end: r.end, seq: r.block.seq })),
    ...findMentionRanges(value, mentionTokens).map(r => ({ start: r.start, end: r.end })),
  ].sort((a, b) => a.start - b.start)
  const ranges = merged.filter((r, i) => i === 0 || r.start >= merged[i - 1].end)
  const nodes: React.ReactNode[] = []
  let last = 0
  ranges.forEach((r, i) => {
    if (r.start > last) nodes.push(<span key={`s${i}`}>{value.slice(last, r.start)}</span>)
    nodes.push(
      // box-decoration-clone keeps the pill background intact if the token wraps
      // across two lines. Tight hug (no padding) so it never shifts text layout.
      <span
        key={`c${i}`}
        className="rounded-md bg-accent-subtle box-decoration-clone"
        {...(r.seq !== undefined ? { 'data-paste-seq': r.seq } : { 'data-mention-token': true })}
      >
        {value.slice(r.start, r.end)}
      </span>,
    )
    last = r.end
  })
  if (last < value.length) nodes.push(<span key="end">{value.slice(last)}</span>)

  return (
    <div
      ref={ref}
      aria-hidden
      data-composer-typo
      className={`pointer-events-none absolute inset-0 overflow-hidden select-none text-transparent whitespace-pre-wrap break-words ${INPUT_TYPO}`}
      style={{ overflowWrap: 'break-word', wordBreak: 'normal' }}
    >
      {nodes}
      {/* A trailing newline isn't given height by a block the way a textarea
          gives it a row; pad with a zero-width char to keep scroll parity. */}
      {value.endsWith('\n') ? '\u200b' : ''}
    </div>
  )
})

export default PasteHighlightLayer
