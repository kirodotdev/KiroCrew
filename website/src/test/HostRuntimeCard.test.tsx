//
// Contract under test: the Host runtime card on System > Services.
//
// The card is the ONLY consumer of the wsl:detect IPC channel, so these cases
// pin the consumer side of that contract: it must disappear entirely (not
// render an empty husk) where the answer cannot exist — a plain browser tab has
// no bridge, and a non-Windows shell has no WSL — and it must degrade to the
// unavailable copy when the main process rejects the sender (a connection
// window pointed at a remote gateway) rather than stay stuck empty.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import HostRuntimeCard from '../pages/system/HostRuntimeCard'

type DetectFn = () => Promise<unknown>

const RUNNING = {
  available: true,
  defaultDistro: 'Ubuntu',
  distros: [
    { name: 'Ubuntu', state: 'running', stateLabel: 'Running', version: 2, isDefault: true },
    { name: 'Debian', state: 'stopped', stateLabel: 'Stopped', version: 2, isDefault: false },
    { name: 'SUSE', state: 'unknown', stateLabel: 'In esecuzione', version: 2, isDefault: false },
  ],
}

function mount(detect: DetectFn | undefined, platform: string | undefined) {
  const win = window as unknown as Record<string, unknown>
  win.wslAPI = detect ? { detect } : undefined
  win.kirocrew = { isElectron: true, platform }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <HostRuntimeCard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  const win = window as unknown as Record<string, unknown>
  delete win.wslAPI
  delete win.kirocrew
})

describe('HostRuntimeCard', () => {
  it('renders nothing in a plain browser tab (no bridge)', () => {
    const { container } = mount(undefined, undefined)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing outside the Windows shell', () => {
    const detect = vi.fn()
    const { container } = mount(detect, 'darwin')
    expect(container).toBeEmptyDOMElement()
    expect(detect).not.toHaveBeenCalled()
  })

  it('lists distros under their OS state labels, with the default called out', async () => {
    const detect = vi.fn().mockResolvedValue(RUNNING)
    mount(detect, 'win32')

    // findBy*: the rows arrive with the query resolution — the title renders
    // in every state, so waiting on it would race the payload.
    await screen.findByText('Default')
    expect(detect).toHaveBeenCalledTimes(1)
    // Three distros, one row each; the default's name appears twice (default
    // row + its own row), so assert per-row state values instead of names.
    expect(screen.getByText('Running')).toBeTruthy()
    expect(screen.getByText('Stopped')).toBeTruthy()
    // An unrecognized locale's label is replaced by the Unknown copy — the raw
    // localized string must never masquerade as a state value a consumer
    // branches on.
    expect(screen.queryByText('In esecuzione')).toBeNull()
    expect(screen.getByText('Unknown')).toBeTruthy()
  })

  it('shows the unknown-value placeholder while detection is in flight, never a false negative', async () => {
    const detect = vi.fn().mockReturnValue(new Promise(() => {}))
    mount(detect, 'win32')

    expect(await screen.findByText('Host runtime')).toBeTruthy()
    expect(screen.getByText('—')).toBeTruthy()
    expect(screen.queryByText(/unavailable/i)).toBeNull()
  })

  it('reports unavailable when the main process rejects the sender', async () => {
    // The wsl:detect handler throws for any WebContents the local gateway did
    // not serve (e.g. a connection window on a remote gateway).
    const detect = vi.fn().mockRejectedValue(new Error('wsl:detect is restricted to the local dashboard'))
    mount(detect, 'win32')

    await waitFor(() =>
      expect(screen.getByText('WSL2 is not available on this host')).toBeTruthy(),
    )
  })

  it('reports unavailable when WSL is present but disabled or uninstalled', async () => {
    const detect = vi.fn().mockResolvedValue({ available: false, distros: [], defaultDistro: null, reason: 'wsl-not-found' })
    mount(detect, 'win32')

    await waitFor(() =>
      expect(screen.getByText('WSL2 is not available on this host')).toBeTruthy(),
    )
  })

  it('names the no-distro case instead of collapsing to a bare title', async () => {
    // WSL present, zero version-2 distros (WSL1-only machine).
    const detect = vi.fn().mockResolvedValue({ available: true, distros: [], defaultDistro: null, reason: 'no-wsl2-distros' })
    mount(detect, 'win32')

    await waitFor(() =>
      expect(screen.getByText('No WSL2 distributions installed')).toBeTruthy(),
    )
  })
})
