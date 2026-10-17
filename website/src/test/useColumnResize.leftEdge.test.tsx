// A right-side rail puts its resize grip on the column's LEFT edge, so the
// drag direction flips: dragging LEFT grows the column. `useColumnResize`
// absorbs that with `edge: 'left'` instead of each such rail hand-rolling the
// same pointer block with a negated delta. These tests pin the flip end to
// end — pointer drag, clamping, persistence, and the keyboard nudge, which
// must follow the GRIP's direction so arrows match the pointer.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import { useColumnResize } from '../hooks/useColumnResize'
import ResizeHandle from '../components/ResizeHandle'

const WIDTH_KEY = 'kc:test:left-edge-col-width'
const MIN = 300
const MAX = 520
const START = 400

const loadWidth = () => {
  const raw = Number(localStorage.getItem(WIDTH_KEY))
  return Number.isFinite(raw) && raw > 0 ? Math.min(MAX, Math.max(MIN, raw)) : START
}

function Harness({ edge }: { edge: 'right' | 'left' }) {
  const col = useColumnResize(WIDTH_KEY, loadWidth, MIN, MAX, undefined, undefined, edge)
  return (
    <div>
      <aside data-testid="col" style={{ width: col.width }} />
      <ResizeHandle handleProps={col.handleProps} label="Resize rail" onNudge={col.nudge} />
    </div>
  )
}

function drag(handle: HTMLElement, from: number, to: number, id = 1) {
  fireEvent.pointerDown(handle, { clientX: from, pointerId: id })
  fireEvent.pointerMove(handle, { clientX: to, pointerId: id })
  fireEvent.pointerUp(handle, { clientX: to, pointerId: id })
}

const widthOf = (el: HTMLElement) => el.style.width

describe('useColumnResize with a left-edge grip', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(WIDTH_KEY, String(START))
  })

  it('a leftward drag GROWS the column and persists the result', () => {
    const { getByTestId, getByRole } = render(<Harness edge="left" />)
    drag(getByRole('separator'), 500, 440)
    expect(widthOf(getByTestId('col'))).toBe(`${START + 60}px`)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(START + 60))
  })

  it('a rightward drag SHRINKS the column', () => {
    const { getByTestId, getByRole } = render(<Harness edge="left" />)
    drag(getByRole('separator'), 500, 560)
    expect(widthOf(getByTestId('col'))).toBe(`${START - 60}px`)
  })

  it('clamps to [min, max] in the flipped directions', () => {
    const { getByTestId, getByRole } = render(<Harness edge="left" />)
    drag(getByRole('separator'), 500, -5000)
    expect(widthOf(getByTestId('col'))).toBe(`${MAX}px`)
    drag(getByRole('separator'), 500, 9000, 2)
    expect(widthOf(getByTestId('col'))).toBe(`${MIN}px`)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(MIN))
  })

  it('arrow keys follow the grip direction: ArrowLeft grows, ArrowRight shrinks', () => {
    const { getByTestId, getByRole } = render(<Harness edge="left" />)
    const handle = getByRole('separator')
    fireEvent.keyDown(handle, { key: 'ArrowLeft' })
    expect(widthOf(getByTestId('col'))).toBe(`${START + 16}px`)
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(widthOf(getByTestId('col'))).toBe(`${START - 16}px`)
  })

  it('the default right edge is unchanged: dragging right grows', () => {
    const { getByTestId, getByRole } = render(<Harness edge="right" />)
    drag(getByRole('separator'), 500, 560)
    expect(widthOf(getByTestId('col'))).toBe(`${START + 60}px`)
    expect(localStorage.getItem(WIDTH_KEY)).toBe(String(START + 60))
  })
})
