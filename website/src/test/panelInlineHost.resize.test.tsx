/**
 * The side panel's host across a resize with no fold (lib/touchLayoutForm.ts
 * `panelInlineHost`). `useIsMobile` commits a new form inside the form-switch
 * morph's update, a frame after the viewport crosses 768px. A render in that
 * frame (any other hook's resize) must still draw the panel in the host of the
 * committed form: moving it early would put the new host in the morph's OLD
 * snapshot. Uses the real `useIsMobile`, with the morph's update held until the
 * test runs it.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'

const morph = vi.hoisted(() => ({ updates: [] as (() => void)[] }))
vi.mock('../lib/layoutFormTransition', async (orig) => ({
  ...(await orig<typeof import('../lib/layoutFormTransition')>()),
  runLayoutFormChange: (update: () => void) => { morph.updates.push(update) },
}))

import { useIsMobile } from '../hooks/useIsMobile'
import { panelInlineHost, saveTouchLayoutChoice, usePanelInlineHost } from '../lib/touchLayoutForm'
import { __resetSidePanelMounted, setSidePanelMounted } from '../hooks/sidePanelMounted'

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
  saveTouchLayoutChoice('auto')
  vi.unstubAllGlobals()
  morph.updates = []
  lists.clear()
  viewport.narrow = false
  viewport.touch = false
  viewport.folded = false
  vi.restoreAllMocks()
  __resetSidePanelMounted()
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

describe("the side panel's host while folded with a panel mounted, where no form switch runs", () => {
  // The fold keeps the form desktop on both sides of the change, so no morph
  // runs; moving the host there would remount the panel and reload its frames.
  // While a panel is mounted the host is held until a real form switch commits.
  beforeEach(() => { setSidePanelMounted(true) })
  it('a resize across 768px while folded keeps the host', () => {
    stubMatchMedia()
    viewport.folded = true
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: false })

    act(() => { crossBreakpoint(true) })
    expect(morph.updates).toHaveLength(0)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })

    // Unfolded at the narrow width, the phone form commits and the host follows it.
    act(() => { viewport.folded = false; signal() })
    act(() => { morph.updates.shift()?.() })
    rerender()
    expect(result.current).toEqual({ isMobile: true, inline: true })
  })

  it('folded at 700px, widened past 768px, then laid flat at that width keeps the host', () => {
    // Unfolded at 951px the form is desktop before and after, so a move there
    // would reload the mounted panel for nothing.
    stubMatchMedia()
    viewport.folded = true
    viewport.narrow = true
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: true })
    act(() => { crossBreakpoint(false) })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: true })
    act(() => { viewport.folded = false; signal() })
    expect(morph.updates).toHaveLength(0)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: true })
  })

  it('a switch that reverts before its update runs records no form change, so the held host stays', () => {
    stubMatchMedia()
    viewport.folded = true
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    act(() => { crossBreakpoint(true) })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
    // The fold flickers: clearing it queues a switch to the phone form, and it
    // returns before that switch's update runs.
    act(() => { viewport.folded = false; signal() })
    expect(morph.updates).toHaveLength(1)
    act(() => { viewport.folded = true; signal() })
    act(() => { morph.updates.shift()!() })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })

  it('a Touch screen layout change while folded keeps the host', () => {
    stubMatchMedia()
    viewport.touch = true
    viewport.folded = true
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Linux; Android 14; Fold) Mobile Safari/537.36')
    const { result, rerender } = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(result.current).toEqual({ isMobile: false, inline: true })

    act(() => { saveTouchLayoutChoice('desktop') })
    expect(morph.updates).toHaveLength(0)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: true })
  })
})

describe("the side panel's host while folded with no panel mounted", () => {
  // No panel, nothing to reload: a wake that switches no form records the
  // form afresh, so the next open takes the current form's host.
  function foldedAt700ThenWidenedThenFlat() {
    stubMatchMedia()
    viewport.folded = true
    viewport.narrow = true
    const hook = renderHook(() => {
      const isMobile = useIsMobile()
      return { isMobile, inline: panelInlineHost({ isMobile }) }
    })
    expect(hook.result.current).toEqual({ isMobile: false, inline: true })
    return hook
  }

  it('folded at 700px, widened past 768px, then laid flat: the shell column host', () => {
    const { result, rerender } = foldedAt700ThenWidenedThenFlat()
    act(() => { crossBreakpoint(false) })
    act(() => { viewport.folded = false; signal() })
    expect(morph.updates).toHaveLength(0)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })

  it('the held panel unmounting refreshes the host with no wake', () => {
    setSidePanelMounted(true)
    const { result, rerender } = foldedAt700ThenWidenedThenFlat()
    act(() => { crossBreakpoint(false) })
    act(() => { viewport.folded = false; signal() })
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: true })
    setSidePanelMounted(false)
    rerender()
    expect(result.current).toEqual({ isMobile: false, inline: false })
  })

  it("the page's subscribed host reader flips on the no-op wake itself, with no other render", () => {
    stubMatchMedia()
    viewport.folded = true
    viewport.narrow = true
    // The reader sessionsDrawer uses, alone: no ChatPage, no other listener,
    // and no rerender() below, so only its own subscription can update it.
    const { result } = renderHook(() => usePanelInlineHost({ isMobile: useIsMobile() }))
    expect(result.current).toBe(true)
    act(() => { crossBreakpoint(false) })
    act(() => { viewport.folded = false; signal() })
    expect(morph.updates).toHaveLength(0)
    expect(result.current).toBe(false)
  })
})
