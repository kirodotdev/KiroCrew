/**
 * KiroPrerequisiteGate's first-run Kiro sign-in hold (`FirstRunSignInHold`): on a
 * page load that began before first-run setup finished, the gateway's
 * `chat_needs_sign_in` (`GET /api/kas-bundle`) holds the app, mounted and
 * hidden, behind the full-screen sign-in, which has a Skip. A returning user is
 * never held.
 */
import { useState } from 'react'
import { notifyManager } from '@tanstack/react-query'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate from '../components/KiroPrerequisiteGate'
import { useFirstRunSignInHeld } from '../components/FirstRunSignInHold'
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
    kasBundleStatus: vi.fn(),
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
// Lost-run bound: three chained queries (the setup status, the gateway's answer,
// then the sign-in status) resolve before the screen settles.
const CHAIN = { timeout: 10_000 }
const CHOOSER = { name: 'Continue with Google' }
const SKIP = { name: 'Skip for now' }

// Stands in for App: reads the hold the way App's overlay container reads it.
function AppStub() {
  const held = useFirstRunSignInHeld()
  return <div data-held={held}>Dashboard loaded</div>
}

// An answer the user is typing and has not saved, as on first run's "Tell Kiro about you".
function RoleField() {
  const [role, setRole] = useState('')
  return <input aria-label="Your role" value={role} onChange={e => setRole(e.target.value)} />
}

const renderGate = (queryDefaults?: Record<string, unknown>, app = <AppStub />) =>
  renderWithProviders(<KiroPrerequisiteGate>{app}</KiroPrerequisiteGate>, { queryDefaults })

// The status request has answered and React has rendered from it.
async function settledStatus() {
  await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalled(), CHAIN)
  await act(async () => {
    await vi.mocked(api.kiroPrerequisite).mock.results[0].value
  })
}

// The gateway's answer to read `call` (0 for the first) has come (or failed) and
// React has rendered from it.
async function settledBundle(call = 0) {
  await waitFor(() => expect(vi.mocked(api.kasBundleStatus).mock.calls.length).toBeGreaterThan(call), CHAIN)
  await act(async () => {
    await vi.mocked(api.kasBundleStatus).mock.results[call].value.catch(() => undefined)
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

const signInNeeded = (needed: boolean) =>
  vi.mocked(api.kasBundleStatus).mockResolvedValue({ chat_needs_sign_in: needed } as never)

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
    nextStatus(firstRun)
    signInNeeded(true)
    renderGate()
    expect(await screen.findByRole('button', CHOOSER, CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('button', SKIP)).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).not.toBeVisible()
    expect(screen.getByText('Dashboard loaded')).toHaveAttribute('data-held', 'true')
  })

  it('Skip renders the app and keeps it mounted through later reads', async () => {
    nextStatus(firstRun)
    signInNeeded(true)
    const { queryClient } = renderGate()
    const skip = await screen.findByRole('button', SKIP, CHAIN)
    const stored = storedKeys()
    fireEvent.click(skip)
    const admitted = screen.getByText('Dashboard loaded')
    await waitFor(() => expect(admitted).toBeVisible(), CHAIN)
    // Skip saves nothing: the gateway still says a sign-in is needed.
    expect(storedKeys()).toEqual(stored)
    expect(api.patchConfig).not.toHaveBeenCalled()
    await refetch(queryClient, 'kiro-prerequisite')
    await refetch(queryClient, 'kas-bundle')
    await refetch(queryClient, 'kas-login')
    expect(queryClient.getQueryData(['kas-bundle'])).toMatchObject({ chat_needs_sign_in: true })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(admitted).toBeVisible()
    expect(admitted).toHaveAttribute('data-held', 'false')
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
  })

  it('keeps what the user typed before the answer through the hold and its Skip', async () => {
    nextStatus(firstRun)
    let answer!: (value: { chat_needs_sign_in: boolean }) => void
    vi.mocked(api.kasBundleStatus).mockReturnValue(new Promise(resolve => { answer = resolve }) as never)
    renderGate(undefined, <RoleField />)
    await waitFor(() => expect(api.kasBundleStatus).toHaveBeenCalled(), CHAIN)
    const role = screen.getByRole('textbox', { name: 'Your role' })
    fireEvent.change(role, { target: { value: 'Data engineer' } })
    await act(async () => {
      answer({ chat_needs_sign_in: true })
    })
    const skip = await screen.findByRole('button', SKIP, CHAIN)
    expect(role).not.toBeVisible()
    fireEvent.click(skip)
    await waitFor(() => expect(role).toBeVisible(), CHAIN)
    expect(screen.getByRole('textbox', { name: 'Your role' })).toBe(role)
    expect(role).toHaveValue('Data engineer')
  })

  it('never holds a returning user, even signed out', async () => {
    nextStatus(returning)
    signInNeeded(true)
    renderGate()
    await settledStatus()
    expect(screen.getByText('Dashboard loaded')).toBeVisible()
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
    expect(api.kasBundleStatus).not.toHaveBeenCalled()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('keeps holding after the first-run marker is written mid-load', async () => {
    nextStatus(firstRun)
    signInNeeded(true)
    const { queryClient } = renderGate()
    expect(await screen.findByRole('button', CHOOSER, CHAIN)).toBeInTheDocument()
    nextStatus(returning)
    await refetch(queryClient, 'kiro-prerequisite')
    expect(queryClient.getQueryData(['kiro-prerequisite'])).toMatchObject({ initial_setup_complete: true })
    expect(screen.getByRole('button', CHOOSER)).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).not.toBeVisible()
  })

  it('admits once a sign-in is stored, and a later signed-out read keeps the app', async () => {
    nextStatus(firstRun)
    signInNeeded(true)
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedIn)
    const { queryClient } = renderGate()
    await settledStatus()
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    const admitted = screen.getByText('Dashboard loaded')
    await waitFor(() => expect(admitted).toBeVisible(), CHAIN)
    // Another reader of the sign-in status (the sign-in notice, the Settings card)
    // brings a sign-out made elsewhere into the shared cache.
    act(() => {
      queryClient.setQueryData(['kas-login'], signedOut)
    })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(admitted).toBeVisible()
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
  })

  it('admits when the sign-in status cannot be read', async () => {
    nextStatus(firstRun)
    signInNeeded(true)
    vi.mocked(api.kasLoginStatus).mockRejectedValue(new Error('token_store_failed'))
    renderGate()
    await settledStatus()
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    await waitFor(() => expect(screen.getByText('Dashboard loaded')).toBeVisible(), CHAIN)
    expect(screen.queryByTestId('kas-login-status-error')).not.toBeInTheDocument()
  })

  it('holds nothing when the gateway cannot say whether a sign-in is needed', async () => {
    nextStatus(firstRun)
    vi.mocked(api.kasBundleStatus).mockRejectedValue(new Error('kas_bundle_status_failed'))
    renderGate()
    await settledStatus()
    await settledBundle()
    expect(screen.getByText('Dashboard loaded')).toBeVisible()
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('does not hold an app already shown unheld when the field turns on', async () => {
    nextStatus(firstRun)
    signInNeeded(false)
    const { queryClient } = renderGate()
    await settledStatus()
    await settledBundle()
    const admitted = screen.getByText('Dashboard loaded')
    // Another reader of the field (the chat's sign-in prompt) brings a later
    // answer into the shared cache.
    act(() => {
      queryClient.setQueryData(['kas-bundle'], { chat_needs_sign_in: true })
    })
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(admitted).toBeVisible()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('decides from the answer read when setup admits, not one cached on the setup screen', async () => {
    nextStatus({ installed: true, acp_supported: false })
    signInNeeded(true)
    // The shipped client's default: a cached answer stays fresh until something refetches it.
    const { queryClient } = renderGate({ staleTime: Infinity })
    await screen.findByRole('button', { name: 'Update Kiro CLI' }, CHAIN)
    // An earlier reader of the field left the setup screen's answer in the shared cache.
    await act(async () => {
      await queryClient.prefetchQuery({ queryKey: ['kas-bundle'], queryFn: () => api.kasBundleStatus() })
    })
    expect(queryClient.getQueryData(['kas-bundle'])).toMatchObject({ chat_needs_sign_in: true })
    // Setup finished on kiro-cli, so the next chat runs on its relay.
    nextStatus(firstRun)
    signInNeeded(false)
    await refetch(queryClient, 'kiro-prerequisite')
    await settledBundle(1)
    expect(screen.getByText('Dashboard loaded')).toBeVisible()
    expect(screen.queryByRole('button', CHOOSER)).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })
})
