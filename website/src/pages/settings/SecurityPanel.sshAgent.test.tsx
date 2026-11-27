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
import type { SshAgentConsentStatus } from '../../api/client/security'

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
const RISK = 'A session could push code or sign commits under your name on GitHub and on any server your SSH keys open.'
const WHAT = 'Lets sessions (the chats and tasks Kiro Crew runs for you) use the SSH keys loaded on this computer to reach GitHub and your own servers.'
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
/** `harness_labels` as the gateway sends it for every harness in
 *  `ACP_BACKEND_ROUTING` (`agent_sdk/backends_routing.py`), keyed in the config
 *  spelling, valued with `tool_gate.label_for`. */
const ROUTING_LABELS = {
  antigravity: 'Google Antigravity', claude: 'claude', codex: 'OpenAI Codex', copilot: 'GitHub Copilot CLI',
  deepseek: 'DeepSeek Harness', goose: 'goose', kas: 'kas', kiro: 'Kiro CLI', mimocode: 'MiMo Code',
  opencode: 'OpenCode', pi: 'Pi', strands: 'Strands',
}

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

    expect(screen.queryByTestId('ssh-agent-mixed-intro')).not.toBeInTheDocument()
    expect(screen.getByText(RISK)).toBeInTheDocument()
    // The risk text makes no claim about the key files: on the standard level
    // they are readable, so "hidden" would be false there.
    expect(screen.getByTestId('ssh-agent-risk')).not.toHaveTextContent(/hidden/i)
    expect(screen.getByText('A session already running keeps it until that session ends.')).toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    // One group of sessions: the single row keeps the generic label.
    expect(within(row).getByTestId('ssh-agent-row-label')).toHaveTextContent('Your SSH keys on this computer')
    expect(within(row).getByText('Not allowed')).toBeInTheDocument()
    // The decision line above the row already says what Allow is for; the row
    // does not repeat it beside the control.
    expect(within(row).queryByText(/git push/)).not.toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
    expect(screen.getByTestId('ssh-agent-allow')).not.toBeDisabled()
    expect(within(row).queryByText(/Applies to new sessions/)).not.toBeInTheDocument()
    expect(within(row).queryByText('Allowed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-revoke')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-no-socket')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-tier-forwards')).not.toBeInTheDocument()
  })

  it('the risk line is the same consequence-first sentence in every scope', async () => {
    for (const state of [consent(), consent({ forwarded_harnesses: ['kiro'] }), consent({ forwarded_without_consent: true })]) {
      const { unmount } = await renderSshAgent(state)
      expect(within(screen.getByTestId('ssh-agent-risk')).getByText(RISK)).toBeInTheDocument()
      unmount()
    }
  })

  it('leads with when to turn it on, then what is shared, then the risk', async () => {
    await renderSshAgent(consent())

    const purpose = screen.getByText(PURPOSE)
    const what = screen.getByText(WHAT)
    const risk = screen.getByText(RISK)
    expect(purpose.compareDocumentPosition(what) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(purpose.compareDocumentPosition(risk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(what.compareDocumentPosition(risk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('ssh-agent-allow-benefit')).not.toBeInTheDocument()
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

    expect(screen.queryByTestId('ssh-agent-mixed-intro')).not.toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('On at your sandbox level')
    expect(within(row).queryByText('Not allowed')).not.toBeInTheDocument()
    expect(within(row).queryByTestId('ssh-agent-tier-group')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-allow')).not.toBeInTheDocument()
    expect(screen.queryByText(/Applies to new sessions/)).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-sandbox-link')).toHaveAttribute('href', expect.stringContaining('/developer?tab=config'))
    // "Until you withdraw this" would be false: withdrawing changes nothing here.
    expect(screen.queryByText(/until you withdraw this/)).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-tier-forwards')).toHaveTextContent(
      /already lets new sessions use your SSH agent, allowed here or not/,
    )
  })

  it('mixed scope: each group of sessions has its own label and status', async () => {
    await renderSshAgent(consent({ forwarded_harnesses: ['claude', 'kiro'], harness_labels: LABELS }))

    expect(screen.getByTestId('ssh-agent-mixed-intro').textContent).toBe('Some sessions already get your SSH agent from their sandbox level.')
    const row = screen.getByTestId('ssh-agent-row')
    // Each row's label says what its group means, and no harness names are listed.
    expect(within(row).getByTestId('ssh-agent-row-label')).toHaveTextContent('Sessions whose sandbox hides your SSH keys')
    expect(within(row).queryByText('OpenAI Codex', { exact: false })).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('Not allowed')
    // The control keeps its one label; the note about the sessions it does NOT
    // decide comes after it rather than between the label and the control.
    const allow = within(row).getByTestId('ssh-agent-allow')
    expect(allow).toHaveTextContent('Allow SSH agent')
    expect(allow.compareDocumentPosition(within(row).getByTestId('ssh-agent-tier-forwards-some')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The sessions that already have the agent read "on", not "Not allowed".
    expect(within(row).getByTestId('ssh-agent-tier-state')).toHaveTextContent('On at your sandbox level')
    expect(within(row).getByTestId('ssh-agent-tier-label')).toHaveTextContent('Sessions whose sandbox level already gives them your SSH agent')
    expect(within(row).queryByTestId('ssh-agent-row-names')).not.toBeInTheDocument()
    expect(within(row).queryByTestId('ssh-agent-tier-names')).not.toBeInTheDocument()
    expect(within(row).queryByText('Kiro CLI', { exact: false })).not.toBeInTheDocument()
    expect(within(row).queryByText('Your SSH keys on this computer')).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-tier-forwards-some')).toHaveTextContent(
      'This setting does not change the sessions your sandbox level already gives your SSH agent to. To stop that, choose the Strict level of Process Sandbox and leave this setting at Not allowed. Both apply to new sessions only.',
    )
    expect(within(row).queryByText('whatever this setting says', { exact: false })).not.toBeInTheDocument()
    // "agent" on this card means the SSH agent only.
    expect(within(row).queryByText(/other agents?\b/)).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-sandbox-link')).toHaveTextContent('Change Process Sandbox')
    expect(screen.queryByText(/until you withdraw this/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-tier-forwards')).not.toBeInTheDocument()
    expect(screen.getByTestId('ssh-agent-allow')).not.toBeDisabled()
  })

  it('mixed scope with the grant held: the decided harnesses read "Allowed"', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT, forwarded_harnesses: ['kiro'], harness_labels: LABELS }))

    expect(screen.getByTestId('ssh-agent-mixed-intro').textContent).toBe('Some sessions already get your SSH agent from their sandbox level.')
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-row-label')).toHaveTextContent('Sessions whose sandbox hides your SSH keys')
    expect(within(row).queryByTestId('ssh-agent-row-names')).not.toBeInTheDocument()
    expect(within(row).queryByTestId('ssh-agent-tier-names')).not.toBeInTheDocument()
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('Allowed')
    expect(screen.getByTestId('ssh-agent-revoke')).toBeInTheDocument()
  })

  it('mixed scope over the real harness set labels each group by what it means', async () => {
    // The usual default, the standard level: Kiro CLI, KAS and Claude Code are not
    // in `tool_gate.ENFORCED_ROUTINGS`, so they get no credential mask and forward;
    // every enforced harness hides ~/.ssh. Neither label is a name list.
    await renderSshAgent(consent({ forwarded_harnesses: ['claude', 'kas', 'kiro'], harness_labels: ROUTING_LABELS }))

    expect(screen.getByTestId('ssh-agent-mixed-intro').textContent).toBe('Some sessions already get your SSH agent from their sandbox level.')
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-row-label').textContent).toBe('Sessions whose sandbox hides your SSH keys')
    expect(within(row).getByTestId('ssh-agent-tier-label').textContent).toBe('Sessions whose sandbox level already gives them your SSH agent')
    expect(within(row).queryByTestId('ssh-agent-row-names')).not.toBeInTheDocument()
    expect(within(row).queryByTestId('ssh-agent-tier-names')).not.toBeInTheDocument()
    expect(within(row).queryByText('OpenAI Codex', { exact: false })).not.toBeInTheDocument()
  })

  it('mixed scope from a gateway that sends no labels still says which sessions the row covers', async () => {
    await renderSshAgent(consent({ forwarded_harnesses: ['kiro', 'acme'] }))

    expect(screen.getByTestId('ssh-agent-mixed-intro').textContent).toBe('Some sessions already get your SSH agent from their sandbox level.')
    // Never an unqualified "Not allowed" while some sessions have the agent.
    expect(screen.getByTestId('ssh-agent-row-label')).toHaveTextContent('Sessions whose sandbox hides your SSH keys')
    expect(screen.getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
    // The forwarded group still gets its own label and status.
    expect(screen.getByTestId('ssh-agent-tier-state')).toHaveTextContent('On at your sandbox level')
    expect(screen.getByTestId('ssh-agent-tier-label')).toHaveTextContent('Sessions whose sandbox level already gives them your SSH agent')
    expect(screen.queryByTestId('ssh-agent-row-names')).not.toBeInTheDocument()
    expect(screen.queryByTestId('ssh-agent-tier-names')).not.toBeInTheDocument()
  })

  it('mixed scope names the harnesses set up here under their group, and no other harness', async () => {
    // The default setup on the standard level: both role settings run KAS, which
    // forwards, so the group this setting decides holds none of them.
    const standard = { forwarded_harnesses: ['claude', 'kas', 'kiro'], harness_labels: ROUTING_LABELS }
    const { unmount } = await renderSshAgent(consent({ ...standard, configured_harnesses: ['kas'] }))
    let row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-row-names').textContent).toBe('None of the harnesses you have set up')
    expect(within(row).getByTestId('ssh-agent-tier-names').textContent).toBe('Harnesses you have set up: Kiro Agent')
    expect(within(row).getByTestId('ssh-agent-allow')).toHaveTextContent('Allow SSH agent')
    expect(within(row).queryByText(/Claude Code|Kiro CLI|OpenAI Codex/)).not.toBeInTheDocument()
    unmount()

    // A crewmate pinned to Codex: Codex is named where this setting decides, an id
    // the verdict does not cover is never named, and Allow keeps its one label.
    await renderSshAgent(consent({ ...standard, configured_harnesses: ['acme', 'codex', 'kas'] }))
    row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-row-names').textContent).toBe('Harnesses you have set up: OpenAI Codex')
    expect(within(row).getByTestId('ssh-agent-tier-names').textContent).toBe('Harnesses you have set up: Kiro Agent')
    expect(within(row).getByTestId('ssh-agent-allow').textContent).toBe('Allow SSH agent')
    expect(within(row).queryByText(/acme|Claude Code|goose/)).not.toBeInTheDocument()
  })

  it('allowed at a level that forwards anyway: the row reads as what sessions get, the button as what it removes', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT, forwarded_without_consent: true }))

    expect(screen.queryByTestId('ssh-agent-mixed-intro')).not.toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByTestId('ssh-agent-tier-forwards')).toBeInTheDocument()
    // Label and badge read as one status: sessions get the agent, from the level.
    expect(within(row).getByTestId('ssh-agent-row-label').textContent).toBe('Sessions get your SSH agent')
    expect(within(row).getByTestId('ssh-agent-state')).toHaveTextContent('On at your sandbox level')
    // The same label as every other state, and true in this one: it removes the
    // permission, not the forward; the help line says the forward stays.
    expect(screen.getByTestId('ssh-agent-revoke')).toHaveTextContent(/^Stop allowing SSH agent$/)
    // What sessions get at this level, then what the permission still decides.
    expect(within(row).getByTestId('ssh-agent-revoke-tier-help')).toHaveTextContent(
      'At this sandbox level, sessions get your SSH agent anyway. Without your permission, they will not get it once you switch Process Sandbox to Strict.',
    )
  })

  it('says what to do first, and holds Allow, when Kiro Crew sees no ssh-agent', async () => {
    await renderSshAgent(consent({ socket_present: false }))

    expect(screen.getByTestId('ssh-agent-no-socket')).toHaveTextContent(
      /Kiro Crew cannot see an SSH agent on this computer. In a terminal, run ssh-add -l./,
    )
    expect(screen.getByTestId('ssh-agent-allow')).toBeDisabled()
    expect(screen.queryByText(/Applies to new sessions/)).not.toBeInTheDocument()
  })

  it('a hand-written grant says where it came from instead of a bare "Allowed"', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: null }))

    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    expect(within(row).getByText("Allowed outside this page, in Kiro Crew's settings file. You can remove it here too.")).toBeInTheDocument()
    expect(within(row).queryByText(/Since /)).not.toBeInTheDocument()
  })

  it('allowed: shows the grant time, says it applies to new sessions, and removing the permission is a plain DELETE', async () => {
    await renderSshAgent(consent({ granted: true, granted_at: GRANTED_AT }))

    expect(screen.queryByTestId('ssh-agent-mixed-intro')).not.toBeInTheDocument()
    const row = screen.getByTestId('ssh-agent-row')
    expect(within(row).getByText('Allowed')).toBeInTheDocument()
    // Neutral, not the warn tone: the risk line already carries the warning.
    expect(within(row).getByTestId('ssh-agent-state')).not.toHaveClass('bg-warn-subtle')
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

  it('a transient first-read failure re-reads when the section is opened again', async () => {
    // The first GET fails with nothing cached (its retries spent); the endpoint
    // then recovers. Reopening the section, with no focus change, must re-read.
    mocked(api.sshAgentConsent)
      .mockRejectedValueOnce(new ApiError(502, 'bad gateway', ''))
      .mockResolvedValue(consent())
    const { rerender } = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    expect(await screen.findByText(LOAD_FAILED, {}, LAZY_CARD_MOUNT)).toBeInTheDocument()

    rerender(<div />)
    rerender(<SecurityPanel />)
    expect(await screen.findByTestId('ssh-agent-row', {}, LAZY_CARD_MOUNT)).toBeInTheDocument()
    expect(api.sshAgentConsent).toHaveBeenCalledTimes(2)
  })

  it('a cached owner-gate refusal is not repeated when the section is opened again', async () => {
    mocked(api.sshAgentConsent).mockRejectedValue(
      new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })),
    )
    const { rerender } = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    expect(await screen.findByTestId('ssh-agent-owner-only', {}, LAZY_CARD_MOUNT)).toBeInTheDocument()

    rerender(<div />)
    rerender(<SecurityPanel />)
    expect(await screen.findByTestId('ssh-agent-owner-only', {}, LAZY_CARD_MOUNT)).toBeInTheDocument()
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

  it('a sign-in while the section is closed re-reads the cached refusal on reopen', async () => {
    // The same stale pre-owner refusal, but the owner leaves the section before
    // signing in, so the card is unmounted when `mc-auth-recovered` fires.
    mocked(api.sshAgentConsent)
      .mockResolvedValueOnce(consent({ granted: true, granted_at: GRANTED_AT }))
      .mockRejectedValueOnce(
        new ApiError(401, 'sign in again', JSON.stringify({ code: 'stale_session_reauth' })),
      )
      .mockResolvedValue(consent({ granted: true, granted_at: GRANTED_AT }))
    const { queryClient, rerender } = renderWithProviders(<SecurityPanel />, { route: '/?section=ssh_agent' })
    await screen.findByTestId('ssh-agent-row', {}, LAZY_CARD_MOUNT)
    await queryClient.refetchQueries({ queryKey: ['ssh-agent-consent'] })
    expect(await screen.findByTestId('ssh-agent-owner-only')).toBeInTheDocument()

    rerender(<div />)
    window.dispatchEvent(new CustomEvent('mc-auth-recovered'))
    rerender(<SecurityPanel />)

    expect(await screen.findByTestId('ssh-agent-row', {}, LAZY_CARD_MOUNT)).toBeInTheDocument()
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
