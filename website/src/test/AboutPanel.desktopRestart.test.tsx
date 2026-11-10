// The desktop app's Settings → About offers Restart gateway through the app
// (window.gatewayRestartAPI), never through the gateway's own POST /api/restart:
// that endpoint replaces the process in place, out from under the app's
// supervisor. The row shows only where the app says its restart works, and it
// is the card's one restart control.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { i18nT } from '../i18n/t'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

const BASE_INFO = {
  version: '0.1.0',
  channel: 'stable',
  stampedChannel: 'stable',
  channelSwitchable: true,
  channelPreference: '',
  platform: 'darwin-arm64',
  packaged: true,
}

type Win = { updateAPI?: unknown; gatewayRestartAPI?: unknown }

function stubFetch() {
  const posts: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: unknown, init?: RequestInit) => {
    if (init?.method === 'POST') posts.push(String(input))
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '{}',
      headers: new Headers({ 'content-type': 'application/json' }),
    }
  }))
  return posts
}

function mountDesktop({
  info = {},
  status = {},
  bridge,
}: {
  info?: Record<string, unknown>
  status?: Record<string, unknown>
  bridge?: { available: () => Promise<boolean>; restart: () => Promise<string> } | null
} = {}) {
  const w = window as unknown as Win
  w.updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue({ ...BASE_INFO, ...info }),
  }
  if (bridge) w.gatewayRestartAPI = bridge
  store.dispatch(sseStatus({ ...BLANK_STATUS, ...status } as never))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

function bridgeAnswering(available: boolean, restart: () => Promise<string> = async () => 'restarting') {
  return { available: vi.fn(async () => available), restart: vi.fn(restart) }
}

/** Every restart control the card can draw, desktop and gateway-side. */
function restartControls() {
  return ['gateway-restart-desktop', 'gateway-restart-standing', 'gateway-restart']
    .flatMap(id => screen.queryAllByTestId(id))
}

describe('desktop About: Restart gateway', () => {
  beforeEach(() => {
    const w = window as unknown as Win
    delete w.updateAPI
    delete w.gatewayRestartAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('restarts through the app after the confirm click, never through /api/restart', async () => {
    const posts = stubFetch()
    const bridge = bridgeAnswering(true)
    mountDesktop({ bridge })

    const row = await screen.findByTestId('desktop-gateway-restart-row')
    expect(row.textContent).toContain(i18nT('pages.settings.aboutPanel.desktop_restart_row_note'))
    fireEvent.click(screen.getByTestId('gateway-restart-desktop'))
    expect(bridge.restart).not.toHaveBeenCalled()
    expect(screen.getByTestId('gateway-restart-desktop').textContent).toContain(i18nT('pages.settings.aboutPanel.restart_confirm'))

    fireEvent.click(screen.getByTestId('gateway-restart-desktop'))
    await waitFor(() => expect(bridge.restart).toHaveBeenCalledTimes(1))
    // "restarting": the app is replacing this page, so the control stays busy.
    await waitFor(() => {
      const btn = screen.getByTestId('gateway-restart-desktop') as HTMLButtonElement
      expect(btn.disabled).toBe(true)
      expect(btn.textContent).toContain(i18nT('pages.settings.aboutPanel.restarting'))
    })
    expect(posts.some(url => url.includes('/api/restart'))).toBe(false)
  })

  it('says so when the app refuses or the bridge fails, and keeps the control', async () => {
    for (const restart of [async () => 'unavailable', async () => { throw new Error('gate') }]) {
      stubFetch()
      mountDesktop({ bridge: bridgeAnswering(true, restart) })
      await screen.findByTestId('desktop-gateway-restart-row')
      fireEvent.click(screen.getByTestId('gateway-restart-desktop'))
      fireEvent.click(screen.getByTestId('gateway-restart-desktop'))
      const error = await screen.findByTestId('desktop-gateway-restart-error')
      expect(error.textContent).toContain(i18nT('pages.settings.aboutPanel.desktop_restart_unavailable'))
      expect((screen.getByTestId('gateway-restart-desktop') as HTMLButtonElement).disabled).toBe(false)
      cleanup()
      vi.unstubAllGlobals()
    }
  })

  it('offers nothing for a gateway this app does not run (adopted gateway, remote crew, no bridge)', async () => {
    for (const bridge of [bridgeAnswering(false), null]) {
      stubFetch()
      mountDesktop({ bridge, status: { update_available: false, update_check_status: 'succeeded' } })
      await screen.findByText(i18nT('pages.settings.aboutPanel.check_for_updates'))
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(screen.queryByTestId('desktop-gateway-restart-row')).toBeNull()
      expect(restartControls()).toHaveLength(0)
      cleanup()
      vi.unstubAllGlobals()
      delete (window as unknown as Win).gatewayRestartAPI
    }
  })

  it('is offered on an externally managed install: restarting is not updating', async () => {
    stubFetch()
    mountDesktop({ info: { disabled: 'externally-managed', managedBy: 'Homebrew' }, bridge: bridgeAnswering(true) })
    await screen.findByTestId('externally-managed-updates')
    await screen.findByTestId('desktop-gateway-restart-row')
    expect(restartControls()).toHaveLength(1)
  })

  it('is the only restart control when the policy-governed gateway card renders its own update body', async () => {
    stubFetch()
    for (const [status, bodyMarker] of [
      // Self-updatable: the body's Update button, which drew a standing
      // gateway-side Restart row beside it.
      [{ update_available: true, update_check_status: 'succeeded', update_can_apply: true, update_managed_by: 'command' },
        () => screen.findByText(i18nT('pages.settings.aboutPanel.update_now'))],
      // Manual update: the body's installer-command card, which drew its own Restart.
      [{ update_available: true, update_check_status: 'succeeded', update_can_apply: false, update_command: 'brew upgrade kiro-crew' },
        () => screen.findByTestId('manual-update-command')],
    ] as const) {
      mountDesktop({
        info: { policyGoverned: true },
        status: { update_bundled_by_app: true, ...status },
        bridge: bridgeAnswering(true),
      })
      await screen.findByTestId('gateway-policy-managed-note')
      await bodyMarker()
      await screen.findByTestId('desktop-gateway-restart-row')
      expect(restartControls().map(el => el.getAttribute('data-testid'))).toEqual(['gateway-restart-desktop'])
      cleanup()
    }
  })

  it('keeps the gateway-side restart on a policy-governed card whose gateway the app adopted', async () => {
    stubFetch()
    for (const [status, kept] of [
      [{ update_available: true, update_check_status: 'succeeded', update_can_apply: true, update_managed_by: 'command' },
        'gateway-restart-standing'],
      [{ update_available: true, update_check_status: 'succeeded', update_can_apply: false, update_command: 'brew upgrade kiro-crew' },
        'gateway-restart'],
    ] as const) {
      const bridge = bridgeAnswering(false)
      mountDesktop({ info: { policyGoverned: true }, status: { update_bundled_by_app: true, ...status }, bridge })
      await screen.findByTestId('gateway-policy-managed-note')
      await screen.findByTestId(kept)
      await waitFor(() => expect(bridge.available).toHaveBeenCalled())
      expect(restartControls().map(el => el.getAttribute('data-testid'))).toEqual([kept])
      cleanup()
    }
  })
})
