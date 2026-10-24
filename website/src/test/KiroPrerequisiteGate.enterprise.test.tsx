/**
 * KiroPrerequisiteGate under an enterprise gateway whose login gate is closed
 * (`enterprise_login_required`): no bypass hands over the dashboard, and the
 * first-run screens still run in order, so each prerequisite keeps its remedy.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate, {
  kiroPrerequisiteIsBlocking,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
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
import { probe, status } from './KiroPrerequisiteGateTestUtils'

const locked = { enterprise_login_required: true, installed: true }

const renderGate = () =>
  renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

describe('KiroPrerequisiteGate enterprise login gate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('blocks and polls fast while the login gate is closed, even with ready latched', () => {
    expect(kiroPrerequisiteIsBlocking(status(locked))).toBe(true)
    expect(kiroPrerequisiteIsBlocking(status({ ...locked, ready: true }))).toBe(true)
    expect(kiroPrerequisiteRefetchInterval(status({ ...locked, ready: true }))).toBe(5_000)
    expect(kiroPrerequisiteRefetchInterval(status({ ready: true }))).toBe(30_000)
  })

  it('shows the sign-in step instead of the dashboard for an established install', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeEnabled()
    expect(screen.getByText('kiro-cli login').tagName).toBe('CODE')
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('holds the dashboard while ready is latched but the gate has not reopened', async () => {
    // The probe can see the login a few seconds before the gateway's own
    // watch reopens the gate; the dashboard would only meet 403s meanwhile.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, ready: true, authenticated: true, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('shows a non-owner the owner screen, never the dashboard', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, setup_allowed: false, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('keeps the install remedy when the CLI is missing', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, installed: false, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('link', { name: /Open Kiro CLI setup/ })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('keeps the sandbox remedy when verification is refused', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({
        ...locked,
        sandbox_unavailable: true,
        sandbox_failure_kind: 'no_backend',
        sandbox_detail: 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)',
      }),
    )
    renderGate()
    expect(await screen.findByText(/unshare\(CLONE_NEWNS\) failed/)).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('ignores a usable independent harness and skips the refused reads', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'claude' })] })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(
      status({ ...locked, initial_setup_complete: true }),
    )
    renderGate()
    expect(await screen.findByRole('button', { name: 'Check sign-in again' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
    expect(api.acpBackends).not.toHaveBeenCalled()
    // Choosing another agent writes config, which the closed gate refuses.
    expect(screen.queryByRole('button', { name: 'Use other coding agents' })).not.toBeInTheDocument()
  })

  it('keeps the app mounted and on screen when an established session relocks', async () => {
    // The relock screen is a follow-up; until then a relock leaves the app as
    // it is, and the gateway refuses its requests while the gate is closed.
    function Draft() {
      const [text, setText] = useState('')
      return <input aria-label="draft" value={text} onChange={e => setText(e.target.value)} />
    }
    const open = status({ ready: true, authenticated: true, installed: true, initial_setup_complete: true })
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(open)
      .mockResolvedValue(status({ ...locked, initial_setup_complete: true }))
    const { queryClient } = renderWithProviders(<KiroPrerequisiteGate><Draft /></KiroPrerequisiteGate>)
    fireEvent.change(await screen.findByLabelText('draft'), { target: { value: 'unsaved json' } })
    await waitFor(() => expect(queryClient.getQueryData(['kiro-prerequisite'])).toBeTruthy())

    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kiro-prerequisite'] }) })
    await waitFor(() => expect(
      (queryClient.getQueryData(['kiro-prerequisite']) as { enterprise_login_required?: boolean })
        .enterprise_login_required,
    ).toBe(true))
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
    expect(screen.queryByRole('button', { name: 'Check sign-in again' })).not.toBeInTheDocument()
  })

  it('opens the dashboard once the gate reports open', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status({ ...locked, initial_setup_complete: true }))
      .mockResolvedValue(status({ ready: true, authenticated: true, installed: true, initial_setup_complete: true }))
    renderGate()
    fireEvent.click(await screen.findByRole('button', { name: 'Check sign-in again' }))
    await waitFor(() => expect(screen.getByText('Dashboard loaded')).toBeInTheDocument())
  })
})
