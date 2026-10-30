// Channel switcher (stable ⇄ insider opt-in) in Settings > About.
//
// Contract under test:
// - switchable production builds render the Stable | Insider control
// - picking the other lane calls updateAPI.setChannel with that lane
// - non-switchable builds (nightly / no setChannel bridge) keep the plain
//   read-only channel row
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

function mountWithUpdateApi(info: Record<string, unknown>, setChannel?: (c: string) => Promise<{ ok: boolean }>) {
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState: () => () => {},
    check: vi.fn().mockResolvedValue({ ok: true }),
    download: vi.fn().mockResolvedValue({ ok: true }),
    install: vi.fn().mockResolvedValue({ ok: true }),
    getInfo: vi.fn().mockResolvedValue(info),
    ...(setChannel ? { setChannel } : {}),
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

describe('AboutPanel channel switcher', () => {
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

  it('renders the switcher for a switchable production build and sets the other lane', async () => {
    const setChannel = vi.fn().mockResolvedValue({ ok: true })
    mountWithUpdateApi(
      {
        version: '0.1.0',
        channel: 'stable',
        stampedChannel: 'stable',
        channelSwitchable: true,
        channelPreference: '',
        platform: 'darwin-arm64',
        packaged: true,
      },
      setChannel,
    )
    const switcher = await screen.findByTestId('channel-switcher')
    expect(switcher).toBeTruthy()
    // jsdom reports zero width, so the responsive measurement would collapse
    // the control to its dropdown mode -- where the overlay is trapped beneath
    // the Platform row by .card-glow's `> * { z-index: 1 }`. collapse={false}
    // keeps both lanes rendered side by side, one click away.
    // Anchored names: the disclosure link in the same row is a <button> whose
    // label names BOTH channels, so an unanchored /stable/i matches it too.
    const stable = screen.getByRole('radio', { name: /^Stable$/ })
    const insider = screen.getByRole('radio', { name: /^Insider$/ })
    expect(switcher.contains(stable)).toBe(true)
    expect(switcher.contains(insider)).toBe(true)
    fireEvent.click(insider)
    await waitFor(() => expect(setChannel).toHaveBeenCalledWith('insider'))
  })

  it('does not call setChannel when re-picking the current lane', async () => {
    const setChannel = vi.fn().mockResolvedValue({ ok: true })
    mountWithUpdateApi(
      { version: '0.1.0', channel: 'stable', stampedChannel: 'stable', channelSwitchable: true, packaged: true },
      setChannel,
    )
    await screen.findByTestId('channel-switcher')
    // Re-pick the CURRENT lane -- onChange fires but the handler must not call
    // setChannel for a no-op selection.
    fireEvent.click(screen.getByRole('radio', { name: /^Stable$/ }))
    await new Promise(r => setTimeout(r, 20))
    expect(setChannel).not.toHaveBeenCalled()
  })

  it('nightly (channelSwitchable=false) keeps the read-only channel row', async () => {
    mountWithUpdateApi({
      version: '0.1.0-nightly.20260722233638',
      channel: 'nightly',
      stampedChannel: 'nightly',
      channelSwitchable: false,
      packaged: true,
    })
    await screen.findByText('nightly')
    expect(screen.queryByTestId('channel-switcher')).toBeNull()
    expect(screen.queryByTestId('locked-by-policy-badge')).toBeNull()
  })

  it('a channel set by policy shows the read-only row with the locked badge', async () => {
    mountWithUpdateApi({
      version: '0.1.0',
      channel: 'stable',
      stampedChannel: 'stable',
      channelSwitchable: false,
      channelLockedByPolicy: true,
      packaged: true,
    })
    await screen.findByTestId('locked-by-policy-badge')
    expect(screen.queryByTestId('channel-switcher')).toBeNull()
  })

  it('a build that cannot follow the policy channel names it and links its build', async () => {
    mountWithUpdateApi({
      version: '0.1.0-nightly.20261026',
      channel: 'nightly',
      stampedChannel: 'nightly',
      channelSwitchable: false,
      channelLockedByPolicy: true,
      requiredChannel: 'stable',
      packaged: true,
      disabled: 'policy-channel',
      downloadUrl: 'https://downloads.example.dev/desktop/stable/latest/KiroCrew.dmg',
    })
    const notice = await screen.findByTestId('updates-policy-channel')
    expect(notice.textContent).toContain("This nightly build can't switch to stable updates")
    expect(notice.textContent).toContain('Your settings, chats and files are kept.')
    const link = notice.querySelector('a')
    expect(link?.getAttribute('href')).toBe('https://downloads.example.dev/desktop/stable/latest/KiroCrew.dmg')
    expect(link?.textContent).toBe('Install the stable app')
    // The row names the installed lane and the requirement, not one for the other.
    expect(screen.getByTestId('channel-requires').textContent).toBe('requires stable')
  })
})
