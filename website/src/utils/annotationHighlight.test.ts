import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** Stand-ins for the CSS Custom Highlight API, which the test DOM lacks. */
const registry = new Map<string, { ranges: Range[] }>()
class FakeHighlight { ranges: Range[]; constructor(...ranges: Range[]) { this.ranges = ranges } }

describe('annotationHighlight', () => {
  beforeEach(() => {
    vi.resetModules()
    registry.clear()
    Object.defineProperty(window, 'Highlight', { configurable: true, value: FakeHighlight })
    Object.defineProperty(globalThis, 'CSS', { configurable: true, value: { highlights: { set: (n: string, h: { ranges: Range[] }) => { registry.set(n, h) }, delete: (n: string) => registry.delete(n) } } })
  })
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).Highlight
    delete (globalThis as unknown as Record<string, unknown>).CSS
  })

  it('paints a range per text node it spans, per owner, and clears only that owner', async () => {
    const { paintAnnotationHighlight, clearAnnotationHighlight } = await import('./annotationHighlight')
    const root = document.createElement('div')
    root.append('alpha ', document.createElement('b'), ' gamma')
    root.querySelector('b')!.append('beta')
    document.body.appendChild(root)
    const range = document.createRange()
    range.setStart(root.firstChild as Text, 2)
    range.setEnd(root.lastChild as Text, 3)
    const a = {}, b = {}
    paintAnnotationHighlight(a, range)
    expect(registry.get('mc-annotate')!.ranges.map(r => r.toString())).toEqual(['pha ', 'beta', ' ga'])
    const other = document.createRange(); other.selectNodeContents(root.querySelector('b')!)
    paintAnnotationHighlight(b, other)
    expect(registry.get('mc-annotate')!.ranges).toHaveLength(4)
    clearAnnotationHighlight(a)
    expect(registry.get('mc-annotate')!.ranges.map(r => r.toString())).toEqual(['beta'])
    clearAnnotationHighlight(b)
    expect(registry.has('mc-annotate')).toBe(false)
    root.remove()
  })

  it('is a no-op where the API is absent', async () => {
    delete (window as unknown as Record<string, unknown>).Highlight
    vi.resetModules()
    const { paintAnnotationHighlight } = await import('./annotationHighlight')
    const range = document.createRange(); range.selectNodeContents(document.body)
    expect(() => paintAnnotationHighlight({}, range)).not.toThrow()
    expect(registry.size).toBe(0)
  })
})
