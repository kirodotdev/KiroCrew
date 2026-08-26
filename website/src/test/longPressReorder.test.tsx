import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { render, fireEvent, act } from '@testing-library/react'
import { useLongPressReorder, LONG_PRESS_MS, LONG_PRESS_SLOP_PX } from '../hooks/useLongPressReorder'

/**
 * The tab strips (chat side panel, bottom terminal dock) are horizontal
 * scrollers whose chips are also reorderable. framer's own drag listener sets
 * `touch-action: pan-y`, which forbids the browser from panning along the drag
 * axis — so a touch swipe reordered the tabs instead of scrolling to the ones
 * past the edge. These pin the split: touch arms a drag only after a stationary
 * hold, a precise pointer still starts one on press.
 */

let captured: ReturnType<typeof useLongPressReorder> | null = null

function Harness() {
  const r = useLongPressReorder()
  captured = r
  return <div data-testid="chip" data-dragging={r.dragging} onPointerDown={r.itemProps.onPointerDown} />
}

function mount() {
  const utils = render(<Harness />)
  const chip = utils.getByTestId('chip')
  const start = vi.spyOn(captured!.itemProps.dragControls, 'start')
  return { ...utils, chip, start }
}

afterEach(() => {
  captured = null
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useLongPressReorder', () => {
  it('never lets framer own the pointer', () => {
    mount()
    // The whole mechanism: with `dragListener` true framer would apply
    // `touch-action: pan-y` and the strip could not be scrolled by touch.
    expect(captured!.itemProps.dragListener).toBe(false)
    expect(captured!.itemProps.style.userSelect).toBe('none')
    expect(captured!.itemProps.draggable).toBe(false)
  })

  it('starts a drag immediately for mouse and pen', () => {
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'mouse', clientX: 10, clientY: 10 })
    expect(start).toHaveBeenCalledTimes(1)
    expect(captured!.dragging).toBe(true)

    fireEvent.pointerUp(window)
    fireEvent.pointerDown(chip, { pointerType: 'pen', clientX: 10, clientY: 10 })
    expect(start).toHaveBeenCalledTimes(2)
  })

  it('arms a touch drag only after a stationary hold', () => {
    vi.useFakeTimers()
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    expect(start).not.toHaveBeenCalled()

    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS - 1) })
    expect(start).not.toHaveBeenCalled()

    act(() => { vi.advanceTimersByTime(1) })
    expect(start).toHaveBeenCalledTimes(1)
    expect(captured!.dragging).toBe(true)
  })

  it('cancels the pending arm once the finger travels — the swipe is a scroll', () => {
    vi.useFakeTimers()
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    fireEvent.pointerMove(window, { clientX: 10 + LONG_PRESS_SLOP_PX + 1, clientY: 10 })

    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS * 2) })
    expect(start).not.toHaveBeenCalled()
    expect(captured!.dragging).toBe(false)
  })

  it('keeps the arm through jitter inside the slop', () => {
    vi.useFakeTimers()
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    fireEvent.pointerMove(window, { clientX: 10 + LONG_PRESS_SLOP_PX, clientY: 10 })

    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    expect(start).toHaveBeenCalledTimes(1)
  })

  it('cancels the pending arm when the finger lifts early — a tap is a tap', () => {
    vi.useFakeTimers()
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    fireEvent.pointerUp(window)

    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS * 2) })
    expect(start).not.toHaveBeenCalled()
  })

  it('blocks native panning only while a drag is live', () => {
    const { chip } = mount()
    const touchmove = () => {
      const e = new Event('touchmove', { bubbles: true, cancelable: true })
      document.dispatchEvent(e)
      return e.defaultPrevented
    }
    // Before: the browser owns the gesture, so the strip scrolls.
    expect(touchmove()).toBe(false)

    fireEvent.pointerDown(chip, { pointerType: 'mouse', clientX: 10, clientY: 10 })
    // During: `touch-action` is read when the touch starts and cannot be
    // changed mid-gesture, so preventDefault is the only way to stop the pan.
    expect(touchmove()).toBe(true)

    fireEvent.pointerUp(window)
    expect(touchmove()).toBe(false)
  })

  it('drops its listeners on unmount', () => {
    vi.useFakeTimers()
    const { chip, start, unmount } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    unmount()

    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS * 2) })
    expect(start).not.toHaveBeenCalled()
  })
})

describe('Reorder.Item call sites', () => {
  // The defect is a framer DEFAULT, so a new strip written the obvious way
  // reintroduces it. Every call site must go through the hook.
  it('every file rendering a Reorder.Item uses the hook', () => {
    const root = join(__dirname, '..')
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const p = join(dir, entry.name)
        if (entry.isDirectory()) {
          if (entry.name === 'node_modules' || entry.name === 'test') continue
          walk(p)
        } else if (/\.tsx$/.test(entry.name)) {
          const src = readFileSync(p, 'utf8')
          if (src.includes('<Reorder.Item') && !src.includes('useLongPressReorder')) offenders.push(p)
        }
      }
    }
    walk(root)
    expect(offenders).toEqual([])
  })
})
