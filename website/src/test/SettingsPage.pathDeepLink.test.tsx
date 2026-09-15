/**
 * Pin: a PATH deep link cold-opens the right pane end to end.
 *
 * Settings navigation is path-based (`/settings/<tab>/<sub>` under the
 * `/settings/*` splat): segment[0] picks the SidePanelLayout tab, segment[1]
 * the SettingsSubNav selection. This test mounts the REAL SettingsPage +
 * SidePanelLayout + ChannelsPanel/SettingsSubNav chain at
 * `/settings/channels/slack` — no clicks, no legacy params, no translation
 * effect — and pins the cold-open contract on both form factors:
 *
 *   - MOBILE (narrow): the Slack detail pane renders, under exactly ONE back
 *     bar — the SubNav's "‹ Channels". SidePanelLayout's own "‹ Settings" bar
 *     and the tab's big title yield (path depth >= 2 on a hostsSubNav tab),
 *     because two stacked back bars is the misread the push stack prevents.
 *   - WIDE: the rail highlights Channels, the SubNav listbox marks Slack
 *     aria-selected, and the Slack pane renders beside the list.
 *
 * Width is driven by useContainerWidth (ResizeObserver) and the mobile branch
 * by useIsMobile — both mocked, since happy-dom reports zero layout sizes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Stub the heavy panels — this test exercises the navigation chain, not panel
// internals. ChannelsPanel renders REAL: it owns the SubNav under test.
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div data-testid="browser-panel" /> }))
// MANDATORY, not tidiness: the `../api/client` mock below exposes a FIXED
// method set, so an unmocked panel calling an absent api method would throw
// during render.
vi.mock('../pages/settings/ComputerUsePanel', () => ({ ComputerUsePanel: () => <div data-testid="computer-use-panel" /> }))
vi.mock('../pages/settings/WebhooksPanel', () => ({ WebhooksPanel: () => <div data-testid="webhooks-panel" /> }))
vi.mock('../pages/settings/RemoteCrewPanel', () => ({ RemoteCrewPanel: () => <div data-testid="remote-crew-panel" /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/PrivacyPanel', () => ({ PrivacyPanel: () => <div data-testid="privacy-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))
vi.mock('../pages/settings/DeveloperPanel', () => ({ DeveloperPanel: () => <div data-testid="developer-panel" /> }))
vi.mock('../pages/settings/ReleasesPanel', () => ({ default: () => <div data-testid="releases-panel" /> }))
// The channel detail panels (rendered inside the real ChannelsPanel).
vi.mock('../pages/settings/SlackPanel', () => ({ SlackPanel: () => <div data-testid="slack-panel" /> }))
vi.mock('../pages/settings/DiscordPanel', () => ({ DiscordPanel: () => <div data-testid="discord-panel" /> }))
vi.mock('../pages/settings/TelegramPanel', () => ({ TelegramPanel: () => <div data-testid="telegram-panel" /> }))
vi.mock('../pages/settings/WebexPanel', () => ({ WebexPanel: () => <div data-testid="webex-panel" /> }))
vi.mock('../pages/settings/WeComPanel', () => ({ WeComPanel: () => <div data-testid="wecom-panel" /> }))
vi.mock('../pages/settings/TeamsPanel', () => ({ TeamsPanel: () => <div data-testid="teams-panel" /> }))
vi.mock('../pages/settings/WeixinPanel', () => ({ WeixinPanel: () => <div data-testid="weixin-panel" /> }))
vi.mock('../pages/settings/IMessagePanel', () => ({ IMessagePanel: () => <div data-testid="imessage-panel" /> }))
vi.mock('../pages/settings/WhatsAppPanel', () => ({ WhatsAppPanel: () => <div data-testid="whatsapp-panel" /> }))

// ChannelsPanel status queries: deterministic configs so no real fetch fires,
// all channels governance-permitted so the Slack pane is the editable panel.
vi.mock('../api/client', () => ({
  api: {
    getSlackConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getDiscordConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getTelegramConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWebexConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWeComConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getTeamsConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWeixinConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getIMessageConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWhatsAppConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getGovernanceChannels: vi.fn().mockResolvedValue({
      slack: true, discord: true, telegram: true, webex: true, wecom: true,
      teams: true, weixin: true, imessage: true, whatsapp: true,
    }),
  },
}))

vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

// Form-factor control: each test sets these BEFORE render. `mobile` drives
// SidePanelLayout's branch; `mockWidth` drives the SubNav's two-pane test.
let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))
let mockWidth: number | null = null
vi.mock('../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, mockWidth],
}))

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
  // The remembered-tab restore reads sessionStorage on mount; a key left by
  // one test must not decide another test's cold-open.
  sessionStorage.clear()
})

describe('path deep link /settings/channels/slack — cold open', () => {
  it('mobile: opens the Slack pane under a single "Channels" back bar', async () => {
    mobile = true
    mockWidth = 390
    renderAt('/settings/channels/slack')

    // The deep link lands in the Channels tab with the Slack detail pane —
    // not the mobile root list. (findBy: the pane renders once the
    // channels-governance query confirms the channel is permitted.)
    expect(await screen.findByTestId('slack-panel')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Settings' })).toBeNull()

    // Exactly ONE back bar, the SubNav's, labeled after the parent level.
    expect(screen.getByRole('button', { name: 'Channels' })).toBeInTheDocument()
    // SidePanelLayout's own "‹ Settings" bar and big-title header yield —
    // path depth >= 2 on a hostsSubNav tab means the SubNav owns navigation.
    expect(screen.queryByRole('button', { name: /Settings/ })).toBeNull()
    expect(screen.queryByTestId('mobile-detail-header')).toBeNull()
  })

  it('wide: highlights Channels in the rail and marks Slack selected in the SubNav', async () => {
    mobile = false
    mockWidth = 1000
    renderAt('/settings/channels/slack')

    // The Slack detail pane renders beside the persistent channel list.
    expect(await screen.findByTestId('slack-panel')).toBeInTheDocument()
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()

    // Rail selection: the Channels tab carries the active tint, Overview
    // does not. (The rail rows are plain buttons; the SubNav's rows are
    // role="option", so the name query cannot cross-match them.)
    expect(screen.getByRole('button', { name: 'Channels' }).className).toContain('bg-accent-subtle')
    expect(screen.getByRole('button', { name: 'Overview' }).className).not.toContain('bg-accent-subtle')

    // SubNav selection: the path's segment[1] is the selected option.
    expect(screen.getByRole('option', { name: /Slack/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('option', { name: /Discord/ })).toHaveAttribute('aria-selected', 'false')
  })
})
