import { describe, it, expect, beforeEach } from 'vitest'
import { screen, act } from '@testing-library/react'

import BrowserLiveView, { clampRect, cornerRect, resizeRect } from '../components/BrowserLiveView'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'

function frameEvent(data: string, sessionKey?: string) {
  return new CustomEvent('kirocrew-browser-frame', {
    detail: { data, format: 'jpeg', ...(sessionKey ? { session_key: sessionKey } : {}) },
  })
}
function toggle() {
  return new CustomEvent('kirocrew-toggle-browser-live')
}

describe('BrowserLiveView', () => {
  beforeEach(() => {
    // The panel persists its size to localStorage; clear it so the default-size
    // assertion is deterministic and tests don't leak dims into each other.
    localStorage.clear()
    // jsdom doesn't implement pointer capture; stub it so the move/resize
    // pointer-down handlers don't throw when they grab the pointer.
    Element.prototype.setPointerCapture = () => {}
    Element.prototype.releasePointerCapture = () => {}
  })

  it('renders nothing until a frame or toggle', () => {
    const { container } = renderWithProviders(<BrowserLiveView />)
    expect(container.firstChild).toBeNull()
  })

  it('auto-opens at the default small size and renders the frame on first browser_frame', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const img = await screen.findByAltText('Live browser session') as HTMLImageElement
    expect(img.src).toContain('data:image/jpeg;base64,QUJD')
    // default => 260x180, an unobtrusive corner thumbnail the user can drag larger
    const dialog = screen.getByRole('dialog') as HTMLElement
    expect(dialog.style.width).toBe('260px')
    expect(dialog.style.height).toBe('180px')
  })

  it('labels the panel with the mirrored session name resolved from the slot store', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'sess-1', title: 'Pipeline triage' }] as never))
    renderWithProviders(<BrowserLiveView />, { store })
    await act(async () => { window.dispatchEvent(frameEvent('QUJD', 'sess-1')) })
    await screen.findByRole('dialog')
    expect(screen.getByText('· Pipeline triage')).toBeInTheDocument()
  })

  it('omits the session label when the frame carries an unknown/absent key', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD', 'ghost')) })
    await screen.findByRole('dialog')
    expect(screen.queryByText(/·/)).toBeNull()
  })

  it('exposes resize handles on every edge and corner', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    await screen.findByRole('dialog')
    // four edges + four corners, each a role="separator" resize handle
    expect(screen.getAllByRole('separator')).toHaveLength(8)
    expect(screen.getByLabelText('Resize live browser view (top-left)')).toBeInTheDocument()
    expect(screen.getByLabelText('Resize live browser view (bottom-right)')).toBeInTheDocument()
  })

  it('toggles between the large preset and the compact default via the header button', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const d = () => screen.getByRole('dialog') as HTMLElement
    expect(d().style.width).toBe('260px')
    // one-click expand -> large preset (clamped to jsdom's 1024x768 viewport)
    const expand = await screen.findByLabelText('Expand live browser view')
    await act(async () => { expand.click() })
    expect(d().style.width).toBe('860px')
    expect(d().style.height).toBe('580px')
    // the free-resize handles remain available alongside the toggle
    expect(screen.getAllByRole('separator')).toHaveLength(8)
    // shrink back to the compact default
    const shrink = await screen.findByLabelText('Shrink live browser view')
    await act(async () => { shrink.click() })
    expect(d().style.width).toBe('260px')
    expect(d().style.height).toBe('180px')
  })

  it('restores a persisted custom size from localStorage', async () => {
    localStorage.setItem('mc-browse-mirror-dims', JSON.stringify({ w: 640, h: 460 }))
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const dialog = screen.getByRole('dialog') as HTMLElement
    expect(dialog.style.width).toBe('640px')
    expect(dialog.style.height).toBe('460px')
  })

  it('minimizes to a corner chip and re-opens from it', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const min = await screen.findByLabelText('Minimize live browser view to corner')
    await act(async () => { min.click() })
    // full panel gone, chip present
    expect(screen.queryByText('Browser — live')).toBeNull()
    const chip = await screen.findByLabelText('Show live browser view')
    await act(async () => { chip.click() })
    expect(await screen.findByText('Browser — live')).toBeInTheDocument()
  })

  it('stays collapsed (chip) when a stray frame arrives after minimize', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const min = await screen.findByLabelText('Minimize live browser view to corner')
    await act(async () => { min.click() })
    expect(screen.queryByText('Browser — live')).toBeNull()
    // a later frame must NOT force the full panel back open
    await act(async () => { window.dispatchEvent(frameEvent('WFla')) })
    expect(screen.queryByText('Browser — live')).toBeNull()
    expect(screen.getByLabelText('Show live browser view')).toBeInTheDocument()
  })

  it('closes fully — no panel and no chip remain', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD', 'sess-1')) })
    const close = await screen.findByLabelText('Close live browser view')
    await act(async () => { close.click() })
    // Unlike minimize, close leaves no chip re-open affordance.
    expect(screen.queryByText('Browser — live')).toBeNull()
    expect(screen.queryByLabelText('Show live browser view')).toBeNull()
  })

  it('stays closed when the dismissed session keeps pumping frames', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD', 'sess-1')) })
    const close = await screen.findByLabelText('Close live browser view')
    await act(async () => { close.click() })
    // The idle active-pump forwards more frames for the SAME session — the mirror
    // must not bounce back open after an explicit close.
    await act(async () => { window.dispatchEvent(frameEvent('WFla', 'sess-1')) })
    expect(screen.queryByText('Browser — live')).toBeNull()
    expect(screen.queryByLabelText('Show live browser view')).toBeNull()
  })

  it('re-opens when a new browse session starts after a close', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD', 'sess-1')) })
    const close = await screen.findByLabelText('Close live browser view')
    await act(async () => { close.click() })
    expect(screen.queryByText('Browser — live')).toBeNull()
    // A genuinely different session_key represents new activity and should surface.
    await act(async () => { window.dispatchEvent(frameEvent('WFla', 'sess-2')) })
    expect(await screen.findByText('Browser — live')).toBeInTheDocument()
  })

  it('opens via the programmatic toggle before any frame arrives', async () => {
    renderWithProviders(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(toggle()) })
    expect(await screen.findByText('Browser — live')).toBeInTheDocument()
    expect(screen.getByText(/Waiting for the browser/)).toBeInTheDocument()
  })
})

describe('BrowserLiveView resize geometry', () => {
  // jsdom viewport is 1024x768; the component uses MARGIN=16, MIN_W=180, MIN_H=120.

  it('resizeRect right edge grows width and pins the left edge', () => {
    expect(resizeRect({ x: 100, y: 100, w: 200, h: 150 }, { r: true }, 50, 0)).toEqual({
      x: 100, y: 100, w: 250, h: 150,
    })
  })

  it('resizeRect left edge grows width and moves x, pinning the right edge', () => {
    // right edge fixed at 300; dragging the grip 50px left -> x=50, w=250
    expect(resizeRect({ x: 100, y: 100, w: 200, h: 150 }, { l: true }, -50, 0)).toEqual({
      x: 50, y: 100, w: 250, h: 150,
    })
  })

  it('resizeRect enforces MIN_W and keeps the opposite edge pinned when shrinking past the minimum', () => {
    const r = resizeRect({ x: 100, y: 100, w: 200, h: 150 }, { l: true }, 200, 0)
    expect(r.w).toBe(180) // can't shrink below MIN_W
    expect(r.x + r.w).toBe(300) // right edge stayed pinned
  })

  it('resizeRect top edge grows height and moves y, pinning the bottom edge', () => {
    expect(resizeRect({ x: 100, y: 100, w: 200, h: 150 }, { t: true }, 0, -50)).toEqual({
      x: 100, y: 50, w: 200, h: 200,
    })
  })

  it('resizeRect corner drives both axes at once', () => {
    expect(resizeRect({ x: 100, y: 100, w: 200, h: 150 }, { t: true, l: true }, -40, -30)).toEqual({
      x: 60, y: 70, w: 240, h: 180,
    })
  })

  it('clampRect leaves an in-bounds rect unchanged', () => {
    const r = { x: 100, y: 100, w: 200, h: 150 }
    expect(clampRect(r)).toEqual(r)
  })

  it('clampRect pulls an off-screen rect back into the viewport', () => {
    expect(clampRect({ x: 2000, y: 2000, w: 200, h: 150 })).toEqual({
      x: 808, y: 602, w: 200, h: 150,
    })
  })

  it('clampRect caps an oversized rect to the viewport, leaving a margin on every edge', () => {
    // vw=1024 vh=768 MARGIN=16: size caps at vw-2*MARGIN / vh-2*MARGIN so the
    // right/bottom edges keep a MARGIN gap (x+w=1008=vw-16, y+h=752=vh-16).
    expect(clampRect({ x: 0, y: 0, w: 5000, h: 5000 })).toEqual({
      x: 16, y: 16, w: 992, h: 736,
    })
  })

  it('cornerRect anchors the default size in the bottom-right corner', () => {
    expect(cornerRect({ w: 260, h: 180 })).toEqual({ x: 748, y: 572, w: 260, h: 180 })
  })
})
