/**
 * A wide markdown table hides columns past its edge. A keyboard can only bring
 * them into view if something in the scroller takes focus. When nothing inside
 * does -- no column grips (a touch screen, or jsdom: they need real layout) and
 * no link in a cell -- the scroller itself is the stop, named, so arrow keys
 * scroll it. When something inside already takes focus, or the table fits, the
 * scroller stays out of the Tab order: a second stop would cost a Tab press per
 * table.
 *
 * jsdom does no layout, so scroll geometry is stubbed the way
 * `MarkdownRenderer.tableOverflowCue.test.tsx` stubs it.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MarkdownRenderer from '../components/MarkdownRenderer'

/** `hidden` px of content beyond the right edge of a 320px scroller. */
function stubGeometry(hidden: number) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(0)
}

const PLAIN = [
  '| Symbol | Price | MACD Hist | Overall |',
  '| --- | --- | --- | --- |',
  '| GOOGL | $344.82 | -0.57 | STRONG SELL |',
].join('\n')

const WITH_LINK = [
  '| Symbol | Source |',
  '| --- | --- |',
  '| GOOGL | [filing](https://example.com/filing) |',
].join('\n')

const scroller = () => screen.getByTestId('table-scroller')

describe('markdown table keyboard stop', () => {
  beforeEach(() => {
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('makes a clipped table with nothing focusable inside a named Tab stop', async () => {
    stubGeometry(240)
    render(<><button type="button">Before</button><MarkdownRenderer content={PLAIN} /></>)
    const region = screen.getByRole('region', { name: 'Table' })
    expect(region).toBe(scroller())
    expect(region).toHaveAttribute('tabindex', '0')
    // The real key path: one Tab from the control before the table lands on it.
    const user = userEvent.setup()
    screen.getByRole('button', { name: 'Before' }).focus()
    await user.tab()
    expect(region).toHaveFocus()
  })

  it('leaves a table that fits out of the Tab order', () => {
    stubGeometry(0)
    render(<MarkdownRenderer content={PLAIN} />)
    expect(scroller()).not.toHaveAttribute('tabindex')
    expect(scroller()).not.toHaveAttribute('role')
    expect(scroller()).not.toHaveAttribute('aria-label')
    expect(screen.queryByRole('region')).toBeNull()
  })

  it('adds no second stop when a cell already holds a link', () => {
    stubGeometry(240)
    render(<MarkdownRenderer content={WITH_LINK} />)
    // The link is the stop: focus on it scrolls its cell into view.
    expect(scroller().querySelector('a[href]')).not.toBeNull()
    expect(scroller()).not.toHaveAttribute('tabindex')
    expect(screen.queryByRole('region')).toBeNull()
  })

  it('draws the focus ring on the table block, which the edge mask cannot clip', () => {
    // A clipped scroller carries a `mask-image`, and a mask clips the outline
    // with everything else the scroller paints: a ring on the stop itself is
    // focused and invisible. jsdom draws nothing, so this pins the rule that
    // moves the ring one level up; a browser screenshot proved it shows.
    const css = readFileSync(join(__dirname, '..', 'styles', 'content-fit.css'), 'utf8')
    const rule = css.match(/\.markdown-table:has\(> \.overflow-x-auto:focus-visible\)\s*\{([^}]*)\}/)
    expect(rule).not.toBeNull()
    expect(rule![1]).toMatch(/outline:\s*2px solid var\(--accent\)/)
    // The scroller is still the block's direct child, as that selector needs.
    stubGeometry(240)
    render(<MarkdownRenderer content={PLAIN} />)
    expect(scroller().parentElement).toBe(screen.getByTestId('markdown-table'))
  })
})
