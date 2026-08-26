/**
 * Settings > Developer tab (DeveloperPanel).
 *
 * Contract under test:
 * - Developer Mode toggle persists to localStorage and fires the dev-mode event
 * - The Updates section is GONE (Beta Channel moved to Settings > About)
 * - "Open Developer page" link renders only while Developer Mode is on and
 *   navigates to /developer
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { DeveloperPanel } from '../pages/settings/DeveloperPanel'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname}</div>
}

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={['/settings?tab=developer']}>
      <DeveloperPanel />
      <LocationProbe />
    </MemoryRouter>
  )
}

describe('DeveloperPanel', () => {
  beforeEach(() => { localStorage.removeItem('mc-dev-mode') })

  it('renders the Developer Mode toggle and no Updates section', () => {
    renderPanel()
    expect(screen.getByText('Developer Mode')).toBeInTheDocument()
    expect(screen.queryByText('Updates')).not.toBeInTheDocument()
    expect(screen.queryByText('Beta Channel (Braveheart)')).not.toBeInTheDocument()
  })

  it('toggling on persists, dispatches the event, and reveals the page link', () => {
    const eventSpy = vi.fn()
    window.addEventListener('mc-dev-mode-changed', eventSpy)
    renderPanel()
    expect(screen.queryByText('Open Developer page')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Developer Mode' }))
    expect(localStorage.getItem('mc-dev-mode')).toBe('1')
    expect(eventSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Open Developer page')).toBeInTheDocument()
    window.removeEventListener('mc-dev-mode-changed', eventSpy)
  })

  it('Open Developer page navigates to /developer', () => {
    localStorage.setItem('mc-dev-mode', '1')
    renderPanel()
    fireEvent.click(screen.getByText('Open Developer page'))
    expect(screen.getByTestId('loc').textContent).toBe('/developer')
  })

  describe('Gateway section', () => {
    type LocalGatewayAPI = { get(): Promise<boolean>; set(enabled: boolean): Promise<boolean> }
    const installBridge = (api: LocalGatewayAPI) => {
      ;(window as unknown as { localGatewayAPI?: LocalGatewayAPI }).localGatewayAPI = api
    }
    afterEach(() => {
      delete (window as unknown as { localGatewayAPI?: LocalGatewayAPI }).localGatewayAPI
    })

    it('is absent without the desktop bridge', () => {
      // A browser tab and the PWA have no gateway of their own to start, so the
      // control must not appear at all rather than appear and do nothing.
      renderPanel()
      expect(screen.queryByText('Gateway')).not.toBeInTheDocument()
      expect(screen.queryByRole('switch', { name: 'Run a local gateway' })).not.toBeInTheDocument()
    })

    it('renders the toggle in the desktop app and writes the flip through', async () => {
      const set = vi.fn(() => Promise.resolve(false))
      installBridge({ get: () => Promise.resolve(true), set })
      renderPanel()

      const toggle = await screen.findByRole('switch', { name: 'Run a local gateway' })
      await waitFor(() => expect(toggle).toBeChecked())
      fireEvent.click(toggle)
      expect(set).toHaveBeenCalledWith(false)
      await waitFor(() => expect(toggle).not.toBeChecked())
    })
  })
})
