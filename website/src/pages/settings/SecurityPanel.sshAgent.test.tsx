/**
 * SSH agent forwarding consent section (Settings -> Security).
 *
 * Mirrors SecurityPanel.fileDelivery.test.tsx, and pins the same narrow set of
 * properties: this card is a VIEW over the grant the `/api/ssh-agent/consent`
 * endpoints own, so what is worth locking in is what a UI can get wrong in a
 * way that MISLEADS an owner about an authorization (the backend's rules are
 * covered in test/test_ssh_auth_sock_consent*.py).
 *
 * The security-critical UI property: clicking Allow must NOT record a grant.
 * It ARMS a request, and the card then shows the host command that finishes
 * it. A card that treated the click as the grant would re-open the
 * "agent-driven browser self-grants" hole the backend split exists to close.
 *
 * Specific to this grant: the risk line is ALWAYS visible (never a tooltip),
 * the card says when the gateway has no ssh-agent at all, and a recorded grant
 * says it applies to NEW sessions so Allow is never read as having changed the
 * session the owner is looking at.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { ArmedSshAgentConsent, SshAgentConsentStatus } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    // The panel's rail reads these on mount regardless of the selected section,
    // and they must RESOLVE: a bare vi.fn() returns undefined, which react-query
    // rejects with "Query data cannot be undefined".
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    sshAgentConsent: vi.fn(),
    sshAgentConsentArmStatus: vi.fn(),
    armSshAgentConsent: vi.fn(),
    revokeSshAgentConsent: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const GRANTED_AT = '2026-10-09T07:00:06+00:00'
const APPROVE = 'kirocrew ssh-agent approve'
const NOT_ARMED: ArmedSshAgentConsent = { armed: false, request_id: null, expires_in: null, approve_command: APPROVE }
const ARMED: ArmedSshAgentConsent = { armed: true, request_id: 'req-1', expires_in: 600, approve_command: APPROVE }

function consent(overrides: Partial<SshAgentConsentStatus> = {}): SshAgentConsentStatus {
  return { granted: false, granted_at: null, socket_present: true, ...overrides }
}

/** Render the panel on the ssh-agent section with the consent query pre-resolved.
 *
 *  Waits on the ROW rather than the card title: the title is present while the
 *  query is still in flight and in the failed-read branch, so waiting on it
 *  hands back a card that has not received `granted` yet. */
async function renderSshAgent(data: SshAgentConsentStatus) {
  ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
  await screen.findByTestId('ssh-agent-row')
  return utils
}

describe('SecurityPanel - SSH agent forwarding consent', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [], counts: {} })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.patchConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
    // Default: nothing armed. Individual tests override to the armed view.
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>).mockResolvedValue(NOT_ARMED)
    ;(api.armSshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(ARMED)
    ;(api.revokeSshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue({ granted: false })
  })

  it('not allowed: shows the always-visible risk line, "Not allowed" and an Allow control', async () => {
    await renderSshAgent(consent())

    // The risk is in the open, not behind a tooltip: it is the whole reason this
    // is a consent rather than a default.
    expect(screen.getByText(/Anything a session runs can push, fetch and sign commits as you/)).toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Not allowed')).toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
    // The two-step is explained at the point of consent.
    expect(within(row).getByText(/Nothing changes until you confirm/)).toBeInTheDocument()
    // Complement: the allowed state appears NOWHERE, so a stray "Allowed"
    // elsewhere in the card cannot make this read as granted.
    expect(within(row).queryByText('Allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-revoke')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-armed')).not.toBeInTheDocument()
    // The gateway has an agent, so the no-socket note is absent.
    expect(screen.queryByTestId('ssh-agent-no-socket')).not.toBeInTheDocument()
  })

  it('says what to do first, and holds Allow, when Kiro Crew sees no ssh-agent', async () => {
    await renderSshAgent(consent({ socket_present: false }))

    expect(screen.getByTestId('ssh-agent-no-socket')).toHaveTextContent(
      /Kiro Crew cannot see an SSH agent on this computer. In a terminal, run ssh-add -l./,
    )
    // A grant made now would forward nothing, so the primary control is held
    // until there is something to forward; the note names the step to take.
    expect(screen.getByTestId('ssh-agent-allow')).toBeDisabled()
    // ...and the "this starts a request you finish in a terminal" help line is
    // not shown under a control that cannot be pressed.
    expect(screen.queryByText(/Nothing changes until you confirm/)).not.toBeInTheDocument()
  })

  it('leads with the purpose, then what it does, then the risk', async () => {
    await renderSshAgent(consent())

    const purpose = screen.getByText('Leave this off unless git push, git fetch over SSH, or commit signing fails inside a session.')
    const risk = screen.getByText(/Anything a session runs can push, fetch and sign commits as you/)
    // DOM order: the reason to press Allow comes before the warning against it.
    expect(purpose.compareDocumentPosition(risk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('a hand-written grant says where it came from instead of a bare "Allowed"', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: null }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    expect(within(row).getByText("Allowed outside this page, in Kiro Crew's settings file. Withdraw turns it off here too.")).toBeInTheDocument()
    expect(within(row).queryByText(/Since /)).not.toBeInTheDocument()
  })

  it('names where the approve command runs, and when someone wants this at all', async () => {
    await renderSshAgent(consent())

    expect(screen.getByText('Leave this off unless git push, git fetch over SSH, or commit signing fails inside a session.')).toBeInTheDocument()
  })

  it('clicking Allow ARMS a step-up (does not record a grant) and shows the host command', async () => {
    // The security-critical property: the click must arm, never grant. Start
    // not-armed so Allow is clickable, then the arm-status flips to armed and
    // the card renders the approve command + the armed-state UX.
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(NOT_ARMED)
      .mockResolvedValue(ARMED)
    await renderSshAgent(consent())

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    await waitFor(() => expect(api.armSshAgentConsent).toHaveBeenCalledTimes(1))
    // The armed step-up block shows the exact host command. This is what proves
    // the click did not itself record the grant.
    const armed = await screen.findByTestId('ssh-agent-armed')
    expect(within(armed).getByText(APPROVE)).toBeInTheDocument()
    expect(within(armed).getByText('Open a terminal on the computer running Kiro Crew (Terminal on macOS, PowerShell on Windows, or the Terminal page inside Kiro Crew) and paste this command within 10 minutes.')).toBeInTheDocument()
    // Once armed, the primary CTA is gone (no re-click, no invisible re-arm,
    // no dead disabled button) and the badge reads "Waiting for approval" (not
    // "Allowed") because no grant was recorded.
    await waitFor(() => expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument())
    expect(within(screen.getByTestId('ssh-agent-row')).queryByRole('button', { name: 'Waiting on this machine' })).not.toBeInTheDocument()
    expect(within(screen.getByTestId('ssh-agent-row')).getByText('Waiting for your terminal command')).toBeInTheDocument()
    expect(screen.queryByText('Allowed')).not.toBeInTheDocument()
    expect(api.revokeSshAgentConsent).not.toHaveBeenCalled()
  })

  it('offers a Copy button on the armed host command', async () => {
    // The host command must be copyable, not hand-selection only: a mistype
    // inside the 10-minute window silently fails the step-up.
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(NOT_ARMED)
      .mockResolvedValue(ARMED)
    await renderSshAgent(consent())
    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    const armed = await screen.findByTestId('ssh-agent-armed')
    const copyBtn = within(armed).getByRole('button', { name: 'Copy the approval command' })
    fireEvent.click(copyBtn)
    // The click routes through the shared clipboard helper; the label flips to
    // the "Copied" acknowledgement on success.
    await waitFor(() => expect(within(armed).getByText('Copied')).toBeInTheDocument())
  })

  it('Cancel on an armed request withdraws it without the step-up and re-reads the server', async () => {
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(ARMED)
      .mockResolvedValue(NOT_ARMED)
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(consent())
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    const armed = await screen.findByTestId('ssh-agent-armed')

    fireEvent.click(within(armed).getByTestId('ssh-agent-cancel'))

    // Cancel is the fail-safe direction, so it is the plain DELETE -- no host
    // command, no nonce.
    await waitFor(() => expect(api.revokeSshAgentConsent).toHaveBeenCalledTimes(1))
    expect(api.armSshAgentConsent).not.toHaveBeenCalled()
    // The armed block goes away once the poll reports nothing pending, and the
    // card explains why rather than silently unmounting the command box.
    await waitFor(() => expect(screen.queryByTestId('ssh-agent-armed')).not.toBeInTheDocument())
    // The card knows the owner cancelled, so it says so rather than hedging
    // "expired or was cancelled".
    expect(await screen.findByTestId('ssh-agent-ended')).toHaveTextContent('Request cancelled. Nothing was allowed.')
    expect(within(screen.getByTestId('ssh-agent-row')).getByText('Not allowed')).toBeInTheDocument()
  })

  it('a FAILED Cancel does not claim the request was cancelled when it later lapses', async () => {
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(ARMED)
      .mockResolvedValueOnce(ARMED)
      .mockResolvedValue(NOT_ARMED)
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(consent())
    ;(api.revokeSshAgentConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('500'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    const armed = await screen.findByTestId('ssh-agent-armed')

    fireEvent.click(within(armed).getByTestId('ssh-agent-cancel'))
    await waitFor(() => expect(api.revokeSshAgentConsent).toHaveBeenCalledTimes(1))
    // The DELETE failed: the request is still armed and approvable, and the
    // card says the save failed rather than pretending it cancelled anything.
    expect(await screen.findByText('Could not save that change.')).toBeInTheDocument()

    // The window then lapses on its own: expiry wording, never "cancelled".
    await waitFor(() => expect(screen.queryByTestId('ssh-agent-armed')).not.toBeInTheDocument(), { timeout: 10000 })
    expect(await screen.findByTestId('ssh-agent-ended')).toHaveTextContent(/ended before it was allowed/)
  })

  it('an armed request that lapses without Cancel reads as expired, not cancelled', async () => {
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(ARMED)
      .mockResolvedValue(NOT_ARMED)
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(consent())
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    await screen.findByTestId('ssh-agent-armed')

    // No click: the next poll simply reports nothing armed (the ~10-minute window lapsed).
    await waitFor(() => expect(screen.queryByTestId('ssh-agent-armed')).not.toBeInTheDocument(), { timeout: 10000 })
    expect(await screen.findByTestId('ssh-agent-ended')).toHaveTextContent('That request ended before it was allowed. Allow SSH agent again to start over.')
    expect(api.revokeSshAgentConsent).not.toHaveBeenCalled()
  })

  it('re-reads from the server after arming instead of trusting its own cache', async () => {
    await renderSshAgent(consent())
    const callsBefore = (api.sshAgentConsent as ReturnType<typeof vi.fn>).mock.calls.length

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    await waitFor(() => expect(api.armSshAgentConsent).toHaveBeenCalledTimes(1))
    // The authority is the server, not this cache: an external app can rewrite
    // any react-query key, so the card must refetch rather than assert the new
    // state locally.
    await waitFor(() =>
      expect((api.sshAgentConsent as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBefore),
    )
  })

  it('re-reads the grant when host approval ends an armed request', async () => {
    // Armed first, then the host approval lands: the arm poll flips to
    // {armed:false} and STOPS, so the terminal render must come from a fresh
    // consent GET, not the stale pre-arm cache (or a completed approval reads
    // as expired).
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(consent())
      .mockResolvedValue(consent({ granted: true, granted_at: GRANTED_AT }))
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce(ARMED)
      .mockResolvedValue(NOT_ARMED)

    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    await screen.findByTestId('ssh-agent-armed')

    // The armed->not-armed transition invalidates the grant query; the refetch
    // returns the live grant, so the row settles on Allowed with a Revoke.
    expect(await screen.findByText('Allowed', {}, { timeout: 10000 })).toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-revoke')).toHaveTextContent('Withdraw')
    // ...and not as expired: the approval landed.
    expect(screen.queryByTestId('ssh-agent-ended')).not.toBeInTheDocument()

    // Revoke in the same page view: the ended note must stay away, because this
    // request WAS approved. Only a request that ended without a grant gets it.
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockResolvedValue(consent())
    fireEvent.click(screen.getByTestId('ssh-agent-revoke'))
    await waitFor(() => expect(api.revokeSshAgentConsent).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('Not allowed', {}, { timeout: 10000 })).toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-ended')).not.toBeInTheDocument()
  })

  it('allowed: shows the grant time, says it applies to new sessions, and Revoke is a plain DELETE', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    // The timestamp is rendered through the locale-aware formatter, so assert
    // the YEAR is present rather than a hardcoded format string.
    expect(within(row).getByText(/Since .*2026/)).toBeInTheDocument()
    // The grant is read at agent spawn: a running session is unaffected, and
    // the card must say so rather than imply the current session changed.
    expect(within(row).getByText(/Applies to new sessions\./)).toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ssh-agent-revoke'))
    await waitFor(() => expect(api.revokeSshAgentConsent).toHaveBeenCalledTimes(1))
    // Revoking never arms anything: there is no step-up in the fail-safe direction.
    expect(api.armSshAgentConsent).not.toHaveBeenCalled()
  })

  it('a FAILED read reports the failure and never renders as "Not allowed"', async () => {
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })

    expect(await screen.findByText('Could not read whether sessions can use your SSH agent.')).toBeInTheDocument()
    // An unreadable authorization state must not be shown as an absent one.
    // Both the row and the reassuring label are absent.
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-row')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()
  })

  it('a failed RE-READ after a successful arm does not keep rendering the stale state', async () => {
    // react-query keeps the last good `data` when a refetch rejects, so a card
    // that renders the row whenever `data` is present would go on showing the
    // pre-arm state for a grant whose state is now unknown. `view` is gated on
    // `isError`, so the failure notice is the only thing that renders.
    await renderSshAgent(consent())
    ;(api.sshAgentConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('refetch down'))

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))
    await waitFor(() => expect(api.armSshAgentConsent).toHaveBeenCalledTimes(1))

    expect(await screen.findByText('Could not read whether sessions can use your SSH agent.')).toBeInTheDocument()
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-row')).not.toBeInTheDocument()
  })

  it('a failed ARM is reported without claiming the state changed', async () => {
    await renderSshAgent(consent())
    ;(api.armSshAgentConsent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('nope'))

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    expect(await screen.findByText('Could not save that change.')).toBeInTheDocument()
    // The row still reads not allowed, because the server never armed it.
    expect(within(screen.getByTestId('ssh-agent-row')).getByText('Not allowed')).toBeInTheDocument()
  })

  it('a failed arm-STATUS read is surfaced, not silently hidden', async () => {
    // A failed arm-status GET must say so rather than dropping the command
    // panel with no explanation. The row still renders (the consent GET
    // succeeded); the arm-status error gets its own notice.
    ;(api.sshAgentConsentArmStatus as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('arm boom'))
    await renderSshAgent(consent())

    expect(await screen.findByText('Could not check whether an approval is in progress.')).toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-row')).toBeInTheDocument()
  })

  /* The RAIL row deliberately carries NO summary, for the reason the
   * flagged-file-delivery row gives: `SettingsSubNav` renders a label and a
   * summary as two adjacent catalog keys, which the render-time i18n gate counts
   * as a `fragment/multi-unit` finding, and `[vs-base]` fails on any per-surface
   * increase. A rail-level read would also hit the owner-gated GET for every
   * non-owner who merely opens Security. Pinned here so re-adding a summary
   * reddens a fast unit test instead of a five-minute browser gate in CI. */
  it('gives the rail row NO summary, even when a grant is held', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))

    const row = screen.getAllByRole('option').find(o => o.textContent?.includes('SSH agent'))
    expect(row).toBeDefined()
    expect(row?.textContent).toBe('SSH agent')
  })
})
