import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { RelayReconnectingView } from './RelayReconnectingView'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseDisconnected } from '../store/dashboardSlice'
import en from '../i18n/locales/en.manual.json'
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'

/** Hidden pixels below the fold, per scroller test id: jsdom does no layout, so the geometry is stubbed. */
function stubScrollGeometry(hiddenBelow: Record<string, number>) {
  const proto = HTMLElement.prototype
  vi.spyOn(proto, 'clientHeight', 'get').mockReturnValue(200)
  vi.spyOn(proto, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) { return 200 + (hiddenBelow[this.dataset.testid ?? ''] ?? 0) })
}

afterEach(() => { vi.restoreAllMocks() })

function mountWith(cause: 'remote_off' | null) {
  const store = createTestStore()
  store.dispatch(sseDisconnected(cause))
  renderWithProviders(<RelayReconnectingView cover="relay" />, { store })
  return screen.getByTestId('relay-reconnecting').textContent || ''
}

describe('RelayReconnectingView', () => {
  it('says the owner turned remote access off, and that the page waits for it', () => {
    const text = mountWith('remote_off')
    expect(text).toContain(en.app.relay_remote_control_off)
    expect(text).toContain(en.app.relay_remote_control_off_detail)
    expect(text).toContain(en.app.relay_remote_control_off_waits)
    expect(text).not.toContain(en.app.relay_crew_dropped)
  })

  it('keeps the plain reconnecting words for a dropped host', () => {
    const text = mountWith(null)
    expect(text).toContain(en.app.relay_crew_dropped)
    expect(text).not.toContain(en.app.relay_remote_control_off)
  })

  it('offers no laundry game to a relay Crew window: the game is for the gateway on this machine', () => {
    mountWith(null)
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })) })
    expect(screen.queryByTestId('wait-game')).toBeNull()
  })

  it('fades the cover\'s bottom edge while its content runs past a short window, and not when it fits', () => {
    const mask = MORE_BELOW_MASK.split(' ')[0]
    stubScrollGeometry({ 'cover-scroll': 120 })
    const store = createTestStore()
    store.dispatch(sseDisconnected(null))
    const { unmount } = renderWithProviders(<RelayReconnectingView cover="host" />, { store })
    fireEvent.scroll(screen.getByTestId('cover-scroll')) // the scroll listener re-measures, as the observer's first delivery does
    expect(screen.getByTestId('cover-scroll').className).toContain(mask)
    unmount()
    vi.restoreAllMocks()
    stubScrollGeometry({})
    renderWithProviders(<RelayReconnectingView cover="host" />, { store })
    fireEvent.scroll(screen.getByTestId('cover-scroll'))
    expect(screen.getByTestId('cover-scroll').className).not.toContain(mask)
  })

  // The DOM here has no layout, so this pins the sizing contract; a real-browser check at 320x640 and 740x360 measured the
  // how-to line inside the window with it, and cut off at 740x360 with the old fixed 56vh height.
  it('sizes the game from the height the lines leave, so its how-to line stays in a short window', () => {
    const store = createTestStore()
    store.dispatch(sseDisconnected(null))
    renderWithProviders(<RelayReconnectingView cover="host" />, { store })
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })) })
    const game = screen.getByTestId('wait-game').className.split(/\s+/)
    expect(game).toEqual(expect.arrayContaining(['flex-1', 'basis-0', 'min-h-40', 'max-h-[480px]']))
    expect(game.some(c => /vh|dvh/.test(c) || /^h-/.test(c))).toBe(false) // no height that assumes a taller window
    expect(screen.getByTestId('cover-content').className.split(/\s+/)).toContain('min-h-full')
    // safe centring: unsafe centring would push overflow above the scroll box's top, out of reach
    expect(screen.getByTestId('cover-content').className.split(/\s+/)).toContain('justify-center-safe')
  })

  it('offers no game while another surface (the update overlay) holds the wait', () => {
    const store = createTestStore()
    store.dispatch(sseDisconnected(null))
    renderWithProviders(<RelayReconnectingView cover="host" offerGame={false} />, { store })
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })) })
    expect(screen.queryByTestId('wait-game')).toBeNull()
    expect(screen.getByTestId('host-offline')).toBeTruthy()
  })
})
