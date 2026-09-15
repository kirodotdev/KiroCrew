import React, { useState } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'

// Drag-to-resize is pointer-only, and the composer disregards the persisted
// manual height outright on a touch device — the manual-mode case below needs a
// pointer for the preference to be honoured at all.
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

/** Count auto-size measurement passes.
 *
 *  A measurement copies the live element's computed box onto the off-screen twin
 *  before reading it, so exactly one `getComputedStyle(el)` happens per pass —
 *  which makes it the cheapest observable proxy for "did this call measure?".
 *  Counting the twin's own reads would not distinguish a pass from the
 *  caret-follow read, which is deliberately outside the memo. */
function countMeasures(ta: HTMLTextAreaElement) {
  const state = { measures: 0 }
  const real = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(((el: Element, pseudo?: string | null) => {
    if (el === ta) state.measures++
    return real(el as Element, pseudo ?? undefined)
  }) as typeof window.getComputedStyle)
  return state
}

/** ChatInput is controlled, and the per-keystroke double call only appears when
 *  the committed value flows back in: the input handler measures, then the
 *  auto-size effect runs again once the new `value` lands. */
function Harness({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial)
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} />
}

describe('composer auto-size measurement memo', () => {
  it('measures once per keystroke, not once per call site', () => {
    renderWithProviders(<Harness />)
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    const state = countMeasures(ta)

    fireEvent.input(ta, { target: { value: 'ab' } })

    // Both call sites ran for this keystroke; only the first found an input
    // changed, so the second must not have measured again.
    expect(state.measures).toBe(1)
  })

  it('re-measures when the content changes', () => {
    renderWithProviders(<Harness />)
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    const state = countMeasures(ta)

    fireEvent.input(ta, { target: { value: 'ab' } })
    fireEvent.input(ta, { target: { value: 'abc' } })

    expect(state.measures).toBe(2)
  })

  it('re-measures when the box width changes at an unchanged value', () => {
    renderWithProviders(<Harness initial="wrapped text" />)
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    Object.defineProperty(ta, 'clientWidth', { configurable: true, get: () => 600 })
    const state = countMeasures(ta)

    fireEvent.input(ta, { target: { value: 'wrapped text!' } })
    expect(state.measures).toBe(1)

    // Same text, narrower box: it wraps to more lines, so the cached height is
    // wrong even though the content did not move.
    Object.defineProperty(ta, 'clientWidth', { configurable: true, get: () => 200 })
    fireEvent.input(ta, { target: { value: 'wrapped text!' } })
    expect(state.measures).toBe(2)
  })

  it('restores the height after a manual resize is reset, without measuring', () => {
    // Manual mode drives the textarea off auto-sizing (inline height:100%) and
    // the double-click reset clears that. The value never changed, so the cached
    // height is still correct: the write-guard re-applies it and no measurement
    // is needed. Eliding the WRITE as well as the measurement would leave the
    // box with no height at all.
    renderWithProviders(<Harness initial="test" />)
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.input(ta, { target: { value: 'test!' } })
    const autoSized = ta.style.height
    expect(autoSized).not.toBe('')
    const state = countMeasures(ta)
    const handle = screen.getByTitle(/Drag to resize/)

    fireEvent.pointerDown(handle, { clientX: 100, clientY: 200 })
    fireEvent.pointerUp(handle, { clientX: 100, clientY: 200 })
    fireEvent.doubleClick(handle)

    expect(localStorage.getItem('mc-input-height')).toBeNull()
    expect(ta.style.height).toBe(autoSized)
    expect(state.measures).toBe(0)
  })

  it('re-measures when the box is resized from outside, with no keystroke (#9979)', () => {
    // happy-dom has no ResizeObserver; install one the test can fire by hand,
    // recording what the composer observes. The composer needs it at mount.
    const instances = new Set<{ cb: ResizeObserverCallback; targets: Set<Element> }>()
    const RO = class {
      cb: ResizeObserverCallback
      targets = new Set<Element>()
      constructor(cb: ResizeObserverCallback) { this.cb = cb; instances.add(this) }
      observe(el: Element) { this.targets.add(el) }
      unobserve(el: Element) { this.targets.delete(el) }
      disconnect() { instances.delete(this) }
    } as unknown as typeof ResizeObserver
    vi.stubGlobal('ResizeObserver', RO)
    try {
      renderWithProviders(<Harness initial="wrapped text" />)
      const ta = screen.getByRole('textbox') as HTMLTextAreaElement
      const mine = [...instances].filter((i) => i.targets.has(ta))
      expect(mine.length).toBeGreaterThan(0)
      let width = 600
      Object.defineProperty(ta, 'clientWidth', { configurable: true, get: () => width })
      const state = countMeasures(ta)
      // Only the observer(s) watching the textarea fire — the shelf and control
      // row observers are not what this case is about.
      const fire = () => { for (const i of mine) i.cb([], {} as ResizeObserver) }

      // The observer baselined the width at mount (happy-dom: 0), so the first
      // notification at 600 IS a width change: one measurement, no keystroke.
      fire()
      expect(state.measures).toBe(1)

      // A resize that did not change the width (the composer's own height write)
      // measures nothing.
      fire()
      expect(state.measures).toBe(1)

      // The column narrowed (window resize, sibling column, panel dock): the
      // value is unchanged, so no other path re-measures — this one must.
      width = 300
      fire()
      expect(state.measures).toBe(2)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
