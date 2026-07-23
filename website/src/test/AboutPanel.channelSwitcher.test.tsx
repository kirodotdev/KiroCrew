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
        <AboutPanel />
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
    // jsdom reports zero width, so SegmentedControl renders its responsive
    // dropdown mode: a toggle labeled with the current lane, options after
    // opening. Open it, then pick Insider.
    fireEvent.click(screen.getByRole('button', { name: /stable/i }))
    const insider = await screen.findByRole('button', { name: /insider/i })
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
    // Open the dropdown (toggle carries the current lane's label), then
    // re-pick the CURRENT lane -- onChange fires but the handler must not
    // call setChannel for a no-op selection.
    fireEvent.click(screen.getByRole('button', { name: /stable/i }))
    const stableButtons = await screen.findAllByRole('button', { name: /stable/i })
    fireEvent.click(stableButtons[stableButtons.length - 1]) // the option, not the toggle
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
  })
})
