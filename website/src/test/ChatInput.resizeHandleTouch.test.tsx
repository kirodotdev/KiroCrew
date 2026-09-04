import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * Drag-to-resize is a POINTER affordance, and on touch it was a one-way trap.
 *
 * The handle is a 6px strip with `touch-action:none` and a zero-px drag threshold
 * sitting directly above the input, so a thumb that lands short pins the height on
 * the spot — and the only way back out is a double-click, which no finger can
 * produce. The pinned value is persisted, so one stray tap sized the composer for
 * good, across reloads.
 *
 * Two halves, and both are needed: the handle must not RENDER on touch (nothing
 * can pin a height), and a height already persisted must be DISREGARDED there
 * (nothing stays pinned from before, or from a desktop session on the same
 * origin). Neither half implies the other.
 */

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

/** Must match ChatInput's own constants. */
const INPUT_DRAG_MIN_H = 93
const MANUAL_H = 300

const COARSE = '(pointer: coarse)'
const NO_HOVER = '(hover: none)'

/** Minimal flip-able matchMedia; the reactive path itself is covered by
 *  src/test/useIsTouchDevice.test.ts, so this only needs a fixed answer. */
function stubPointer(matches: Record<string, boolean>) {
  const original = window.matchMedia
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      get matches() { return matches[query] ?? false },
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
  return () => Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: original })
}

let restore: (() => void) | null = null

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => { restore?.(); restore = null })

const outer = () => screen.getByLabelText('Message input').closest('.input-area') as HTMLElement

describe('ChatInput composer resize handle', () => {
  it('renders no drag handle on a coarse pointer', () => {
    restore = stubPointer({ [COARSE]: true, [NO_HOVER]: true })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.queryByTestId('composer-resize-handle')).toBeNull()
    // The affordance itself, not just this suite's handle on it: the element
    // carried the class long before it carried a testid, so a revert of the gate
    // fails here instead of passing on a selector that matches nothing either way.
    expect(outer().querySelectorAll('.cursor-row-resize')).toHaveLength(0)
  })

  it('still renders the drag handle on a mouse pointer', () => {
    restore = stubPointer({ [COARSE]: false, [NO_HOVER]: false })
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.getByTestId('composer-resize-handle')).toBeTruthy()
    expect(outer().querySelectorAll('.cursor-row-resize')).toHaveLength(1)
  })

  /**
   * The handle's 6px box is the ONLY separation between whatever strip sits above
   * the composer (the follow-up options row, the tip band) and the input box
   * itself. Gating the whole element on pointer type therefore also removed that
   * gap on touch, and the options row rendered flush against the input — measured
   * at 390px as chipRow.bottom === composer.top, against 6px on a mouse.
   *
   * So the invariant is on the BOX, not on the handle: something 6px tall stands
   * between the strip and the composer under both pointer types. Asserted as the
   * element's own height rather than by name, so swapping which element provides
   * it stays free while removing the gap fails.
   */
  it('keeps the 6px gap above the composer on touch, where the handle is gone', () => {
    restore = stubPointer({ [COARSE]: true, [NO_HOVER]: true })
    // Rendered WITH the options strip on purpose: that strip is what the gap
    // separates, and without it nothing sits between the gap and the composer, so
    // the position assertion below would hold no matter where the gap went.
    renderWithProviders(
      <ChatInput {...defaultProps} followUpOptions={['Ship it', 'Hold off']} onFollowUpSelect={vi.fn()} />,
    )
    const gap = screen.getByTestId('composer-top-gap')
    expect(gap.className).toContain('h-[6px]')
    const area = outer()
    const kids = Array.from(area.children)
    const boxIdx = kids.findIndex(k => k.contains(screen.getByLabelText('Message input')))
    const gapIdx = kids.indexOf(gap)
    expect(boxIdx).toBeGreaterThan(-1)
    expect(gapIdx).toBeLessThan(boxIdx)
    // The options strip must be ABOVE the gap, not between it and the composer —
    // a spacer stranded at the top of the stack separates nothing.
    const stripIdx = kids.findIndex(k => k.contains(screen.getByText('Ship it')))
    expect(stripIdx).toBeGreaterThan(-1)
    expect(stripIdx).toBeLessThan(gapIdx)
    // And nothing that renders its own height may sit between gap and box, or the
    // measured 6px is not what the eye gets.
    for (const between of kids.slice(gapIdx + 1, boxIdx)) {
      expect(between.children.length).toBe(0)
    }
  })

  it('provides the gap via the handle itself on a mouse pointer', () => {
    restore = stubPointer({ [COARSE]: false, [NO_HOVER]: false })
    renderWithProviders(<ChatInput {...defaultProps} />)
    // Exactly one 6px separator, never both — a spacer alongside the handle
    // would silently double the gap on desktop.
    expect(screen.queryByTestId('composer-top-gap')).toBeNull()
    expect(screen.getByTestId('composer-resize-handle').className).toContain('h-[6px]')
  })

  it('disregards a persisted manual height on a coarse pointer', () => {    restore = stubPointer({ [COARSE]: true, [NO_HOVER]: true })
    localStorage.setItem('mc-input-height', String(MANUAL_H))
    renderWithProviders(<ChatInput {...defaultProps} />)
    // The manual-height floor is the observable side of the preference being live.
    expect(outer().style.minHeight).toBe('')
  })

  it('honours a persisted manual height on a mouse pointer', () => {
    restore = stubPointer({ [COARSE]: false, [NO_HOVER]: false })
    localStorage.setItem('mc-input-height', String(MANUAL_H))
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(outer().style.minHeight).toBe(`${INPUT_DRAG_MIN_H}px`)
  })
})
