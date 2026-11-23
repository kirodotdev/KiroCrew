/**
 * The chat's "Sign in to Kiro" link, end to end through the REAL Kiro account tab.
 *
 * `SettingsPage.agentHarness.test.tsx` pins routing and landing with the card
 * replaced by a sentinel. This renders the real card on its own tab, with Kiro
 * CLI (not KAS) as the configured harness, and asserts the whole chain: the tab
 * opens, the card mounts, the highlight hook rings it and strips the parameter.
 * It also pins what the Agent Harness tab keeps: one line and a link, no card.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(() => Promise.resolve({ agent: { acp_backend: harness.value } })),
      patchConfig: vi.fn(() => Promise.resolve({})),
      acpBackends: vi.fn(() =>
        Promise.resolve({
          backends: ['kiro', 'kas', 'claude'].map(id => ({
            id,
            policy_id: id || 'kiro',
            selectable: true,
            independent_setup: id === 'claude',
            installed: 'installed',
            missing_components: [],
            install_command: '',
            restart_required: false,
          })),
        }),
      ),
      acpBackendRecheck: vi.fn(),
      // Never settles: the card shows its loader, which is enough for the anchor
      // to exist. The sign-in flow itself is pinned in KiroSignInCard.test.tsx.
      kasLoginStatus: vi.fn(() => new Promise(() => {})),
      kasLoginSources: vi.fn(() => new Promise(() => {})),
    },
  }
})

const choices = vi.hoisted(() => ({ value: ['kiro', 'claude', 'kas'] }))
const harness = vi.hoisted(() => ({ value: 'kiro' }))
vi.mock('../components/settingRef/useConfigSchema', async importOriginal => {
  const schema = () => new Map([['agent.acp_backend', { path: 'agent.acp_backend', type: 'enum', enum: choices.value }]])
  return {
    ...(await importOriginal<typeof import('../components/settingRef/useConfigSchema')>()),
    useConfigSchema: schema,
    useConfigSchemaQuery: () => ({ schema: schema(), error: null }),
  }
})

// The run cards beside the switch (Subagent Settings + Warm Pool) read a config
// shape this suite does not seed; they are pinned in CfgTabCoverage.test.tsx.
vi.mock('../pages/overview/KiroCrewCfgTab', () => ({ AgentRunSettings: () => <div /> }))

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

vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import SettingsPage from '../pages/SettingsPage'
import { KIRO_SIGN_IN_PATH, KIRO_SIGN_IN_SETTING_ID } from '../pages/developer/kiroSignInLink'
import { createSettingsProvider } from '../components/commandPalette/providers/settingsProvider'
import type { NavigateFunction } from 'react-router-dom'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../hooks/useSettingHighlight'

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
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const loc = () => screen.getByTestId('loc').textContent ?? ''

/** Waits that cross SettingsPage's lazy `KiroSignInCard` / `AgentBackendTab`
 *  imports: a cold worker can take past the default 1 s to load that chunk, so
 *  these carry an explicit deadline. */
const LAZY_TAB = { timeout: 10_000 }

beforeEach(() => {
  choices.value = ['kiro', 'claude', 'kas']
  harness.value = 'kiro'
  sessionStorage.clear()
  // Not available in jsdom; the hook scrolls the anchor into view.
  Element.prototype.scrollIntoView = vi.fn()
})

describe('Settings > Kiro account sign-in deep link, through the real tab', () => {
  it('opens the Kiro account tab on a non-KAS harness, mounts the card, and rings it', async () => {
    renderAt(KIRO_SIGN_IN_PATH)
    const anchor = await screen.findByTestId('kiro-sign-in-card', {}, LAZY_TAB)
    expect(anchor).toHaveAttribute('data-setting-key', KIRO_SIGN_IN_HIGHLIGHT_ANCHOR)
    // The full card on its own tab, not the in-detail fold.
    expect(anchor.tagName).not.toBe('DETAILS')
    expect(anchor).toHaveTextContent('Kiro sign-in')
    // Rung by the Settings highlight hook, and the parameter consumed.
    await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1), LAZY_TAB)
    expect(anchor.style.outlineOffset).toBe('4px')
    await waitFor(() => expect(loc()).toBe('/settings/kiro-account'))
    expect(screen.getAllByText('Kiro account').length).toBeGreaterThan(0)
  })

  it('has no Kiro account tab on a build that cannot use the sign-in and has no credits to show', async () => {
    choices.value = ['kiro', 'claude']
    harness.value = 'claude'
    renderAt('/settings/agent')
    await screen.findByRole('radio', { name: 'Kiro CLI' }, LAZY_TAB)
    expect(screen.queryByText('Kiro account')).toBeNull()
  })

  it('keeps the Kiro account tab for its Credits on a Kiro harness whose build cannot use the sign-in', async () => {
    choices.value = ['kiro', 'claude']
    renderAt('/settings/kiro-account')
    const card = await screen.findByTestId('kiro-credits-card', {}, LAZY_TAB)
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
    // No sign-in card means no draft to lose, so the failed reading (this
    // suite does not mock the usage read) keeps its hand-off to the agent.
    expect(await within(card).findByRole('button', { name: /Ask the agent/i }, LAZY_TAB)).toBeInTheDocument()
  })

  it('keeps one line and a link in the Kiro Agent row, and no card', async () => {
    renderAt('/settings/agent')
    const kas = await screen.findByRole('radio', { name: 'Kiro Agent' }, LAZY_TAB)
    fireEvent.click(kas)
    const link = await screen.findByTestId('kiro-account-status-link', {}, LAZY_TAB)
    expect(link).toHaveTextContent('Go to Kiro account')
    expect(link).toHaveAttribute('href', KIRO_SIGN_IN_PATH)
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })

  it.each(['sign out', 'switch account', 'logout', 'log out'])(
    'Settings search for "%s" lands on the sign-in card through the real tab',
    async query => {
      const nav = vi.fn()
      const provider = createSettingsProvider(nav as unknown as NavigateFunction, {
        decisionsEnabled: true,
        tipsEnabled: true,
        updateSwitches: { app: true, gateway: true },
      })
      const results = await Promise.resolve(provider.search(query))
      const hit = results.find(r => r.id === `settings:${KIRO_SIGN_IN_SETTING_ID}`)
      expect(hit, `no sign-in hit for "${query}"`).toBeTruthy()
      // Among the first results a user sees, not buried.
      expect(results.indexOf(hit!)).toBeLessThan(5)
      hit!.onActivate?.()
      const route = nav.mock.calls[0][0] as string

      renderAt(route)
      const anchor = await screen.findByTestId('kiro-sign-in-card', {}, LAZY_TAB)
      await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1), LAZY_TAB)
      await waitFor(() => expect(loc()).toBe('/settings/kiro-account'))
    },
  )
})
