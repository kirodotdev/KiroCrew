import { composedTextOffset, indexComposedText } from './composedText'

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
 * mis-anchor the highlight. Both ends are measured in the composed text index
 * (`indexComposedText`), the same space the highlighter's `rangeForAnchor`
 * searches. On a body with no open shadow root that index is the concatenated
 * `textContent`, which `Range.toString` mirrors. Inside a highlighted code block (a shadow tree a Range taken
 * from the light DOM cannot measure into) the index is the only way to get an
 * offset at all, and it keeps the code's line breaks in the quote. Leading
 * whitespace the user dragged over is skipped so the offset lands on the
 * quote's first character.
 */
export function anchorFromRange(root: Node, range: Range): RangeAnchor | null {
  const idx = indexComposedText(root)
  const from = composedTextOffset(idx, range.startContainer, range.startOffset)
  const to = composedTextOffset(idx, range.endContainer, range.endOffset)
  if (from == null || to == null || to < from) return null
  const raw = idx.text.slice(from, to)
  if (!raw.trim()) return null
  const quote = raw.trim()
  const startOffset = from + (raw.length - raw.trimStart().length)
  const endOffset = startOffset + quote.length
  return {
    quote,
    prefix: idx.text.slice(Math.max(0, startOffset - 32), startOffset),
    suffix: idx.text.slice(endOffset, endOffset + 32),
    startOffset,
    endOffset,
  }
}
