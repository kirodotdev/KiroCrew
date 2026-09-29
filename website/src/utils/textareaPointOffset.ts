/**
 * Map a viewport point to a character offset in a textarea's value, and
 * find where that offset sits on screen.
 *
 * A textarea exposes no hit test for its text, so this lays an invisible
 * mirror `<div>` over it with the same box, font and wrapping, scrolled the
 * same way, and asks the browser's caret hit test
 * (`caretPositionFromPoint`, or WebKit/Blink's `caretRangeFromPoint`) where
 * the point falls inside the mirror's single text node. Returns `null` when
 * the point is outside the textarea or the browser has neither API, so the
 * caller can fall back to the caret.
 */

/** Keeps a trailing newline's empty last line measurable. */
const ZERO_WIDTH_SPACE = '\u200b'

/** Computed-style properties that decide where text wraps and sits. */
const MIRRORED = [
  'boxSizing', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
  'borderTopStyle', 'borderRightStyle', 'borderBottomStyle', 'borderLeftStyle',
  'fontFamily', 'fontSize', 'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch',
  'lineHeight', 'letterSpacing', 'wordSpacing', 'textIndent', 'textTransform',
  'textAlign', 'direction', 'tabSize', 'wordBreak', 'overflowWrap',
] as const

type CaretDoc = Document & {
  caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null
  caretRangeFromPoint?: (x: number, y: number) => Range | null
}

/** Where a drop would land: the text offset, and the insertion caret's
 *  viewport box (absent when it cannot be measured). */
export interface TextareaDropTarget {
  offset: number
  caret?: { left: number; top: number; height: number }
}

function caretAt(doc: CaretDoc, x: number, y: number): { node: Node; offset: number } | null {
  if (typeof doc.caretPositionFromPoint === 'function') {
    const pos = doc.caretPositionFromPoint(x, y)
    return pos ? { node: pos.offsetNode, offset: pos.offset } : null
  }
  if (typeof doc.caretRangeFromPoint === 'function') {
    const range = doc.caretRangeFromPoint(x, y)
    return range ? { node: range.startContainer, offset: range.startOffset } : null
  }
  return null
}

/**
 * Move an offset that falls inside a word to the nearer edge of that word.
 * A pointer release has none of a caret's precision, so a drop between two
 * letters means "near this word", never "split it".
 */
function snapOutOfWord(value: string, at: number): number {
  const isWord = (ch: string | undefined) => ch !== undefined && !/\s/.test(ch)
  if (!isWord(value[at - 1]) || !isWord(value[at])) return at
  let start = at
  while (isWord(value[start - 1])) start--
  let end = at
  while (isWord(value[end])) end++
  return at - start <= end - at ? start : end
}

/**
 * The drop target under a viewport point: the offset, moved out of any word
 * it falls inside and then through `adjust` (the host's own clamp, so the
 * preview and the insert agree), and the viewport box of a caret at that
 * offset, which a drag indicator can draw so the user sees where a release
 * will land.
 */
export function textareaDropTargetAtPoint(
  textarea: HTMLTextAreaElement, x: number, y: number,
  adjust?: (value: string, offset: number) => number,
): TextareaDropTarget | null {
  const doc = textarea.ownerDocument as CaretDoc
  if (typeof doc.caretPositionFromPoint !== 'function' && typeof doc.caretRangeFromPoint !== 'function') return null
  const rect = textarea.getBoundingClientRect()
  if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) return null
  const view = doc.defaultView
  if (!view) return null
  const style = view.getComputedStyle(textarea)
  const value = textarea.value

  const mirror = doc.createElement('div')
  const ms = mirror.style
  for (const prop of MIRRORED) ms[prop] = style[prop]
  // The textarea's vertical scrollbar narrows its text column; the mirror
  // has none, so widen its right padding by the same amount to wrap alike.
  const borderX = (parseFloat(style.borderLeftWidth) || 0) + (parseFloat(style.borderRightWidth) || 0)
  const scrollbar = Math.max(0, textarea.offsetWidth - textarea.clientWidth - borderX)
  if (scrollbar) ms.paddingRight = `${(parseFloat(style.paddingRight) || 0) + scrollbar}px`
  ms.boxSizing = 'border-box'
  ms.position = 'fixed'
  ms.left = `${rect.left}px`
  ms.top = `${rect.top}px`
  ms.width = `${rect.width}px`
  ms.height = `${rect.height}px`
  ms.margin = '0'
  ms.overflow = 'hidden'
  ms.whiteSpace = 'pre-wrap'
  // Hit-testable but unseen: `visibility: hidden` and `pointer-events: none`
  // would both make the hit test skip the mirror.
  ms.opacity = '0'
  ms.zIndex = '2147483647'
  // A trailing newline adds a line only once something follows it.
  const text = doc.createTextNode(value + ZERO_WIDTH_SPACE)
  mirror.appendChild(text)
  doc.body.appendChild(mirror)
  try {
    mirror.scrollTop = textarea.scrollTop
    mirror.scrollLeft = textarea.scrollLeft
    const hit = caretAt(doc, x, y)
    if (!hit) return null
    let offset: number
    if (hit.node === text) {
      offset = Math.max(0, Math.min(hit.offset, value.length))
    } else if (hit.node === mirror) {
      // The point landed on the mirror box itself, in padding outside the
      // text. Decide by geometry rather than the child index the hit test
      // reports: above the first line is the start, anything else the end.
      const all = doc.createRange()
      all.selectNodeContents(text)
      const first = typeof all.getClientRects === 'function' ? all.getClientRects()[0] : undefined
      offset = first && y < first.top ? 0 : value.length
    } else {
      return null
    }
    offset = snapOutOfWord(value, offset)
    if (adjust) offset = Math.max(0, Math.min(adjust(value, offset), value.length))
    // Measure the caret at `offset` by the character after it (the zero-width
    // tail at the end), so a wrapped line reports the line it starts.
    const at = doc.createRange()
    at.setStart(text, offset)
    at.setEnd(text, offset + 1)
    const box = typeof at.getClientRects === 'function' ? at.getClientRects()[0] : undefined
    if (!box || !box.height) return { offset }
    const top = Math.max(rect.top, box.top)
    const bottom = Math.min(rect.bottom, box.bottom)
    if (bottom <= top) return { offset }
    return { offset, caret: { left: box.left, top, height: bottom - top } }
  } finally {
    mirror.remove()
  }
}

