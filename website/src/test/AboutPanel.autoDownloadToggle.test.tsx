//
// Contract under test: the desktop auto-download opt-out.
//
// Auto-download is ON by default in the shell, so this toggle is the ONLY place
// a user can decline the background download. Two properties matter beyond "it
// renders": it must read ON when the shell reports nothing (an older preload
// has no such field, and defaulting that to OFF would misreport a shell that is
// in fact downloading), and it must disappear entirely rather than throw when
// the bridge is absent.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

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
) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
    ...(setAutoDownload ? { setAutoDownload } : {}),
  }
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

function toggle() {
  return screen.getAllByRole('switch', { name: /auto-update on restart/i })[0]
}

// The toggle renders as soon as the BRIDGE exists, which is before getInfo()
// resolves -- so an assertion on aria-checked must wait for the info payload or
// it reads the pre-load default and passes for the wrong reason. The platform
// row is rendered only from `info`, so it is the arrival signal.
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

  it('is absent when the shell exposes no setter, instead of rendering a dead control', async () => {
    mount({ ...BASE_INFO, autoDownload: true })
    // Anchor on something that DOES render, so the negative assertion cannot
    // pass merely because the panel had not mounted yet.
    await waitFor(() => expect(screen.getAllByText(/check for updates/i)[0]).toBeTruthy())
    expect(screen.queryAllByRole('switch', { name: /auto-update on restart/i })).toHaveLength(0)
  })
})
