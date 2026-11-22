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
 *   - The chat's "Sign in to Kiro" affordance (`KIRO_SIGN_IN_PATH`) opens the
 *     Kiro account tab and lands ON the sign-in card, whose anchor mounts late
 *     (its lazy chunk), consuming the highlight only once it has rung it.
 *   - The old `/developer?tab=agent-backend` URL -- bookmarks, and tabs opened
 *     before this build -- is forwarded to the Agent Harness tab, and an old
 *     sign-in link (there or on the Agent Harness tab itself) to the Kiro
 *     account tab, so it still rings the card.
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
        {loaded ? null : 'Loading configuration…'}
      </div>
    )
  },
}))

vi.mock('../pages/developer/KiroSignInCard', () => ({
  KiroSignInCard: () => {
    // Mounts the anchor late, standing in for the card's first status read --
    // the case a first-tick lookup gets wrong.
    const [loaded, setLoaded] = useState(false)
    useEffect(() => {
      const t = setTimeout(() => setLoaded(true), 250)
      return () => clearTimeout(t)
    }, [])
    return (
      <div data-testid="kiro-account-tab">
        {loaded ? <div data-testid="anchor" data-setting-key="kiro-sign-in" /> : 'Checking…'}
      </div>
    )
  },
}))

// Subagent Settings + Warm Pool: their editors are pinned in CfgTabCoverage.test.tsx;
// only their placement on this tab is tested here.
vi.mock('../pages/overview/KiroCrewCfgTab', () => ({
  AgentRunSettings: () => <div data-testid="agent-run-settings" />,
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

/** Waits that cross SettingsPage's lazy `KiroSignInCard` import: a cold worker
 *  can take past the default 1 s to load that chunk. */
const LAZY_TAB = { timeout: 10_000 }

async function expectCardRungAndHighlightConsumed() {
  expect(await screen.findByTestId('kiro-account-tab', {}, LAZY_TAB)).toBeInTheDocument()
  // The anchor is not there yet: the highlight must survive, not be stripped
  // on the first tick as an unknown id.
  expect(loc()).toMatch(SIGN_IN_HIGHLIGHT)
  const anchor = await screen.findByTestId('anchor', {}, LAZY_TAB)
  await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1), LAZY_TAB)
  expect(anchor.style.outlineOffset).toBe('4px')
  await waitFor(() => expect(loc()).not.toContain('highlight='))
  // The tab stays in the path; only the consumed highlight goes.
  expect(loc()).toBe('/settings/kiro-account')
}

describe('Settings > Agent Harness', () => {
  it('is a Settings tab that renders the coding-agent switch', async () => {
    renderAt('/settings/agent')
    expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
    expect(screen.getAllByText('Agent Harness').length).toBeGreaterThan(0)
  })

  it('hosts the Subagent Settings and Warm Pool cards beside the switch', async () => {
    renderAt('/settings/agent')
    // Two independent lazy imports behind two Suspense boundaries: either may
    // settle first, so wait for each on its own.
    expect(await screen.findByTestId('agent-run-settings', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(await screen.findByTestId('agent-backend-tab', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('the chat sign-in link opens the Kiro account tab and rings the sign-in card once it mounts', async () => {
    expect(KIRO_SIGN_IN_PATH.startsWith('/settings/kiro-account?')).toBe(true)
    renderAt(KIRO_SIGN_IN_PATH)
    await expectCardRungAndHighlightConsumed()
  })

  it('forwards the old Developer-page sign-in link to the Kiro account tab', async () => {
    renderAt('/developer?tab=agent-backend&highlight=key%3Akiro-sign-in')
    await expectCardRungAndHighlightConsumed()
  })

  it.each([
    '/settings/agent?highlight=key%3Akiro-sign-in',
    '/settings/agent?highlight=agent.kiro-sign-in',
    '/settings?tab=agent&highlight=key%3Akiro-sign-in',
  ])('forwards the old Agent Harness sign-in link %s to the Kiro account tab', async route => {
    renderAt(route)
    await expectCardRungAndHighlightConsumed()
  })

  it('forwards a bare old /developer?tab=agent-backend link to the tab', async () => {
    renderAt('/developer?tab=agent-backend')
    expect(await screen.findByTestId('agent-backend-tab')).toBeInTheDocument()
    await waitFor(() => expect(loc()).toBe('/settings/agent'))
  })

  it('is not a page of Settings > Developer', async () => {
    renderAt('/developer')
    await waitFor(() => expect(loc()).toBe('/settings/developer'))
    expect(screen.queryByText('Agent Backend')).toBeNull()
    expect(screen.queryByTestId('agent-backend-tab')).toBeNull()
  })
})
