/**
 * The lane scrolls its recorded row back into place when it comes back on
 * screen without remounting.
 *
 * The desktop sessions sidebar stays mounted while closed and is taken out of
 * the document instead (SessionsSidebarChrome). Taking an element out of the
 * document drops its scroll offset, so the restore must run again when the
 * lane reappears, not only on mount.
 *
 * happy-dom has no layout: rows report a rect derived from a fixed height and
 * the lane's scrollTop, and the ResizeObserver is driven by hand.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { useRef } from 'react'
import { render, fireEvent, act } from '@testing-library/react'
import { _resetLaneScrollMemory, useLaneScrollMemory } from '../hooks/useLaneScrollMemory'

const ROW_H = 50
const observers: { cb: ResizeObserverCallback; fire: () => void }[] = []

class ManualResizeObserver {
  cb: ResizeObserverCallback
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb
    observers.push({ cb, fire: () => cb([], this as unknown as ResizeObserver) })
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

function Lane() {
  const ref = useRef<HTMLDivElement>(null)
  const { onScroll } = useLaneScrollMemory('tree', ref)
  return (
    <div data-testid="lane" ref={ref} onScroll={onScroll}>
      {Array.from({ length: 40 }, (_, i) => <div key={i} data-session-row={`chat-${i}`} />)}
    </div>
  )
}

describe('useLaneScrollMemory — the lane comes back on screen', () => {
  let shownHeight = 300
  beforeEach(() => {
    _resetLaneScrollMemory()
    observers.length = 0
    shownHeight = 300
    vi.stubGlobal('ResizeObserver', ManualResizeObserver)
    // Run animation frames at once, so the restore's corrections land inside act().
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
    vi.stubGlobal('cancelAnimationFrame', () => {})
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('restores the recorded top row after the lane leaves the document and returns', () => {
    const { getByTestId } = render(<Lane />)
    const lane = getByTestId('lane') as HTMLDivElement
    Object.defineProperty(lane, 'clientHeight', { configurable: true, get: () => shownHeight })
    lane.getBoundingClientRect = () => ({ top: 0, bottom: shownHeight } as DOMRect)
    lane.querySelectorAll<HTMLElement>('[data-session-row]').forEach((row, i) => {
      row.getBoundingClientRect = () => {
        const top = i * ROW_H - lane.scrollTop
        return { top, bottom: top + ROW_H } as DOMRect
      }
    })

    // The person scrolls so row 12 sits at the top; the lane records it.
    act(() => { lane.scrollTop = 12 * ROW_H; fireEvent.scroll(lane) })

    // Taken out of the document: the browser resets the offset and the
    // observer reports a lane with no size.
    act(() => { shownHeight = 0; lane.scrollTop = 0; observers.forEach(o => o.fire()) })
    expect(lane.scrollTop).toBe(0)

    // Back on screen.
    act(() => { shownHeight = 300; observers.forEach(o => o.fire()) })
    expect(lane.scrollTop).toBe(12 * ROW_H)
  })
})
