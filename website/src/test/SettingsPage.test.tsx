/**
 * Tests for the Settings page tab roster.
 *
 * Regression guard ported (PARTIAL) from the upstream project: a stale-branch
 * merge once overwrote SettingsPage.tsx from an older base and
 * dropped the Browser tab whose panel file survived — so no panel test failed;
 * there was simply no test asserting SettingsPage *lists* the tabs. These close
 * that gap for the fork's tab roster. (The upstream Cloud Sync assertion is
 * dropped — the fork has no Cloud-Sync tab; we assert the Browser tab
 * instead. There is no Provider tab: KiroCrew collapsed to its single KiroACP /
 * kiro-cli provider, so there is no provider to select.)
 *
 * Since the nav regroup, the five chat integrations live under ONE Channels
 * tab (rows inside ChannelsPanel), the sidebar carries Preferences/System
 * group headers, and legacy ?tab=slack style deep links remap to
 * ?tab=channels&channel=slack.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Stub the heavy panels — we are testing the tab roster, not panel internals.
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div data-testid="browser-panel" /> }))
// MANDATORY, not tidiness: the `../api/client` mock below exposes a FIXED method
// set, so an unmocked ComputerUsePanel calling api.getComputerUseConfig() would
// throw during render.
vi.mock('../pages/settings/ComputerUsePanel', () => ({ ComputerUsePanel: () => <div data-testid="computer-use-panel" /> }))
vi.mock('../pages/settings/InstancesPanel', () => ({ InstancesPanel: () => <div data-testid="instances-panel" /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))
vi.mock('../pages/settings/SlackPanel', () => ({ SlackPanel: () => <div data-testid="slack-panel" /> }))
vi.mock('../pages/settings/DiscordPanel', () => ({ DiscordPanel: () => <div data-testid="discord-panel" /> }))
vi.mock('../pages/settings/TelegramPanel', () => ({ TelegramPanel: () => <div data-testid="telegram-panel" /> }))
vi.mock('../pages/settings/WebexPanel', () => ({ WebexPanel: () => <div data-testid="webex-panel" /> }))
vi.mock('../pages/settings/WeComPanel', () => ({ WeComPanel: () => <div data-testid="wecom-panel" /> }))
vi.mock('../pages/settings/TeamsPanel', () => ({ TeamsPanel: () => <div data-testid="teams-panel" /> }))
vi.mock('../pages/settings/DeveloperPanel', () => ({ DeveloperPanel: () => <div data-testid="developer-panel" /> }))

// ChannelsPanel renders real (it owns the remap target) — silence its status
// queries with deterministic configs so no real fetch fires.
vi.mock('../api/client', () => ({
  api: {
    getSlackConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getDiscordConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getTelegramConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWebexConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWeComConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    // ChannelsPanel gates each channel on the `channels` governance policy;
    // all-permitted default so the remap tests see the editable panel render.
    getGovernanceChannels: vi.fn().mockResolvedValue({
      slack: true, discord: true, telegram: true, webex: true, wecom: true, teams: true,
    }),
    getTeamsConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
  },
}))

vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

// SidePanelLayout → useIsMobile reads window.matchMedia at module load; jsdom lacks it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

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

describe('SettingsPage tabs', () => {
  it('lists the Browser tab (restored after the aaf7cfe revert)', () => {
    renderAt('/settings')
    expect(screen.getByText('Browser')).toBeInTheDocument()
  })

  it('does not list a Provider tab (KiroACP is the only provider)', () => {
    renderAt('/settings')
    expect(screen.queryByText('Provider')).not.toBeInTheDocument()
  })

  it('renders the BrowserPanel when the browser tab is active', () => {
    renderAt('/settings?tab=browser')
    expect(screen.getByTestId('browser-panel')).toBeInTheDocument()
  })

  it('lists the Computer Use tab', () => {
    renderAt('/settings')
    expect(screen.getByText('Computer Use')).toBeInTheDocument()
  })

  it('renders the ComputerUsePanel when the computer-use tab is active', () => {
    renderAt('/settings?tab=computer-use')
    expect(screen.getByTestId('computer-use-panel')).toBeInTheDocument()
  })

  it('lists a single Channels tab instead of per-channel tabs', () => {
    renderAt('/settings')
    expect(screen.getByText('Channels')).toBeInTheDocument()
    // The five integrations are rows inside the Channels tab, not sidebar tabs.
    for (const name of ['Discord', 'Telegram', 'Webex', 'WeCom']) {
      expect(screen.queryByText(name)).not.toBeInTheDocument()
    }
  })

  it('renders Preferences and System group headers in the sidebar', () => {
    renderAt('/settings')
    expect(screen.getByText('Preferences')).toBeInTheDocument()
    expect(screen.getByText('System')).toBeInTheDocument()
  })

  it('renders the channel list when the channels tab is active', () => {
    renderAt('/settings?tab=channels')
    for (const name of ['Slack', 'Discord', 'Telegram', 'Webex', 'WeCom']) {
      expect(screen.getByText(name)).toBeInTheDocument()
    }
  })

  it('remaps legacy ?tab=discord to the channels tab with discord selected', async () => {
    renderAt('/settings?tab=discord')
    expect(await screen.findByTestId('discord-panel')).toBeInTheDocument()
  })

  it('remaps legacy ?tab=slack to the channels tab with slack selected', async () => {
    renderAt('/settings?tab=slack')
    expect(await screen.findByTestId('slack-panel')).toBeInTheDocument()
  })

  it('remaps legacy ?tab=wecom to the channels tab with wecom selected', async () => {
    renderAt('/settings?tab=wecom')
    expect(await screen.findByTestId('wecom-panel')).toBeInTheDocument()
  })
})
