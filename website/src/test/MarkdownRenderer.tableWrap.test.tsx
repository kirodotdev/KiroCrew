import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * A wide markdown table on a phone used to render one CHARACTER per line,
 * vertically, in every cell — a 10-column scanner table was unreadable.
 *
 * The cause was inheritance, not the table's own styling. The message bubble
 * sets `overflow-wrap: anywhere` and `word-break: break-word` so an unbreakable
 * token cannot widen a message past the viewport, and table cells inherited
 * both. `anywhere` participates in MIN-CONTENT sizing, so each cell's
 * min-content became a single character — which removes the one guarantee that
 * keeps a table legible, namely that a table is never squeezed below its
 * min-content width. `w-full` then compressed the table to the container and
 * every column collapsed to one glyph.
 *
 * These assertions are deliberately on the CLASS CONTRACT rather than on
 * measured geometry: jsdom performs no layout, so the squeeze itself cannot be
 * observed here. Each one pins a property whose removal reintroduces the bug.
 */
describe('markdown table wrapping', () => {
  const renderTable = () => {
    const md = [
      '| Symbol | Price | MACD Hist | Overall |',
      '| --- | --- | --- | --- |',
      '| GOOGL | $344.82 | -0.57 | STRONG SELL |',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={md} />)
    const table = container.querySelector('table')
    if (!table) throw new Error('no table rendered')
    return table
  }

  it('does not inherit the bubble\'s intrinsic-size-collapsing wrap rules', () => {
    const cls = renderTable().className
    // `anywhere` is the property that collapsed min-content to one character.
    expect(cls).toContain('[overflow-wrap:normal]')
    // Resetting overflow-wrap alone is NOT sufficient: Chrome still shrinks
    // columns on the inherited `word-break: break-word`, which splits `$765.72`
    // into `$76 / 5.72`. Verified in a real browser at 390px.
    expect(cls).toContain('[word-break:normal]')
  })

  it('treats the container width as a floor, not a ceiling', () => {
    const cls = renderTable().className
    // `w-full` caps the table at the container, so the wrapper's
    // `overflow-x-auto` can never engage; `min-w-full` still fills the
    // container for a narrow table but lets a wide one overflow and scroll.
    expect(cls).toContain('min-w-full')
    expect(cls.split(/\s+/)).not.toContain('w-full')
  })

  it('keeps the table in a horizontally scrollable wrapper', () => {
    const wrapper = renderTable().parentElement
    expect(wrapper?.className).toContain('overflow-x-auto')
  })

  it('never breaks a column header mid-label', () => {
    const table = renderTable()
    const ths = Array.from(table.querySelectorAll('th'))
    expect(ths.length).toBeGreaterThan(0)
    for (const th of ths) expect(th.className).toContain('whitespace-nowrap')
  })
})
