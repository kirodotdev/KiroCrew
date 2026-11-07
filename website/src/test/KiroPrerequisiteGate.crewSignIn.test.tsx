/**
 * KiroPrerequisiteGate when KAS signs in with Kiro Crew's own sign-in
 * (`crew_sign_in`): a session has no credential until someone signs in here, so
 * the dashboard waits behind Crew's sign-in screen and opens once one is held.
 */
import { notifyManager } from '@tanstack/react-query'
import { act, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
    kasLoginStatus: vi.fn(),
  },
}))

vi.mock('../providers/adapters/acp', () => ({ clearCachedModels: vi.fn() }))

import { api } from '../api/client'
import { status } from './KiroPrerequisiteGateTestUtils'

const established = { installed: true, ready: true, initial_setup_complete: true }
const signedOut = { authenticated: false, provider: null, identity: null, transport: 'loopback' as const }
const signedIn = { authenticated: true, provider: 'Google', identity: 'social', transport: 'loopback' as const }
// Lost-run bound: two chained queries (the prerequisite status, then the
// sign-in status the mounted KasLoginGate reads) resolve before the screen settles.
const CHAIN = { timeout: 10_000 }

const renderGate = () =>
  renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

// The status request has answered and React has rendered from it.
async function settledStatus() {
  await waitFor(() => expect(api.kiroPrerequisite).toHaveBeenCalled(), CHAIN)
  await act(async () => {
    await vi.mocked(api.kiroPrerequisite).mock.results[0].value
  })
}

describe('KiroPrerequisiteGate with Crew-owned sign-in', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kas' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('holds the dashboard behind the sign-in while nobody is signed in', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...established, crew_sign_in: true }))
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedOut)
    renderGate()
    expect(await screen.findByRole('button', { name: 'Continue with Google' }, CHAIN)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  // The gate renders the dashboard while the status is still loading, so each
  // case below first waits for the status to resolve: a dashboard found before
  // that proves nothing about the verdict.
  it('opens the dashboard once a sign-in is held, without mounting it twice', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...established, crew_sign_in: true }))
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedIn)
    renderGate()
    // Rendered while the status is still loading; the same element must
    // survive both reads resolving, or the whole app mounted a second time.
    const first = await screen.findByText('Dashboard loaded', undefined, CHAIN)
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    await act(async () => {
      await vi.mocked(api.kasLoginStatus).mock.results[0].value
    })
    expect(first).toBeInTheDocument()
    expect(screen.getByText('Dashboard loaded')).toBe(first)
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).not.toBeInTheDocument()
  })

  it('keeps the admitted dashboard mounted when a later read says signed out', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...established, crew_sign_in: true }))
    vi.mocked(api.kasLoginStatus).mockResolvedValue(signedIn)
    const { queryClient } = renderGate()
    await waitFor(() => expect(api.kasLoginStatus).toHaveBeenCalled(), CHAIN)
    await act(async () => {
      await vi.mocked(api.kasLoginStatus).mock.results[0].value
    })
    const admitted = screen.getByText('Dashboard loaded')
    // A sign-out made elsewhere (another tab, the CLI) reaches this tab on the
    // next status read. Unmounting the app here would lose its unsaved drafts.
    // Observers are notified on a timer by default; synchronous notification
    // lets `act` see the re-render the new read causes, with no wait.
    notifyManager.setScheduler((cb) => cb())
    try {
      vi.mocked(api.kasLoginStatus).mockResolvedValue(signedOut)
      await act(async () => {
        await queryClient.refetchQueries({ queryKey: ['kas-login'] })
      })
    } finally {
      notifyManager.setScheduler((cb) => setTimeout(cb, 0))
    }
    expect(queryClient.getQueryData(['kas-login'])).toEqual(signedOut)
    expect(screen.getByText('Dashboard loaded')).toBe(admitted)
    expect(screen.queryByRole('button', { name: 'Continue with Google' })).not.toBeInTheDocument()
  })

  it('leaves the dashboard alone when kiro-cli owns the sign-in', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ ...established, crew_sign_in: false }))
    renderGate()
    await settledStatus()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('never holds a non-owner, who cannot sign the gateway in', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...established, crew_sign_in: true, setup_allowed: false }),
    )
    renderGate()
    await settledStatus()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })
})
