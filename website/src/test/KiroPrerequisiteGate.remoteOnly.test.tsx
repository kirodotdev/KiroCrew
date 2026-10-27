/**
 * KiroPrerequisiteGate: the desktop app's "only using crews on other machines"
 * exit from first-run setup. The main process owns the switch; the page only
 * shows the exit where the bridge says it works and starts it.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
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

import { api } from '../api/client'
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

const QUESTION = 'Only using crews on other machines?'
const SKIP = 'Skip agent setup'

describe('KiroPrerequisiteGate remote-only exit', () => {
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  afterEach(() => {
    delete window.remoteCrewSetupAPI
  })

  it('is absent in a browser, where there is no bridge', async () => {
    render()
    await screen.findByText('Get Kiro CLI')
    expect(screen.queryByText(QUESTION)).toBeNull()
    expect(screen.queryByRole('button', { name: SKIP })).toBeNull()
  })

  it('is absent where the desktop app says the switch would be refused', async () => {
    const bridge = installBridge({ available: false })
    render()
    await screen.findByText('Get Kiro CLI')
    await waitFor(() => expect(bridge.available).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: SKIP })).toBeNull()
  })

  it('is absent when the availability check itself fails', async () => {
    const bridge = installBridge({ available: () => Promise.reject(new Error('refused')) })
    render()
    await screen.findByText('Get Kiro CLI')
    await waitFor(() => expect(bridge.available).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: SKIP })).toBeNull()
  })

  it('never asks a returning user, whose dashboard never shows setup', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ initial_setup_complete: true }))
    const bridge = installBridge()
    render()
    await screen.findByText('Dashboard loaded')
    expect(bridge.available).not.toHaveBeenCalled()
  })

  it('starts the switch, and a closed form leaves setup as it was', async () => {
    const bridge = installBridge({ start: async () => 'cancelled' })
    render()
    expect(await screen.findByText(QUESTION)).toBeTruthy()
    const button = await screen.findByRole('button', { name: SKIP })
    fireEvent.click(button)
    await waitFor(() => expect(bridge.start).toHaveBeenCalledTimes(1))
    expect(bridge.start).toHaveBeenCalledWith()
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
    expect(screen.getByText('Get Kiro CLI')).toBeTruthy()
  })

  it('stays disabled while the window leaves for the crew', async () => {
    installBridge({ start: async () => 'switching' })
    render()
    const button = await screen.findByRole('button', { name: SKIP })
    fireEvent.click(button)
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(true))
  })

  it('hides itself when the switch is no longer available', async () => {
    installBridge({ start: async () => 'unavailable' })
    render()
    fireEvent.click(await screen.findByRole('button', { name: SKIP }))
    await waitFor(() => expect(screen.queryByRole('button', { name: SKIP })).toBeNull())
  })

  it('says so when the form could not be opened, and can be pressed again', async () => {
    installBridge({ start: () => Promise.reject(new Error('restricted')) })
    render()
    const button = await screen.findByRole('button', { name: SKIP })
    fireEvent.click(button)
    expect((await screen.findByRole('alert')).textContent).toBe(
      "Couldn't open the remote crew form. Try again.",
    )
    expect((button as HTMLButtonElement).disabled).toBe(false)
  })
})
