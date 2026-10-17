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

type HoldRelease = (e: PointerEvent, target: HTMLElement) => void

function Harness({ onHoldRelease }: { onHoldRelease?: HoldRelease }) {
  const r = useLongPressReorder({ onHoldRelease })
  captured = r
  return <div data-testid="chip" data-dragging={r.dragging} onPointerDown={r.itemProps.onPointerDown} />
}

function mount(onHoldRelease?: HoldRelease) {
  const utils = render(<Harness onHoldRelease={onHoldRelease} />)
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

  // A middle- or right-button press must not arm a drag: there is no middle-
  // or right-drag gesture, so a non-primary press has nothing to reorder. This
  // was also a suspect for the side-panel middle-click-to-close report, but a
  // real-browser test ruled it out (with the guard removed, a middle-click
  // still closed the tab), so this test pins correctness, not that symptom.
  // Only the primary button reorders.
  it('does not arm a drag for a non-primary mouse button', () => {
    const { chip, start } = mount()
    fireEvent.pointerDown(chip, { pointerType: 'mouse', button: 1, clientX: 10, clientY: 10 })
    expect(start).not.toHaveBeenCalled()
    expect(captured!.dragging).toBe(false)

    fireEvent.pointerDown(chip, { pointerType: 'mouse', button: 2, clientX: 10, clientY: 10 })
    expect(start).not.toHaveBeenCalled()

    // The primary button on the same element still reorders.
    fireEvent.pointerDown(chip, { pointerType: 'mouse', button: 0, clientX: 10, clientY: 10 })
    expect(start).toHaveBeenCalledTimes(1)
    expect(captured!.dragging).toBe(true)
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

  // One hold, two outcomes. The arm itself is unchanged by the option: the
  // drag is live from the 450ms mark either way, and only what the finger does
  // next tells a reorder from a menu.
  it('fires the hold-release action when the finger lifts in place after arming', () => {
    vi.useFakeTimers()
    const onHoldRelease = vi.fn()
    const { chip, start } = mount(onHoldRelease)
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    expect(start).toHaveBeenCalledTimes(1)
    expect(captured!.dragging).toBe(true)
    expect(onHoldRelease).not.toHaveBeenCalled()

    fireEvent.pointerUp(window, { clientX: 12, clientY: 11 })
    expect(onHoldRelease).toHaveBeenCalledTimes(1)
    expect(onHoldRelease.mock.calls[0][1]).toBe(chip)
    expect(captured!.dragging).toBe(false)
  })

  it('does not fire the hold-release action once the armed finger has travelled', () => {
    vi.useFakeTimers()
    const onHoldRelease = vi.fn()
    const { chip, start } = mount(onHoldRelease)
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    expect(start).toHaveBeenCalledTimes(1)

    fireEvent.pointerMove(window, { clientX: 10 + LONG_PRESS_SLOP_PX + 1, clientY: 10 })
    fireEvent.pointerUp(window)
    expect(onHoldRelease).not.toHaveBeenCalled()
    expect(captured!.dragging).toBe(false)
  })

  // Lifting before the arm is a tap, and a tap is not a hold-release.
  it('does not fire the hold-release action for a tap', () => {
    vi.useFakeTimers()
    const onHoldRelease = vi.fn()
    const { chip } = mount(onHoldRelease)
    fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS - 1) })
    fireEvent.pointerUp(window)
    act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
    expect(onHoldRelease).not.toHaveBeenCalled()
  })

  // A Radix ContextMenuTrigger around the chip arms its own 700ms touch timer
  // and skips it when the press arrives default-prevented. Only a touch press
  // with a hold-release owner is prevented: a mouse press is not (framer needs
  // its compat events elsewhere) and neither is a touch press on a chip with
  // no menu.
  it('default-prevents the touch press only when it owns a hold-release', () => {
    const owned = mount(vi.fn())
    expect(fireEvent.pointerDown(owned.chip, { pointerType: 'touch', clientX: 10, clientY: 10 })).toBe(false)
    fireEvent.pointerUp(window)
    expect(fireEvent.pointerDown(owned.chip, { pointerType: 'mouse', clientX: 10, clientY: 10 })).toBe(true)
    fireEvent.pointerUp(window)
    owned.unmount()

    const bare = mount()
    expect(fireEvent.pointerDown(bare.chip, { pointerType: 'touch', clientX: 10, clientY: 10 })).toBe(true)
  })

  // Android raises a native `contextmenu` on a long press and cancels the touch
  // unless it is prevented — which would open the menu mid-arm AND kill the
  // drag. While the touch is down the chip swallows it; once the finger is up
  // the chip's own opener must get through.
  it('swallows a native contextmenu for exactly as long as the touch is down', () => {
    vi.useFakeTimers()
    const seenAtDocument = vi.fn()
    document.addEventListener('contextmenu', seenAtDocument)
    try {
      const { chip } = mount(vi.fn())
      fireEvent.pointerDown(chip, { pointerType: 'touch', clientX: 10, clientY: 10 })
      // Pending phase.
      expect(fireEvent.contextMenu(chip)).toBe(false)
      expect(seenAtDocument).not.toHaveBeenCalled()
      // Armed phase.
      act(() => { vi.advanceTimersByTime(LONG_PRESS_MS) })
      expect(fireEvent.contextMenu(chip)).toBe(false)
      expect(seenAtDocument).not.toHaveBeenCalled()
      // Released.
      fireEvent.pointerUp(window)
      expect(fireEvent.contextMenu(chip)).toBe(true)
      expect(seenAtDocument).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('contextmenu', seenAtDocument)
    }
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
