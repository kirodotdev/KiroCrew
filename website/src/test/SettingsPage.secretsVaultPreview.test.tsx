/**
 * Pin: the Secrets vault redesign preview (`PREVIEW_SECRETS_VAULT`) decides
 * which panel draws Settings > Secrets, and whether that tab hosts a sub-nav.
 *
 * Off (the default) is the single-page `SecretsPanelLegacy` with the tab's own
 * mobile header; on is the list-detail `SecretsPanel`, whose SubNav owns the
 * mobile header at path depth 2. Toggling applies without a reload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pages/settings/SecretsPanel', () => ({ SecretsPanel: () => <div data-testid="secrets-panel-new" /> }))
vi.mock('../pages/settings/SecretsPanelLegacy', () => ({ SecretsPanelLegacy: () => <div data-testid="secrets-panel-legacy" /> }))
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
vi.mock('../pages/settings/ReleasesPanel', () => ({ default: () => <div data-testid="releases-panel" /> }))
vi.mock('../api/client', () => ({ api: {} }))
vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))
vi.mock('../hooks/useContainerWidth', () => ({ useContainerWidth: () => [{ current: null }, 1000] }))

import SettingsPage from '../pages/SettingsPage'
import { PREVIEW_SECRETS_VAULT, setPreviewFlag } from '../utils/previewFlags'

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

describe('Settings > Secrets behind the Secrets vault redesign preview', () => {
  it('draws the legacy panel while the preview is off, and the new one once it is turned on', async () => {
    renderAt('/settings/secrets')
    expect(await screen.findByTestId('secrets-panel-legacy')).toBeInTheDocument()
    expect(screen.queryByTestId('secrets-panel-new')).toBeNull()

    act(() => { setPreviewFlag(PREVIEW_SECRETS_VAULT, true) })
    expect(await screen.findByTestId('secrets-panel-new', undefined, { timeout: LAZY_PANEL_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByTestId('secrets-panel-legacy')).toBeNull()
  })

  it('hosts a sub-nav only with the preview on', async () => {
    mobile = true
    renderAt('/settings/secrets/GITHUB_TOKEN')
    await screen.findByTestId('secrets-panel-legacy')
    // Off: the tab keeps its own mobile header, as on main.
    expect(screen.getByTestId('mobile-detail-header')).toBeInTheDocument()

    act(() => { setPreviewFlag(PREVIEW_SECRETS_VAULT, true) })
    await screen.findByTestId('secrets-panel-new', undefined, { timeout: LAZY_PANEL_TIMEOUT_MS })
    // On: the panel's SubNav owns navigation at path depth 2.
    expect(screen.queryByTestId('mobile-detail-header')).toBeNull()
  })
})
