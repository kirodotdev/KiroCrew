/**
 * A session tab's close and pin buttons are 11px glyphs. Under
 * `(pointer: coarse)` each must be a 24x24 tap target (WCAG 2.5.8) without
 * moving anything on screen, and the two must not overlap.
 *
 * jsdom has no layout and does not evaluate media queries, so the geometry is
 * computed from the coarse-pointer utilities the buttons carry, the same way the
 * browser resolves them: padding grows the box, an equal negative margin keeps
 * its layout footprint. The tab clips its children (`overflow-hidden`), and a
 * clipped part of a box is not hit-testable, so the target must also stay inside
 * the tab's own padding.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import SessionTabStrip from '../components/SessionTabStrip'

const SLOTS = [
  { key: 'chat-1', title: 'Design review' },
  { key: 'chat-2', title: 'Release notes' },
] as unknown as RootState['dashboard']['slots']

const COARSE = '[@media(pointer:coarse)]:'
/** Tailwind's spacing step: `gap-2` and `px-2` are 8px. */
const SPACING_PX = 4
const MIN_TARGET_PX = 24

function renderStrip() {
  const defaults = createTestStore().getState()
  const store = createTestStore({ dashboard: { ...defaults.dashboard, slots: SLOTS, unreadSlots: [] } })
  render(
    <Provider store={store}>
      <SessionTabStrip tabs={['chat-1', 'chat-2']} activeKey="chat-1" onSelect={vi.fn()} onClose={vi.fn()} onReorder={vi.fn()} onTogglePin={vi.fn()} />
    </Provider>,
  )
}

/** Px value of a coarse-pointer arbitrary utility such as `-ml-[6.5px]`, or null. */
function coarsePx(el: Element, utility: string): number | null {
  const prefix = `${COARSE}${utility}-[`
  const token = [...el.classList].find(c => c.startsWith(prefix))
  if (!token) return null
  const value = Number(/\[([\d.]+)px\]$/.exec(token)?.[1])
  return utility.startsWith('-') ? -value : value
}

/** Px value of a plain spacing utility such as `gap-2`. */
function spacingPx(el: Element, utility: string): number {
  const token = [...el.classList].find(c => new RegExp(`^${utility}-\\d+$`).test(c))
  expect(token, `${utility}-* on the tab`).toBeDefined()
  return Number(token!.split('-').pop()) * SPACING_PX
}

/** The button's box and margins on a coarse pointer, per side. */
function coarseBox(button: HTMLElement) {
  const glyph = Number(button.querySelector('svg')!.getAttribute('width'))
  const pad = coarsePx(button, 'p') ?? 0
  const all = coarsePx(button, '-m') ?? 0
  const y = coarsePx(button, '-my') ?? all
  const side = (s: 'l' | 'r') => coarsePx(button, `-m${s}`) ?? all
  return { glyph, size: glyph + 2 * pad, top: y, bottom: y, left: side('l'), right: side('r') }
}

describe('SessionTabStrip: tab icon buttons on a coarse pointer', () => {
  it('makes the close and pin buttons 24px targets that keep their layout and do not overlap', () => {
    renderStrip()
    const tab = screen.getByRole('tab', { name: /Design review/ })
    const pin = screen.getByRole('button', { name: /^Pin tab Design review/ })
    const close = screen.getByRole('button', { name: /^Close tab Design review/ })
    const gap = spacingPx(tab, 'gap')
    const endPadding = spacingPx(tab, 'px')

    for (const [name, b] of [['close', coarseBox(close)], ['pin', coarseBox(pin)]] as const) {
      expect(b.size, `${name} tap target`).toBeGreaterThanOrEqual(MIN_TARGET_PX)
      // Same footprint as the bare glyph, so nothing on screen moves.
      expect(b.size + b.top + b.bottom, `${name} block footprint`).toBe(b.glyph)
    }
    const c = coarseBox(close)
    const p = coarseBox(pin)
    expect(c.size + c.left + c.right, 'close inline footprint').toBe(c.glyph)
    // The pin may take more room than its glyph (the gap it adds before the
    // close), never less.
    expect(p.size + p.left + p.right).toBeGreaterThanOrEqual(p.glyph)
    // Pin box end to close box start: the pin's trailing margin, the tab's
    // gap, the close's leading margin.
    expect(p.right + gap + c.left, 'space between the pin and close targets').toBeGreaterThanOrEqual(0)
    // The close target ends inside the tab's end padding, where the clip is.
    expect(-c.right, 'close target past the tab edge').toBeLessThanOrEqual(endPadding)
  })

  it('gives an inactive tab its close target too', () => {
    renderStrip()
    const close = screen.getByRole('button', { name: /^Close tab Release notes/ })
    expect(coarseBox(close).size).toBeGreaterThanOrEqual(MIN_TARGET_PX)
  })
})
