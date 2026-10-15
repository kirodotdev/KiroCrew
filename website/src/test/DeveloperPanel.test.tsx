/**
 * Settings > Developer tab (DeveloperPanel).
 *
 * Contract under test:
 * - Developer Mode toggle persists to localStorage and fires the dev-mode event
 * - The Updates section is GONE (Beta Channel moved to Settings > About)
 * - "Open Developer page" link renders only while Developer Mode is on and
 *   navigates to /developer
 * - The Feature Previews section (moved here from the Developer page) is
 *   mounted, unconditionally, between Developer Tools and Gateway
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { DeveloperPanel } from '../pages/settings/DeveloperPanel'
import { api } from '../api/client'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname}</div>
}

// The Feature Previews section this panel mounts carries one card whose switch is
// backend config (Decisions), so the panel now reads the shared
// `['kirocrewConfig']` cache and needs a provider. `retry: false` keeps the
// stubbed read from being retried after a test ends.
function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/settings?tab=developer']}>
        <DeveloperPanel />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('DeveloperPanel', () => {
  beforeEach(() => {
    localStorage.removeItem('mc-dev-mode')
    // Stubbed rather than left to reach the network: every case here is about the
    // panel's own sections, and a real config read cannot succeed under vitest.
    // `decisionsCard.test.tsx` owns that card's states.
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
  })

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

  describe('Feature Previews section', () => {
    // The cards moved here from the standalone Developer page's own tab. The
    // per-card behaviour (fail-closed reads, ingress links, one key per flag) is
    // pinned in previewSurfaces.test.tsx against the section in isolation; this
    // pins that DeveloperPanel actually MOUNTS it, sits it between the two
    // consent gates, and does not gate it behind Developer Mode — a reader who
    // has not unlocked the Developer page must still find the opt-ins.
    beforeEach(() => { localStorage.removeItem('mc-preview-webhooks') })

    it('renders the preview toggles with Developer Mode off', () => {
      renderPanel()
      expect(screen.getByRole('heading', { name: 'Feature Previews' })).toBeInTheDocument()
      expect(screen.getByRole('switch', { name: 'Webhooks' })).toHaveAttribute('aria-checked', 'false')
      expect(screen.getByRole('switch', { name: 'Mates' })).toHaveAttribute('aria-checked', 'false')
      expect(screen.getByRole('switch', { name: 'Chat on a crew' })).toHaveAttribute('aria-checked', 'false')
    })

    it('orders the section after Developer Tools', () => {
      // Intent-frequency order: the two consent gates first, the desktop-only
      // Gateway switch (when present) last.
      renderPanel()
      const headings = screen.getAllByRole('heading').map(h => h.textContent)
      expect(headings.indexOf('Developer Tools')).toBeLessThan(headings.indexOf('Feature Previews'))
    })

    it('flipping a preview writes its own flag and offers the hidden page', () => {
      renderPanel()
      fireEvent.click(screen.getByRole('switch', { name: 'Webhooks' }))
      expect(localStorage.getItem('mc-preview-webhooks')).toBe('1')
      expect(localStorage.getItem('mc-dev-mode')).toBeNull()
      fireEvent.click(screen.getByText('Open Webhooks'))
      expect(screen.getByTestId('loc').textContent).toBe('/webhooks')
    })
  })

  describe('Automatic cards', () => {
    it('has its own card outside Feature Previews, off by default', async () => {
      renderPanel()
      const sw = await screen.findByRole('switch', { name: /automatic cards for all sessions/i })
      expect(screen.getByTestId('developer-automatic-cards')).toContainElement(sw)
      expect(sw).toHaveAttribute('aria-checked', 'false')
      const previews = document.querySelector('[data-setting-key="feature-previews-section"]')
      expect(previews).not.toBeNull()
      expect(previews!.contains(sw)).toBe(false)
    })
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

    it('names the tunnel prerequisite and links the guide that sets it up (#3810)', async () => {
      // "Off" only works when something else already answers on this app's
      // port; the app never opens that forward, so the row must say so before
      // the user flips it, not after the next launch fails.
      installBridge({ get: () => Promise.resolve(true), set: vi.fn(() => Promise.resolve(false)) })
      renderPanel()
      await screen.findByRole('switch', { name: 'Run a local gateway' })
      expect(screen.getByText(/already reaches a crew on another machine/)).toBeInTheDocument()
      // A hand-run tunnel alone is refused as a foreign holder (#3810 review),
      // so the copy must say the crew is saved for the port AND name where
      // the app-held tunnel is switched on.
      expect(screen.getByText(/“Set Remote Host…” in the Connection menu/)).toBeInTheDocument()
      expect(screen.getByText(/Keep an SSH tunnel to this crew open/)).toBeInTheDocument()
      expect(screen.getByText(/Settings → Remote Crew connections stop when this is off/)).toBeInTheDocument()
      const link = screen.getByRole('link', { name: /How to set up an SSH tunnel/ })
      expect(link).toHaveAttribute(
        'href',
        'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md#ssh-tunnel-laptop',
      )
      expect(link).toHaveAttribute('target', '_blank')
    })
  })
})
