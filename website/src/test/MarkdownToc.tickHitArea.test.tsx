/**
 * The outline rail's ticks are its only targets on a touch screen: a touch tap
 * never opens the labelled flyout (touchTapReveal.test.tsx). Each tick is a
 * 2px bar in a 6px-tall button with 6px of dead gap below it, so a tap that
 * missed the bar by a few pixels did nothing.
 *
 * Under `(pointer: coarse)` each tick's button fills its whole 12px row pitch
 * and is 24px wide, while the pitch and the bar stay exactly as they are, so
 * the rail looks the same and a mouse gets the same layout as before.
 *
 * jsdom does no layout and has no media queries, so the geometry is read from
 * the Tailwind classes the rail renders, the way touchHitArea.test.ts reads
 * index.css: `h-[Npx]` / `gap-[Npx]` / `w-[Npx]`, plain for every pointer and
 * `pointer-coarse:` for a touch screen.
 */
import { useRef } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import MarkdownOutlineRail from '../components/MarkdownToc'

class InertObserver { observe() {} unobserve() {} disconnect() {} }
beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', InertObserver)
  vi.stubGlobal('ResizeObserver', InertObserver)
})
afterEach(() => { vi.unstubAllGlobals() })

function Doc() {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div style={{ position: 'relative' }}>
      <div ref={ref}>
        <h1>Title</h1>
        <h2>Second</h2>
        <h3>Third</h3>
      </div>
      <MarkdownOutlineRail containerRef={ref} />
    </div>
  )
}

/** The px value of utility `prop` on `el`, under `variant` (`''` = every pointer). */
function px(el: Element, prop: 'h' | 'w' | 'gap', variant: '' | 'pointer-coarse:'): number | undefined {
  for (const cls of el.className.split(/\s+/)) {
    if (!cls.startsWith(variant)) continue
    const util = cls.slice(variant.length)
    if (variant === '' && util.includes(':')) continue
    if (util === `${prop}-0`) return 0
    const m = new RegExp(`^${prop}-\\[(\\d+)px\\]$`).exec(util)
    if (m) return Number(m[1])
  }
  return undefined
}

/** Box of a tick button, falling back to the every-pointer value. */
function box(tick: Element, strip: Element, coarse: boolean) {
  const v = coarse ? 'pointer-coarse:' : ''
  const h = (coarse ? px(tick, 'h', v) : undefined) ?? px(tick, 'h', '')!
  const gap = (coarse ? px(strip, 'gap', v) : undefined) ?? px(strip, 'gap', '')!
  const bar = tick.firstElementChild as HTMLElement
  return { h, gap, pitch: h + gap, w: coarse ? px(tick, 'w', v) : undefined, barW: parseFloat(bar.style.width) }
}

describe('MarkdownToc rail tick hit area', () => {
  const ticks = () => ['Title', 'Second', 'Third'].map(name => screen.getByRole('button', { name }))

  it('on a touch screen each tick fills its row and is at least 24px wide', () => {
    render(<Doc />)
    for (const tick of ticks()) {
      const strip = tick.parentElement!
      const touch = box(tick, strip, true)
      // No dead gap: the buttons tile the column, so a tap between two bars
      // lands on the nearer tick instead of on nothing.
      expect(touch.gap).toBe(0)
      expect(touch.h).toBe(touch.pitch)
      expect(touch.w ?? 0).toBeGreaterThanOrEqual(24)
      // The bar stays pinned to the right edge of the wider button.
      expect(tick.className.split(/\s+/)).toContain('justify-end')
    }
  })

  it('keeps the same row pitch and bar, so nothing visibly moves', () => {
    render(<Doc />)
    for (const tick of ticks()) {
      const strip = tick.parentElement!
      const mouse = box(tick, strip, false)
      const touch = box(tick, strip, true)
      expect(mouse).toMatchObject({ h: 6, gap: 6, pitch: 12 })
      expect(touch.pitch).toBe(mouse.pitch)
      expect(touch.barW).toBe(mouse.barW)
      expect((tick.firstElementChild as HTMLElement).className).toContain('h-[2px]')
    }
    // The h1 bar is still the widest and the deeper ones taper.
    expect(ticks().map(t => parseFloat((t.firstElementChild as HTMLElement).style.width))).toEqual([16, 11, 8])
  })
})
