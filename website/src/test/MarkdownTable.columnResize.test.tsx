import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

const TABLE = '| Name | Value |\n| --- | --- |\n| alpha | 1 |'
const CELL_WIDTH = 120

// jsdom has no layout, so give every header cell a fixed box. With no layout
// the hook renders no grips at all (see the last test).
describe('markdown table column resize', () => {
  const originals: Record<string, PropertyDescriptor | undefined> = {}
  const stub = (prop: 'offsetWidth' | 'offsetHeight' | 'offsetLeft' | 'offsetTop', get: (el: HTMLElement) => number) => {
    originals[prop] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, prop)
    Object.defineProperty(HTMLElement.prototype, prop, { configurable: true, get() { return get(this as HTMLElement) } })
  }
  beforeEach(() => {
    stub('offsetWidth', el => (el.tagName === 'TH' ? CELL_WIDTH : 0))
    stub('offsetHeight', el => (el.tagName === 'THEAD' ? 36 : 0))
    stub('offsetLeft', el => (el.tagName === 'TH' ? (el as HTMLTableCellElement).cellIndex * CELL_WIDTH : 0))
    stub('offsetTop', () => 0)
  })
  afterEach(() => {
    for (const [prop, d] of Object.entries(originals)) {
      if (d) Object.defineProperty(HTMLElement.prototype, prop, d)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop]
    }
  })

  it('puts one grip at the right edge of every header cell, inside the scroll wrapper', () => {
    render(<MarkdownRenderer content={TABLE} />)
    const grips = screen.getAllByTestId('table-column-grip')
    expect(grips).toHaveLength(2)
    expect(grips[1].style.left).toBe(`${2 * CELL_WIDTH - 6}px`)
    expect(grips[0].parentElement).toHaveClass('overflow-x-auto')
    expect(screen.getAllByRole('separator')[0]).toHaveAttribute('aria-valuenow', String(CELL_WIDTH))
  })

  it('leaves the table on auto layout until a column is resized', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const table = container.querySelector('table')!
    expect(table.style.tableLayout).toBe('')
    expect(table.querySelector('colgroup')).toBeNull()
  })

  it('widens one column from its laid-out width and the table with it, then resets', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const table = container.querySelector('table')!
    const [first] = screen.getAllByRole('separator')
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    expect(table.style.tableLayout).toBe('fixed')
    const cols = Array.from(table.querySelectorAll('col')).map(c => c.style.width)
    expect(cols).toEqual([`${CELL_WIDTH + 16}px`, `${CELL_WIDTH}px`])
    expect(table.style.width).toBe(`${2 * CELL_WIDTH + 16}px`)
    expect(first).toHaveAttribute('aria-valuenow', String(CELL_WIDTH + 16))

    // Back to every laid-out width: back to auto layout, no colgroup.
    fireEvent.keyDown(first, { key: 'Enter' })
    expect(table.style.tableLayout).toBe('')
    expect(table.querySelector('colgroup')).toBeNull()
  })

  it('clamps a column to its minimum width', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const [first] = screen.getAllByRole('separator')
    for (let i = 0; i < 10; i++) fireEvent.keyDown(first, { key: 'ArrowLeft', shiftKey: true })
    expect(container.querySelector('col')!.style.width).toBe('48px')
  })

  it('never narrows a column auto layout made wider than the drag cap on a widen gesture', () => {
    const WIDE = 800
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
      configurable: true,
      get() {
        const el = this as HTMLElement
        if (el.tagName !== 'TH') return 0
        return (el as HTMLTableCellElement).cellIndex === 0 ? WIDE : CELL_WIDTH
      },
    })
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const [first] = screen.getAllByRole('separator')
    expect(first).toHaveAttribute('aria-valuemax', String(WIDE))
    fireEvent.keyDown(first, { key: 'ArrowRight' })
    // Already at the cap: the widen changes nothing, so auto layout stays.
    expect(container.querySelector('colgroup')).toBeNull()
    fireEvent.keyDown(first, { key: 'ArrowLeft' })
    expect(container.querySelector('col')!.style.width).toBe(`${WIDE - 16}px`)
  })

  it('gives a raw-HTML table with a spanned header cell no grips', () => {
    const SPANNED = '<table><thead><tr><th colspan="2">Both</th><th>C</th></tr></thead><tbody><tr><td>a</td><td>b</td><td>c</td></tr></tbody></table>'
    const { container } = render(<MarkdownRenderer content={SPANNED} />)
    expect(container.querySelector('th[colspan="2"]')).not.toBeNull()
    expect(screen.queryAllByTestId('table-column-grip')).toHaveLength(0)
  })

  it('gives a raw-HTML table with a spanned body cell no grips', () => {
    const SPANNED = '<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td colspan="2">ab</td></tr></tbody></table>'
    const { container } = render(<MarkdownRenderer content={SPANNED} />)
    expect(container.querySelector('td[colspan="2"]')).not.toBeNull()
    expect(screen.queryAllByTestId('table-column-grip')).toHaveLength(0)
  })

  it('gives a raw-HTML table with a rowspan="0" cell (spans to the end of its section) no grips', () => {
    const SPANNED = '<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td rowspan="0">a</td><td>b</td></tr><tr><td>c</td></tr></tbody></table>'
    const { container } = render(<MarkdownRenderer content={SPANNED} />)
    expect(container.querySelector('td[rowspan="0"]')).not.toBeNull()
    expect(screen.queryAllByTestId('table-column-grip')).toHaveLength(0)
  })

  it('renders no grips without real layout', () => {
    for (const [prop, d] of Object.entries(originals)) {
      if (d) Object.defineProperty(HTMLElement.prototype, prop, d)
    }
    render(<MarkdownRenderer content={TABLE} />)
    expect(screen.queryAllByTestId('table-column-grip')).toHaveLength(0)
  })

  it('makes each table one tab stop, with ArrowUp/ArrowDown moving between its grips', () => {
    const { container } = render(<MarkdownRenderer content={`${TABLE}\n\ntext\n\n${TABLE}`} />)
    const tables = Array.from(container.querySelectorAll('[data-testid="markdown-table"]'))
    expect(tables).toHaveLength(2)
    const stops = (root: Element) => Array.from(root.querySelectorAll('[role="separator"]')).map(s => s.getAttribute('tabindex'))
    for (const t of tables) expect(stops(t)).toEqual(['0', '-1'])

    const [first, second] = Array.from(tables[0].querySelectorAll<HTMLElement>('[role="separator"]'))
    first.focus()
    fireEvent.keyDown(first, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(second)
    expect(stops(tables[0])).toEqual(['-1', '0'])
    // Navigation never resizes, and the other table's stop is untouched.
    expect(container.querySelector('col')).toBeNull()
    expect(stops(tables[1])).toEqual(['0', '-1'])
    fireEvent.keyDown(second, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(first)
  })

  it('renders no grips on a touch device', () => {
    const original = window.matchMedia
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: q === '(pointer: coarse)', media: q, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
    try {
      render(<MarkdownRenderer content={TABLE} />)
      expect(screen.queryAllByTestId('table-column-grip')).toHaveLength(0)
    } finally {
      window.matchMedia = original
    }
  })

  it('resizes a column by a pointer drag', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const table = container.querySelector('table')!
    const [first] = screen.getAllByRole('separator')
    fireEvent.pointerDown(first, { clientX: 100, pointerId: 1 })
    fireEvent.pointerMove(first, { clientX: 160, pointerId: 1 })
    fireEvent.pointerUp(first, { clientX: 160, pointerId: 1 })
    expect(table.style.tableLayout).toBe('fixed')
    expect(Array.from(table.querySelectorAll('col')).map(c => c.style.width)).toEqual([`${CELL_WIDTH + 60}px`, `${CELL_WIDTH}px`])
  })

  it('leaves the table on auto layout after a motionless click on a grip', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />)
    const table = container.querySelector('table')!
    const [first] = screen.getAllByRole('separator')
    fireEvent.pointerDown(first, { clientX: 100, pointerId: 1 })
    fireEvent.pointerUp(first, { clientX: 100, pointerId: 1 })
    expect(table.style.tableLayout).toBe('')
    expect(table.querySelector('colgroup')).toBeNull()
  })

  describe('with real sub-pixel widths and observers', () => {
    let fraction = 0
    let observed: Element[] = []
    let notify: () => void = () => {}
    let frames: FrameRequestCallback[] = []
    beforeEach(() => {
      fraction = 0
      observed = []
      frames = []
      vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
        const w = this.tagName === 'TH' ? CELL_WIDTH + fraction : 0
        return { width: w, height: 0, top: 0, left: 0, right: w, bottom: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
      })
      // The overflow fade (useScrollEdges) runs its own observers on the
      // scroller and the table; `observed`/`notify` track only the column
      // sizer's, the one observer that watches the header row.
      vi.stubGlobal('ResizeObserver', class {
        private readonly els: Element[] = []
        constructor(private readonly cb: () => void) {}
        observe(el: Element) {
          this.els.push(el)
          if (el.tagName === 'THEAD') { observed = this.els; notify = this.cb }
        }
        disconnect() {}
        unobserve() {}
      })
      vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length })
      vi.stubGlobal('cancelAnimationFrame', () => {})
    })
    afterEach(() => {
      vi.restoreAllMocks()
      vi.unstubAllGlobals()
    })
    const flush = () => act(() => { const f = frames; frames = []; for (const cb of f) cb(0) })

    it('freezes the laid-out width rounded UP, not offsetWidth', () => {
      fraction = 0.4
      const { container } = render(<MarkdownRenderer content={TABLE} />)
      const [first] = screen.getAllByRole('separator')
      fireEvent.keyDown(first, { key: 'ArrowRight' })
      expect(Array.from(container.querySelectorAll('col')).map(c => c.style.width))
        .toEqual([`${CELL_WIDTH + 1 + 16}px`, `${CELL_WIDTH + 1}px`])
    })

    it('picks up a width change even when every right edge stays put', () => {
      const { container } = render(<MarkdownRenderer content={TABLE} />)
      fraction = 0.4
      act(() => notify())
      flush()
      const [first] = screen.getAllByRole('separator')
      fireEvent.keyDown(first, { key: 'ArrowRight' })
      expect(container.querySelector('col')!.style.width).toBe(`${CELL_WIDTH + 1 + 16}px`)
    })

    it('measures fresh widths on a resize after a full reset, not the first resize\'s', () => {
      const { container } = render(<MarkdownRenderer content={TABLE} />)
      const [first] = screen.getAllByRole('separator')
      fireEvent.keyDown(first, { key: 'ArrowRight' })
      fireEvent.keyDown(first, { key: 'Enter' })
      expect(container.querySelector('colgroup')).toBeNull()
      fraction = 0.4
      act(() => notify())
      flush()
      fireEvent.keyDown(screen.getAllByRole('separator')[0], { key: 'ArrowRight' })
      expect(Array.from(container.querySelectorAll('col')).map(c => c.style.width))
        .toEqual([`${CELL_WIDTH + 1 + 16}px`, `${CELL_WIDTH + 1}px`])
    })

    it('watches only the header boxes, and measures at most once per frame', () => {
      const { container } = render(<MarkdownRenderer content={TABLE} />)
      const table = container.querySelector('table')!
      expect(observed).not.toContain(table)
      expect(observed).toContain(table.tHead)
      expect(observed.filter(el => el.tagName === 'TH')).toHaveLength(2)
      act(() => { notify(); notify(); notify() })
      expect(frames).toHaveLength(1)
    })
  })
})
