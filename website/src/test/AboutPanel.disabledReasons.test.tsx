// Which explanation Settings > About shows when auto-update is switched off.
//
// Contract under test: every `disabled` reason the Electron updater can report
// renders its OWN message. The branch is a ternary chain ending in the platform
// string, so an unmapped reason does not fail loudly -- it silently renders
// "unavailable on this platform", which blames the OS and hides the real fix.
// That is exactly what happened when `disabled: 'channel'` was added for Windows
// on the stable channel: the platform HAS an update lane, just not on the channel
// the install tracks, and the way back is the switcher in this same panel.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

function mountWithUpdateApi(info: Record<string, unknown>) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
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

describe('AboutPanel disabled-update reasons', () => {
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
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    vi.unstubAllGlobals()
  })

  it('names the channel, not the platform, when the channel has no lane', async () => {
    mountWithUpdateApi({
      version: '0.1.0',
      platform: 'win32-x64',
      packaged: true,
      disabled: 'channel',
    })
    expect(await screen.findByText(/release channel has no builds/i)).toBeTruthy()
    // The platform message would be the wrong diagnosis and offers no way back.
    expect(screen.queryByText(/unavailable in this build on this platform/i)).toBeNull()
  })

  it('still names the platform when the platform itself has no lane', async () => {
    mountWithUpdateApi({
      version: '0.1.0',
      platform: 'freebsd-x64',
      packaged: true,
      disabled: 'platform',
    })
    expect(
      await screen.findByText(/unavailable in this build on this platform/i),
    ).toBeTruthy()
    expect(screen.queryByText(/release channel has no builds/i)).toBeNull()
  })

  it('keeps the dev-build and read-only-volume reasons distinct', async () => {
    mountWithUpdateApi({ version: '0.1.0', platform: 'darwin-arm64', disabled: 'dev' })
    expect(await screen.findByText(/development build/i)).toBeTruthy()
    cleanup()

    mountWithUpdateApi({
      version: '0.1.0',
      platform: 'darwin-arm64',
      packaged: true,
      disabled: 'volume',
    })
    expect(await screen.findByText(/read-only disk image/i)).toBeTruthy()
  })
})
