/**
 * The coding-agent switch lives on Settings > Agent Harness (`/settings/agent`).
 * It used to be the Developer page's `agent-backend` tab, whose sidebar entry
 * only shows in Developer Mode -- so a user who finished first-run setup with
 * Claude Code had no visible way to switch agents or reach the Kiro sign-in card.
 *
 * Pinned here, end to end through the real SettingsPage + DeveloperPage:
 *
 *   - Settings lists the tab and renders the switch on it; the Developer page no
 *     longer offers the tab.
 *   - The chat's "Sign in to Kiro" affordance (`KIRO_SIGN_IN_PATH`) opens that
 *     tab and lands ON the sign-in card, whose anchor mounts late (after the
 *     tab's config query), consuming the highlight only once it has rung it.
 *   - The old `/developer?tab=agent-backend` URL -- bookmarks, and tabs opened
 *     before this build -- is forwarded to the new tab WITH its highlight, so
 *     an old sign-in link still rings the card instead of dropping the reader
 *     on the Developer page's first tab.
 *
 * The switch itself is replaced by a sentinel; its own behaviour is pinned in
 * `AgentBackendTab.test.tsx`. Only placement, routing and landing are tested.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

vi.mock('../pages/developer/AgentBackendTab', () => ({
  AgentBackendTab: () => {
    // Mounts the anchor late, standing in for the config query the real tab
    // awaits before it renders the card -- the case a first-tick lookup gets wrong.
    const [loaded, setLoaded] = useState(false)
    useEffect(() => {
      const t = setTimeout(() => setLoaded(true), 250)
      return () => clearTimeout(t)
    }, [])
    return (
      <div data-testid="agent-backend-tab">
        {loaded ? <div data-testid="anchor" data-setting-key="kiro-sign-in" /> : 'Loading configuration…'}
      </div>
    )
  },
}))

// Settings panels: heavy and irrelevant here.
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div /> }))
vi.mock('../pages/settings/ComputerUsePanel', () => ({ ComputerUsePanel: () => <div /> }))
vi.mock('../pages/settings/WebhooksPanel', () => ({ WebhooksPanel: () => <div /> }))
vi.mock('../pages/settings/RemoteCrewPanel', () => ({ RemoteCrewPanel: () => <div /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div /> }))
vi.mock('../pages/settings/PrivacyPanel', () => ({ PrivacyPanel: () => <div /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div /> }))
vi.mock('../pages/settings/DeveloperPanel', () => ({ DeveloperPanel: () => <div /> }))
vi.mock('../pages/settings/ReleasesPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/ChannelsPanel', () => ({ ChannelsPanel: () => <div />, CHANNEL_KEYS: [] }))

// Developer page tabs: heavy and irrelevant here.
vi.mock('../pages/overview/MemoryGraphTab', () => ({ default: () => <div /> }))
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div data-testid="logs-tab" /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => <div />,
  AgentCfgTab: () => <div />,
}))

vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import SettingsPage from '../pages/SettingsPage'
import DeveloperPage from '../pages/DeveloperPage'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'

function LocationProbe() {
  const { pathname, search } = useLocation()
  return <div data-testid="loc">{pathname + search}</div>
}

function renderAt(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/settings/*" element={<SettingsPage />} />
          <Route path="/developer" element={<DeveloperPage />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const loc = () => screen.getByTestId('loc').textContent ?? ''
// The colon may or may not be percent-encoded depending on serialization.
const SIGN_IN_HIGHLIGHT = /highlight=key(:|%3A)kiro-sign-in/

beforeEach(() => {
  sessionStorage.clear()
  // Not available in jsdom; the hook scrolls the anchor into view.
  Element.prototype.scrollIntoView = vi.fn()
})

async function expectCardRungAndHighlightConsumed() {
  expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
  // The anchor is not there yet: the highlight must survive, not be stripped
  // on the first tick as an unknown id.
  expect(loc()).toMatch(SIGN_IN_HIGHLIGHT)
  const anchor = await screen.findByTestId('anchor')
  await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1))
  expect(anchor.style.outlineOffset).toBe('4px')
  await waitFor(() => expect(loc()).not.toContain('highlight='))
  // The tab stays in the path; only the consumed highlight goes.
  expect(loc()).toBe('/settings/agent')
}

describe('Settings > Agent Harness', () => {
  it('is a Settings tab that renders the coding-agent switch', async () => {
    renderAt('/settings/agent')
    expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
    expect(screen.getAllByText('Agent Harness').length).toBeGreaterThan(0)
  })

  it('the chat sign-in link opens the tab and rings the Kiro sign-in card once it mounts', async () => {
    expect(KIRO_SIGN_IN_PATH.startsWith('/settings/agent?')).toBe(true)
    renderAt(KIRO_SIGN_IN_PATH)
    await expectCardRungAndHighlightConsumed()
  })

  it('forwards the old Developer-page sign-in link, highlight included', async () => {
    renderAt('/developer?tab=agent-backend&highlight=key%3Akiro-sign-in')
    await expectCardRungAndHighlightConsumed()
  })

  it('forwards a bare old /developer?tab=agent-backend link to the tab', async () => {
    renderAt('/developer?tab=agent-backend')
    expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
    await waitFor(() => expect(loc()).toBe('/settings/agent'))
  })

  it('is no longer a Developer page tab', async () => {
    renderAt('/developer')
    expect(await screen.findByTestId('logs-tab')).toBeInTheDocument()
    expect(screen.queryByText('Agent Backend')).toBeNull()
    expect(screen.queryByTestId('agent-backend-tab')).toBeNull()
  })
})
