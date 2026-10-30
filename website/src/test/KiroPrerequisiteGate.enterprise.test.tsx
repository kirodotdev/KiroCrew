/**
 * KiroPrerequisiteGate under an enterprise gateway whose login gate is closed
 * (`enterprise_login_required`): no bypass hands over the dashboard, and the
 * first-run screens still run in order, so each prerequisite keeps its remedy.
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type React from 'react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate, {
  kiroPrerequisiteIsBlocking,
  kiroPrerequisiteRefetchInterval,
} from '../components/KiroPrerequisiteGate'
import Modal from '../components/Modal'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { Dialog, DialogContent, DialogTitle } from '../components/ui/dialog'
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

  // A relock of a dashboard in use: the app stays mounted and a display-only
  // notice rides beside it, above any open dialog, without touching it.
  function Draft() {
    const [text, setText] = useState('')
    return <input aria-label="draft" value={text} onChange={e => setText(e.target.value)} />
  }
  function RadixEditor() {
    const [open, setOpen] = useState(true)
    return (
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent aria-label="Editor">
          <DialogTitle>Editor</DialogTitle>
          <Draft />
        </DialogContent>
      </Dialog>
    )
  }
  function ModalEditor() {
    const [open, setOpen] = useState(true)
    return <Modal open={open} onClose={() => setOpen(false)} title="Editor"><Draft /></Modal>
  }
  const open = status({ ready: true, authenticated: true, installed: true, initial_setup_complete: true })
  const relockedStatus = status({ ...locked, initial_setup_complete: true })
  async function relockTo(ui: React.ReactElement, after: ReturnType<typeof status>) {
    vi.mocked(api.kiroPrerequisite).mockResolvedValueOnce(open).mockResolvedValue(after)
    const view = renderWithProviders(<KiroPrerequisiteGate>{ui}</KiroPrerequisiteGate>)
    fireEvent.change(await screen.findByLabelText('draft'), { target: { value: 'unsaved json' } })
    await waitFor(() => expect(view.queryClient.getQueryData(['kiro-prerequisite'])).toBeTruthy())
    await act(async () => { await view.queryClient.refetchQueries({ queryKey: ['kiro-prerequisite'] }) })
    await waitFor(() => expect(view.queryClient.getQueryData(['kiro-prerequisite'])).toEqual(after))
    return view
  }
  async function relock(ui: React.ReactElement, after = relockedStatus) {
    const view = await relockTo(ui, after)
    await screen.findByTestId('enterprise-relock-notice')
    return view
  }

  it('keeps a page draft and shows the sign-in notice beside the app until the gate reopens', async () => {
    const { queryClient, container } = await relock(<Draft />)
    const notice = screen.getByTestId('enterprise-relock-notice')
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
    expect(within(notice).getByText('Run this in a terminal to sign in again. This notice clears once you do.')).toBeInTheDocument()
    expect(within(notice).getByText('kiro-cli login')).toBeInTheDocument()
    // The notice is portaled away, nothing else joins the app's own flow, and
    // no first-run chrome replaces the app.
    expect(container).not.toContainElement(notice)
    expect(container.textContent).toBe('')
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()

    // Signing in reopens the gate: the next poll clears the notice, draft kept.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(open)
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kiro-prerequisite'] }) })
    await waitFor(() => expect(screen.queryByTestId('enterprise-relock-notice')).not.toBeInTheDocument())
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
  })

  it('is display only: no pointer input, nothing focusable, no event captured', async () => {
    await relock(<Draft />)
    const notice = screen.getByTestId('enterprise-relock-notice')
    expect(notice).toHaveClass('pointer-events-none')
    expect(notice.querySelectorAll('button, a, input, select, textarea, [tabindex]')).toHaveLength(0)
    const seen: string[] = []
    const record = (e: Event) => { seen.push(e.type) }
    const types = ['pointerdown', 'mousedown', 'click', 'keydown', 'focusin', 'wheel', 'touchmove']
    for (const t of types) document.addEventListener(t, record)
    try {
      fireEvent.pointerDown(notice)
      fireEvent.mouseDown(notice)
      fireEvent.click(notice)
      fireEvent.keyDown(notice, { key: ',', altKey: true })
      fireEvent.focusIn(notice)
      fireEvent.wheel(notice)
      fireEvent.touchMove(notice)
      expect(seen).toEqual(types)
    } finally {
      for (const t of types) document.removeEventListener(t, record)
    }
  })

  it('leaves an open Radix dialog and its draft alone on relock', async () => {
    await relock(<RadixEditor />)
    expect(screen.getByRole('dialog', { name: 'Editor' })).toBeInTheDocument()
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
  })

  it('leaves an open Modal and its draft alone on relock', async () => {
    await relock(<ModalEditor />)
    expect(screen.getByRole('dialog', { name: 'Editor' })).toBeInTheDocument()
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
  })

  it('draws the notice in the local pane overlay, so a remote Crew in front hides it', async () => {
    // App mounts LocalOverlayRoot inside the gate; the notice must land in its
    // container, which App hides while a remote Crew tab is active.
    await relock(<LocalOverlayRoot hidden><Draft /></LocalOverlayRoot>)
    const notice = screen.getByTestId('enterprise-relock-notice')
    const local = document.querySelector<HTMLElement>('[data-local-overlay-root]')
    expect(local).toContainElement(notice)
    expect(local).toHaveStyle({ display: 'none' })
  })

  it('names the bare command with no hint when the gateway serves the bare command', async () => {
    await relock(<Draft />)
    const notice = screen.getByTestId('enterprise-relock-notice')
    expect(within(notice).getByText('kiro-cli login')).toBeInTheDocument()
    expect(within(notice).queryByText(/its own copy of Kiro CLI/)).not.toBeInTheDocument()
  })

  it.each([
    ['the bundled desktop CLI', { bundled_cli: true, login_command: "'/Applications/Kiro Crew.app/Contents/Resources/kiro-cli/kiro-cli' login" }],
    ['a KIROCREW_KIRO_BIN override off PATH', { bundled_cli: false, login_command: "'/opt/custom/kiro-cli' login" }],
  ])('points %s to the sign-in screen for the exact command, still display only', async (_label, extra) => {
    await relock(<Draft />, status({ ...locked, initial_setup_complete: true, ...extra }))
    const notice = screen.getByTestId('enterprise-relock-notice')
    // The hint is the primary line, and neither the bare command nor the
    // run-it line shows: that command is not this gateway's.
    const hint = within(notice).getByText(/its own copy of Kiro CLI.*new window or tab.*exact command.*copy button/)
    expect(hint).toHaveClass('text-sm', 'text-text-strong')
    expect(within(notice).queryByText('kiro-cli login')).not.toBeInTheDocument()
    expect(within(notice).queryByText(/Run this in a terminal/)).not.toBeInTheDocument()
    // The long command itself is never on the card, which stays display only.
    expect(notice.textContent).not.toContain(extra.login_command)
    expect(notice).toHaveClass('pointer-events-none', 'select-none')
    expect(notice.querySelectorAll('button, a, input, select, textarea, [tabindex]')).toHaveLength(0)
  })

  it('tells a non-owner to ask the owner to sign in again, with no sign-in command', async () => {
    await relock(<Draft />, status({ ...locked, initial_setup_complete: true, setup_allowed: false }))
    const notice = screen.getByTestId('enterprise-relock-notice')
    expect(within(notice).getByText(/Ask the .* owner to sign this gateway in again\. This notice clears once they do\./)).toBeInTheDocument()
    // A non-owner cannot sign in, so neither the instruction nor the command shows.
    expect(within(notice).queryByText(/Run this in a terminal/)).not.toBeInTheDocument()
    expect(within(notice).queryByText('kiro-cli login')).not.toBeInTheDocument()
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
  })

  it('oss: an established install that signs out shows no notice and keeps the app', async () => {
    // No enterprise_login_required: the oss status shape.
    await relockTo(<Draft />, status({ installed: true, authenticated: false, ready: false, initial_setup_complete: true }))
    expect(screen.queryByTestId('enterprise-relock-notice')).not.toBeInTheDocument()
    expect(screen.queryByText(/Run this in a terminal/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('draft')).toHaveValue('unsaved json')
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
