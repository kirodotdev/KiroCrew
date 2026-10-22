/**
 * KiroPrerequisiteGate: choosing an agent -- the installers, switching to an
 * installed harness and back to Kiro CLI, scoped re-checks, the sandbox and Kiro
 * CLI version checks that decide whether a choice can proceed (sandbox-blocked
 * agents, ACP support, the Update command), whether an already-configured agent
 * is admitted, and persisting the first-run marker. The Kiro CLI checks are in
 * KiroPrerequisiteGate.test.tsx.
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KiroPrerequisiteGate, {
  acpBackendsRefetchInterval,
} from '../components/KiroPrerequisiteGate'
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
import { probe, status } from './KiroPrerequisiteGateTestUtils'

describe('KiroPrerequisiteGate agent choice', () => {
  const codeBlock = (text: string) =>
    screen.queryByText((_, el) => el?.tagName === 'CODE' && el.textContent === text)
  const CURL = 'curl -fsSL https://cli.kiro.dev/install | bash'
  const IRM = "irm 'https://cli.kiro.dev/install.ps1' | iex"
  const render = () => renderWithProviders(
    <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
  )
  // The gate renders the app while the config/probe reads are in flight, so a
  // "dashboard is shown" assertion only means something once they have landed.
  const settle = async () => {
    await waitFor(() => expect(api.acpBackends).toHaveBeenCalled())
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    await act(() => new Promise(resolve => setTimeout(resolve, 20)))
  }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [] })
  })

  it('shows the one-line Kiro CLI installer for a Linux or macOS host only', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Linux' }))
    render()

    await screen.findByText(/Run this on the Linux gateway host to install it/)
    expect(codeBlock(CURL)).toBeInTheDocument()
    expect(codeBlock(IRM)).not.toBeInTheDocument()
    // Shown to copy, never run: there is still no install button.
    expect(screen.queryByRole('button', { name: /Install Kiro CLI/ })).not.toBeInTheDocument()
    // A host that has unzip gets no prerequisite line, and the installer line
    // is not phrased as a second step.
    expect(screen.queryByTestId('kiro-gate-unzip-missing')).not.toBeInTheDocument()
    expect(screen.queryByText(/^Then run this/)).not.toBeInTheDocument()
  })

  it('names the unzip the Linux installer needs, above its command, when the host lacks it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Linux', unzip_missing: true }))
    render()

    const notice = await screen.findByTestId('kiro-gate-unzip-missing')
    expect(notice).toHaveTextContent(/First, install unzip\. The installer needs it/)
    expect(notice).toHaveTextContent(/or your system's package manager/)
    // The installer line reads as the second step.
    expect(screen.getByText('Then run this on the Linux gateway host to install Kiro CLI.')).toBeInTheDocument()
    expect(screen.queryByText(/^Run this on the Linux gateway host/)).not.toBeInTheDocument()
    expect(within(notice).getByText('Debian or Ubuntu')).toBeInTheDocument()
    expect(codeBlock('sudo apt install unzip')).toBeInTheDocument()
    expect(within(notice).getByText('Fedora or RHEL')).toBeInTheDocument()
    expect(codeBlock('sudo dnf install unzip')).toBeInTheDocument()
    // Read before the command it unblocks.
    const curl = codeBlock(CURL)
    expect(curl).toBeInTheDocument()
    expect(notice.compareDocumentPosition(curl as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows no unzip line on a non-Linux host or once Kiro CLI is installed', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'macOS', unzip_missing: true }))
    const first = render()
    await screen.findByText(/Run this on the macOS gateway host/)
    expect(screen.queryByTestId('kiro-gate-unzip-missing')).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true, unzip_missing: true }))
    render()
    await screen.findByRole('heading', { name: 'Get Kiro CLI' })
    expect(screen.queryByTestId('kiro-gate-unzip-missing')).not.toBeInTheDocument()
  })

  it('has no separate sign-in step: the Kiro CLI card carries sign-in once installed', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    const first = render()
    await screen.findByText(/Run this on the Linux gateway host/)
    expect(screen.queryByRole('heading', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))
    render()
    const card = (await screen.findByRole('heading', { name: 'Get Kiro CLI' })).closest('div.card-glow')
    expect(card).not.toBeNull()
    expect(screen.queryByRole('heading', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
    expect(within(card as HTMLElement).getByText('kiro-cli login')).toBeInTheDocument()
    expect(within(card as HTMLElement).queryByText(/cli\.kiro\.dev\/install/)).not.toBeInTheDocument()
    // The installed state leads the card, and is said once: not repeated in
    // the footer below the other-agents section.
    expect(within(card as HTMLElement).getByText(/Kiro CLI is installed/)).toBeInTheDocument()
    expect(screen.getAllByText(/Kiro CLI is installed/)).toHaveLength(1)
  })

  it('shows the PowerShell installer for a Windows host only', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Windows' }))
    render()

    await screen.findByText(/Run this on the Windows gateway host/)
    expect(codeBlock(IRM)).toBeInTheDocument()
    expect(codeBlock(CURL)).not.toBeInTheDocument()
  })

  it('shows both installers, labelled, when the platform is unknown', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'freebsd' }))
    render()

    await screen.findByText(/Run the command for the gateway host's platform/)
    expect(codeBlock(CURL)).toBeInTheDocument()
    expect(codeBlock(IRM)).toBeInTheDocument()
    // An unlisted platform (FreeBSD, an unnamed distro) still maps to one of
    // the two commands: the Unix label says it covers more than the two names.
    expect(screen.getByText('macOS and Linux (and other Unix-like systems)')).toBeInTheDocument()
    expect(screen.getByText('Windows (PowerShell)')).toBeInTheDocument()
  })

  it('keeps other agents collapsed behind a disclosure while Kiro CLI is the default', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    const toggle = await screen.findByRole('button', { name: 'Use other coding agents' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByRole('radio', { name: /Claude Code/ })).toBeChecked()
  })

  it.each([
    ['not installed', false],
    ['installed but signed out', true],
  ])('does not call Kiro CLI required when it is %s', async (_state, installed) => {
    // Another coding agent can finish setup instead of Kiro CLI, so the card
    // reports Kiro CLI's state and carries no step badge claiming it is needed.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed }))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    const card = (await screen.findByRole('heading', { name: 'Get Kiro CLI' })).closest('div.card-glow') as HTMLElement
    // A word match, not the exact label: a badge rendered with its catalog key
    // missing shows `components.kiroPrerequisiteGate.required`, and is caught too.
    expect(within(card).queryAllByText(/\brequired\b/i)).toEqual([])
    expect(screen.getByRole('button', { name: 'Use other coding agents' })).toBeInTheDocument()
  })

  it('offers only backends explicitly eligible for independent setup', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe({ id: 'kiro', policy_id: 'kiro', independent_setup: false }),
        probe({ id: 'kas', policy_id: 'kas', independent_setup: false }),
        probe({ id: 'codex', policy_id: 'codex' }),
        probe({ id: 'goose', policy_id: 'goose', selectable: false }),
        probe({ id: 'future', policy_id: 'future-harness', independent_setup: false }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const radios = await screen.findAllByRole('radio')
    expect(radios.map(r => r.closest('label')?.textContent)).toEqual([
      // Named exactly as Settings → Agent names it: a harness that panel has no
      // translated name for renders under the server's policy id here too, so
      // one agent is never called two things on two screens.
      expect.stringContaining('codex'),
    ])
    expect(screen.queryByText('future-harness')).not.toBeInTheDocument()
  })

  it('switches to an installed agent and opens the dashboard', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      return {}
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))

    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps a failed switch in the row of the agent it was attempted for, naming the save as what failed', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'codex', policy_id: 'codex' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    const notice = await screen.findByTestId('other-agent-switch-error')
    // The save failed; the agent did not. The row still says the agent is ready,
    // and a notice that blamed the agent would leave the reader with two
    // contradicting lines to choose between.
    expect(notice).toHaveTextContent('Could not save the agent choice. Press Use Claude Code to try again.')
    expect(notice).not.toHaveTextContent('Could not switch to Claude Code')
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    // Inside the row, beside the Use button that triggered it, not at the
    // foot of the list under a different agent's line.
    expect(within(detail).getByTestId('other-agent-switch-error')).toBe(notice)
    expect(within(detail).getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(within(notice).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

    // Another agent's row does not inherit a failure it never had.
    fireEvent.click(screen.getByRole('radio', { name: 'codex' }))
    expect(screen.getByRole('radio', { name: 'codex' })).toBeChecked()
    expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument()

    // Back on the attempted agent, the notice is still there, and its Use
    // button is the retry.
    fireEvent.click(screen.getByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByTestId('other-agent-switch-error')).toHaveTextContent('Could not save the agent choice.')
    vi.mocked(api.patchConfig).mockResolvedValue({})
    fireEvent.click(screen.getByRole('button', { name: /Use Claude Code/ }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledTimes(2))
    expect(api.patchConfig).toHaveBeenLastCalledWith('agent.acp_backend', 'claude')
    await waitFor(() => expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument())
  })

  it('shows a failed switch back to Kiro CLI beside the button that attempted it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(500, 'Config write failed'))
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    fireEvent.click(fallback)
    const notice = await screen.findByTestId('use-kiro-instead-error')
    expect(notice).toHaveTextContent('Could not switch back to Kiro CLI. Press Use Kiro CLI instead to try again.')
    expect(fallback.parentElement).toBe(notice.parentElement)
    // The agent rows carry no notice for a switch they did not attempt.
    expect(screen.queryByTestId('other-agent-switch-error')).not.toBeInTheDocument()
  })

  it('explains that the agent was saved when only setup-marker persistence failed and nothing else moved', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    // The config read keeps answering Kiro, so the gate cannot lift on the
    // saved choice; the server's own partial-success sentence stays on screen.
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(
      503,
      'Agent selection was saved, but setup completion could not be recorded. Try again.',
      JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    const notice = await screen.findByTestId('other-agent-switch-error')
    expect(notice).toHaveTextContent('Agent selection was saved, but setup completion could not be recorded. Try again.')
    expect(notice).not.toHaveTextContent('Could not save the agent choice')
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
  })

  it('opens the dashboard when the agent choice saved but the setup marker did not', async () => {
    // A 503 whose body says config_saved: the config on disk names the new
    // agent, so re-reading it is what lets a saved, usable agent through. Left
    // on the failure path alone, the gate would keep showing "Set up Kiro" and
    // every retry would reproduce the same 503.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockImplementation(async () => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      throw new ApiError(
        503,
        'Agent selection was saved, but setup completion could not be recorded. Try again.',
        JSON.stringify({ code: 'setup_marker_write_failed', config_saved: true }),
      )
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))

    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'claude'))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(2)
    expect(api.patchConfig).toHaveBeenCalledTimes(1)
  })

  it('does not re-read the config after a failure that saved nothing', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    vi.mocked(api.patchConfig).mockRejectedValue(new ApiError(
      503,
      'Setup completion could not be recorded.',
      JSON.stringify({ code: 'setup_marker_write_failed', config_saved: false }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: /Use Claude Code/ }))
    await screen.findByTestId('other-agent-switch-error')
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('shows a missing agent\'s install command and will not switch to it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({
        installed: 'missing',
        missing_components: ['claude-agent-acp'],
        install_command: 'npm install -g @zed-industries/claude-agent-acp',
      })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    expect(await screen.findByText('Install Claude Code on the gateway host.')).toBeInTheDocument()
    expect(codeBlock('npm install -g @zed-industries/claude-agent-acp')).toBeInTheDocument()
    // The command IS the answer. A "Missing: claude-agent-acp" line beside it
    // names a second thing, and a reader second-guesses which one to install.
    expect(screen.queryByText(/^Missing:/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeDisabled()
  })

  it('names the missing components only when there is no install command to show', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', missing_components: ['claude-agent-acp'] })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    expect(await screen.findByText('Missing: claude-agent-acp')).toBeInTheDocument()
  })

  it('opens the picked agent\'s detail directly under its own row', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe(),
        probe({ id: 'codex', policy_id: 'codex', installed: 'missing', install_command: 'npm install -g @zed-industries/codex-acp' }),
        probe({ id: 'goose', policy_id: 'goose', installed: 'missing' }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    // The row that owns the detail is the one whose radio sits in the same
    // outline -- not whichever row happens to be last in the list.
    const owner = () => {
      const details = screen.getAllByTestId('other-agent-detail')
      expect(details).toHaveLength(1)
      return within(details[0].parentElement as HTMLElement).getByRole('radio')
    }
    expect(owner()).toHaveAccessibleName('Claude Code')

    fireEvent.click(screen.getByRole('radio', { name: 'codex' }))
    expect(owner()).toHaveAccessibleName('codex')
    expect(owner()).toBeChecked()
    expect(codeBlock('npm install -g @zed-industries/codex-acp')).toBeInTheDocument()
  })

  it('scopes the Kiro card\'s re-check to Kiro CLI, beside each agent\'s own Check again', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    render()

    const kiroCard = (await screen.findByRole('heading', { name: 'Get Kiro CLI' })).closest('div.card-glow') as HTMLElement
    // The footer line read as Kiro-only once other agents were on screen.
    expect(screen.queryByText(/is required on the Linux gateway host/)).not.toBeInTheDocument()
    expect(within(kiroCard).getByRole('button', { name: 'Check again for Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use other coding agents' }))
    const detail = await screen.findByTestId('other-agent-detail')
    // Two re-check buttons can be on screen at once and they check different
    // things, so no two of them carry the same label: the Kiro card's names
    // Kiro CLI, the agent's sits inside the row that names the agent.
    expect(screen.getAllByRole('button', { name: /Check again/ })).toEqual([
      within(kiroCard).getByRole('button', { name: 'Check again for Kiro CLI' }),
      within(detail).getByRole('button', { name: 'Check again' }),
    ])
  })

  it('offers no Check again on an agent that is installed and ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'pi', policy_id: 'pi', installed: 'unknown' })],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const ready = await screen.findByTestId('other-agent-detail')
    expect(within(ready).getByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(within(ready).getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(within(ready).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()

    // A failed check is still something a re-check can settle, so it keeps one.
    fireEvent.click(screen.getByRole('radio', { name: 'pi' }))
    const unverified = screen.getByTestId('other-agent-detail')
    const recheck = within(unverified).getByRole('button', { name: 'Check again' })
    expect(recheck).toBeEnabled()
    expect(screen.getByText('Check failed')).toBeInTheDocument()
    expect(within(unverified).getByText(
      'Kiro Crew could not confirm that pi is installed. Press Check again to verify the installation before using this agent. Setup stays open until your chosen agent is ready. You can switch agents here, or later in Settings > Agent Harness.',
    )).toBeInTheDocument()
    expect(unverified).not.toHaveTextContent('This host cannot provide that sandbox')
    const useAgent = within(unverified).getByRole('button', { name: /Use pi/ })
    expect(useAgent).toBeDisabled()
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe({ id: 'pi', policy_id: 'pi' }) })
    fireEvent.click(recheck)
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('pi'))
    expect(await screen.findByText('pi is installed and ready to use.')).toBeInTheDocument()
    expect(useAgent).toBeEnabled()
    expect(within(unverified).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
    // Checking a row is not choosing it: only Use may save the new agent.
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('names no single agent as the engine in the intro', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    render()

    expect(await screen.findByText(/Get a coding agent running on the/)).toBeInTheDocument()
    expect(screen.getByText('Linux gateway host').tagName).toBe('STRONG')
    expect(screen.queryByText(/agent engine/)).not.toBeInTheDocument()
  })

  it('gives a failed check and a not-yet-active install two different badges', async () => {
    // UX round 4: "Not verified" beside "Found, not active yet" read as the same
    // "it might be there" shrug, so a user could not tell whether their agent
    // needed a re-check or was confirmed and merely not picked up yet. One badge
    // says the CHECK failed; the other says the install is confirmed and not
    // active. Neither is an instruction: the detail line carries the remedy.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [
        probe({ installed: 'unknown' }),
        probe({ id: 'pi', policy_id: 'pi', installed: 'installed', restart_required: true }),
      ],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    const failed = await screen.findByText('Check failed')
    const inactive = screen.getByText('Installed, not active yet')
    expect(failed).toBeInTheDocument()
    expect(inactive).toBeInTheDocument()
    expect(failed.textContent).not.toBe(inactive.textContent)
    // Neither badge borrows the other's key word: "installed" belongs to the
    // confirmed row only, "check" to the failed row only.
    expect(failed.textContent).not.toMatch(/installed/i)
    expect(inactive.textContent).not.toMatch(/check|verif/i)
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
    expect(screen.queryByText('Found, not active yet')).not.toBeInTheDocument()
  })

  it.each([
    ['', 'empty'],
    ['gateway', 'the non-owner redaction'],
    ['Unknown', 'the gateway\u2019s own unknown label'],
    ['freebsd', 'a raw sys.platform value'],
  ])('names no platform in the intro or eyebrow when the platform is %j (%s)', async (platform) => {
    // UX round 4: `status.platform || 'local'` printed "local gateway host" in
    // the intro while the Kiro card, a few lines down, admitted it did not know
    // the host's OS. An unrecognised platform now selects a platform-free intro
    // and eyebrow rather than a filler word.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    render()

    const intro = await screen.findByText(/Get a coding agent running on the/)
    expect(intro).toHaveTextContent(
      'Get a coding agent running on the gateway host, then the dashboard will open automatically.',
    )
    expect(screen.getByText('gateway host').tagName).toBe('STRONG')
    expect(intro).not.toHaveTextContent(/local|Linux|macOS|Windows|Unknown|freebsd/)
    // The eyebrow "SETUP -> Linux gateway" drops the platform the same way.
    const eyebrow = screen.getByText('Setup').parentElement
    expect(eyebrow).toHaveTextContent('Gateway host')
    expect(eyebrow).not.toHaveTextContent(/local|Linux|macOS|Windows|Unknown|freebsd/)
    expect(screen.queryByText(/local gateway/)).not.toBeInTheDocument()
    // The Kiro card still asks for the platform's command, as before.
    expect(screen.getByText(/Run the command for the gateway host's platform/)).toBeInTheDocument()
  })

  it('still names a recognised platform in the intro and eyebrow', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Linux' }))
    render()

    const intro = await screen.findByText(/Get a coding agent running on the/)
    expect(intro).toHaveTextContent(
      'Get a coding agent running on the Linux gateway host, then the dashboard will open automatically.',
    )
    expect(screen.getByText('Linux gateway host').tagName).toBe('STRONG')
    expect(screen.getByText('Setup').parentElement).toHaveTextContent('Linux gateway')
    expect(screen.queryByText('Gateway host')).not.toBeInTheDocument()
  })

  it('retries a failed agent recheck from its single scoped control', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(500, 'Probe failed'))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('The check for Claude Code failed. Press Check again.')
    expect(within(notice).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(screen.queryByTestId('other-agent-recheck-error')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(api.acpBackendRecheck).toHaveBeenLastCalledWith('claude')
  })

  it('shows a saved recheck with failed setup persistence as a retryable partial success', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(
      503,
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
      JSON.stringify({ code: 'setup_marker_write_failed' }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    const notice = await screen.findByTestId('other-agent-recheck-error')
    expect(notice).toHaveTextContent('Agent check completed, but setup completion could not be recorded. Press Check again.')
    expect(within(screen.getByTestId('other-agent-detail')).getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('applies the fresh row from a saved-but-marker-failed recheck 503 while still showing the message', async () => {
    // The 503 carries the fresh probe row: the re-probe itself succeeded and only
    // the marker write did not. The row is applied exactly as on success (the agent
    // reads installed and ready), and the marker-write message still surfaces in
    // place of the generic recheck-failed line.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ installed: 'missing' })] })
    vi.mocked(api.acpBackendRecheck).mockRejectedValueOnce(new ApiError(
      503,
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
      JSON.stringify({ code: 'setup_marker_write_failed', backend: probe() }),
    ))
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(screen.getByTestId('other-agent-recheck-error')).toHaveTextContent(
      'Agent check completed, but setup completion could not be recorded. Press Check again.',
    )
  })

  it('re-checks one agent through the cache-dropping endpoint and applies the answer', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'installed', restart_required: true })],
    })
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    // The badge is status only, so a reader scanning the list gets one fact per
    // row; the instruction lives in the detail, beside the button it names,
    // which also says WHERE a restart happens if it comes to that, in the same
    // term as every sibling line: the gateway host, not "this machine".
    expect(await screen.findByText('Installed, not active yet')).toBeInTheDocument()
    expect(screen.queryByText('Found, not active yet')).not.toBeInTheDocument()
    expect(screen.queryByText('Installed, press Check again')).not.toBeInTheDocument()
    expect(screen.getByText(
      'Claude Code is installed on the gateway host. Press Check again to pick it up. If this line is still here after that, restart Kiro Crew on that host.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/on this machine/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Restart needed/)).not.toBeInTheDocument()
    const detail = screen.getByTestId('other-agent-detail')
    fireEvent.click(within(detail).getByRole('button', { name: 'Check again' }))

    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
    expect(await screen.findByText('Claude Code is installed and ready to use.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
  })

  it.each(['Linux', 'macOS'] as const)('keeps a configured unverified agent in setup until Check again confirms installation on %s', async (platform) => {
    const unverified = probe({ installed: 'unknown' })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [unverified] })
    render()

    // Wait for the settled setup state, not the initial pending dashboard render.
    expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
    expect(screen.queryByText(/Finish installing it below/)).not.toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBeNull()
    expect(acpBackendsRefetchInterval(status({ platform }), true, unverified)).toBe(30_000)

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: unverified })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled())
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBeNull()

    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    await waitFor(() => expect(localStorage.getItem('kirocrew:kiro-setup-complete')).toBe('1'))
    expect(acpBackendsRefetchInterval(status({ platform }), true, probe())).toBe(false)
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it.each(['Linux', 'macOS'] as const)('opens the dashboard for a configured, installed non-Kiro agent with no Kiro CLI on %s', async (platform) => {
    // An operator using an installed Claude Code with no Kiro CLI must not be
    // held behind checks about an agent they do not run.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('disables a sandbox-blocked agent choice before saving when the host has no sandbox', async () => {
    const sandboxStatus = { sandbox_backend_available: false }
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(sandboxStatus))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' }), probe()],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const useAgent = screen.getByRole('button', { name: /Use codex/ })
    expect(useAgent).toBeDisabled()
    const detail = screen.getByTestId('other-agent-detail')
    // Plain words for what the block means, in place of the bare badge text
    // repeated as a heading: a reader who does not know what a sandbox is here
    // otherwise has a disabled button, a command, and no idea why.
    expect(within(detail).getByText(
      'Kiro Crew runs codex inside a sandbox that keeps your credentials out of its reach. This host cannot provide that sandbox right now, so codex stays unavailable until it can.',
    )).toBeInTheDocument()
    expect(within(detail).getByText(/Run this on the gateway host for a full prerequisite report/)).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Copy command' })).toHaveTextContent('kirocrew doctor')
    // Not a dead end: the row keeps its own Check again, so the one enabled
    // act is not copying a command. The verdict it re-takes lives on the
    // prerequisite status, so that is re-read alongside the harness probe.
    vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe({ id: 'codex', policy_id: 'codex' }) })
    const recheck = within(detail).getByRole('button', { name: 'Check again' })
    expect(recheck).toBeEnabled()
    const statusReads = vi.mocked(api.kiroPrerequisite).mock.calls.length
    fireEvent.click(recheck)
    await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('codex'))
    await waitFor(() => expect(vi.mocked(api.kiroPrerequisite).mock.calls.length).toBeGreaterThan(statusReads))
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toHaveClass('text-warn-text')
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
    expect(detail).not.toHaveTextContent('ready to use')
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it('names the sandbox SETTING, not the host, when the host sandbox works but is turned off', async () => {
    // `sandbox_facts` reports `sandbox_backend_available: true` (the host can
    // build a sandbox) AND lists every enforced harness in
    // `sandbox_blocked_backends` when `agent.sandbox` is `off`. "This host
    // cannot provide that sandbox" is then false, and Check again / doctor
    // re-measure a host that is fine — the remedy is the setting alone.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: true,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' }), probe()],
    })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const useAgent = screen.getByRole('button', { name: /Use codex/ })
    expect(useAgent).toBeDisabled()
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText(
      "Kiro Crew runs codex inside a sandbox that keeps your credentials out of its reach, but the sandbox is turned off in Kiro Crew's settings (agent.sandbox). Turn it back on to use codex.",
    )).toBeInTheDocument()
    // Neither remedy for a host verdict is offered: nothing they measure is the cause.
    expect(within(detail).queryByText(/This host cannot provide that sandbox/)).not.toBeInTheDocument()
    expect(within(detail).queryByText(/Run this on the gateway host/)).not.toBeInTheDocument()
    expect(within(detail).queryByText('kirocrew doctor')).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
    // The badge says what the detail says: off, not unavailable.
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText('Sandbox turned off')).toHaveClass('text-warn-text')
    expect(within(selectedRow!).queryByText("Host can't sandbox")).not.toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
    fireEvent.click(useAgent)
    expect(api.patchConfig).not.toHaveBeenCalled()

    // The setting blocks only the enforced harnesses the gateway listed: an
    // unlisted one is still installed, ready, and offered with no re-check.
    fireEvent.click(screen.getByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    expect(screen.getByTestId('other-agent-detail')).toHaveTextContent('ready to use')
    expect(within(screen.getByTestId('other-agent-detail')).queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
  })

  it('keeps the host verdict when the host has no sandbox, whatever the blocked list says', async () => {
    // Both flags at once is a host that cannot build a sandbox: the setting is
    // moot there, so the host-verdict copy and its remedies stay.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: false,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'codex', policy_id: 'codex' })] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    const detail = screen.getByTestId('other-agent-detail')
    expect(within(detail).getByText(/This host cannot provide that sandbox/)).toBeInTheDocument()
    expect(within(detail).queryByText(/turned off in Kiro Crew's settings/)).not.toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Check again' })).toBeEnabled()
    const selectedRow = screen.getByRole('radio', { name: 'codex' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Sandbox turned off')).not.toBeInTheDocument()
  })

  it('lets a non-enforced agent start on a backend-less host that permits unsandboxed exec', async () => {
    // Native Windows: no OS sandbox backend, but unsandboxed exec is the
    // platform default. Claude Code needs no Crew OS credential mask, so it is
    // usable and finishes setup even though `sandbox_backend_available` is false.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: false,
      unsandboxed_exec_permitted: true,
    }))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeEnabled()
    const detail = screen.getByTestId('other-agent-detail')
    expect(detail).toHaveTextContent('ready to use')
    expect(within(detail).queryByText(/This host cannot provide that sandbox/)).not.toBeInTheDocument()
    const selectedRow = screen.getByRole('radio', { name: 'Claude Code' }).closest('label')
    expect(within(selectedRow!).getByText('Installed')).toBeInTheDocument()
    expect(within(selectedRow!).queryByText("Host can't sandbox")).not.toBeInTheDocument()
  })

  it('fails closed on an older gateway that omits the unsandboxed-exec field', async () => {
    // No `unsandboxed_exec_permitted` at all (older gateway): the client treats
    // its absence as "not permitted", so a backend-less host still blocks the
    // agent rather than admitting it on a field it never received.
    const older = status({ sandbox_backend_available: false })
    delete (older as { unsandboxed_exec_permitted?: boolean }).unsandboxed_exec_permitted
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(older)
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    fireEvent.click(await screen.findByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'Claude Code' }))
    expect(screen.getByRole('button', { name: /Use Claude Code/ })).toBeDisabled()
    const selectedRow = screen.getByRole('radio', { name: 'Claude Code' }).closest('label')
    expect(within(selectedRow!).getByText("Host can't sandbox")).toBeInTheDocument()
    expect(within(selectedRow!).queryByText('Installed')).not.toBeInTheDocument()
  })

  it.each([
    { sandbox_backend_available: false },
    { sandbox_backend_available: true, sandbox_blocked_backends: ['codex'] },
  ])('offers Kiro fallback for a configured sandbox-blocked agent ($sandbox_backend_available, $sandbox_blocked_backends)', async (sandboxStatus) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status(sandboxStatus))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' })],
    })
    vi.mocked(api.patchConfig).mockResolvedValue({})
    render()

    const fallback = await screen.findByRole('button', { name: 'Use Kiro CLI instead' })
    expect(fallback).toBeEnabled()
    expect(screen.getByText(/is set to use codex, which isn't ready/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Use codex/ })).toBeDisabled()
    fireEvent.click(fallback)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kiro'))
  })

  it.each([false, undefined])('keeps configured Codex in setup without confirmed host sandbox capability (%s)', async (available) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      sandbox_backend_available: available,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'codex', policy_id: 'codex' })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(acpBackendsRefetchInterval(status({ sandbox_backend_available: available }), true, probe())).toBe(30_000)
  })

  it.each(['codex', 'claude'] as const)('honors the effective-tier sandbox refusal list for %s', async (backend) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      sandbox_backend_available: true,
      sandbox_blocked_backends: ['codex'],
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: backend } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: backend, policy_id: backend })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded') !== null).toBe(backend === 'claude')
  })

  it('preserves ready Kiro CLI delegation on Windows without a Crew sandbox backend', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      installed: true,
      authenticated: true,
      ready: true,
      sandbox_backend_available: false,
    }))
    render()

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps the sandbox remedy visible for an installed foreign harness', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      sandbox_unavailable: true,
      sandbox_failure_kind: 'no_backend',
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Kiro CLI is installed but could not be verified' })).toBeInTheDocument()
  })

  it('uses KAS when the config has no manual harness choice', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      acp_supported: false,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: {} })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'kas', policy_id: 'kas', independent_setup: false })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Your Kiro CLI is out of date' })).toBeInTheDocument()
  })

  it('never tells a signed-out user their too-old Kiro CLI is signed in', async () => {
    // The ACP probe runs for a signed-out CLI too, so this card is reachable
    // with `authenticated: false`. Its copy must then hold for both readers:
    // installed and too old, with no claim about sign-in either way.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      acp_supported: false,
    }))
    render()

    expect(await screen.findByRole('heading', { name: 'Your Kiro CLI is out of date' })).toBeInTheDocument()
    expect(screen.getByText(
      'Kiro CLI is installed, but this version predates the acp command Kiro Crew launches every session through. Update it and this page continues on its own.',
    )).toBeInTheDocument()
    expect(screen.queryByText(/signed in/i)).not.toBeInTheDocument()
  })

  it('says under the Update button that it runs the command on the host, with the command verbatim', async () => {
    // The button is not a copy action: the gateway's update_cli spawns the
    // CLI's own `update` on the host. The line under it says so, and names
    // the command in code type from the status, never from a catalog.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      acp_supported: false,
      update_command: 'kiro-cli update',
    }))
    render()

    const update = await screen.findByRole('button', { name: 'Update Kiro CLI' })
    const line = screen.getByTestId('kiro-gate-update-runs-on-host')
    expect(line).toHaveTextContent('Runs kiro-cli update on the gateway host for you.')
    const code = within(line).getByText('kiro-cli update')
    expect(code.tagName).toBe('CODE')
    // Directly under the button, in the pinned footer beside it.
    expect(update.nextElementSibling).toBe(line)
    expect(within(screen.getByTestId('gate-footer')).getByTestId('kiro-gate-update-runs-on-host')).toBe(line)
  })

  it('makes no run-it-for-you claim for the desktop app\'s bundled Kiro CLI, which the gateway will not self-update', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      acp_supported: false,
      bundled_cli: true,
    }))
    render()

    expect(await screen.findByRole('button', { name: 'Update Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-gate-update-runs-on-host')).not.toBeInTheDocument()
  })

  it('does not require Kiro CLI ACP support for an independent harness', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ acp_supported: false }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('does not bypass setup for an unclassified future backend', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'future' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ id: 'future', policy_id: 'future', independent_setup: false })],
    })
    render()

    await settle()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('does not let Kiro CLI\'s own screens hold a usable non-Kiro agent', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      initial_setup_complete: true,
      acp_supported: false,
    }))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'codex' } })
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe({ id: 'codex', policy_id: 'codex' })] })
    render()

    await settle()
    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('Kiro CLI update needed')).not.toBeInTheDocument()
  })

  it('keeps setup up for a configured agent that is not installed, with a way back to Kiro', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe({ installed: 'missing', install_command: 'npm install -g x' })],
    })
    vi.mocked(api.patchConfig).mockResolvedValue({})
    render()

    expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use other coding agents' }))
      .toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Use Kiro CLI instead' }))
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.acp_backend', 'kiro'))
  })

  it.each([false, true])('surfaces a config read failure without bypassing the Kiro checks (installed: %s)', async (installed) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed }))
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new ApiError(500, 'Config read failed'))
    vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
    render()

    const notice = await screen.findByTestId('kiro-gate-config-error')
    expect(notice).toHaveAttribute('role', 'alert')
    // First-run words: every sibling line on this screen says "coding agent",
    // and "agent backend" is the Developer tab's term for the same thing.
    expect(notice).toHaveTextContent('Could not check which coding agent is set up.')
    expect(notice).not.toHaveTextContent(/backend/i)
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    // Visible without expanding the alternative-agent picker. A usable probe
    // alone cannot bypass Kiro checks when the configured agent is unknown.
    expect(screen.getByRole('button', { name: 'Use other coding agents' }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    if (installed) {
      expect(codeBlock(CURL)).not.toBeInTheDocument()
      expect(screen.getByText('kiro-cli login')).toBeInTheDocument()
    } else {
      expect(codeBlock(CURL)).toBeInTheDocument()
      expect(screen.queryByText('kiro-cli login')).not.toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: installed ? 'Check sign-in again' : 'Check again for Kiro CLI' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByTestId('kiro-gate-config-error')).not.toBeInTheDocument())
    expect(api.kirocrewConfig).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('preserves the unsaved agent choice while retrying a config read failure', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockRejectedValue(new ApiError(500, 'Config read failed'))
    vi.mocked(api.acpBackends).mockResolvedValue({
      backends: [probe(), probe({ id: 'codex', policy_id: 'codex' })],
    })
    render()

    const notice = await screen.findByTestId('kiro-gate-config-error')
    fireEvent.click(screen.getByRole('button', { name: 'Use other coding agents' }))
    fireEvent.click(await screen.findByRole('radio', { name: 'codex' }))
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()

    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByTestId('kiro-gate-config-error')).not.toBeInTheDocument())
    expect(screen.getByRole('radio', { name: 'codex' })).toBeChecked()
    expect(api.patchConfig).not.toHaveBeenCalled()
  })

  it.each(['kiro', 'claude'])('surfaces a harness probe failure and keeps the Kiro checks (configured: %s)', async (configured) => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: configured } })
    vi.mocked(api.acpBackends).mockRejectedValue(new ApiError(403, 'forbidden'))
    render()

    expect(await screen.findByText('Set up Kiro')).toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: 'Use other coding agents' })
    if (configured === 'kiro') fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const notice = await screen.findByTestId('other-agents-probe-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Could not check the other agents on this host. You can choose one later in Settings > Agent Harness.')
    expect(within(notice).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/which isn't ready on this host/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use Kiro CLI instead' })).not.toBeInTheDocument()
    expect(api.patchConfig).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: 'Get Kiro CLI' })).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('reads neither the config nor the probe for a ready Kiro install', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))
    render()

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
    expect(api.acpBackends).not.toHaveBeenCalled()
  })

  describe('persisting the first-run marker when the poll admits a configured agent', () => {
    // The poll is read-only: it can admit THIS browser but never writes the
    // durable `initial_setup_complete` marker. Without a write, every later
    // browser and every non-owner stays on first-run setup for good.
    const BACKENDS_KEY = ['acpBackends']
    const missing = () => probe({ installed: 'missing', install_command: 'npm install -g x' })
    // "Dashboard loaded" is painted while the probe is still in flight, so a
    // "did not fire" assertion means nothing until the probe has LANDED and
    // the effect has had a turn on it.
    const probeLanded = async (queryClient: QueryClient) => {
      await waitFor(() => expect(queryClient.getQueryState(BACKENDS_KEY)?.status).toBe('success'))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
    }

    beforeEach(() => {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
      vi.mocked(api.acpBackendRecheck).mockResolvedValue({ backend: probe() })
    })

    it('fires the recheck POST exactly once for the configured agent when the poll flips to ready', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [missing()] })
      const { queryClient } = render()

      // Not installed yet: setup stays up and nothing is written.
      expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()

      // The next poll sees the install. The gate admits AND persists.
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)

      // Re-polls and re-renders with the same verdict do not repeat it.
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] }))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('fires once when the configured agent is already installed on first load', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledWith('claude'))
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
    })

    it('fires again only on a new flip: the agent going away and coming back', async () => {
      // Not a repeat of the same verdict — the verdict dropped to "cannot
      // start" in between, and the marker may still be unwritten (the first
      // POST could have refused to write it for the same reason).
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))

      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [missing()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText(/is set to use Claude Code, which isn't ready/)).toBeInTheDocument()
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)

      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
    })

    it('does not fire when the marker is already written', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ initial_setup_complete: true }))
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await probeLanded(queryClient)
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()
    })

    it('still admits this browser when the POST fails, and does not retry on its own', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 20)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('surfaces a failed marker write over the dashboard without blocking it', async () => {
      // The dashboard is shown (this browser is admitted), but the silent write
      // failure would leave the operator unaware that other browsers/non-owners
      // stay stuck on setup. It must be visible AND non-blocking.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(
        503,
        'Agent selection was saved, but setup completion could not be recorded. Try again.',
        JSON.stringify({ code: 'setup_marker_write_failed' }),
      ))
      render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      const notice = await screen.findByTestId('kiro-gate-marker-write-error')
      expect(notice).toHaveAttribute('role', 'alert')
      // The server's own sentence is carried verbatim beneath the plain title.
      expect(notice).toHaveTextContent("Setup completion wasn't recorded")
      expect(notice).toHaveTextContent('setup completion could not be recorded')
      // The overlay wrapper is click-through so the app beneath stays operable;
      // only the notice card itself captures pointer events.
      const region = screen.getByTestId('kiro-gate-marker-write-error-region')
      expect(region.className).toContain('pointer-events-none')
      expect(notice.className).toContain('pointer-events-auto')
    })

    it('retries the marker write from the notice and clears it on success', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck)
        .mockRejectedValueOnce(new ApiError(503, 'Marker write failed'))
        .mockResolvedValue({ backend: probe() })
      render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      // Fired once automatically and failed; it must NOT retry on its own.
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      const notice = await screen.findByTestId('kiro-gate-marker-write-error')

      // The notice's own Retry re-POSTs for the configured backend...
      fireEvent.click(within(notice).getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(2))
      expect(api.acpBackendRecheck).toHaveBeenLastCalledWith('claude')
      // ...and a success clears the notice, with the dashboard still up.
      await waitFor(() => expect(screen.queryByTestId('kiro-gate-marker-write-error')).toBeNull())
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    })

    it('does not re-fire the marker write on its own after it fails', async () => {
      // The effect writes one POST per flip: a failed write changes no
      // dependency, so there is no spin. Only the Retry button re-fires it.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      const { queryClient } = render()

      await screen.findByTestId('kiro-gate-marker-write-error')
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      // Re-polls and re-renders with the same failed verdict do not repeat it.
      await act(() => queryClient.invalidateQueries({ queryKey: BACKENDS_KEY }))
      await act(() => queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] }))
      // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
      await act(() => new Promise(resolve => setTimeout(resolve, 30)))
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
      // Still visible, still one POST.
      expect(screen.getByTestId('kiro-gate-marker-write-error')).toBeInTheDocument()
    })

    it('dismisses the marker-write notice without another POST', async () => {
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status())
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      vi.mocked(api.acpBackendRecheck).mockRejectedValue(new ApiError(503, 'Marker write failed'))
      render()

      const notice = await screen.findByTestId('kiro-gate-marker-write-error')
      await waitFor(() => expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1))
      fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
      await waitFor(() => expect(screen.queryByTestId('kiro-gate-marker-write-error')).toBeNull())
      // Dismissing is not a retry: the dashboard stays up and no POST is sent.
      expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
      expect(api.acpBackendRecheck).toHaveBeenCalledTimes(1)
    })

    it('does not fire for a viewer the status says cannot run setup', async () => {
      // A non-owner never gets a probe (the GET answers 403), but the status
      // bit is the client's own word for it and is honoured on its own too.
      vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ setup_allowed: false }))
      vi.mocked(api.acpBackends).mockResolvedValue({ backends: [probe()] })
      const { queryClient } = render()

      expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
      await probeLanded(queryClient)
      expect(api.acpBackendRecheck).not.toHaveBeenCalled()
    })
  })
})
