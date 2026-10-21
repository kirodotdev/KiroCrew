import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import PastePreviewTooltip from '../components/PastePreviewTooltip'

const ANCHOR = { left: 10, top: 10, below: true }
const ROW = 16.5 // 11px font at leading-[1.5]
const PRE_HEIGHT = 200 // PREVIEW_MAX_HEIGHT - 40

/** Fake layout: the <pre> is `preHeight` tall and every line wraps to
 *  `rowsPerLine` rows, so line i's first row starts at i * rowsPerLine * ROW.
 *  `inset` models a real browser's inline span rect, which starts at the
 *  font's content area a few px below the row top and ends as far above
 *  the last row's bottom. */
function mockWrappedLayout(rowsPerLine: number, { inset = 0, preHeight = PRE_HEIGHT }: { inset?: number; preHeight?: number } = {}) {
  const orig = Element.prototype.getBoundingClientRect
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this.tagName === 'PRE') return new DOMRect(0, 0, 400, preHeight)
    if (this.hasAttribute('data-preview-line')) {
      const i = [...this.parentElement!.children].indexOf(this)
      return new DOMRect(0, i * rowsPerLine * ROW + inset, 400, rowsPerLine * ROW - 2 * inset)
    }
    return orig.call(this)
  })
}

const longLines = (n: number) => Array.from({ length: n }, (_, i) => `line ${i + 1} ${'x'.repeat(120)}`).join('\n')

afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('PastePreviewTooltip footer count', () => {
  it('counts every line the height cap hides when lines wrap', () => {
    // 2 rows per line: line 6's first row ends at 181.5px, inside 200px; line 7
    // starts at 198px, so only a 2px sliver of its first row shows and it is hidden.
    mockWrappedLayout(2)
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={longLines(40)} seq={1} />)
    expect(screen.getByTestId('paste-preview-1-more').textContent).toContain('34 more lines')
  })

  it('counts a line whose first row is visible as shown, even when its later rows are clipped', () => {
    // 5 rows per line: line 3 starts at 165px and its first row ends at 181.5px,
    // so it counts though clipped; line 4 starts at 247.5px, below the box.
    mockWrappedLayout(5)
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={longLines(10)} seq={1} />)
    expect(screen.getByTestId('paste-preview-1-more').textContent).toContain('7 more lines')
  })

  it('shows a footer for a short paste whose wrapped lines overflow', () => {
    // Under the 12-line cap, but wrapping pushes lines 7 and 8 out of view.
    mockWrappedLayout(2)
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={longLines(8)} seq={1} />)
    expect(screen.getByTestId('paste-preview-1-more').textContent).toContain('2 more lines')
  })

  it('shows no footer for one long line that overflows the box', () => {
    // A single wrapped line (e.g. minified JSON) 20 rows tall: it fills the preview.
    mockWrappedLayout(20)
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={'x'.repeat(5000)} seq={1} />)
    expect(screen.queryByTestId('paste-preview-1-more')).toBeNull()
  })

  it('keeps the line-cap count when nothing wraps', () => {
    mockWrappedLayout(1)
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={longLines(20)} seq={1} />)
    expect(screen.getByTestId('paste-preview-1-more').textContent).toContain('8 more lines')
  })

  it('shows no footer when every line fits, with inline rects inset from their rows', () => {
    // 5 one-row lines in an unclipped 82.5px <pre>; line 5's rect starts at
    // 68px, so its rect top + one row (84.5px) passes the box bottom though
    // the whole row is visible.
    mockWrappedLayout(1, { inset: 2, preHeight: 5 * ROW })
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={['a', 'b', 'c', 'd', 'e'].join('\n')} seq={1} />)
    expect(screen.queryByTestId('paste-preview-1-more')).toBeNull()
  })

  it('counts lines the height cap hides, with inline rects inset from their rows', () => {
    // 2 rows per line: line 7's rect starts at 200px, at the box bottom.
    mockWrappedLayout(2, { inset: 2 })
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content={longLines(40)} seq={1} />)
    expect(screen.getByTestId('paste-preview-1-more').textContent).toContain('34 more lines')
  })
})

describe('PastePreviewTooltip hint', () => {
  it('shows the hint line when one is passed', () => {
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content="a" seq={1} hint="Double-click to expand" />)
    expect(screen.getByTestId('paste-preview-1-hint').textContent).toBe('Double-click to expand')
  })

  it('shows no hint line without one', () => {
    render(<PastePreviewTooltip open panelId="p" anchor={ANCHOR} content="a" seq={1} />)
    expect(screen.queryByTestId('paste-preview-1-hint')).toBeNull()
  })
})
