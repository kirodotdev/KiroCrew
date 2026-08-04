import { forwardRef } from 'react'
import { findTokenRanges, type PasteBlock } from '../utils/pasteTokens'

/** Shared typography between the chat textarea and this highlight mirror. MUST
 *  stay identical to the textarea's box/font classes or the chip backgrounds
 *  drift off the token text. */
export const INPUT_TYPO = 'px-4 pt-3 pb-1 text-sm font-body leading-normal'

interface Props {
  value: string
  blocks: PasteBlock[]
}

/**
 * Backdrop mirror that paints a chip-style background behind each collapsed
 * paste token in the chat textarea, so `[ Paste #N · M lines ]` reads as a
 * clickable pill (signalling it can be expanded) instead of bare literal text.
 *
 * Why a mirror: a <textarea> can't render styled inline spans. We render an
 * aria-hidden div with the EXACT same text/typography sitting directly behind
 * the transparent-background textarea; its text is transparent so only the
 * token chips' backgrounds show, while the real textarea text + caret + native
 * selection stay on top and fully interactive. Vertical scroll is synced by the
 * textarea's onScroll handler (see ChatInput).
 */
const PasteHighlightLayer = forwardRef<HTMLDivElement, Props>(function PasteHighlightLayer({ value, blocks }, ref) {
  const ranges = findTokenRanges(value, blocks)
  const nodes: React.ReactNode[] = []
  let last = 0
  ranges.forEach((r, i) => {
    if (r.start > last) nodes.push(<span key={`s${i}`}>{value.slice(last, r.start)}</span>)
    nodes.push(
      // box-decoration-clone keeps the pill background intact if the token wraps
      // across two lines. Tight hug (no padding) so it never shifts text layout.
      <span key={`c${i}`} className="rounded-md bg-accent-subtle box-decoration-clone">
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
