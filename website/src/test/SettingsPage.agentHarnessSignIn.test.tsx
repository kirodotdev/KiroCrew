/**
 * The chat's "Sign in to Kiro" link, end to end through the REAL Agent Harness tab.
 *
 * `SettingsPage.agentHarness.test.tsx` pins routing and landing with the tab
 * replaced by a sentinel that mounts the anchor by itself. Since the redesign the
 * anchor lives INSIDE the KAS row's detail, which is open only when KAS is the
 * checked row -- so landing depends on the tab reading the highlight and opening
 * that row, which a sentinel cannot prove. This renders the real tab (Kiro CLI
 * configured, so KAS is NOT the row that opens by default) and asserts the whole
 * chain: the KAS row is checked, the anchor mounts in its detail, the highlight
 * hook rings it and strips the parameter.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kirocrewConfig: vi.fn(() => Promise.resolve({ agent: { acp_backend: '' } })),
      patchConfig: vi.fn(() => Promise.resolve({})),
      acpBackends: vi.fn(() =>
        Promise.resolve({
          backends: ['', 'kas', 'claude'].map(id => ({
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
    },
  }
})

vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchema: () =>
    new Map([['agent.acp_backend', { path: 'agent.acp_backend', type: 'enum', enum: ['', 'claude', 'kas'] }]]),
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

vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

import SettingsPage from '../pages/SettingsPage'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
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

beforeEach(() => {
  sessionStorage.clear()
  // Not available in jsdom; the hook scrolls the anchor into view.
  Element.prototype.scrollIntoView = vi.fn()
})

describe('Settings > Agent Harness sign-in deep link, through the real tab', () => {
  it('opens the KAS row, mounts the sign-in inside its detail, and rings it', async () => {
    renderAt(KIRO_SIGN_IN_PATH)
    // Kiro CLI is the configured harness, so the tab would open on it by itself;
    // the highlight is what moves the check to KAS.
    const kas = await screen.findByRole('radio', { name: 'KAS (kiro-agent)' })
    await waitFor(() => expect(kas).toBeChecked())
    expect(screen.getByRole('radio', { name: 'Kiro CLI' })).toHaveAttribute('aria-current', 'true')

    const anchor = await screen.findByTestId('kiro-sign-in-card')
    expect(anchor).toHaveAttribute('data-setting-key', KIRO_SIGN_IN_HIGHLIGHT_ANCHOR)
    expect(screen.getByTestId('agent-harness-detail').contains(anchor)).toBe(true)
    // The compact, in-detail form: a section with its own heading, not a Card.
    expect(anchor.tagName).toBe('SECTION')
    expect(anchor).toHaveTextContent('Kiro sign-in')

    // Rung by the Settings highlight hook, and the parameter consumed.
    await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalledTimes(1))
    expect(anchor.style.outlineOffset).toBe('4px')
    await waitFor(() => expect(loc()).toBe('/settings/agent'))
  })

  it('without the highlight, opens on the configured harness and shows no sign-in', async () => {
    renderAt('/settings/agent')
    const kiro = await screen.findByRole('radio', { name: 'Kiro CLI' })
    await waitFor(() => expect(kiro).toBeChecked())
    expect(screen.getByRole('radio', { name: 'KAS (kiro-agent)' })).not.toBeChecked()
    expect(screen.queryByTestId('kiro-sign-in-card')).toBeNull()
  })
})
