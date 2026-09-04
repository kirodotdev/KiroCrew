/**
 * usePointerDrag — a drag must terminate on EVERY capture-ending path.
 *
 * The hook's contract is capture-based dragging (setPointerCapture on
 * pointer-down, per its own doc comment "survives the pointer leaving the
 * element bounds (capture)"). The Pointer Events spec delivers
 * `lostpointercapture` as the terminal event when capture ends for any
 * reason: explicit release, a capture steal by another element, or a
 * browser-initiated cancellation. If the hook ignores it, a drag whose
 * capture dies mid-gesture never fires onEnd, and consumer onStart side
 * effects stay stuck while the component remains mounted — unmount guards
 * never run. Several resizer consumers set `document.body.style.userSelect =
 * 'none'` in onStart (one stranded drag makes the whole page unselectable
 * until some later drag happens to clean it up, #8271's symptom); others
 * pin body.cursor page-wide or strand teardown-critical dragging flags.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import React from 'react'
import { usePointerDrag, type PointerDragOptions } from './usePointerDrag'

function Handle(props: PointerDragOptions) {
  const drag = usePointerDrag(props)
  return <div data-testid="drag-handle" {...drag} />
}

function renderHandle(props: PointerDragOptions) {
  const utils = render(<Handle {...props} />)
  return { ...utils, handle: utils.getByTestId('drag-handle') }
}

describe('usePointerDrag capture-loss termination', () => {
  it('fires onEnd when pointer capture is lost mid-drag (the stuck-drag path)', () => {
    const onStart = vi.fn()
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onStart, onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    expect(onStart).toHaveBeenCalledTimes(1)

    // Capture dies without a pointerup/pointercancel ever reaching the
    // element (capture steal / browser cancellation). lostpointercapture is
    // the only notification the element gets.
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('reports `committed` on a capture-loss end exactly like a normal end', () => {
    const onEnd = vi.fn()
    // threshold 0 commits immediately on pointer-down.
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({ committed: true })
  })

  it('normal pointerup still ends the drag exactly once', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12, clientY: 12 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('does not double-fire onEnd when lostpointercapture follows a normal release', () => {
    // Browsers fire lostpointercapture after EVERY capture release, including
    // the releasePointerCapture the hook itself performs in a normal end —
    // the end path must stay idempotent per drag.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 12, clientY: 12 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })

  it('ignores a stray lostpointercapture with no active drag', () => {
    const onStart = vi.fn()
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onStart, onMove: () => {}, onEnd })

    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onStart).not.toHaveBeenCalled()
    expect(onEnd).not.toHaveBeenCalled()
  })

  it('a new drag after a capture-loss end works from a clean slate', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })
    expect(onEnd).toHaveBeenCalledTimes(1)

    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 30, clientY: 30 })
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 35, clientY: 30 })
    expect(onEnd).toHaveBeenCalledTimes(2)
  })

  it('ends a capture-loss drag from the last moved-to position, not the event coordinates', () => {
    // The Pointer Events spec does not define pointer coordinates on
    // lostpointercapture; browsers commonly deliver 0,0. If the end payload
    // trusted them, dx would resolve to ≈ -startX and consumers that
    // persist(apply(sign * dx)) in onEnd would commit a clamped/collapsed
    // layout to storage — a persisted wrong layout on the rare path.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 150, clientY: 110 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1, clientX: 0, clientY: 0 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({
      dx: 50, dy: 10, x: 150, y: 110, committed: true,
    })
  })

  it('a normal pointerup still ends from its own (real) coordinates', () => {
    // Control: up/cancel coordinates are spec-defined — they stay
    // authoritative and refresh the tracked position.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 150, clientY: 100 })
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 160, clientY: 105 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({ dx: 60, dy: 5, x: 160, y: 105 })
  })

  it('a capture loss before any move ends at the drag origin (dx 0), not at 0,0', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1, clientX: 0, clientY: 0 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({
      dx: 0, dy: 0, x: 100, y: 100, committed: false,
    })
  })
})

describe('usePointerDrag pointercancel sentinel coordinates', () => {
  // pointercancel is platform-fired (touch scroll takeover, pen leaving range,
  // palm rejection) — the user never chose its position. Pointer Events L3
  // requires its coordinates to match the last dispatched pointer event, but
  // engines have shipped pointercancel with 0,0 (the spec carries an explicit
  // late "clarification about pointercancel coordinates" because behavior
  // diverged). Trusting the event's coordinates is therefore at best equal to
  // the hook's own tracker and at worst a dx of ≈ -startX that resizers
  // persist to storage. The end payload must derive from the tracker.

  it('a pointercancel with sentinel 0,0 coordinates ends from the last tracked position', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 150, clientY: 110 })
    // Legacy-engine shape: cancel delivered with default-initialized coords.
    fireEvent.pointerCancel(handle, { pointerId: 1, clientX: 0, clientY: 0 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({
      dx: 50, dy: 10, x: 150, y: 110, committed: true,
    })
  })

  it('a pointercancel before any move ends at the drag origin (dx 0), not at 0,0', () => {
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerCancel(handle, { pointerId: 1, clientX: 0, clientY: 0 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({
      dx: 0, dy: 0, x: 100, y: 100, committed: false,
    })
  })

  it('a spec-conformant pointercancel (coordinates match the last dispatched event) ends identically', () => {
    // Control: on an engine following Pointer Events L3 §4.2.7 the cancel
    // carries the last dispatched coordinates — exactly what the tracker
    // holds — so deriving from the tracker changes nothing.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd, threshold: 0 })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 150, clientY: 110 })
    fireEvent.pointerCancel(handle, { pointerId: 1, clientX: 150, clientY: 110 })

    expect(onEnd).toHaveBeenCalledTimes(1)
    expect(onEnd.mock.calls[0][0]).toMatchObject({
      dx: 50, dy: 10, x: 150, y: 110, committed: true,
    })
  })

  it('does not double-fire onEnd when lostpointercapture follows a pointercancel', () => {
    // The spec's implicit-release steps fire lostpointercapture immediately
    // after pointercancel for a captured pointer — the same idempotence
    // contract as the pointerup twin above.
    const onEnd = vi.fn()
    const { handle } = renderHandle({ onMove: () => {}, onEnd })

    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 10, clientY: 10 })
    fireEvent.pointerCancel(handle, { pointerId: 1, clientX: 0, clientY: 0 })
    fireEvent.lostPointerCapture(handle, { pointerId: 1 })

    expect(onEnd).toHaveBeenCalledTimes(1)
  })
})
