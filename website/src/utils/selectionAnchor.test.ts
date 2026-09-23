import { afterEach, describe, expect, it } from 'vitest'
import { anchorFromRange } from './selectionAnchor'

const mounted: HTMLElement[] = []
/** A container of one `<p>` per string, as the rendered markdown body is. */
function mount(...paragraphs: string[]): HTMLElement {
  const el = document.createElement('div')
  for (const text of paragraphs) {
    const p = document.createElement('p')
    p.textContent = text
    el.appendChild(p)
  }
  document.body.appendChild(el)
  mounted.push(el)
  return el
}
afterEach(() => { mounted.splice(0).forEach(el => el.remove()) })

function rangeOver(node: Text, start: number, end: number): Range {
  const r = document.createRange()
  r.setStart(node, start)
  r.setEnd(node, end)
  return r
}

describe('anchorFromRange', () => {
  it('pins the offset to THIS occurrence of a repeated quote, not the first', () => {
    const root = mount('alpha beta gamma beta delta')
    const text = root.firstChild!.firstChild as Text
    const second = 'alpha beta gamma beta delta'.indexOf('beta', 7)
    const a = anchorFromRange(root, rangeOver(text, second, second + 4))!
    expect(a.quote).toBe('beta')
    expect(a.startOffset).toBe(second)
    expect(a.endOffset).toBe(second + 4)
    expect(a.prefix).toBe('alpha beta gamma ')
    expect(a.suffix).toBe(' delta')
  })

  it('skips leading whitespace the drag caught, so the offset lands on the first letter', () => {
    const root = mount('alpha beta gamma')
    const text = root.firstChild!.firstChild as Text
    // " beta " selected, from the space before to the space after.
    const a = anchorFromRange(root, rangeOver(text, 5, 11))!
    expect(a.quote).toBe('beta')
    expect(a.startOffset).toBe(6)
    expect(a.endOffset).toBe(10)
  })

  it('measures in Range.toString space across block boundaries (no innerText newlines)', () => {
    const root = mount('first line', 'second line')
    const text = root.lastChild!.firstChild as Text
    const a = anchorFromRange(root, rangeOver(text, 0, 6))!
    expect(a.quote).toBe('second')
    // "first line" is 10 characters; Range.toString inserts nothing between blocks.
    expect(a.startOffset).toBe(10)
    expect(a.prefix).toBe('first line')
    expect(a.suffix).toBe(' line')
  })

  it('caps prefix and suffix at 32 characters and clamps at the document edges', () => {
    const body = 'x'.repeat(50) + 'QUOTE' + 'y'.repeat(50)
    const root = mount(body)
    const text = root.firstChild!.firstChild as Text
    const a = anchorFromRange(root, rangeOver(text, 50, 55))!
    expect(a.prefix).toBe('x'.repeat(32))
    expect(a.suffix).toBe('y'.repeat(32))
    const edge = anchorFromRange(root, rangeOver(text, 0, 3))!
    expect(edge.prefix).toBe('')
    expect(edge.startOffset).toBe(0)
  })

  it('returns null for a whitespace-only selection', () => {
    const root = mount('alpha   beta')
    const text = root.firstChild!.firstChild as Text
    expect(anchorFromRange(root, rangeOver(text, 5, 8))).toBeNull()
  })
})
