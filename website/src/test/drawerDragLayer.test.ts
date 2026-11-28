/**
 * A finger drag gives the panel and its scrim their own compositor layers, and
 * the settle's arrival takes them away again.
 *
 * Without the layer, every touchmove's transform write repaints the panel and
 * the page under it; a phone rasterizes that on the main thread and the drag
 * drops to half the frame rate the settle runs at. The layer must not outlive
 * the drag either: the app shell's nav drawer stays mounted at rest.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

vi.mock('framer-motion', async (importOriginal) => ({
  ...(await importOriginal<typeof import('framer-motion')>()),
  // Arrive at once, so the release's settle reports its completion in-test.
  animate: (_x: unknown, _to: unknown, opts: { onComplete?: () => void }) => {
    opts.onComplete?.()
    return { stop: vi.fn() }
  },
}))

const { motionValue } = await import('framer-motion')
const { useDrawerSwipe, registerDrawerTargets } = await import('../hooks/useDrawerSwipe')

function touch(type: string, clientX: number, timeStamp: number): TouchEvent {
  const t = { clientX, clientY: 0 } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  else init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  Object.defineProperty(e, 'timeStamp', { value: timeStamp })
  return e
}

describe('useDrawerSwipe — compositor layer for the drag', () => {
  let root: HTMLDivElement
  let panel: HTMLDivElement
  let scrim: HTMLDivElement
  let mounted: boolean

  beforeEach(() => {
    root = document.createElement('div')
    panel = document.createElement('div')
    scrim = document.createElement('div')
    document.body.append(root, panel, scrim)
    mounted = false
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  function bind() {
    const x = motionValue(0)
    registerDrawerTargets(x, {
      panel: () => (mounted ? panel : null),
      scrim: () => (mounted ? scrim : null),
      travel: () => 400,
    })
    renderHook(() => useDrawerSwipe({ current: root }, {
      enabled: true, open: false, x,
      // The opening drag mounts the panel a beat after the gesture locks.
      onGestureOpen: () => { mounted = true },
      onSettle: vi.fn(),
    }))
  }

  it('layers the panel and scrim while the finger drives them', () => {
    bind()
    act(() => { root.dispatchEvent(touch('touchstart', 200, 0)) })
    expect(panel.style.willChange).toBe('')
    act(() => {
      root.dispatchEvent(touch('touchmove', 240, 16))
      root.dispatchEvent(touch('touchmove', 260, 32))
    })
    expect(panel.style.willChange).toBe('transform')
    expect(scrim.style.willChange).toBe('opacity')
  })

  it('drops both layers once the release has settled', () => {
    bind()
    act(() => {
      root.dispatchEvent(touch('touchstart', 200, 0))
      root.dispatchEvent(touch('touchmove', 240, 16))
      root.dispatchEvent(touch('touchmove', 300, 32))
      root.dispatchEvent(touch('touchend', 300, 48))
    })
    expect(panel.style.willChange).toBe('')
    expect(scrim.style.willChange).toBe('')
  })
})
