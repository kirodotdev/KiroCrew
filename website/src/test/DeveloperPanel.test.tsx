/**
 * Settings > Developer tab (DeveloperPanel).
 *
 * Contract under test:
 * - The tab is a SubNav: Feature Previews first, then the internals pages
 *   (Logs, System, Telemetry, Storage, MCP Pool, Memory, Config, Debug Tools,
 *   Archive), every one listed with no consent gate in front of it
 * - There is no Developer Mode toggle and no Updates section
 * - The Feature Previews page holds Developer Tools, Feature Previews and
 *   (desktop only) Gateway
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { DeveloperPanel } from '../pages/settings/DeveloperPanel'
import { api } from '../api/client'

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{loc.pathname}</div>
}

// The Feature Previews section this panel mounts carries one card whose switch is
// backend config (Decisions), so the panel reads the shared
// `['kirocrewConfig']` cache and needs a provider. `retry: false` keeps the
// stubbed read from being retried after a test ends.
function renderPanel(path = '/settings/developer/feature-previews') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <DeveloperPanel basePath="/settings" />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const PAGES = ['Feature Previews', 'Logs', 'System', 'Telemetry', 'Storage', 'MCP Management', 'Memory', 'Config', 'Debug tools', 'Archive']

describe('DeveloperPanel', () => {
  beforeEach(() => {
    // Stubbed rather than left to reach the network: every case here is about the
    // panel's own sections, and a real config read cannot succeed under vitest.
    // `decisionsCard.test.tsx` owns that card's states.
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
  })

  it('has no Developer Mode toggle and no Updates section', () => {
    renderPanel()
    expect(screen.queryByText('Developer Mode')).not.toBeInTheDocument()
    expect(screen.queryByText('Open Developer page')).not.toBeInTheDocument()
    expect(screen.queryByText('Updates')).not.toBeInTheDocument()
  })

  it('lists Feature Previews first, then every internals page', () => {
    renderPanel()
    const list = screen.getByRole('listbox', { name: 'Developer' })
    const names = within(list).getAllByRole('option').map(o => o.textContent?.trim())
    expect(names).toEqual(PAGES)
  })

  it('choosing a page navigates to its path segment', () => {
    renderPanel()
    fireEvent.click(screen.getByRole('option', { name: 'Storage' }))
    expect(screen.getByTestId('loc').textContent).toBe('/settings/developer/storage')
  })

  describe('Feature Previews section', () => {
    // The cards moved here from the standalone Developer page's own tab. The
    // per-card behaviour (fail-closed reads, ingress links, one key per flag) is
    // pinned in previewSurfaces.test.tsx against the section in isolation; this
    // pins that DeveloperPanel actually MOUNTS it on its Feature Previews page.
    beforeEach(() => { localStorage.removeItem('mc-preview-webhooks') })

    it('renders the preview toggles', () => {
      renderPanel()
      expect(screen.getByRole('heading', { name: 'Feature Previews' })).toBeInTheDocument()
      expect(screen.getByRole('switch', { name: 'Webhooks' })).toHaveAttribute('aria-checked', 'false')
      expect(screen.queryByRole('switch', { name: 'Mates' })).toBeNull()
      expect(screen.getByRole('switch', { name: 'Chat on a crew' })).toHaveAttribute('aria-checked', 'false')
    })

    it('orders the section after Developer Tools', () => {
      // The desktop-only Gateway switch (when present) comes last.
      renderPanel()
      const headings = screen.getAllByRole('heading').map(h => h.textContent)
      expect(headings.indexOf('Developer Tools')).toBeLessThan(headings.indexOf('Feature Previews'))
    })

    it('flipping a preview writes its own flag and offers the hidden page', () => {
      renderPanel()
      fireEvent.click(screen.getByRole('switch', { name: 'Webhooks' }))
      expect(localStorage.getItem('mc-preview-webhooks')).toBe('1')
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
