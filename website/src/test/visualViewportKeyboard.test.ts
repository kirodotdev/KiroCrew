import { describe, expect, it, vi, afterEach } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { renderHook, act } from '@testing-library/react'
import { useVisualViewport } from '../hooks/useVisualViewport'

const raw = (p: string) => readFile(join(__dirname, '..', p), 'utf8')
// Strip comments before matching. The rules below are explained in prose that
// quotes the very class names being asserted against, and a raw-text negative
// match hits the comment instead of the code.
const src = async (p: string) =>
  (await raw(p)).replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')

type FakeVV = {
  height: number; offsetTop: number
  addEventListener: (t: string, f: () => void) => void
  removeEventListener: (t: string, f: () => void) => void
  /** Fires ONE event type. Keyed per type on purpose: a fake that notifies every
   *  handler regardless of type cannot tell a missing `scroll` registration from a
   *  present one, which makes any assertion about it vacuous. */
  fire: (type: 'resize' | 'scroll') => void
}
const installFakeVV = (height: number, offsetTop = 0): FakeVV => {
  const subs: Record<string, Array<() => void>> = { resize: [], scroll: [] }
  const vv: FakeVV = {
    height, offsetTop,
    addEventListener: (t, f) => { (subs[t] ??= []).push(f) },
    removeEventListener: (t, f) => { const a = subs[t] ?? []; const i = a.indexOf(f); if (i >= 0) a.splice(i, 1) },
    fire: (t) => (subs[t] ?? []).forEach(f => f()),
  }
  Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true, writable: true })
  return vv
}

afterEach(() => {
  Object.defineProperty(window, 'visualViewport', { value: undefined, configurable: true, writable: true })
  vi.restoreAllMocks()
})

describe('useVisualViewport', () => {
  it('reports the visual viewport, not the layout one', () => {
    installFakeVV(494, 0)
    const { result } = renderHook(() => useVisualViewport())
    // window.innerHeight in jsdom is 768 by default -- the point is that the hook
    // does NOT report it once a visual viewport exists.
    expect(result.current.height).toBe(494)
    expect(result.current.height).not.toBe(window.innerHeight)
  })

  it('follows a keyboard opening and the scroll it causes', () => {
    const vv = installFakeVV(844, 0)
    const { result } = renderHook(() => useVisualViewport())
    expect(result.current).toEqual({ height: 844, offsetTop: 0 })
    // Keyboard opens: the height shrinks -- a `resize`.
    act(() => { vv.height = 494; vv.fire('resize') })
    expect(result.current.height).toBe(494)
  })

  it('follows the scroll iOS performs to reveal the focused input', () => {
    const vv = installFakeVV(494, 0)
    const { result } = renderHook(() => useVisualViewport())
    // iOS keeps the height and moves the ORIGIN. Only a `scroll` listener sees this,
    // so this case is what makes that registration falsifiable.
    act(() => { vv.offsetTop = 120; vv.fire('scroll') })
    expect(result.current.offsetTop).toBe(120)
  })

  it('falls back to the layout viewport where visualViewport is absent', () => {
    const { result } = renderHook(() => useVisualViewport())
    expect(result.current).toEqual({ height: window.innerHeight, offsetTop: 0 })
  })
})

describe('CommandPalette overlay', () => {
  it('is pinned to the visual viewport rather than inset-0', async () => {
    const s = await src('components/CommandPalette.tsx')
    expect(s, 'expected the hook').toContain('useVisualViewport()')
    expect(s).toMatch(/className="fixed left-0 right-0 z-\[9999\]/)
    expect(s).toMatch(/style=\{\{ top: vv\.offsetTop, height: vv\.height \}\}/)
    expect(s, 'inset-0 would measure the layout viewport again')
      .not.toMatch(/className="fixed inset-0 z-\[9999\]/)
  })

  it('sizes the panel against that box, so the percentages resolve', async () => {
    const s = await src('components/CommandPalette.tsx')
    // `vh` measures the LAYOUT viewport, which the keyboard does not shrink on iOS.
    // A percentage of the overlay works precisely because the overlay now has a
    // definite pixel height.
    // Explicit px off the visual viewport. NOT a percentage: a percentage MARGIN
    // resolves against the containing block's WIDTH, so `mt-[8%]` measured 31px
    // rather than the 68px it reads like. At rest these equal the previous
    // 12vh / 70vh exactly, so the at-rest panel is unchanged.
    expect(s).toMatch(/marginTop: Math\.round\(vv\.height \* 0\.12\)/)
    expect(s).toMatch(/maxHeight: Math\.round\(vv\.height \* 0\.70\)/)
    expect(s, 'no vh or percentage may size the panel box')
      .not.toMatch(/mt-\[12vh\]|max-h-\[70vh\]|mt-\[\d+%\]|max-h-\[\d+%\]/)
  })
})
