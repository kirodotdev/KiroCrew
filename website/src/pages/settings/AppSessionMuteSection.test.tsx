/**
 * Settings → Notifications → Sources → App sessions: one switch per enabled app,
 * writing `apps.<name>.mute_sessions`. A stored mute shows as on (a disabled app
 * keeps its row while its mute is stored), and flipping a switch PATCHes that
 * app's path and nothing else.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    listApps: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { AppSessionMuteSection } from './AppSessionMuteSection'
import type { InstalledApp } from '../../components/appstore/types'

const app = (name: string, displayName: string, enabled = true) =>
  ({ name, displayName, enabled, version: '1.0.0', installedAt: '', manifest: { name, version: '1.0.0', displayName, description: '', author: '' } }) as InstalledApp

let serverCfg: { apps?: Record<string, { mute_sessions?: unknown }> }

beforeEach(() => {
  vi.clearAllMocks()
  serverCfg = { apps: { 'zeta-app': { mute_sessions: true }, 'quiet-app': { mute_sessions: true } } }
  vi.mocked(api.kirocrewConfig).mockImplementation(async () => structuredClone(serverCfg))
  vi.mocked(api.patchConfig).mockImplementation(async (path: string, value: unknown) => {
    const [, name] = path.split('.')
    serverCfg.apps = { ...serverCfg.apps, [name]: { mute_sessions: value } }
    return { ok: true }
  })
  vi.mocked(api.listApps).mockResolvedValue([app('zeta-app', 'Zeta'), app('alpha-app', 'Alpha'), app('off-app', 'Off', false), app('quiet-app', 'Quiet', false)])
})

describe('AppSessionMuteSection', () => {
  it('shows each app\'s stored mute and writes only the flipped app\'s path', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={qc}><AppSessionMuteSection /></QueryClientProvider>)
    const alpha = await screen.findByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Alpha' }) })
    const zeta = screen.getByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Zeta' }) })
    expect(screen.queryByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Off' }) })).toBeNull()
    await waitFor(() => expect(zeta).toHaveAttribute('aria-checked', 'true'))
    expect(alpha).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Quiet' }) })).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(alpha)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('apps.alpha-app.mute_sessions', true))
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(alpha).toHaveAttribute('aria-checked', 'true'))
  })

  it('a second quick click on one switch waits for the first save, so the later click is stored', async () => {
    const resolvers: Array<() => void> = []
    vi.mocked(api.patchConfig).mockImplementation((path: string, value: unknown) => new Promise(resolve => {
      resolvers.push(() => {
        const [, name] = path.split('.')
        serverCfg.apps = { ...serverCfg.apps, [name]: { mute_sessions: value } }
        resolve({ ok: true })
      })
    }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={qc}><AppSessionMuteSection /></QueryClientProvider>)
    const alpha = await screen.findByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Alpha' }) })
    const zeta = screen.getByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Zeta' }) })
    await waitFor(() => expect(zeta).toHaveAttribute('aria-checked', 'true'))

    fireEvent.click(alpha)
    await waitFor(() => expect(alpha).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(alpha)
    await waitFor(() => expect(alpha).toHaveAttribute('aria-checked', 'false'))
    await act(async () => {})
    // Answer the newest request first, the order that stores the first click
    // when both are in flight; a queued second save only starts afterwards.
    resolvers.pop()?.()
    resolvers.pop()?.()
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledTimes(2))
    resolvers.pop()?.()
    expect(vi.mocked(api.patchConfig).mock.calls.map(call => call[1])).toEqual([true, false])
    await waitFor(() => expect(serverCfg.apps?.['alpha-app']?.mute_sessions).toBe(false))
    await waitFor(() => expect(alpha).toHaveAttribute('aria-checked', 'false'))
  })

  it('a failed app list load offers the agent hand-off', async () => {
    vi.mocked(api.listApps).mockRejectedValue(new Error('boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={qc}><AppSessionMuteSection /></QueryClientProvider>)
    const notice = await screen.findByTestId('app-session-mutes-error')
    expect(within(notice).getByText(i18nT('components.askAgent.ask_the_agent'))).toBeTruthy()
  })

  it('a failed config load says why the switches are unavailable', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new Error('boom'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={qc}><AppSessionMuteSection /></QueryClientProvider>)
    const notice = await screen.findByTestId('app-session-mutes-error')
    expect(within(notice).getByText(i18nT('pages.settings.appSessionMutes.config_load_failed'))).toBeTruthy()
    const alpha = await screen.findByRole('switch', { name: i18nT('pages.settings.appSessionMutes.row_label', { app: 'Alpha' }) })
    expect(alpha).toHaveAttribute('aria-disabled', 'true')
  })
})
