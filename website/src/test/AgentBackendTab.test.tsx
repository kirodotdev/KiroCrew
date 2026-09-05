/**
 * AgentBackendTab — Developer > Agent Backend switch.
 *
 * Two independent gates, and the tests exist mostly to keep them from being
 * collapsed into one. The SCHEMA gate is a build/edition fact from
 * `GET /api/config/schema`: a backend the build cannot serve must not be
 * selectable, and one a later edition adds must become selectable with no change
 * here. The PROBE gate is a machine fact from `GET /api/acp-backends`: a backend
 * whose components are absent must not be selectable either, and must say what to
 * install. Its `unknown` verdict — and every way the probe can fail to answer at
 * all — must leave the option ENABLED, because an optimistic disable costs a user
 * a control they were entitled to and an install they did not need.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, schemaMock, acpBackendsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { acp_backend: '' } })),
  schemaMock: vi.fn(),
  acpBackendsMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock, acpBackends: acpBackendsMock },
}))

vi.mock('../components/settingRef/useConfigSchema', () => ({
  useConfigSchema: () => schemaMock(),
}))

import { AgentBackendTab } from '../pages/developer/AgentBackendTab'

/** A schema map advertising exactly `values` for the backend field. */
function schemaWith(values: string[] | undefined) {
  if (!values) return undefined
  return new Map([['agent.acp_backend', { path: 'agent.acp_backend', type: 'enum', enum: values }]])
}

/**
 * One `GET /api/acp-backends` row, defaulted to the uninteresting answer
 * (selectable and installed) so each test states only the field it is about.
 */
function probeRow(id: string, over: Partial<{ selectable: boolean; installed: string; missing_components: string[]; install_command: string; restart_required: boolean }> = {}) {
  return {
    id,
    policy_id: id || 'kiro',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...over,
  }
}

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AgentBackendTab />
    </QueryClientProvider>,
  )
}

const button = (name: string) => screen.getByRole('button', { name })

beforeEach(() => {
  patchConfigMock.mockClear()
  patchConfigMock.mockResolvedValue({})
  kirocrewConfigMock.mockClear()
  kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: '' } })
  // The shipped core: every known agent is selectable. Claude Code is in the public
  // baseline because acp/client.py owns its whole spawn path and the adapter it needs
  // is a public npm package -- the only thing that used to be missing was the switch.
  schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
  // Default to NO probe information — a 404 from a gateway that predates the
  // endpoint. Every test that does not opt in therefore pins the pre-probe
  // behaviour: schema-only gating, nothing disabled or annotated by the probe.
  acpBackendsMock.mockClear()
  acpBackendsMock.mockRejectedValue(new Error('404 Not Found'))
})

describe('AgentBackendTab', () => {
  it('offers all three backends', async () => {
    wrap()
    expect(await screen.findByRole('button', { name: 'Kiro CLI' })).toBeInTheDocument()
    expect(button('Claude Code')).toBeInTheDocument()
    expect(button('KAS (kiro-agent)')).toBeInTheDocument()
  })

  it('puts the two kiro-family harnesses first and sorts the adapters after', async () => {
    // KAS is not an adapter -- it is kiro-cli's own ACP relay, resolved from the same
    // binary and sharing kiro's install verdict -- so it sits beside Kiro CLI rather
    // than under 'k' in the byte order, which had landed it behind every adapter whose
    // id happens to start earlier ('claude', 'codex'). Order is a product decision, so
    // it is pinned here: a later edit to the comparator cannot quietly restore the
    // alphabet.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'codex']))
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude'), probeRow('kas'), probeRow('codex')],
    })
    wrap()
    await waitFor(() => expect(button('codex')).toBeEnabled())

    const labels = ['Kiro CLI', 'KAS (kiro-agent)', 'Claude Code', 'codex']
    const rendered = screen
      .getAllByRole('button')
      .map(b => b.textContent?.trim())
      .filter(text => text && labels.includes(text))
    expect(rendered).toEqual(labels)
  })

  it('reflects the configured backend as the pressed option', async () => {
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    wrap()
    await waitFor(() => expect(button('KAS (kiro-agent)')).toHaveAttribute('aria-pressed', 'true'))
    expect(button('Kiro CLI')).toHaveAttribute('aria-pressed', 'false')
  })

  it('treats a missing acp_backend as kiro-cli rather than as unset', async () => {
    // `''` is a real backend id, so an absent field must land on Kiro CLI — not
    // leave every option unpressed.
    kirocrewConfigMock.mockResolvedValue({ agent: {} })
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('saves the picked backend to agent.acp_backend', async () => {
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: 'KAS (kiro-agent)' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'kas'))
  })

  it('hides a backend the deployment may not select, rather than dimming it', async () => {
    // A greyed chip invites the reader to go find out how to enable it. Under a
    // managed policy there is nothing they can do -- the answer is not on their
    // machine -- and advertising a forbidden option is the opposite of what the
    // restriction is for. So the row leaves entirely: chip AND status line.
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(button('KAS (kiro-agent)')).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Claude Code' })).not.toBeInTheDocument()
    expect(screen.queryByText('Claude Code')).not.toBeInTheDocument()
  })

  it('keeps the selected backend visible even if it reads as unselectable', async () => {
    // The backend degrades a denied persisted value to the floor on load, so this
    // should not arise. If it ever does, a control rendering no pressed chip is a
    // worse failure than one extra row.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(button('Claude Code')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('derives each row status instead of asserting per-agent capabilities', async () => {
    // The status line is one of three derived strings, so a claim this component
    // cannot substantiate ("isolates what it runs in an OS sandbox", "shares one
    // process across sessions") has nowhere to live. Kiro CLI is the
    // all-supported descriptor; any other selectable agent is Experimental, not a
    // feature list.
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.getByText('Default. All features supported.')).toBeInTheDocument()
    expect(screen.getAllByText('Experimental')).toHaveLength(2)
    // No row carries prose beyond those.
    expect(screen.queryByText(/OS sandbox|steered mid-turn|Anthropic/)).not.toBeInTheDocument()
  })

  it('offers a retry instead of a false selection when the config read fails', async () => {
    // `?? KIRO` is correct for a config that omits the key, and wrong for a read
    // that FAILED: defaulting would paint Kiro CLI as pressed, telling an operator
    // running KAS that they are on Kiro. The control must not render at all.
    kirocrewConfigMock.mockRejectedValue(new Error('offline'))
    wrap()
    expect(await screen.findByText('Could not load the agent backend.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Kiro CLI' })).not.toBeInTheDocument()

    // The retry re-reads, so a transient failure is recoverable in place.
    kirocrewConfigMock.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(button('KAS (kiro-agent)')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('associates a disabled option with the reason it is disabled', async () => {
    // Dimming conveys "unavailable" but not WHY, and the reason lives outside the
    // button in a sibling <dl>. Without aria-describedby a screen reader gets
    // visual proximity, which is no association at all. The remaining disabled
    // reasons are all actionable, so the association is what carries the action.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude', {
          installed: 'missing',
          missing_components: ['claude-agent-acp'],
          install_command: 'npm i -g @agentclientprotocol/claude-agent-acp',
        }),
        probeRow('kas'),
      ],
    })
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeDisabled())
    const describedBy = button('Claude Code').getAttribute('aria-describedby')
    expect(describedBy).toBe('agent-backend-status-claude')
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      'Missing on this machine: claude-agent-acp. Install with: npm i -g @agentclientprotocol/claude-agent-acp',
    )

    // KIRO is the empty string, so its id must not end in a bare separator.
    expect(button('Kiro CLI').getAttribute('aria-describedby')).toBe('agent-backend-status-kiro')
  })

  it('shows a backend the schema starts advertising, with no edit here', async () => {
    // Visibility is off the schema, not a per-agent literal: widening the enum makes
    // the row appear and read as Experimental without touching this component.
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Claude Code' })).not.toBeInTheDocument()

    cleanup()
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeEnabled())
    expect(screen.getAllByText('Experimental')).toHaveLength(2)
  })

  it('does not offer a backend the deployment may not select', async () => {
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Claude Code' })).not.toBeInTheDocument()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('offers an agent this frontend has no name for, under its policy id', async () => {
    // The case a hard-coded candidate list could not express. An edition calls
    // `register_selectable_backend`, so the id reaches the schema enum and the probe
    // payload -- but nothing in this file knows it exists. Before, the row was
    // filtered out of a literal `[KIRO, CLAUDE, KAS]` and the only control that sets
    // `agent.acp_backend` could not offer a backend the wire already accepted.
    //
    // `policy_id` carries the label because it is the name a governance rule spells,
    // so it is already a word rather than an internal token. Untranslated on purpose:
    // legible beats a chip with no text, and a core agent that ships selectable earns
    // a real translated entry instead.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'codex']))
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude'), probeRow('kas'), probeRow('codex')],
    })
    wrap()
    await waitFor(() => expect(button('codex')).toBeEnabled())

    // Reachable, not merely rendered: the click has to write the id the wire accepts.
    fireEvent.click(button('codex'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'codex'))
  })

  it('hides a known-but-unselectable agent even when the probe lists it', async () => {
    // `GET /api/acp-backends` returns a row per id the CORE knows, which is a wider
    // set than the deployment may select -- codex ships known and not selectable. So
    // widening `candidates` to the probe payload must not smuggle in a row the schema
    // excludes, or the panel would offer an option PATCH answers 400 for.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude'),
        probeRow('kas'),
        probeRow('codex', { selectable: false }),
      ],
    })
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'codex' })).not.toBeInTheDocument()
    expect(screen.queryByText('codex')).not.toBeInTheDocument()
  })

  it('saves the Claude Code selection the shipped build offers', async () => {
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeEnabled())

    fireEvent.click(button('Claude Code'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
  })

  it('leaves every option visible and selectable while the schema is still loading', async () => {
    // Flashing disabled and then live reads as a broken control; the PATCH
    // allowlist is the real gate, so an optimistic enable costs one refusal. The
    // same reasoning forbids hiding a row on a missing answer.
    schemaMock.mockReturnValue(undefined)
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeEnabled())
    expect(button('Kiro CLI')).toBeEnabled()
    expect(button('KAS (kiro-agent)')).toBeEnabled()
  })

  it('surfaces a rejected save', async () => {
    patchConfigMock.mockRejectedValueOnce(new Error('nope'))
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: 'KAS (kiro-agent)' }))
    expect(await screen.findByText('Could not save the agent backend.')).toBeInTheDocument()
  })

  it('keeps showing the server value after a rejected save', async () => {
    // No optimistic write: the pressed option must still be the backend the
    // server last confirmed, not the one the click attempted.
    patchConfigMock.mockRejectedValueOnce(new Error('nope'))
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: 'KAS (kiro-agent)' }))
    await screen.findByText('Could not save the agent backend.')
    expect(button('Kiro CLI')).toHaveAttribute('aria-pressed', 'true')
    expect(button('KAS (kiro-agent)')).toHaveAttribute('aria-pressed', 'false')
  })

  it('disables a backend this machine is missing, and names the install command', async () => {
    // The gap the probe exists to close: the build serves KAS, so the schema
    // lights it up, but the components are not on this machine. Disabling without
    // saying what is absent leaves the user with a dead control and no remedy.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude', { installed: 'unknown' }),
        probeRow('kas', {
          installed: 'missing',
          missing_components: ['kiro-agent', 'kiro-agent-acp'],
          install_command: 'npm install -g @kiro/agent',
        }),
      ],
    })
    wrap()
    await waitFor(() => expect(button('KAS (kiro-agent)')).toBeDisabled())
    expect(
      screen.getByText(
        'Missing on this machine: kiro-agent, kiro-agent-acp. Install with: npm install -g @kiro/agent',
      ),
    ).toBeInTheDocument()
    // Its own row still carries the reason, so the disabled button describes itself.
    const describedBy = button('KAS (kiro-agent)').getAttribute('aria-describedby')
    expect(describedBy).toBe('agent-backend-status-kas')
    expect(document.getElementById(describedBy!)).toHaveTextContent('Missing on this machine')
    // A backend the machine HAS is untouched by another one's verdict.
    expect(button('Kiro CLI')).toBeEnabled()
  })

  it('states the missing components without a command when the server has none to give', async () => {
    // `install_command: ''` means there is nothing to suggest. Naming the absent
    // components is still actionable; inventing a command is not.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('kas', { installed: 'missing', missing_components: ['kiro-agent'] })],
    })
    wrap()
    await waitFor(() => expect(button('KAS (kiro-agent)')).toBeDisabled())
    expect(screen.getByText('Missing on this machine: kiro-agent')).toBeInTheDocument()
    expect(screen.queryByText(/Install with/)).not.toBeInTheDocument()
  })

  it('leaves a backend ENABLED when the install check could not be completed', async () => {
    // `unknown` is the check failing, not the binary being absent. Collapsing it
    // onto missing would disable a control the user may be entitled to and send
    // them to install something they already have.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('kas', { installed: 'unknown' })],
    })
    wrap()
    await waitFor(() =>
      expect(
        screen.getByText('Could not check whether this is installed on this machine.'),
      ).toBeInTheDocument(),
    )
    expect(button('KAS (kiro-agent)')).toBeEnabled()
    // And the line must not read as an absent install.
    expect(screen.queryByText(/Missing on this machine/)).not.toBeInTheDocument()

    // Enabled means genuinely usable: the PATCH allowlist is the real gate.
    fireEvent.click(button('KAS (kiro-agent)'))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.acp_backend', 'kas'))
  })

  it('falls back to schema-only gating when the probe endpoint refuses (403)', async () => {
    // Owner-only endpoint, and absent entirely on an older gateway. Neither is a
    // verdict about this machine, so nothing may be disabled, hidden or annotated by
    // it — the tab behaves exactly as it did before the endpoint existed.
    acpBackendsMock.mockRejectedValue(new Error('403 Forbidden'))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(button('Claude Code')).toBeEnabled()
    expect(button('KAS (kiro-agent)')).toBeEnabled()
    expect(screen.getByText('Default. All features supported.')).toBeInTheDocument()
    expect(screen.getAllByText('Experimental')).toHaveLength(2)
    expect(screen.queryByText(/Missing on this machine|Could not check/)).not.toBeInTheDocument()
  })

  it('never flashes disabled while the probe is still in flight', async () => {
    // A pending answer is absent information. Gating on it would dim a live
    // control on every slow load, then un-dim it — which reads as broken.
    acpBackendsMock.mockReturnValue(new Promise(() => {}))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(button('KAS (kiro-agent)')).toBeEnabled()
    expect(screen.queryByText(/Missing on this machine|Could not check/)).not.toBeInTheDocument()
  })

  it('hides a row the probe reports as not selectable, whatever the schema says', async () => {
    // The probe's own `selectable` is honoured, not just the schema's enum: here the
    // schema advertises Claude Code and only the probe field says otherwise. An agent
    // the deployment may not select leaves entirely, so its install verdict never
    // appears -- naming a command for something that would be refused anyway is worse
    // than silence.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude', {
          selectable: false,
          installed: 'missing',
          missing_components: ['claude-code'],
          install_command: 'npm install -g @anthropic-ai/claude-code',
        }),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    wrap()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Claude Code' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/Missing on this machine|claude-code/)).not.toBeInTheDocument()
  })

  it('leaves a selectable, installed backend reading exactly as it did before', async () => {
    // The probe adds lines for the two cases it can report; it must not rewrite
    // the ordinary ones. An all-installed answer is indistinguishable from no
    // answer at all in this view.
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude'), probeRow('kas')],
    })
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.getByText('Default. All features supported.')).toBeInTheDocument()
    expect(screen.getAllByText('Experimental')).toHaveLength(2)
    expect(screen.queryByText(/Missing on this machine|Could not check/)).not.toBeInTheDocument()
    expect(button('KAS (kiro-agent)')).toBeEnabled()
    expect(button('Claude Code')).toBeEnabled()
  })

  it('disables an installed backend this gateway must restart to use', async () => {
    // The one case where a POSITIVE install verdict still gates the control: the
    // binary is on disk, but this process cached its absence, so the click would
    // reach a spawn that fails. Offering it would be the "told you it was ready,
    // then failed the session" trap the probe exists to prevent.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude', { restart_required: true }),
        probeRow('kas'),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeDisabled())
    expect(
      screen.getByText('Installed on this machine, but this gateway must restart before it can be used.'),
    ).toBeInTheDocument()
    // Must not read as absent — the operator already installed it.
    expect(screen.queryByText(/Missing on this machine/)).not.toBeInTheDocument()
  })

  it('hides an unselectable row rather than telling the user to restart', async () => {
    // Restart advice is wasted work for an agent the deployment may not select:
    // restarting changes nothing. Hiding outranks every actionable line.
    acpBackendsMock.mockResolvedValue({
      backends: [
        probeRow(''),
        probeRow('claude', { selectable: false, restart_required: true }),
        probeRow('kas'),
      ],
    })
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))
    wrap()
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Claude Code' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/must restart/)).not.toBeInTheDocument()
  })

  it('re-asks the probe on an interval under the app\'s real staleTime: Infinity', async () => {
    // The bug this pins is invisible to a test that builds its own QueryClient with
    // default options: the app's global client sets `staleTime: Infinity` and its own
    // comment says freshness is driven "exclusively by WebSocket push
    // (invalidateQueries on server events)" — and there is no server event for this
    // probe. Inheriting that default made the answer permanent for the life of the
    // page, so an operator who ran the install command the panel gave them was left
    // looking at a disabled option with no way to re-ask short of a reload.
    //
    // So this test mirrors the REAL default, not the convenient one.
    vi.useFakeTimers()
    try {
      acpBackendsMock.mockResolvedValue({
        backends: [
          probeRow(''),
          probeRow('claude', {
            installed: 'missing',
            missing_components: ['claude-agent-acp'],
            install_command: 'npm i -g @agentclientprotocol/claude-agent-acp',
          }),
          probeRow('kas'),
        ],
      })
      schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas']))

      const qc = new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: Infinity } },
      })
      render(
        <QueryClientProvider client={qc}>
          <AgentBackendTab />
        </QueryClientProvider>,
      )
      await vi.waitFor(() => expect(button('Claude Code')).toBeDisabled())

      // The operator installs the adapter the panel just named.
      acpBackendsMock.mockResolvedValue({
        backends: [probeRow(''), probeRow('claude'), probeRow('kas')],
      })
      await vi.advanceTimersByTimeAsync(31_000)

      await vi.waitFor(() => expect(button('Claude Code')).toBeEnabled())
      expect(screen.queryByText(/Missing on this machine/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('states that a pre-approval in Claude\'s own settings skips Crew\'s gate', async () => {
    // The DEFAULT path is gated -- Claude asks, that becomes session/request_permission,
    // and Crew decides. What escapes is narrower: a tool already pre-approved in
    // Claude's own settings never asks, because the SDK approves an allow-rule match
    // before consulting the client. Those settings include a .claude/settings.json in
    // the project, the copy an operator did not write. The line must say the narrow
    // thing: claiming Crew never gates Claude at all would be false.
    wrap()
    await waitFor(() => expect(button('Claude Code')).toBeEnabled())
    expect(screen.getByText(/normally asks before it acts/)).toBeInTheDocument()
    expect(screen.getByText(/pre-approved in Claude's own settings/)).toBeInTheDocument()
  })

  it('does not put that caveat on the other agents', async () => {
    // Kiro CLI and KAS run under Crew's own approval path with no settings file that
    // can pre-approve past it, so claiming otherwise for them would be false and would
    // train the reader to ignore the line.
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.getAllByText(/normally asks before it acts/)).toHaveLength(1)
  })

  it('drops the caveat with the row when Claude Code is not selectable', async () => {
    schemaMock.mockReturnValue(schemaWith(['', 'kas']))
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.queryByText(/normally asks before it acts/)).not.toBeInTheDocument()
  })

  it('tells a Codex operator that being installed is not being signed in', async () => {
    // The gap the install line cannot cover. codex-acp ships its own Codex binary, so
    // `installed` answers the whole binary question -- and a session with no credential
    // still dies on the first turn, with nothing on the page having said what was
    // absent. Both branches of the remedy must be named: Codex's own sign-in, and a
    // model provider in ~/.codex/config.toml for credentials that come from elsewhere.
    schemaMock.mockReturnValue(schemaWith(['', 'claude', 'kas', 'codex']))
    acpBackendsMock.mockResolvedValue({
      backends: [probeRow(''), probeRow('claude'), probeRow('kas'), probeRow('codex')],
    })
    wrap()
    await waitFor(() => expect(button('codex')).toBeEnabled())
    expect(screen.getByText(/Codex signs in on its own/)).toBeInTheDocument()
    expect(screen.getByText(/~\/\.codex\/config\.toml/)).toBeInTheDocument()
  })

  it('says the credential is not checked here rather than implying it is', async () => {
    // The reason this is a standing caveat and not a probe line: the panel does not
    // read those files, and a `missing` verdict would DISABLE the switch for an
    // operator who is authenticated by a path the check cannot see. The sentence has
    // to disclaim the measurement, or the reader takes silence for a green light.
    schemaMock.mockReturnValue(schemaWith(['', 'codex']))
    acpBackendsMock.mockResolvedValue({ backends: [probeRow(''), probeRow('codex')] })
    wrap()
    await waitFor(() => expect(button('codex')).toBeEnabled())
    expect(screen.getByText(/Neither is checked here/)).toBeInTheDocument()
  })

  it('does not put the Codex caveat on the other agents', async () => {
    // Kiro CLI and KAS authenticate through Crew's own identity store, so telling
    // their reader to finish a separate sign-in would be false.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow(''), probeRow('claude'), probeRow('kas')] })
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.queryByText(/Codex signs in on its own/)).not.toBeInTheDocument()
  })

  it('states that the set is decided at gateway start', async () => {
    // The only place the policy semantics can be surfaced: nothing in the UI can
    // detect a not-yet-applied policy edit, so an operator who edits the policy
    // and sees no change needs this sentence to know it is not a bug.
    acpBackendsMock.mockResolvedValue({ backends: [probeRow('')] })
    wrap()
    await waitFor(() => expect(button('Kiro CLI')).toBeEnabled())
    expect(screen.getByText(/decided when the gateway starts/)).toBeInTheDocument()
  })
})
