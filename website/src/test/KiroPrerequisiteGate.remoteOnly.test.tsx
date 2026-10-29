/**
 * KiroPrerequisiteGate: the desktop app's "connect to a crew on another
 * machine" exit from first-run setup. The main process owns the switch; the page only
 * shows the exit where the bridge says it works and starts it.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate from '../components/KiroPrerequisiteGate'
import { renderWithProviders } from './helpers'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn(),
}))

vi.mock('../api/client', async () => ({
  ApiError: (await import('../api/apiError')).ApiError,
  api: {
    kiroPrerequisite: vi.fn(),
    repairKiroPrerequisiteSpecs: vi.fn(),
    kirocrewConfig: vi.fn(),
    acpBackends: vi.fn(),
    acpBackendRecheck: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api, type KiroPrerequisiteStatus } from '../api/client'
import { status } from './KiroPrerequisiteGateTestUtils'

type Outcome = 'switching' | 'cancelled' | 'unavailable'

function installBridge({
  available = true,
  start = async (): Promise<Outcome> => 'cancelled',
}: {
  available?: boolean | (() => Promise<boolean>)
  start?: () => Promise<Outcome>
} = {}) {
  const bridge = {
    available: vi.fn(typeof available === 'function' ? available : async () => available),
    start: vi.fn(start),
  }
  window.remoteCrewSetupAPI = bridge
  return bridge
}

const QUESTION = 'Your crews run on another computer?'
const CONNECT = 'Connect to a crew on another machine'
// Initial waits cover prerequisite query -> config query -> setup screen render
// -> bridge available(); enterprise-locked screens skip config and the bridge.
const SETUP_CHAIN_TIMEOUT_MS = 5_000
const SETUP_CHAIN_WAIT = { timeout: SETUP_CHAIN_TIMEOUT_MS }
const FAILURE_SCREENS: {
  name: string
  overrides: Partial<KiroPrerequisiteStatus>
  heading: RegExp
}[] = [
  {
    name: 'sandbox unavailable',
    overrides: { sandbox_unavailable: true, installed: true, sandbox_failure_kind: 'no_backend' },
    heading: /installed.*verified/i,
  },
  {
    name: 'probe error',
    overrides: { installed: true, probe_error: 'CLI probe failed', probe_status: 1 },
    heading: /could not check Kiro CLI/i,
  },
  {
    name: 'outdated CLI',
    overrides: { installed: true, acp_supported: false },
    heading: /out of date/i,
  },
]

describe('KiroPrerequisiteGate remote-only exit', () => {
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kiroPrerequisite).mockReset().mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockReset().mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockReset().mockResolvedValue({ backends: [] })
  })

  afterEach(() => {
    delete window.remoteCrewSetupAPI
  })

  it('is absent in a browser, where there is no bridge', async () => {
    render()
    await screen.findByText('Get Kiro CLI', undefined, SETUP_CHAIN_WAIT)
    expect(screen.queryByText(QUESTION)).toBeNull()
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
  })

  it('is absent where the desktop app says the switch would be refused', async () => {
    const bridge = installBridge({ available: false })
    render()
    await screen.findByText('Get Kiro CLI', undefined, SETUP_CHAIN_WAIT)
    await waitFor(() => expect(bridge.available).toHaveBeenCalled(), SETUP_CHAIN_WAIT)
    await act(async () => { await expect(bridge.available.mock.results[0].value).resolves.toBe(false) })
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
  })

  it('is absent when the availability check itself fails', async () => {
    const bridge = installBridge({ available: () => Promise.reject(new Error('refused')) })
    render()
    await screen.findByText('Get Kiro CLI', undefined, SETUP_CHAIN_WAIT)
    await waitFor(() => expect(bridge.available).toHaveBeenCalled(), SETUP_CHAIN_WAIT)
    await act(async () => { await expect(bridge.available.mock.results[0].value).rejects.toThrow('refused') })
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
  })

  it.each(FAILURE_SCREENS)('offers the exit on the first-run $name screen', async ({ overrides, heading }) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(overrides))
    const bridge = installBridge()
    render()
    await screen.findByRole('heading', { name: heading }, SETUP_CHAIN_WAIT)
    const button = await screen.findByRole('button', { name: CONNECT }, SETUP_CHAIN_WAIT)
    expect(screen.getByText(QUESTION)).toBeTruthy()
    fireEvent.click(button)
    await waitFor(() => expect(bridge.start).toHaveBeenCalledTimes(1))
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    expect(screen.getByRole('heading', { name: heading })).toBeTruthy()
  })

  it.each(FAILURE_SCREENS)('keeps the bridge eligibility check on $name', async ({ overrides, heading }) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(overrides))
    const bridge = installBridge({ available: false })
    render()
    await screen.findByRole('heading', { name: heading }, SETUP_CHAIN_WAIT)
    await waitFor(() => expect(bridge.available).toHaveBeenCalled(), SETUP_CHAIN_WAIT)
    await act(async () => { await expect(bridge.available.mock.results[0].value).resolves.toBe(false) })
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
  })

  it.each(FAILURE_SCREENS)('never offers the exit on enterprise-locked $name', async ({ overrides, heading }) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...overrides, enterprise_login_required: true }))
    const bridge = installBridge()
    render()
    await screen.findByRole('heading', { name: heading }, SETUP_CHAIN_WAIT)
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
    expect(bridge.available).not.toHaveBeenCalled()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
  })

  it.each(FAILURE_SCREENS)('never offers the exit to a non-owner with $name', async ({ overrides }) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...overrides, setup_allowed: false }))
    const bridge = installBridge()
    render()
    // The outdated-CLI branch precedes the owner gate; both must omit the exit.
    await screen.findByRole('heading', {
      name: overrides.acp_supported === false ? /out of date/i : /owner needs to finish setup/i,
    }, SETUP_CHAIN_WAIT)
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
    expect(bridge.available).not.toHaveBeenCalled()
  })

  it('never offers the exit on enterprise-locked install setup', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ enterprise_login_required: true }))
    const bridge = installBridge()
    render()
    await screen.findByText('Get Kiro CLI', undefined, SETUP_CHAIN_WAIT)
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
    expect(bridge.available).not.toHaveBeenCalled()
  })

  it('does not offer an exit without a status proving the enterprise gate is open', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new Error('Status unavailable'))
    const bridge = installBridge()
    render()
    // Failed prerequisite query -> SetupStatusError; no config or bridge query.
    await screen.findByTestId('kiro-gate-status-error', undefined, SETUP_CHAIN_WAIT)
    expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
    expect(bridge.available).not.toHaveBeenCalled()
  })

  it.each([{ name: 'signed out', overrides: {} }, ...FAILURE_SCREENS])(
    'never probes the exit for a returning user with $name', async ({ overrides }) => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...overrides, initial_setup_complete: true }))
      const bridge = installBridge()
      const { queryClient } = render()
      // Dashboard loaded is also the pending placeholder, so wait for the
      // prerequisite -> config query chain to settle before asserting absence.
      await waitFor(() => {
        expect(queryClient.getQueryData<KiroPrerequisiteStatus>(['kiro-prerequisite'])?.initial_setup_complete).toBe(true)
        expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('success')
      }, SETUP_CHAIN_WAIT)
      expect(screen.queryByRole('button', { name: CONNECT })).toBeNull()
      expect(bridge.available).not.toHaveBeenCalled()
    },
  )

  it('starts the switch, and a closed form leaves setup as it was', async () => {
    const bridge = installBridge({ start: async () => 'cancelled' })
    render()
    expect(await screen.findByText(QUESTION, undefined, SETUP_CHAIN_WAIT)).toBeTruthy()
    const button = await screen.findByRole('button', { name: CONNECT }, SETUP_CHAIN_WAIT)
    fireEvent.click(button)
    await waitFor(() => expect(bridge.start).toHaveBeenCalledTimes(1))
    expect(bridge.start).toHaveBeenCalledWith()
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    expect(screen.getByText('Get Kiro CLI')).toBeTruthy()
  })

  it('stays disabled while the window leaves for the crew', async () => {
    installBridge({ start: async () => 'switching' })
    render()
    const button = await screen.findByRole('button', { name: CONNECT }, SETUP_CHAIN_WAIT)
    fireEvent.click(button)
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(true))
  })

  it('hides itself when the switch is no longer available', async () => {
    installBridge({ start: async () => 'unavailable' })
    render()
    fireEvent.click(await screen.findByRole('button', { name: CONNECT }, SETUP_CHAIN_WAIT))
    await waitFor(() => expect(screen.queryByRole('button', { name: CONNECT })).toBeNull())
  })

  it('says so when the form could not be opened, and can be pressed again', async () => {
    installBridge({ start: () => Promise.reject(new Error('restricted')) })
    render()
    const button = await screen.findByRole('button', { name: CONNECT }, SETUP_CHAIN_WAIT)
    fireEvent.click(button)
    expect((await screen.findByRole('alert')).textContent).toBe(
      "Couldn't open the connection form. Try again.",
    )
    expect((button as HTMLButtonElement).disabled).toBe(false)
  })
})
