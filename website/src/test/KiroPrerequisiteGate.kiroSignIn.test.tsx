/**
 * KiroPrerequisiteGate's first-run Kiro sign-in hold (`FirstRunSignInHold`): on a
 * page load that began before first-run setup finished, a status reporting
 * `kiro_sign_in_needed` holds the app behind the full-screen sign-in, which has a
 * Skip. A returning user is never held.
 */
import { notifyManager } from '@tanstack/react-query'
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
    updateKiroPrerequisiteCli: vi.fn(),
    kirocrewConfig: vi.fn(),
    acpBackends: vi.fn(),
    acpBackendRecheck: vi.fn(),
    patchConfig: vi.fn(),
    kasLoginStatus: vi.fn(),
  },
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api } from '../api/client'
import type { KiroPrerequisiteStatus } from '../api/client'
import { status } from './KiroPrerequisiteGateTestUtils'

const firstRun = { installed: true, ready: true, initial_setup_complete: false }
const returning = { ...firstRun, initial_setup_complete: true }
const signedOut = { authenticated: false, provider: null, identity: null, transport: 'loopback' as const }
const signedIn = { authenticated: true, provider: 'Google', identity: 'social', transport: 'loopback' as const }
// Lost-run bound: two chained queries (the setup status, then the sign-in
// status) resolve before the screen settles.
const CHAIN = { timeout: 10_000 }
const CHOOSER = { name: 'Continue with Google' }
const SKIP = { name: 'Skip for now' }

const renderGate = () =>
  renderWithProviders(<KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>)

// The status request has answered and React has rendered from it.
async function settledStatus() {
  await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalled(), CHAIN)
  await act(async () => {
    await vi.mocked(api.kiroPrerequisite).mock.results[0].value
  })
}

async function refetch(queryClient: ReturnType<typeof renderGate>['queryClient'], key: string) {
  await act(async () => {
    await queryClient.refetchQueries({ queryKey: [key] })
  })
}

const storedKeys = () => Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).sort()

const nextStatus = (overrides: Partial<KiroPrerequisiteStatus>) =>
  vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(overrides))

describe('KiroPrerequisiteGate first-run Kiro sign-in hold', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kas' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedOut)
    // Observers are notified on a timer by default. Synchronous notification lets
    // `act` flush the render a resolved query causes, so a test that read the
    // status has also seen the gate render from it.
    notifyManager.setScheduler((cb) => cb())
  })
  afterEach(() => {
    notifyManager.setScheduler((cb) => setTimeout(cb, 0))
  })

  it('holds a first run behind the sign-in, with a Skip', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    renderGate()
    expect(await screen.findByRole('button', CHOOSER, CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('button', SKIP)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('Skip renders the app and keeps it mounted through later reads', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    const { queryClient } = renderGate()
    const skip = await screen.findByRole('button', SKIP, CHAIN)
    const stored = storedKeys()
    fireEvent.click(skip)
    const admitted = await screen.findByText('Dashboard loaded', undefined, CHAIN)
    // Skip saves nothing: the server still says a sign-in is needed.
    expect(storedKeys()).toEqual(stored)
    expect(api.patchConfig).not.toHaveBeenCalled()
    await refetch(queryClient, 'kiro-prerequisite')
    await refetch(queryClient, 'kas-login')
    expect(queryClient.getQueryData(['kiro-prerequisite'])).toMatchObject({ kiro_sign_in_needed: true })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
  })

  it('never holds a returning user, even signed out', async () => {
    nextStatus({ ...returning, kiro_sign_in_needed: true })
    renderGate()
    await settledStatus()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('keeps holding after the first-run marker is written mid-load', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    const { queryClient } = renderGate()
    expect(await screen.findByRole('button', CHOOSER, CHAIN)).toBeInTheDocument()
    nextStatus({ ...returning, kiro_sign_in_needed: true })
    await refetch(queryClient, 'kiro-prerequisite')
    expect(queryClient.getQueryData(['kiro-prerequisite'])).toMatchObject({ initial_setup_complete: true })
    expect(screen.getByRole('button', CHOOSER)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('admits once a sign-in is stored, and a later signed-out read keeps the app', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedIn)
    const { queryClient } = renderGate()
    await settledStatus()
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    const admitted = await screen.findByText('Dashboard loaded', undefined, CHAIN)
    // Another reader of the sign-in status (the sign-in notice, the Settings card)
    // brings a sign-out made elsewhere into the shared cache.
    act(() => {
      queryClient.setQueryData(['kas-login'], signedOut)
    })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
  })

  it('admits when the sign-in status cannot be read', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    vi.mocked(api.kasLoginStatus).mockRejectedValue(new Error('token_store_failed'))
    renderGate()
    await settledStatus()
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    expect(await screen.findByText('Dashboard loaded', undefined, CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('kas-login-status-error')).not.toBeInTheDocument()
  })

  it('does not hold an app already shown unheld when the field turns on', async () => {
    nextStatus({ ...firstRun, kiro_sign_in_needed: false })
    const { queryClient } = renderGate()
    await settledStatus()
    const admitted = screen.getByText('Dashboard loaded')
    nextStatus({ ...firstRun, kiro_sign_in_needed: true })
    await refetch(queryClient, 'kiro-prerequisite')
    expect(queryClient.getQueryData(['kiro-prerequisite'])).toMatchObject({ kiro_sign_in_needed: true })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('holds the app a first-run CLI update admits when its answer carries the field', async () => {
    nextStatus({ ...firstRun, ready: false, acp_supported: false })
    vi.mocked(api.updateKiroPrerequisiteCli).mockResolvedValue(
      status({ ...firstRun, kiro_sign_in_needed: true }),
    )
    renderGate()
    fireEvent.click(await screen.findByRole('button', { name: 'Update Kiro CLI' }, CHAIN))
    await waitFor(() => expect(api.updateKiroPrerequisiteCli).toHaveBeenCalledTimes(1), CHAIN)
    // The update's answer becomes the cached status; the app it admits waits
    // behind the sign-in.
    expect(await screen.findByRole('button', CHOOSER, CHAIN)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })
})
