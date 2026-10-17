//
// Contract under test: the desktop auto-download opt-out.
//
// Auto-download is ON by default in the shell, and this switch (shared with the
// What's-new modal) is how a user declines the background download. Beyond "it
// renders": it must read ON when the shell reports nothing (an older preload
// has no such field, and defaulting that to OFF would misreport a shell that is
// in fact downloading), it must be held until the shell has answered, and it
// must disappear entirely rather than throw when the bridge is absent.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { updateInfoQuery } from '../api/updateInfoQuery'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18nT } from '../i18n/t'

const BASE_INFO = {
  version: '0.1.0',
  channel: 'stable',
  stampedChannel: 'stable',
  channelSwitchable: true,
  channelPreference: '',
  platform: 'darwin-arm64',
  packaged: true,
}

function mount(
  info: Record<string, unknown>,
  setAutoDownload?: (v: boolean) => Promise<{ ok: boolean }>,
  { noGetInfo = false }: { noGetInfo?: boolean } = {},
) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    ...(noGetInfo ? {} : { getInfo: vi.fn().mockResolvedValue(info) }),
    ...(setAutoDownload ? { setAutoDownload } : {}),
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { qc, ...render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  ) }
}

function toggle() {
  return screen.getAllByRole('switch', { name: /install app updates automatically/i })[0]
}

// The row renders as soon as the BRIDGE exists, held until getInfo() resolves,
// so an assertion on aria-checked must wait for the info payload or it reads
// the held placeholder and passes for the wrong reason. The platform row is
// rendered only from `info`, so it is the arrival signal.
async function waitForInfo() {
  await waitFor(() => expect(screen.getAllByText('darwin-arm64')[0]).toBeTruthy())
}

describe('AboutPanel desktop auto-download toggle', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({}),
      text: async () => '',
      headers: new Headers({ 'content-type': 'application/json' }),
    }))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('reads ON when the shell reports autoDownload: true', async () => {
    mount({ ...BASE_INFO, autoDownload: true }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    expect(toggle().getAttribute('aria-checked')).toBe('true')
  })

  it('reads OFF only when the shell explicitly reports false', async () => {
    mount({ ...BASE_INFO, autoDownload: false }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    expect(toggle().getAttribute('aria-checked')).toBe('false')
  })

  it('reads ON when the field is absent (older shell, but auto-download is the default)', async () => {
    mount({ ...BASE_INFO }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    // `undefined` must not render as OFF: that would tell the user nothing is
    // downloading while the shell downloads anyway.
    expect(toggle().getAttribute('aria-checked')).toBe('true')
  })

  it('turning it off calls the bridge with false', async () => {
    const setAutoDownload = vi.fn().mockResolvedValue({ ok: true })
    mount({ ...BASE_INFO, autoDownload: true }, setAutoDownload)
    await waitForInfo()
    fireEvent.click(toggle())
    await waitFor(() => expect(setAutoDownload).toHaveBeenCalledWith(false))
  })

  it('moves while it saves, then keeps the info the shell answers with, with no re-read', async () => {
    let answer: (r: { ok: boolean; info?: Record<string, unknown> }) => void = () => {}
    const setAutoDownload = vi.fn(() => new Promise<{ ok: boolean }>(resolve => { answer = resolve }))
    mount({ ...BASE_INFO, autoDownload: true }, setAutoDownload)
    await waitForInfo()
    const getInfo = (window as unknown as { updateAPI: { getInfo: ReturnType<typeof vi.fn> } }).updateAPI.getInfo

    fireEvent.click(toggle())
    await waitFor(() => expect(toggle().getAttribute('aria-checked')).toBe('false'))
    await act(async () => { answer({ ok: true, info: { ...BASE_INFO, autoDownload: false } }) })
    expect(toggle().getAttribute('aria-checked')).toBe('false')
    expect(getInfo).toHaveBeenCalledTimes(1)
  })

  it('says a re-read failed, and keeps the last value read', async () => {
    const { qc } = mount({ ...BASE_INFO, autoDownload: false }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    const api = (window as unknown as { updateAPI: { getInfo: ReturnType<typeof vi.fn> } }).updateAPI
    api.getInfo.mockRejectedValue(new Error('zzq'))
    await act(async () => { await qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey }) })
    expect(await screen.findByTestId('auto-download-refresh-error')).toHaveTextContent(
      i18nT('pages.settings.aboutPanel.auto_download_refresh_failed'))
    expect(toggle().getAttribute('aria-checked')).toBe('false')
  })

  it('shows what the app switch does beside it, not behind a tip', async () => {
    // Read side by side with the gateway switch, so the pair tells apart at a glance.
    mount({ ...BASE_INFO, autoDownload: true }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    const row = toggle().closest('[role="button"]') as HTMLElement
    expect(row).toHaveTextContent(i18nT('pages.settings.aboutPanel.install_app_updates_automatically_hint'))
    expect(row.querySelector('[data-settings-hint]')).toBeNull()
  })

  it('holds the row, anchor included, until a slow getInfo() answers', async () => {
    // A search deep link probes for the row once, ~100 ms after the tab opens;
    // a getInfo() slower than that must not leave it nothing to find.
    mount(new Promise(resolve => setTimeout(() => resolve({ ...BASE_INFO, autoDownload: false }), 150)) as never,
      vi.fn().mockResolvedValue({ ok: true }))
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'about.install-app-updates-automatically')
    expect(document.querySelector(`[data-setting-label="${i18nT(entry!.labelKey!)}"]`)).not.toBeNull()
    // Held at the shell's own default, never at a guessed OFF.
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(toggle().getAttribute('aria-checked')).toBe('true')

    await waitForInfo()
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    expect(toggle().getAttribute('aria-checked')).toBe('false')
  })

  it('goes away once the updater reports itself disabled', async () => {
    const { qc } = mount({ ...BASE_INFO, disabled: 'dev' }, vi.fn().mockResolvedValue({ ok: true }))
    await waitFor(() => expect(qc.getQueryState(updateInfoQuery.queryKey)?.status).toBe('success'))
    expect(screen.queryByRole('switch', { name: /install app updates automatically/i })).toBeNull()
  })

  it('says a failed re-read on the card when no app switch is there to say it', async () => {
    const { qc } = mount({ ...BASE_INFO, disabled: 'dev' }, vi.fn().mockResolvedValue({ ok: true }))
    await waitFor(() => expect(qc.getQueryState(updateInfoQuery.queryKey)?.status).toBe('success'))
    expect(screen.queryByTestId('update-info-error')).toBeNull()
    const api = (window as unknown as { updateAPI: { getInfo: ReturnType<typeof vi.fn> } }).updateAPI
    api.getInfo.mockRejectedValue(new Error('zzq'))
    await act(async () => { await qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey }) })
    expect(await screen.findByTestId('update-info-error')).toBeTruthy()
  })

  it('says a failed re-read once, on the app switch, when the switch is drawn', async () => {
    const { qc } = mount({ ...BASE_INFO, autoDownload: true }, vi.fn().mockResolvedValue({ ok: true }))
    await waitForInfo()
    const api = (window as unknown as { updateAPI: { getInfo: ReturnType<typeof vi.fn> } }).updateAPI
    api.getInfo.mockRejectedValue(new Error('zzq'))
    await act(async () => { await qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey }) })
    expect(await screen.findByTestId('auto-download-refresh-error')).toBeTruthy()
    expect(screen.queryByTestId('update-info-error')).toBeNull()
  })

  it('stays usable at the shell default after a failed getInfo(), with the notice held through a retry', async () => {
    let answer: (info: Record<string, unknown>) => void = () => {}
    const { qc } = mount({}, vi.fn().mockResolvedValue({ ok: true }))
    const api = (window as unknown as { updateAPI: { getInfo: ReturnType<typeof vi.fn> } }).updateAPI
    api.getInfo.mockReset()
    api.getInfo.mockRejectedValueOnce(new Error('zzq')).mockImplementation(() => new Promise(resolve => { answer = resolve }))
    await act(async () => { await qc.resetQueries({ queryKey: updateInfoQuery.queryKey }) })
    expect(await screen.findByTestId('update-info-error')).toBeTruthy()
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    expect(toggle().getAttribute('aria-checked')).toBe('true')

    act(() => { void qc.invalidateQueries({ queryKey: updateInfoQuery.queryKey }) })
    await waitFor(() => expect(qc.getQueryState(updateInfoQuery.queryKey)?.fetchStatus).toBe('fetching'))
    expect(screen.getByTestId('update-info-error')).toBeTruthy()
    act(() => answer({ ...BASE_INFO, autoDownload: false }))
    await waitFor(() => expect(screen.queryByTestId('update-info-error')).toBeNull())
  })

  describe("the gateway's switch in a desktop window", () => {
    const gatewayRow = () => document.querySelector('[data-setting-key="auto_update"]')
    const effect = (update_auto_effect: string) =>
      store.dispatch(sseStatus({ uptime: '1m', sessions: 0, messages: 0, update_auto_effect } as never))
    afterEach(() => { store.dispatch(sseStatus({ uptime: '1m', sessions: 0, messages: 0 } as never)) })

    // The rule's case table is `utils/updateSwitches.test.ts`; this proves About
    // feeds it the updater's answer and the effect.
    it('is drawn where the app updater is disabled and the gateway installs', async () => {
      // A dev or package-managed build attached to a cli.sh service: that gateway
      // installs and restarts on its own switch, so the switch must be reachable,
      // and a `key:auto_update` deep link must find it.
      effect('install')
      const { qc } = mount({ ...BASE_INFO, disabled: 'dev' }, vi.fn().mockResolvedValue({ ok: true }))
      await waitFor(() => expect(qc.getQueryState(updateInfoQuery.queryKey)?.status).toBe('success'))
      expect(gatewayRow()).not.toBeNull()
      expect(screen.queryByRole('switch', { name: /install app updates automatically/i })).toBeNull()
    })

    it("is not drawn for the app's own gateway, which reports notify", async () => {
      effect('notify')
      mount({ ...BASE_INFO, autoDownload: true }, vi.fn().mockResolvedValue({ ok: true }))
      await waitForInfo()
      expect(gatewayRow()).toBeNull()
    })
  })

  it('says the read failed on a shell with no getInfo', async () => {
    mount(undefined as never, vi.fn().mockResolvedValue({ ok: true }), { noGetInfo: true })
    expect(await screen.findByTestId('update-info-error')).toBeTruthy()
  })

  it('is absent when the shell exposes no setter, instead of rendering a dead control', async () => {
    mount({ ...BASE_INFO, autoDownload: true })
    // Anchor on something that DOES render, so the negative assertion cannot
    // pass merely because the panel had not mounted yet.
    await waitFor(() => expect(screen.getAllByText(/check for updates/i)[0]).toBeTruthy())
    expect(screen.queryAllByRole('switch', { name: /install app updates automatically/i })).toHaveLength(0)
  })
})
