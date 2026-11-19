import { describe, it, expect } from 'vitest'
import { act, screen } from '@testing-library/react'
import { RelayReconnectingView } from './RelayReconnectingView'
import { renderWithProviders, createTestStore } from '../test/helpers'
import { sseDisconnected } from '../store/dashboardSlice'
import en from '../i18n/locales/en.manual.json'

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
})
