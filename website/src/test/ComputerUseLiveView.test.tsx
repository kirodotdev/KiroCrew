import { describe, it, expect, beforeEach } from 'vitest'
import { screen, act } from '@testing-library/react'

import ComputerUseLiveView, {
  applyGrip,
  dockBox,
  fitToViewport,
} from '../components/ComputerUseLiveView'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'

/** Dispatch the window event the WS layer emits for a `computer_use_frame`. */
function pushFrame(data: string, extra: Record<string, unknown> = {}) {
  window.dispatchEvent(
    new CustomEvent('kirocrew-computer-use-frame', {
      detail: { data, format: 'jpeg', ...extra },
    }),
  )
}

function pushToggle() {
  window.dispatchEvent(new CustomEvent('kirocrew-toggle-computer-use-live'))
}

describe('ComputerUseLiveView', () => {
  beforeEach(() => {
    // The panel persists its size; clear storage so the default-size assertions
    // are deterministic and one test cannot leak dimensions into the next.
    localStorage.clear()
  })

  it('renders nothing before any frame arrives — the no-frame default', () => {
    const { container } = renderWithProviders(<ComputerUseLiveView />)
    expect(container.firstChild).toBeNull()
  })

  it('stays hidden for a frame that carries no image data', async () => {
    // A payload the gateway would never emit; the panel must not reveal an empty
    // window on a malformed event either.
    const { container } = renderWithProviders(<ComputerUseLiveView />)
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent('kirocrew-computer-use-frame', { detail: { format: 'jpeg' } }),
      )
    })
    expect(container.firstChild).toBeNull()
  })

  it('reveals at the compact size and renders the relayed frame', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    const img = (await screen.findByAltText('Live desktop view')) as HTMLImageElement
    // The media type is a literal server-side and client-side: always JPEG.
    expect(img.src).toContain('data:image/jpeg;base64,QUJD')
    const panel = screen.getByRole('dialog') as HTMLElement
    expect(panel.style.width).toBe('300px')
    expect(panel.style.height).toBe('210px')
  })

  it('shows the empty-state explanation (including the secure-field carve-out) until a frame lands', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushToggle() })
    expect(await screen.findByText(/No desktop frames yet/)).toBeInTheDocument()
    expect(screen.getByText(/password field/)).toBeInTheDocument()
    expect(screen.queryByAltText('Live desktop view')).toBeNull()
  })

  it('names the mirrored application in the header', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD', { app: 'Finder' }) })
    expect(await screen.findByText('Desktop — Finder')).toBeInTheDocument()
  })

  it('falls back to a generic header when the frame names no app', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    expect(await screen.findByText('Desktop — live')).toBeInTheDocument()
  })

  it('labels the driving session from the slot store, never from the wire', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'sess-9', title: 'Desktop triage' }] as never))
    renderWithProviders(<ComputerUseLiveView />, { store })
    await act(async () => { pushFrame('QUJD', { session_key: 'sess-9' }) })
    await screen.findByRole('dialog')
    expect(screen.getByText('· Desktop triage')).toBeInTheDocument()
  })

  it('omits the session label for an unknown session key', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD', { session_key: 'not-in-store' }) })
    await screen.findByRole('dialog')
    expect(screen.queryByText(/·/)).toBeNull()
  })

  it('exposes all eight resize grips', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    await screen.findByRole('dialog')
    expect(screen.getAllByRole('separator')).toHaveLength(8)
    expect(screen.getByLabelText('Resize live desktop view (top left)')).toBeInTheDocument()
    expect(screen.getByLabelText('Resize live desktop view (bottom right)')).toBeInTheDocument()
  })

  it('swaps between the roomy preset and the compact default from the header', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    const panel = () => screen.getByRole('dialog') as HTMLElement
    expect(panel().style.width).toBe('300px')
    const enlarge = await screen.findByLabelText('Enlarge live desktop view')
    await act(async () => { enlarge.click() })
    expect(panel().style.width).toBe('900px')
    expect(panel().style.height).toBe('620px')
    const shrink = await screen.findByLabelText('Shrink live desktop view')
    await act(async () => { shrink.click() })
    expect(panel().style.width).toBe('300px')
    expect(panel().style.height).toBe('210px')
  })

  it('restores a persisted size', async () => {
    localStorage.setItem('mc-computer-mirror-box', JSON.stringify({ width: 520, height: 380 }))
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    const panel = screen.getByRole('dialog') as HTMLElement
    expect(panel.style.width).toBe('520px')
    expect(panel.style.height).toBe('380px')
  })

  it('minimizes to a corner chip and re-opens from it', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    const minimize = await screen.findByLabelText('Minimize live desktop view to corner')
    await act(async () => { minimize.click() })
    expect(screen.queryByRole('dialog')).toBeNull()
    const chip = await screen.findByLabelText('Show live desktop view')
    await act(async () => { chip.click() })
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('keeps a minimized panel collapsed when later frames arrive', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD') })
    const minimize = await screen.findByLabelText('Minimize live desktop view to corner')
    await act(async () => { minimize.click() })
    await act(async () => { pushFrame('WFla') })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByLabelText('Show live desktop view')).toBeInTheDocument()
  })

  it('closes fully — the close affordance leaves no panel and no chip', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD', { session_key: 'sess-1' }) })
    const close = await screen.findByLabelText('Close live desktop view')
    await act(async () => { close.click() })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByLabelText('Show live desktop view')).toBeNull()
  })

  it('stays closed while the dismissed session keeps sending frames', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD', { session_key: 'sess-1' }) })
    const close = await screen.findByLabelText('Close live desktop view')
    await act(async () => { close.click() })
    await act(async () => { pushFrame('WFla', { session_key: 'sess-1' }) })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByLabelText('Show live desktop view')).toBeNull()
  })

  it('re-opens when a different session starts driving after a close', async () => {
    renderWithProviders(<ComputerUseLiveView />)
    await act(async () => { pushFrame('QUJD', { session_key: 'sess-1' }) })
    const close = await screen.findByLabelText('Close live desktop view')
    await act(async () => { close.click() })
    await act(async () => { pushFrame('WFla', { session_key: 'sess-2' }) })
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})

describe('ComputerUseLiveView geometry', () => {
  // jsdom viewport is 1024x768; EDGE_GAP=16, MIN_WIDTH=200, MIN_HEIGHT=140.

  it('dockBox parks the compact panel in the bottom-left corner', () => {
    // Bottom-LEFT (not bottom-right) so the panel never stacks on the browse mirror.
    expect(dockBox({ width: 300, height: 210 })).toEqual({
      left: 16, top: 542, width: 300, height: 210,
    })
  })

  it('fitToViewport leaves an in-bounds box untouched', () => {
    const box = { left: 100, top: 100, width: 300, height: 210 }
    expect(fitToViewport(box)).toEqual(box)
  })

  it('fitToViewport slides an off-screen box back into view', () => {
    expect(fitToViewport({ left: 5000, top: 5000, width: 300, height: 210 })).toEqual({
      left: 708, top: 542, width: 300, height: 210,
    })
  })

  it('fitToViewport shrinks an oversized box, keeping a gap on every edge', () => {
    expect(fitToViewport({ left: 0, top: 0, width: 9999, height: 9999 })).toEqual({
      left: 16, top: 16, width: 992, height: 736,
    })
  })

  it('applyGrip east grows the width and pins the west edge', () => {
    expect(applyGrip({ left: 100, top: 100, width: 300, height: 210 }, 'e', 60, 0)).toEqual({
      left: 100, top: 100, width: 360, height: 210,
    })
  })

  it('applyGrip west moves the left edge and pins the east edge', () => {
    const out = applyGrip({ left: 100, top: 100, width: 300, height: 210 }, 'w', -50, 0)
    expect(out).toEqual({ left: 50, top: 100, width: 350, height: 210 })
    expect(out.left + out.width).toBe(400) // east edge unmoved
  })

  it('applyGrip refuses to invert the box, holding the minimum width', () => {
    const out = applyGrip({ left: 100, top: 100, width: 300, height: 210 }, 'w', 900, 0)
    expect(out.width).toBe(200) // MIN_WIDTH
    expect(out.left + out.width).toBe(400) // east edge still pinned
  })

  it('applyGrip north moves the top edge and pins the bottom edge', () => {
    const out = applyGrip({ left: 100, top: 200, width: 300, height: 210 }, 'n', 0, -60)
    expect(out).toEqual({ left: 100, top: 140, width: 300, height: 270 })
    expect(out.top + out.height).toBe(410)
  })

  it('applyGrip on a corner drives both axes at once', () => {
    expect(applyGrip({ left: 100, top: 100, width: 300, height: 210 }, 'se', 40, 30)).toEqual({
      left: 100, top: 100, width: 340, height: 240,
    })
  })

  it('applyGrip clamps a resize to the viewport gap', () => {
    // Dragging the east grip far right stops at vw - EDGE_GAP = 1008.
    const out = applyGrip({ left: 100, top: 100, width: 300, height: 210 }, 'e', 5000, 0)
    expect(out.left + out.width).toBe(1008)
  })
})
