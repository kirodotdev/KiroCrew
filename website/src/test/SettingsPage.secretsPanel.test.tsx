/**
 * Pin: Settings > Secrets draws the list-detail `SecretsPanel`, whose SubNav
 * owns the mobile header at path depth 2.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pages/settings/SecretsPanel', () => ({ SecretsPanel: () => <div data-testid="secrets-panel-new" /> }))
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChannelsPanel', () => ({ ChannelsPanel: () => <div data-testid="channels-panel" />, CHANNEL_KEYS: [] }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div data-testid="browser-panel" /> }))
vi.mock('../pages/settings/ComputerUsePanel', () => ({ ComputerUsePanel: () => <div data-testid="computer-use-panel" /> }))
vi.mock('../pages/settings/WebhooksPanel', () => ({ WebhooksPanel: () => <div data-testid="webhooks-panel" /> }))
vi.mock('../pages/settings/RemoteCrewPanel', () => ({ RemoteCrewPanel: () => <div data-testid="remote-crew-panel" /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/PrivacyPanel', () => ({ PrivacyPanel: () => <div data-testid="privacy-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))
vi.mock('../pages/settings/DeveloperPanel', () => ({ DeveloperPanel: () => <div data-testid="developer-panel" /> }))
vi.mock('../pages/settings/AboutPanel', () => ({ AboutPanel: () => <div data-testid="about-panel" /> }))
vi.mock('../api/client', () => ({ api: {} }))
vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))
vi.mock('../hooks/useContainerWidth', () => ({ useContainerWidth: () => [{ current: null }, 1000] }))

import SettingsPage from '../pages/SettingsPage'

function renderAt(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <SettingsPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  mobile = false
})

/** The new panel is `React.lazy` in SettingsPage (`import('./settings/SecretsPanel')`,
 *  mocked above): the find that waits on it gets this bounded deadline. */
const LAZY_PANEL_TIMEOUT_MS = 5000

describe('Settings > Secrets', () => {
  it('draws the list-detail panel', async () => {
    renderAt('/settings/secrets')
    expect(await screen.findByTestId('secrets-panel-new', undefined, { timeout: LAZY_PANEL_TIMEOUT_MS })).toBeInTheDocument()
  })

  it('hosts a sub-nav, so the panel owns the mobile header at depth 2', async () => {
    mobile = true
    renderAt('/settings/secrets/GITHUB_TOKEN')
    await screen.findByTestId('secrets-panel-new', undefined, { timeout: LAZY_PANEL_TIMEOUT_MS })
    expect(screen.queryByTestId('mobile-detail-header')).toBeNull()
  })
})
