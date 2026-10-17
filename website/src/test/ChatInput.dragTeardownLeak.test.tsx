import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The composer resize handle can leave the tree on its own, mid-drag, while
 * ChatInput itself stays mounted: `showGhost` flips on for the approval ghost
 * swap, and the pointer type going coarse swaps the handle for a plain spacer.
 * Pointer capture dies with the element, so the terminal `lostpointercapture`
 * fires on a DETACHED node — React's root listener never sees it, and no
 * onEnd is ever delivered for the drag that was live at that moment.
 *
 * The globals set in onStart (page-wide `row-resize` cursor, text-selection
 * suppression on <body>) then stay stuck until the next drag or a full
 * component unmount — the old teardown guard was keyed to COMPONENT unmount
 * only, which is exactly the case that does not happen here.
 *
 * The teardown must therefore be keyed to the HANDLE's lifecycle (which also
 * covers component unmount one level up), and onEnd itself must restore the
 * globals even when the wrapper ref is already gone — whichever path runs
 * first wins; the other must be a no-op.
 */

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

const COARSE = '(pointer: coarse)'
const NO_HOVER = '(hover: none)'

/**
 * Mutable, REACTIVE matchMedia stub: `useIsTouchDevice` subscribes via
 * addEventListener('change'), so flipping the answer must also notify the
 * captured listeners or the component never re-renders. (The fixed-answer stub
 * in ChatInput.resizeHandleTouch.test.tsx deliberately drops listeners; this
 * suite is ABOUT the flip, so it keeps them.)
 */
function stubReactivePointer() {
  const original = window.matchMedia
  const matches: Record<string, boolean> = { [COARSE]: false, [NO_HOVER]: false }
  const listeners = new Set<() => void>()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      get matches() { return matches[query] ?? false },
      media: query,
      addEventListener: (_: string, cb: () => void) => { listeners.add(cb) },
      removeEventListener: (_: string, cb: () => void) => { listeners.delete(cb) },
      dispatchEvent: () => false,
    }),
  })
  return {
    flipToTouch() {
      matches[COARSE] = true
      matches[NO_HOVER] = true
      listeners.forEach(cb => cb())
    },
    flipToMouse() {
      matches[COARSE] = false
      matches[NO_HOVER] = false
      listeners.forEach(cb => cb())
    },
    restore() {
      Object.defineProperty(window, 'matchMedia', {
        writable: true, configurable: true, value: original,
      })
    },
  }
}

let pointer: ReturnType<typeof stubReactivePointer>

beforeEach(() => {
  localStorage.clear()
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  pointer = stubReactivePointer()
})
afterEach(() => { pointer.restore() })

const handle = () => screen.getByTestId('composer-resize-handle')

describe('ChatInput drag teardown when the handle leaves mid-drag', () => {
  it('restores the body suppression when the handle unmounts mid-drag (component stays mounted)', () => {
    renderWithProviders(<ChatInput {...defaultProps} value="test" />)

    fireEvent.pointerDown(handle(), { clientX: 100, clientY: 200 })
    expect(document.body.style.cursor).toBe('row-resize')
    expect(document.body.style.userSelect).toBe('none')

    // Pointer type goes coarse mid-drag: the handle is swapped for the plain
    // 6px spacer while ChatInput stays mounted. No pointerup/lostpointercapture
    // can reach React once the element is detached — teardown must not depend
    // on one arriving.
    act(() => { pointer.flipToTouch() })
    expect(screen.queryByTestId('composer-resize-handle')).toBeNull()

    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    // And no phantom height commit: the drag never ended over the wrapper.
    expect(localStorage.getItem('mc-input-height')).toBeNull()
  })

  it('a drag after the handle returns starts and ends clean (no stale dragging state)', () => {
    renderWithProviders(<ChatInput {...defaultProps} value="test" />)

    // Leak scenario first, so any stale `dragging` flag from it would poison
    // the next gesture.
    fireEvent.pointerDown(handle(), { clientX: 100, clientY: 200 })
    act(() => { pointer.flipToTouch() })
    act(() => { pointer.flipToMouse() })

    // Fresh gesture on the re-mounted handle must behave like a first drag.
    fireEvent.pointerDown(handle(), { clientX: 100, clientY: 250 })
    expect(document.body.style.cursor).toBe('row-resize')
    fireEvent.pointerUp(handle(), { clientX: 100, clientY: 240 })
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    // The completed drag commits its height (jsdom measures 0 — the value is
    // irrelevant here; the WRITE is what proves onEnd ran its commit branch).
    expect(localStorage.getItem('mc-input-height')).not.toBeNull()
  })

  it('handle swap with no drag live is a no-op on body styles', () => {
    renderWithProviders(<ChatInput {...defaultProps} value="test" />)
    document.body.style.cursor = 'progress' // sentinel: teardown must not clobber others' state
    act(() => { pointer.flipToTouch() })
    expect(document.body.style.cursor).toBe('progress')
    document.body.style.cursor = ''
  })

  it('still restores the body suppression on full component unmount mid-drag', () => {
    // The old guard covered exactly this; the handle-lifecycle teardown that
    // replaces it must keep the guarantee — effect cleanups run on unmount too.
    const { unmount } = renderWithProviders(<ChatInput {...defaultProps} value="test" />)
    fireEvent.pointerDown(handle(), { clientX: 100, clientY: 200 })
    expect(document.body.style.cursor).toBe('row-resize')
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })
})
