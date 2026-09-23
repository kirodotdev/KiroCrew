/**
 * The durable anchor a live DOM selection leaves behind, measured inside the
 * container the comment belongs to. Every host that lets a comment box open
 * over selected text (the file viewer, the artifact page, the remote artifact
 * page, the chat side panel's artifact layer) derives the same record from the
 * same Range — this is the one copy of that arithmetic.
 */
export interface RangeAnchor {
  /** The selected text, trimmed (what is quoted and matched). */
  quote: string
  /** Up to 32 characters of rendered text before the quote. */
  prefix: string
  /** Up to 32 characters of rendered text after the quote. */
  suffix: string
  /** Offset of the quote's first character in `root`'s rendered text. */
  startOffset: number
  /** `startOffset + quote.length`. */
  endOffset: number
}

/**
 * Derive the anchor of `range` (already clamped into `root`, see
 * `containedSelectionRange`) or null when it selects only whitespace.
 *
 * The offset comes from the Range, NOT from `indexOf(quote)`: indexOf finds the
 * FIRST occurrence, so selecting a later repeat of the same words would store
 * the prefix/suffix (and the offsets sent to the store) for the wrong spot and
 * mis-anchor the highlight. `Range.toString()` is used for both the full text
 * and the pre-selection slice so the offset space is consistent — `innerText`
 * inserts block newlines that `Range.toString` omits, and the highlighter works
 * off `textContent`, which `Range.toString` mirrors. Leading whitespace the
 * user dragged over is skipped (`raw.length - raw.trimStart().length`) so the
 * offset lands on the quote's first character.
 */
export function anchorFromRange(root: Node, range: Range): RangeAnchor | null {
  const raw = range.toString()
  if (!raw.trim()) return null
  const quote = raw.trim()
  const fullRange = document.createRange()
  fullRange.selectNodeContents(root)
  const full = fullRange.toString()
  const preRange = document.createRange()
  preRange.setStart(root, 0)
  preRange.setEnd(range.startContainer, range.startOffset)
  const startOffset = preRange.toString().length + (raw.length - raw.trimStart().length)
  const endOffset = startOffset + quote.length
  return {
    quote,
    prefix: full.slice(Math.max(0, startOffset - 32), startOffset),
    suffix: full.slice(endOffset, endOffset + 32),
    startOffset,
    endOffset,
  }
}
