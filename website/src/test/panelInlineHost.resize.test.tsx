/**
 * The side panel's host across a resize with no fold (lib/touchLayoutForm.ts
 * `panelInlineHost`). `useIsMobile` commits a new form inside the form-switch
 * morph's update, a frame after the viewport crosses 768px. A render in that
 * frame (any other hook's resize) must still draw the panel in the host of the
 * committed form: moving it early would put the new host in the morph's OLD
 * snapshot. Uses the real `useIsMobile`, with the morph's update held until the
 * test runs it.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

const morph = vi.hoisted(() => ({ updates: [] as (() => void)[] }))
vi.mock('../lib/layoutFormTransition', async (orig) => ({
  ...(await orig<typeof import('../lib/layoutFormTransition')>()),
  runLayoutFormChange: (update: () => void) => { morph.updates.push(update) },
}))

import { useIsMobile } from '../hooks/useIsMobile'
import { panelInlineHost } from '../lib/touchLayoutForm'

const viewport = { narrow: false, touch: false, folded: false }
const lists = new Set<(e: Event) => void>()
function stubMatchMedia() {
  vi.stubGlobal('matchMedia', (q: string) => ({
    // A desktop browser by default (fine pointer, no fold, the width decides);
    // `touch` makes it a touch-first phone, `folded` reports a fold.
    get matches() {
      if (q.includes('pointer: coarse') || q.includes('hover: none')) return viewport.touch
      if (q.includes('viewport-segments')) return viewport.folded
      return q.includes('max-width') ? viewport.narrow : false
    },
    media: q,
    addEventListener: (_t: string, cb: (e: Event) => void) => { lists.add(cb) },
    removeEventListener: (_t: string, cb: (e: Event) => void) => { lists.delete(cb) },
    addListener: () => {}, removeListener: () => {},
  }))
}
function signal() { for (const cb of [...lists]) cb(new Event('change')) }
function crossBreakpoint(narrow: boolean) { viewport.narrow = narrow; signal() }

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  morph.updates = []
  lists.clear()
  viewport.narrow = false
  viewport.touch = false
  viewport.folded = false
  vi.restoreAllMocks()
})

describe("the side panel's host across a resize with no fold", () => {
  it('stays in the committed form\'s host until the form switch commits, both ways', () => {
    stubMatchMedia()
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: false })

    // Narrower than 768px: the switch is pending; a render before its update
    // keeps the desktop form and the shell's column.
    act(() => { crossBreakpoint(true) })
    expect(morph.updates).toHaveLength(1)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
    act(() => { morph.updates.shift()!() })
    expect(result.current).toEqual({ isMobile: true, inline: true })

    // Back past 768px: the inline host holds until the desktop form commits.
    act(() => { crossBreakpoint(false) })
    rerender()
    expect(result.current).toEqual({ isMobile: true, inline: true })
    act(() => { morph.updates.shift()!() })
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })

  it('a window that starts narrow and widens records the desktop form at the commit', () => {
    stubMatchMedia()
    viewport.narrow = true
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: true, inline: true })
    act(() => { crossBreakpoint(false) })
    rerender()
    expect(result.current).toEqual({ isMobile: true, inline: true })
    act(() => { morph.updates.shift()!() })
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })
})

describe("the side panel's host in a window with no viewport, with the real useIsMobile", () => {
  /** A pane InstancesViewport keeps warm with display:none reports 0x0 and
   *  matches the narrow query; shown, it reports its real size. */
  function setViewport(width: number, height: number) {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
    viewport.narrow = width < 768
    signal()
  }
  afterEach(() => { setViewport(1024, 768) })

  it('a desktop pane first mounted hidden keeps the shell host, hidden and on its first reveal', () => {
    stubMatchMedia()
    setViewport(0, 0)
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: false })
    act(() => { setViewport(1024, 768) })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })

  it('a desktop pane hidden after it was shown keeps the shell host', () => {
    stubMatchMedia()
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: false })
    act(() => { setViewport(0, 0) })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })
})

describe("the side panel's host across a fold on a touch-first phone, with the real useIsMobile", () => {
  it('stays inline before and after the fold commits, and before and after the unfold commits', () => {
    stubMatchMedia()
    viewport.touch = true
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Linux; Android 14; Fold) Mobile Safari/537.36')
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: true, inline: true })

    act(() => { viewport.folded = true; signal() })
    rerender()
    expect(result.current).toEqual({ isMobile: true, inline: true })
    act(() => { morph.updates.shift()!() })
    expect(result.current).toEqual({ isMobile: false, inline: true })

    act(() => { viewport.folded = false; signal() })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: true })
    act(() => { morph.updates.shift()!() })
    expect(result.current).toEqual({ isMobile: true, inline: true })
  })
})
