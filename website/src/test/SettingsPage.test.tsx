/**
 * Tests for the Settings page tab roster.
 *
 * Regression guard ported (PARTIAL) from MeshClawWebsite dfbc99cd: the aaf7cfe
 * stale-branch merge once overwrote SettingsPage.tsx from an older base and
 * dropped the Browser tab whose panel file survived — so no panel test failed;
 * there was simply no test asserting SettingsPage *lists* the tabs. These close
 * that gap for the fork's tab roster. (Upstream's Cloud Sync assertion is
 * dropped — the fork has no GitFarm Cloud-Sync tab; we assert the Browser tab
 * instead. There is no Provider tab: KiroCrew collapsed to its single KiroACP /
 * kiro-cli provider, so there is no provider to select.)
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// Stub the heavy panels — we are testing the tab roster, not panel internals.
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div data-testid="browser-panel" /> }))
vi.mock('../pages/settings/InstancesPanel', () => ({ InstancesPanel: () => <div data-testid="instances-panel" /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))
vi.mock('../pages/settings/SlackPanel', () => ({ SlackPanel: () => <div data-testid="slack-panel" /> }))
vi.mock('../pages/settings/DiscordPanel', () => ({ DiscordPanel: () => <div data-testid="discord-panel" /> }))
vi.mock('../pages/settings/GeneralPanel', () => ({ GeneralPanel: () => <div data-testid="general-panel" /> }))

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
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SettingsPage />
    </MemoryRouter>
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

  it('lists the Discord tab', () => {
    renderAt('/settings')
    expect(screen.getByText('Discord')).toBeInTheDocument()
  })

  it('renders the DiscordPanel when the discord tab is active', () => {
    renderAt('/settings?tab=discord')
    expect(screen.getByTestId('discord-panel')).toBeInTheDocument()
  })
})
