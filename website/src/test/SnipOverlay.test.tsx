import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const fakeFile = new File(['x'], 'snip-1.png', { type: 'image/png' })

// Canvas pixel ops can't run in jsdom — stub crop/encode, keep normalizeRect real.
vi.mock('../hooks/useScreenSnip', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return {
    ...actual,
    cropCanvas: vi.fn(() => ({}) as HTMLCanvasElement),
    canvasToFile: vi.fn(async () => fakeFile),
  }
})

import SnipOverlay from '../components/SnipOverlay'
import { canvasToFile } from '../hooks/useScreenSnip'

const frame = {
  width: 400,
  height: 200,
  toDataURL: () => 'data:image/png;base64,AAAA',
} as unknown as HTMLCanvasElement

function setup() {
  const onComplete = vi.fn()
  const onCancel = vi.fn()
  const onError = vi.fn()
  render(<SnipOverlay frame={frame} onComplete={onComplete} onCancel={onCancel} onError={onError} />)
  const surface = screen.getByTestId('snip-surface')
  surface.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 200, height: 100, right: 200, bottom: 100, x: 0, y: 0, toJSON() {} }) as DOMRect
  return { onComplete, onCancel, onError, surface }
}

function drag(surface: HTMLElement, x1: number, y1: number, x2: number, y2: number) {
  fireEvent.mouseDown(surface, { clientX: x1, clientY: y1 })
  fireEvent.mouseMove(surface, { clientX: x2, clientY: y2 })
  fireEvent.mouseUp(surface, { clientX: x2, clientY: y2 })
}

beforeEach(() => vi.clearAllMocks())

describe('SnipOverlay', () => {
  it('renders an accessible crop dialog', () => {
    setup()
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveAccessibleName(/crop/i)
  })

  it('cancels on Escape', () => {
    const { onCancel } = setup()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('cancels when the Cancel button is clicked', () => {
    const { onCancel } = setup()
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('has no "Use selection" button (capture is automatic on release)', () => {
    setup()
    expect(screen.queryByRole('button', { name: /use selection/i })).toBeNull()
  })

  it('captures immediately on mouse-up after a drag — no confirm click', async () => {
    const { onComplete, surface } = setup()
    drag(surface, 10, 10, 60, 40)
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(fakeFile))
  })

  it('does not capture on a click with no drag (below threshold)', () => {
    const { onComplete, surface } = setup()
    fireEvent.mouseDown(surface, { clientX: 20, clientY: 20 })
    fireEvent.mouseUp(surface, { clientX: 20, clientY: 20 })
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('ignores a tiny sub-threshold drag (stray click jitter)', () => {
    const { onComplete, surface } = setup()
    drag(surface, 20, 20, 23, 22)
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('captures even when the mouse is released outside the surface', async () => {
    const { onComplete, surface } = setup()
    fireEvent.mouseDown(surface, { clientX: 10, clientY: 10 })
    fireEvent.mouseMove(surface, { clientX: 60, clientY: 40 })
    fireEvent.mouseUp(window) // released off the image — must still capture
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith(fakeFile))
  })

  it('closes via onCancel when encoding the crop fails', async () => {
    vi.mocked(canvasToFile).mockRejectedValueOnce(new Error('encode failed'))
    const { onCancel, surface } = setup()
    drag(surface, 10, 10, 60, 40)
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1))
  })

  it('surfaces a message via onError when encoding fails', async () => {
    vi.mocked(canvasToFile).mockRejectedValueOnce(new Error('encode failed'))
    const { onError, surface } = setup()
    drag(surface, 10, 10, 60, 40)
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.any(String)))
  })

  it('computes the frame data URL once (memoized) across drag re-renders', () => {
    const toDataURL = vi.fn(() => 'data:image/png;base64,AAAA')
    const memoFrame = { width: 400, height: 200, toDataURL } as unknown as HTMLCanvasElement
    render(<SnipOverlay frame={memoFrame} onComplete={vi.fn()} onCancel={vi.fn()} onError={vi.fn()} />)
    const surface = screen.getByTestId('snip-surface')
    surface.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100, right: 200, bottom: 100, x: 0, y: 0, toJSON() {} }) as DOMRect
    fireEvent.mouseDown(surface, { clientX: 10, clientY: 10 })
    fireEvent.mouseMove(surface, { clientX: 30, clientY: 25 })
    fireEvent.mouseMove(surface, { clientX: 60, clientY: 40 })
    expect(toDataURL).toHaveBeenCalledTimes(1)
  })
})
