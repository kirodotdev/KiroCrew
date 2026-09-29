import { afterEach, describe, expect, it, vi } from 'vitest'
import { textareaDropTargetAtPoint } from '../utils/textareaPointOffset'

const textareaOffsetAtPoint = (t: HTMLTextAreaElement, x: number, y: number) =>
  textareaDropTargetAtPoint(t, x, y)?.offset ?? null

type CaretDoc = Document & {
  caretPositionFromPoint?: unknown
  caretRangeFromPoint?: unknown
}

function mountTextarea(value: string) {
  const textarea = document.createElement('textarea')
  textarea.value = value
  document.body.appendChild(textarea)
  vi.spyOn(textarea, 'getBoundingClientRect').mockReturnValue(
    { left: 10, top: 20, right: 210, bottom: 80, width: 200, height: 60, x: 10, y: 20, toJSON: () => ({}) } as DOMRect,
  )
  return textarea
}

/** The single text node the util lays over the textarea, while it is up. */
function mirrorText(): Text | null {
  const div = [...document.body.children].find(el => el.tagName === 'DIV') as HTMLDivElement | undefined
  return (div?.firstChild as Text | null) ?? null
}

afterEach(() => {
  const doc = document as CaretDoc
  delete doc.caretPositionFromPoint
  delete doc.caretRangeFromPoint
  document.body.replaceChildren()
  vi.restoreAllMocks()
})

describe('textareaDropTargetAtPoint', () => {
  it('returns null when the browser has no caret hit test', () => {
    const textarea = mountTextarea('hello world')
    expect(textareaOffsetAtPoint(textarea, 50, 40)).toBeNull()
  })

  it('reads the offset from caretPositionFromPoint on the mirror text', () => {
    const textarea = mountTextarea('hello world')
    const doc = document as CaretDoc
    const seen: Array<string | null> = []
    doc.caretPositionFromPoint = vi.fn(() => {
      const node = mirrorText()!
      seen.push(node.data)
      return { offsetNode: node, offset: 6 }
    })
    expect(textareaOffsetAtPoint(textarea, 50, 40)).toBe(6)
    expect(doc.caretPositionFromPoint).toHaveBeenCalledWith(50, 40)
    // The mirror carries the value plus a zero-width tail and is removed after.
    expect(seen).toEqual(['hello world\u200b'])
    expect(mirrorText()).toBeNull()
  })

  it('falls back to caretRangeFromPoint and clamps past the zero-width tail', () => {
    const textarea = mountTextarea('abc\n')
    const doc = document as CaretDoc
    doc.caretRangeFromPoint = vi.fn(() => {
      const range = document.createRange()
      range.setStart(mirrorText()!, 5)
      return range
    })
    expect(textareaOffsetAtPoint(textarea, 50, 70)).toBe(4)
  })

  it('maps a hit on the mirror box by geometry: above the text is the start, else the end', () => {
    const textarea = mountTextarea('abc')
    const doc = document as CaretDoc
    // The hit test reports child index 0 even below the text; geometry wins.
    doc.caretPositionFromPoint = vi.fn(() => ({ offsetNode: mirrorText()!.parentNode!, offset: 0 }))
    vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue(
      [{ top: 30, bottom: 48, left: 20, right: 60 }] as unknown as DOMRectList,
    )
    expect(textareaOffsetAtPoint(textarea, 200, 75)).toBe(3)
    expect(textareaOffsetAtPoint(textarea, 12, 22)).toBe(0)
  })

  it('returns null for a point outside the textarea or a hit elsewhere', () => {
    const textarea = mountTextarea('abc')
    const doc = document as CaretDoc
    const other = document.createTextNode('elsewhere')
    doc.caretPositionFromPoint = vi.fn(() => ({ offsetNode: other, offset: 2 }))
    expect(textareaOffsetAtPoint(textarea, 5, 40)).toBeNull()
    expect(doc.caretPositionFromPoint).not.toHaveBeenCalled()
    expect(textareaOffsetAtPoint(textarea, 50, 40)).toBeNull()
  })

  it('moves an offset inside a word to the nearer edge, then through the host clamp', () => {
    const textarea = mountTextarea('see here')
    const doc = document as CaretDoc
    let hit = 2
    doc.caretPositionFromPoint = vi.fn(() => ({ offsetNode: mirrorText()!, offset: hit }))
    expect(textareaDropTargetAtPoint(textarea, 50, 40)?.offset).toBe(3)
    hit = 1
    expect(textareaDropTargetAtPoint(textarea, 50, 40)?.offset).toBe(0)
    hit = 6
    expect(textareaDropTargetAtPoint(textarea, 50, 40)?.offset).toBe(4)
    hit = 4
    expect(textareaDropTargetAtPoint(textarea, 50, 40)?.offset).toBe(4)
    const adjust = vi.fn(() => 99)
    expect(textareaDropTargetAtPoint(textarea, 50, 40, adjust)?.offset).toBe(8)
    expect(adjust).toHaveBeenCalledWith('see here', 4)
  })

  it('reports the caret box at the offset, clipped to the textarea', () => {
    const textarea = mountTextarea('a bc')
    const doc = document as CaretDoc
    doc.caretPositionFromPoint = vi.fn(() => ({ offsetNode: mirrorText()!, offset: 1 }))
    const rects = vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue(
      [{ left: 31, top: 70, bottom: 90, right: 38, height: 20 }] as unknown as DOMRectList,
    )
    // Line box runs past the textarea's bottom (80): the caret is clipped.
    expect(textareaDropTargetAtPoint(textarea, 50, 75)).toEqual({ offset: 1, caret: { left: 31, top: 70, height: 10 } })
    rects.mockReturnValue([] as unknown as DOMRectList)
    expect(textareaDropTargetAtPoint(textarea, 50, 75)).toEqual({ offset: 1 })
  })

  it('lays the mirror over the textarea, hit-testable but invisible', () => {
    const textarea = mountTextarea('abc')
    textarea.style.fontSize = '17px'
    const doc = document as CaretDoc
    let style: CSSStyleDeclaration | null = null
    doc.caretPositionFromPoint = vi.fn(() => {
      style = (mirrorText()!.parentNode as HTMLElement).style
      return null
    })
    textareaOffsetAtPoint(textarea, 50, 40)
    expect(style).not.toBeNull()
    const s = style as unknown as CSSStyleDeclaration
    expect(s.position).toBe('fixed')
    expect([s.left, s.top, s.width, s.height]).toEqual(['10px', '20px', '200px', '60px'])
    expect(s.whiteSpace).toBe('pre-wrap')
    expect(s.opacity).toBe('0')
    expect(s.visibility).not.toBe('hidden')
    expect(s.pointerEvents).not.toBe('none')
    expect(s.fontSize).toBe('17px')
  })
})

