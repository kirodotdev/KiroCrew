/**
 * KiroPrerequisiteGate: the agent picker -- its intro and retry copy in every
 * shipped locale, and which probe verdicts leave Use enabled. Choosing an agent
 * is in KiroPrerequisiteGate.agentChoice.test.tsx.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate from '../components/KiroPrerequisiteGate'
import { renderWithProviders } from './helpers'

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
  copyCode: vi.fn(),
}))

vi.mock('../api/client', async () => ({
  // The REAL ApiError, not a stand-in: the shared `setupMarkerError*` /
  // `agentChoiceSaved` helpers in `api/acpBackend` narrow with `instanceof`
  // against `api/apiError`, so an error the test builds has to be that same
  // class or every marker-write assertion silently reads `null`. `apiError` is
  // the side-effect-free split, so importing it here pulls in none of client's
  // transport graph.
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

import { api, ApiError } from '../api/client'
import type { AcpBackendProbe } from '../api/client'
import { CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'
import { probe, status } from './KiroPrerequisiteGateTestUtils'

describe('KiroPrerequisiteGate agent picker copy (UX round 5)', () => {
  const INTRO = /Already use another coding agent\?/
  const SWITCH_LATER = 'You can switch agents at any time in Settings > Agent Harness.'
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )
  const openPicker = async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
  }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('does not tell the user to pick an agent above a list that offers none', async () => {
    // "Pick one that is installed on the gateway host" directly above "This
    // build offers no other coding agents" was an instruction and its own
    // refutation on consecutive lines.
    render()
    await openPicker()

    expect(await screen.findByText('This build offers no other coding agents.')).toBeInTheDocument()
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()
  })

  it('withholds the intro while the list is still being checked', async () => {
    // Nothing to pick from yet either; the intro arrives with the list.
    let resolveBackends: (v: { backends: AcpBackendProbe[] }) => void = () => {}
    vi.mocked(api.acpBackends).mockReturnValue(
      new Promise(resolve => { resolveBackends = resolve }),
    )
    render()
    await openPicker()

    expect(await screen.findByText('Checking which agents are installed…')).toBeInTheDocument()
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()

    await act(async () => { resolveBackends({ backends: [probe()] }) })
    expect(await screen.findByRole('radio', { name: 'Claude Code' })).toBeInTheDocument()
    expect(screen.getByText(INTRO)).toBeInTheDocument()
  })

  it('withholds the intro when the probe failed, leaving the notice its own pointer', async () => {
    vi.mocked(api.acpBackends).mockRejectedValue(new ApiError(403, 'forbidden'))
    render()
    await openPicker()

    const notice = await screen.findByTestId('other-agents-probe-error')
    expect(notice).toHaveTextContent('You can choose one later in Settings > Agent Harness.')
    expect(screen.queryByText(INTRO)).not.toBeInTheDocument()
  })

  it('keeps the intro above a list with agents to pick from', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()
    await openPicker()

    await screen.findByRole('radio', { name: 'Claude Code' })
    expect(screen.getByText(INTRO)).toHaveTextContent('Pick one that is installed on the gateway host.')
  })

  it('names the Use button, by its rendered label, as the retry for a failed agent save', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()
    await openPicker()

    const useButton = await screen.findByRole('button', { name: /Use Claude Code/ })
    fireEvent.click(useButton)
    const notice = await screen.findByTestId('other-agent-switch-error')
    // The retry it names is the control that exists in this row, spelled as
    // that control spells itself. No control on this screen is called Try again.
    const label = useButton.textContent?.trim()
    expect(label).toBe('Use Claude Code')
    expect(notice).toHaveTextContent(`Press ${label} to try again.`)
    expect(notice).not.toHaveTextContent(/\bTry again\./)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('names the Use Kiro CLI instead button, by its rendered label, as the retry for a failed switch back', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    fireEvent.click(fallback)
    const notice = await screen.findByTestId('use-kiro-instead-error')
    expect(notice).toHaveTextContent(`Press ${fallback.textContent?.trim()} to try again.`)
    expect(notice).not.toHaveTextContent(/\bTry again\./)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })

  it('says a failed re-check in the same words as the Check failed badge', async () => {
    // Badge "Check failed" + notice "Could not re-check pi" read as two
    // different problems on one row. One check, one failure, one vocabulary.
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'pi', policy_id: 'pi', installed: 'unknown' })],
    })
    vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(500, 'Probe failed'))
    render()
    await openPicker()

    const badge = await screen.findByText('Check failed')
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('The check for Pi failed. Press Check again.')
    expect(notice).not.toHaveTextContent(/re-check|Could not/)
    // Same two words carry the verdict in both places.
    expect(badge.textContent).toMatch(/check/i)
    expect(badge.textContent).toMatch(/fail/i)
    expect(notice.textContent).toMatch(/check/i)
    expect(notice.textContent).toMatch(/fail/i)
    // The badge stays short and does not borrow the positive sibling's word.
    expect(badge.textContent).not.toMatch(/installed/i)
    expect(badge.textContent).not.toBe('Installed, not active yet')
  })

  it('says "switch agents later" once when the configured-agent notice already says it', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    render()

    await screen.findByText(/is set to use Claude Code, which isn't ready/)
    const lines = screen.getAllByText(SWITCH_LATER)
    expect(lines).toHaveLength(1)
    // The one that stays is the notice's, beside Use Kiro CLI instead.
    expect(lines[0].parentElement).toBe(
      screen.getByRole('button', { name: 'Use Kiro CLI instead' }).parentElement,
    )
  })

  it('keeps the list footer\'s "switch agents later" line when no configured-agent notice is up', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()
    await openPicker()

    await screen.findByRole('radio', { name: 'Claude Code' })
    expect(screen.getAllByText(SWITCH_LATER)).toHaveLength(1)
    expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument()
  })

  it('brings the footer line back once the notice leaves with a successful switch to Kiro CLI', async () => {
    // The notice and the footer swap rather than stack: while the configured
    // agent is not ready the notice carries the line; after Use Kiro CLI
    // instead saves, the notice is gone and the foot of the list says it.
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
      return {}
    })
    render()

    await screen.findByText(/is set to use Claude Code, which isn't ready/)
    expect(screen.getAllByText(SWITCH_LATER)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Use Kiro CLI instead' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kiro'))
    await waitFor(() => expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument())
    // Still on setup (Kiro CLI is not installed), the list still open.
    expect(screen.getByRole('radio', { name: 'Claude Code' })).toBeInTheDocument()
    const lines = screen.getAllByText(SWITCH_LATER)
    expect(lines).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Use Kiro CLI instead' })).not.toBeInTheDocument()
  })

  describe('every locale quotes its own button labels in the retry notices', () => {
    // The English guard above reads the rendered button; the other catalogs
    // are only ever seen by a reader of that language, so the same drift —
    // a relabelled button leaving a notice pointing at a name that is no
    // longer on screen — is pinned per locale here. The generated
    // pseudolocale is excluded: its per-key mangling does not preserve
    // cross-key containment, and it regenerates from English anyway.
    const generated = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))
    const shipped = Object.keys(CATALOGS).filter(code => !generated.has(code))
    const gate = (code: string) => (
      (CATALOGS[code].translation as { components: { kiroPrerequisiteGate: Record<string, string> } })
        .components.kiroPrerequisiteGate
    )

    it('covers every shipped language', () => {
      expect(shipped).toEqual(expect.arrayContaining(['en', 'de', 'ja', 'zh-CN']))
      expect(shipped.length).toBeGreaterThanOrEqual(12)
    })

    it.each(shipped)('%s: the failed-save notice names that locale\'s Use {{name}} button', (code) => {
      const g = gate(code)
      expect(g.could_not_save_agent_choice).toContain(g.use_agent)
      expect(g.use_agent).toContain('{{name}}')
    })

    it.each(shipped)('%s: the failed switch-back notice names that locale\'s Use Kiro CLI instead button', (code) => {
      const g = gate(code)
      expect(g.could_not_switch_to_kiro).toContain(g.use_kiro_cli_instead)
    })

    it.each(shipped)('%s: the failed re-check notice names that locale\'s Check again button', (code) => {
      const g = gate(code)
      expect(g.agent_recheck_failed).toContain(g.check_again)
    })

    it.each(shipped)('%s: the two sandbox pills name different causes, and neither is the host screen\'s title', (code) => {
      // Same warn colour, opposite remedies: only the words tell them apart.
      const g = gate(code)
      expect(g.agent_host_cannot_sandbox).toBeTruthy()
      expect(g.agent_sandbox_turned_off).toBeTruthy()
      expect(g.agent_host_cannot_sandbox).not.toBe(g.agent_sandbox_turned_off)
      expect(g.agent_host_cannot_sandbox).not.toBe(g.sandbox_unavailable)
    })

    it.each(shipped)('%s: the update helper keeps the command slot in code tags', (code) => {
      // The command is interpolated from the gateway, never translated.
      expect(gate(code).update_kiro_cli_runs_on_host).toMatch(/<0>\{\{command\}\}<\/0>/)
    })
  })
})

/**
 * The shared probe verdict, rendered (#14517). The same states as
 * acpBackend.eligibility.test.ts and Settings > Agent Harness's block, so a surface
 * that stopped reading the shared verdict fails here. First-run setup is the
 * STRICT reader (`acpProbeConfirmsUse`): finishing setup is durable, so a failed
 * check disables Use here where Settings leaves it live.
 */
describe('KiroPrerequisiteGate shared probe eligibility', () => {
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
  })

  it.each([
    ['installed', {}, true],
    ['missing', { installed: 'missing', missing_components: ['claude-agent-acp'] }, false],
    ['restart required', { restart_required: true }, false],
    ['check failed', { installed: 'unknown' }, false],
  ] as const)('%s: Use enabled = %s', async (_label, over, enabled) => {
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ ...over })] })
    render()
    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const use = await screen.findByRole('button', { name: /Use Claude Code/ })
    if (enabled) expect(use).toBeEnabled()
    else expect(use).toBeDisabled()
  })

  it('unselectable: not offered at all', async () => {
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe({ selectable: false }),
        probe({ id: 'codex', policy_id: 'codex' }),
      ],
    })
    render()
    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    expect(await screen.findByRole('radio', { name: 'OpenAI Codex' })).toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: 'Claude Code' })).toBeNull()
  })
})
