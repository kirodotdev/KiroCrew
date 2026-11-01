/**
 * SSH agent forwarding consent section (Settings -> Security).
 *
 * This card is a VIEW over the grant the `/api/ssh-agent/consent` endpoints own,
 * so what is worth locking in is what a UI can get wrong in a way that MISLEADS
 * an owner about an authorization (the backend's rules are covered in
 * test/test_ssh_auth_sock_consent_grant.py).
 *
 * Specific to this grant: the risk line is ALWAYS visible (never a tooltip),
 * the card says when the gateway has no ssh-agent at all, an unreadable state
 * never renders as "Not allowed", and a recorded grant says it applies to NEW
 * sessions so Allow is never read as having changed the session the owner is
 * looking at.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { refocusWindow } from '../../test/focusBarrier'
import type { SshAgentConsentStatus } from '../../api/client'

vi.mock('../../api/client', async () => ({
  // The real class: the card's retry policy tells an owner-gate 403 apart with `instanceof`.
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
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
    grantSshAgentConsent: vi.fn(),
    revokeSshAgentConsent: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { SecurityPanel } from './SecurityPanel'

const GRANTED_AT = '2026-10-09T07:00:06+00:00'
const RISK = /Anything a session runs can push, fetch and sign commits as you/
const PURPOSE = 'Turn this on if git push, git fetch over SSH, or commit signing fails inside a session.'
const LOAD_FAILED = 'Could not read whether sessions can use your SSH agent.'
const SAVE_FAILED = 'Could not save the SSH agent setting.'
const OWNER_ONLY = 'Only the dashboard owner can see or change whether sessions can use the SSH agent.'

function consent(overrides: Partial<SshAgentConsentStatus> = {}): SshAgentConsentStatus {
  return {
    granted: false,
    granted_at: null,
    socket_present: true,
    forwarded_without_consent: false,
    forwarded_harnesses: [],
    ...overrides,
  }
}

/** The GET's `harness_labels`, as the server spells them (claude and kiro get
 *  the shared translated names instead). */
const LABELS = { claude: 'claude', codex: 'OpenAI Codex', kiro: 'Kiro CLI' }

const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>

/** The first wait after a render settles a chain: `SecurityPanel` loads the card
 *  through a lazy `import()`, and only then does the card start its consent query.
 *  Both must finish before the first card state shows, so that wait gets more than
 *  Testing Library's one-second default. */
const LAZY_CARD_MOUNT = { timeout: 5000 }

/** Render the panel on the ssh-agent section with the consent query pre-resolved.
 *
 *  Waits on the ROW rather than the card title: the title is present while the
 *  query is still in flight and in the failed-read branch. */
async function renderSshAgent(data: SshAgentConsentStatus) {
  mocked(api.sshAgentConsent).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
  await screen.findByTestId('ssh-agent-row', {}, LAZY_CARD_MOUNT)
  return utils
}

describe('SecurityPanel - SSH agent forwarding consent', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocked(api.deniedCommands).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    mocked(api.securityPosture).mockResolvedValue({ controls: [], counts: {} })
    mocked(api.governancePolicy).mockResolvedValue({
      version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
    })
    mocked(api.kirocrewConfig).mockResolvedValue({})
    mocked(api.patchConfig).mockResolvedValue({ ok: true })
    mocked(api.tailnetStatus).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
    mocked(api.grantSshAgentConsent).mockResolvedValue({ granted: true, granted_at: GRANTED_AT })
    mocked(api.revokeSshAgentConsent).mockResolvedValue({ granted: false })
  })

  it('not allowed: shows the always-visible risk line, "Not allowed" and an Allow control', async () => {
    await renderSshAgent(consent())

    expect(screen.getByText(RISK)).toBeInTheDocument()
    // The risk text makes no claim about the key files: on the standard level
    // they are readable, so "hidden" would be false there.
    expect(screen.getByTestId('ssh-agent-risk')).not.toHaveTextContent(/hidden/i)
    expect(screen.getByText('A session already running keeps it until that session ends.')).toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Not allowed')).toBeInTheDocument()
    // What Allow gets the owner sits beside the control.
    expect(within(row).getByTestId('ssh-agent-allow-benefit')).toHaveTextContent(
      'Lets new sessions run git push, git fetch and commit signing with your SSH keys.',
    )
    expect(screen.getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
    expect(screen.getByTestId('ssh-agent-allow')).not.toBeDisabled()
    expect(within(row).getByText('Applies to new sessions. You can withdraw it at any time.')).toBeInTheDocument()
    expect(within(row).queryByText('Allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-revoke')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-no-socket')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-tier-forwards')).not.toBeInTheDocument()
  })

  it('leads with the purpose, then the risk', async () => {
    await renderSshAgent(consent())

    const purpose = screen.getByText(PURPOSE)
    const risk = screen.getByText(RISK)
    expect(purpose.compareDocumentPosition(risk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('clicking Allow records the grant through one POST and re-reads the server', async () => {
    await renderSshAgent(consent())
    const callsBefore = mocked(api.sshAgentConsent).mock.calls.length
    mocked(api.sshAgentConsent).mockResolvedValue(consent({ granted: true, granted_at: GRANTED_AT }))

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    await waitFor(() => expect(api.grantSshAgentConsent).toHaveBeenCalledTimes(1))
    // The authority is the server, not this cache: the card refetches rather
    // than asserting the new state locally.
    await waitFor(() =>
      expect(mocked(api.sshAgentConsent).mock.calls.length).toBeGreaterThan(callsBefore),
    )
    expect(await within(screen.getByTestId('ssh-agent-row')).findByText('Allowed')).toBeInTheDocument()
    expect(api.revokeSshAgentConsent).not.toHaveBeenCalled()
  })

  it('says the sandbox level already forwards the agent: an "on" badge, no Allow, a link to the sandbox', async () => {
    await renderSshAgent(consent({ forwarded_without_consent: true }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('On at your sandbox level')
    expect(within(row).queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()
    expect(screen.queryByText('Applies to new sessions. You can withdraw it at any time.')).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-sandbox-link')).toHaveAttribute('href', expect.stringContaining('/developer?tab=config'))
    // "Until you withdraw this" would be false: withdrawing changes nothing here.
    expect(screen.queryByText(/until you withdraw this/)).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-tier-forwards')).toHaveTextContent(
      /already lets new sessions use your SSH agent, allowed here or not/,
    )
  })

  it('mixed scope: the row names the harnesses this setting decides, the note names the ones it does not', async () => {
    await renderSshAgent(consent({ forwarded_harnesses: ['claude', 'kiro'], harness_labels: LABELS }))

    const row = screen.getByTestId('ssh-agent-row')
    // Product names, the shared translated label first, else the server's.
    expect(within(row).getByTestId('ssh-agent-row-label')).toHaveTextContent('SSH agent in sessions whose sandbox hides your SSH keys')
    expect(within(row).getByTestId('ssh-agent-row-names')).toHaveTextContent('New OpenAI Codex sessions')
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('Not allowed')
    // The control names its outcome, and the note about the sessions it does
    // NOT decide comes after it rather than between the label and the control.
    const allow = within(row).getByTestId('ssh-agent-allow')
    expect(allow).toHaveTextContent('Allow for these sessions')
    expect(allow.compareDocumentPosition(within(row).getByTestId('ssh-agent-tier-forwards-some')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(row).getByTestId('ssh-agent-tier-forwards-some')).toHaveTextContent(
      'This setting decides only for the sessions above. New Claude Code, Kiro CLI sessions have your SSH agent at your current sandbox level, whatever this setting says. To stop that, choose the Strict level of Process Sandbox and leave this setting at Not allowed. Both apply to new sessions only.',
    )
    // "agent" on this card means the SSH agent only.
    expect(within(row).queryByText(/other agents?\b/)).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-sandbox-link')).toHaveTextContent('Change Process Sandbox')
    expect(screen.queryByText(/until you withdraw this/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-tier-forwards')).not.toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-allow')).not.toBeDisabled()
  })

  it('mixed scope with the grant held: the decided harnesses read "Allowed"', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT, forwarded_harnesses: ['kiro'], harness_labels: LABELS }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-row-names')).toHaveTextContent('New Claude Code, OpenAI Codex sessions')
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('Allowed')
    expect(screen.getByTestId('ssh-agent-revoke')).toBeInTheDocument()
  })

  it('mixed scope from a gateway that sends no labels keeps the generic row label', async () => {
    await renderSshAgent(consent({ forwarded_harnesses: ['kiro'] }))

    expect(screen.getByTestId('ssh-agent-row-label')).toHaveTextContent('The SSH agent on this computer')
    expect(screen.queryByTestId('ssh-agent-row-names')).not.toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
  })

  it('keeps the tier note when allowed, so Withdraw is not read as stopping the forward', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT, forwarded_without_consent: true }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-tier-forwards')).toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('On at your sandbox level')
    // The grant counts only at the Strict level here, and the label says so.
    expect(screen.getByTestId('ssh-agent-revoke')).toHaveTextContent('Withdraw for Strict level')
    // Withdraw says what it does here: removes the grant, sessions keep the agent.
    expect(within(row).getByTestId('ssh-agent-revoke-tier-help')).toHaveTextContent(
      'Withdraw removes your permission. Sessions keep the SSH agent until the sandbox level is strict.',
    )
  })

  it('says what to do first, and holds Allow, when Kiro Crew sees no ssh-agent', async () => {
    await renderSshAgent(consent({ socket_present: false }))

    expect(screen.getByTestId('ssh-agent-no-socket')).toHaveTextContent(
      /Kiro Crew cannot see an SSH agent on this computer. In a terminal, run ssh-add -l./,
    )
    expect(screen.getByTestId('ssh-agent-allow')).toBeDisabled()
    expect(screen.queryByText('Applies to new sessions. You can withdraw it at any time.')).not.toBeInTheDocument()
  })

  it('a hand-written grant says where it came from instead of a bare "Allowed"', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: null }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    expect(within(row).getByText("Allowed outside this page, in Kiro Crew's settings file. Withdraw turns it off here too.")).toBeInTheDocument()
    expect(within(row).queryByText(/Since /)).not.toBeInTheDocument()
  })

  it('allowed: shows the grant time, says it applies to new sessions, and Withdraw is a plain DELETE', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    expect(within(row).getByText(/Since .*2026/)).toBeInTheDocument()
    expect(within(row).getByText('Applies to new sessions.')).toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('ssh-agent-revoke'))
    await waitFor(() => expect(api.revokeSshAgentConsent).toHaveBeenCalledTimes(1))
    expect(api.grantSshAgentConsent).not.toHaveBeenCalled()
  })

  it('a FAILED read reports the failure and never renders as "Not allowed"', async () => {
    mocked(api.sshAgentConsent).mockRejectedValue(new Error('boom'))
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })

    expect(await screen.findByText(LOAD_FAILED, {}, LAZY_CARD_MOUNT)).toBeInTheDocument()
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-row')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()
  })

  it('a non-owner is told only the owner can see it, and the refused read is not repeated on focus', async () => {
    mocked(api.sshAgentConsent).mockRejectedValue(
      new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })),
    )
    const { queryClient } = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })

    expect(await screen.findByTestId('ssh-agent-owner-only', {}, LAZY_CARD_MOUNT)).toHaveTextContent(OWNER_ONLY)
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-row')).not.toBeInTheDocument()
    expect(api.sshAgentConsent).toHaveBeenCalledTimes(1)

    // The barrier returns once react-query's focus pass has run, so the count
    // below is read after the pass that would have re-read the route.
    await refocusWindow(queryClient)
    expect(api.sshAgentConsent).toHaveBeenCalledTimes(1)
  })

  it('a stale pre-owner session that signs in again as the owner re-reads the grant', async () => {
    // A read that worked, then a restart that left this tab's session predating
    // the owner claim: the refusal keeps the earlier data, which the client's
    // own recovery refetch skips.
    mocked(api.sshAgentConsent)
      .mockResolvedValueOnce(consent({ granted: true, granted_at: GRANTED_AT }))
      .mockRejectedValueOnce(
        new ApiError(401, 'sign in again', JSON.stringify({ code: 'stale_session_reauth' })),
      )
      .mockResolvedValue(consent({ granted: true, granted_at: GRANTED_AT }))
    const { queryClient } = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    await screen.findByTestId('ssh-agent-row', {}, LAZY_CARD_MOUNT)

    await queryClient.refetchQueries({ queryKey: ['ssh-agent-consent'] })
    expect(await screen.findByTestId('ssh-agent-owner-only')).toBeInTheDocument()

    window.dispatchEvent(new CustomEvent('mc-auth-recovered'))
    expect(await screen.findByTestId('ssh-agent-row')).toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-owner-only')).not.toBeInTheDocument()
    expect(api.sshAgentConsent).toHaveBeenCalledTimes(3)
  })

  it('a re-auth 403 (X-Auth-Required) is not the owner gate: the generic failed read', async () => {
    mocked(api.sshAgentConsent).mockRejectedValue(new ApiError(403, 'sign in', '', true))
    renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })

    expect(await screen.findByText(LOAD_FAILED, {}, LAZY_CARD_MOUNT)).toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-owner-only')).not.toBeInTheDocument()
  })

  it('a failed RE-READ after Allow does not keep rendering the stale state', async () => {
    // react-query keeps the last good `data` when a refetch rejects; `view` is
    // gated on `isError`, so the failure notice is the only thing that renders.
    await renderSshAgent(consent())
    mocked(api.sshAgentConsent).mockRejectedValue(new Error('refetch down'))

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))
    await waitFor(() => expect(api.grantSshAgentConsent).toHaveBeenCalledTimes(1))

    expect(await screen.findByText(LOAD_FAILED)).toBeInTheDocument()
    expect(screen.queryByText('Not allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-row')).not.toBeInTheDocument()
  })

  it('a failed grant is reported without claiming the state changed', async () => {
    await renderSshAgent(consent())
    mocked(api.grantSshAgentConsent).mockRejectedValue(new Error('nope'))

    fireEvent.click(screen.getByTestId('ssh-agent-allow'))

    expect(await screen.findByText(SAVE_FAILED)).toBeInTheDocument()
    expect(within(screen.getByTestId('ssh-agent-row')).getByText('Not allowed')).toBeInTheDocument()
  })

  it('a failed withdraw is reported and the row still reads Allowed', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))
    mocked(api.revokeSshAgentConsent).mockRejectedValue(new Error('500'))

    fireEvent.click(screen.getByTestId('ssh-agent-revoke'))

    expect(await screen.findByText(SAVE_FAILED)).toBeInTheDocument()
    expect(within(screen.getByTestId('ssh-agent-row')).getByText('Allowed')).toBeInTheDocument()
  })

  /* The RAIL row deliberately carries NO summary: `SettingsSubNav` renders a
   * label and a summary as two adjacent catalog keys, which the render-time i18n
   * gate counts as a `fragment/multi-unit` finding, and a rail-level read would
   * hit the owner-gated GET for every non-owner who merely opens Security. */
  it('gives the rail row NO summary, even when a grant is held', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))

    const row = screen.getAllByRole('option').find(o => o.textContent?.includes('SSH agent'))
    expect(row).toBeDefined()
    expect(row?.textContent).toBe('SSH agent')
  })
})
