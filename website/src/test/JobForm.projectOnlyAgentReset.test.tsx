import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    // Rendering JobForm reaches both: `useAgents` (in a child) calls
    // `agentCatalog`, and the chat-folder picker calls `chatFolders`. Neither
    // is this file's subject; unstubbed they throw or render an unrelated
    // error notice into the form under test.
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

function messageJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *', ...overrides,
  } as CronJob
}

/**
 * Selecting a project-scoped agent (only offered because a working
 * directory was set), then clearing that project directory, previously left
 * the picker's `agent` state untouched: `effectiveAgents` correctly fell
 * back to the global roster once `projectAgents` cleared, but the SELECTED
 * name was never reset, so save sent that now-unresolvable name with an
 * empty `project_path`. Kiro cannot resolve a project agent without its
 * project scope and silently falls back to the default agent's prompt,
 * tools, and permissions -- with no error surfaced anywhere to say so.
 * Fixed by clearing the selection back to the global default whenever it is
 * not a name the global roster itself recognizes.
 */
describe('JobForm clears a project-only agent selection when the project directory is cleared', () => {
  it('resets the agent picker to the default once the project agent is no longer resolvable', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[{
          name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
          description: 'built-in', source: 'kirocrew',
        }]}
        defaultAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())

    // Select the project-only agent via the AgentSelector's listbox. Wait
    // for the roster to have propagated through useQuery (an extra render
    // hop past the raw fetch call above) before clicking the option -- a
    // bare `getByRole` can race that hop and see an empty listbox.
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    // Now clear the project directory -- the project-only agent must no
    // longer be shown as selected.
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })

    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).toHaveTextContent('default'),
    )
    expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-bot')
    const note = screen.getByTestId('jobform-agent-reset-note')
    expect(note).toHaveTextContent('repo-bot')
    expect(note).toHaveTextContent('project directory was cleared')
    expect(note).not.toHaveTextContent("isn't defined in this project")
  })

  it('leaves a global agent selection untouched when the project directory clears', async () => {
    // A global agent that happens to be selected must survive the folder
    // clearing -- this gate only targets names the global roster does not
    // recognize.
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[
          { name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default', description: 'built-in', source: 'kirocrew' },
          { name: 'ea-dev', kiro_agent: 'ea-dev', workspace: 'ea', memory_store: 'ea', description: 'ea agent', source: 'kirocrew' },
        ]}
        defaultAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(screen.getByRole('option', { name: /ea-dev/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev'))

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev')
  })

  it('keeps a global roster row selected when the global catalog failed and the directory clears', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({
      agents: [{
        name: 'ea-dev', kiro_agent: 'ea-dev', workspace: 'ea', memory_store: 'ea',
        description: 'global agent', source: 'kirocrew', scope: 'global',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[]}
        defaultAgent=""
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /ea-dev/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /ea-dev/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev'))

    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })

    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev'))
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
  })

  it('does not clear an existing job binding when the global roster is empty or still loading', async () => {
    // Opus 4.8 finding: `!projectPath` runs on EVERY mount (every existing
    // job has an empty project_path by default), and `agents.some(...)` on
    // an empty/not-yet-loaded roster is always false for ANY saved agent
    // name -- without the `agents.length > 0` guard, opening ANY existing
    // message job while the roster fetch failed or is still in flight would
    // silently clear its persisted agent binding, and a later save would
    // overwrite it with the default even though the user changed nothing.
    renderWithProviders(
      <JobForm
        job={messageJob({ agent: 'ea-dev' })}
        agents={[]}
        defaultAgent=""
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    // The job's saved agent must still be shown as selected -- not reset to
    // the (also empty) default just because the roster hasn't loaded.
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev')
  })

  it('clears a project agent on a project-only install, where the global roster is legitimately empty', async () => {
    // GPT 5.6 Review F2. The guard that fixed the test above tested the
    // WRONG thing: it asked "is the global roster non-empty?" as a proxy for
    // "has the roster loaded?". Those diverge on an install whose agents are
    // all project-scoped -- the roster IS loaded and IS legitimately empty --
    // so the clear was skipped, the unresolvable name was saved with an empty
    // project_path, and every fire silently ran the default agent instead.
    //
    // The fix decides on positive knowledge: the name came from
    // `projectAgents`, so we KNOW it was the folder's. That also cannot
    // misfire on a crew-bound form (`CrewWakeSection` passes `agents={[]}`)
    // or on an app agent under `~/.kiro/agents/`, neither of which is ever
    // in the folder's roster.
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[]}
        defaultAgent=""
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })

    // The project agent must be gone even though the global roster is empty.
    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-bot'),
    )
  })

  it('keeps the reset notice readable after the debounce settles, not just for 250ms', async () => {
    // The notice appeared and then VANISHED. Clearing the field fires the
    // effect TWICE: once when `projectPath` empties, then again when
    // `debouncedProjectPath` (250ms) settles and `enabled: !!debouncedProjectPath`
    // turns the roster query off, moving `projectAgentsData` to `undefined` --
    // a dependency change. That second run saw the agent the first run had
    // already cleared, so `cleared` computed false and it wrote
    // `setAgentResetFrom('')`, erasing its own acknowledgment.
    //
    // Every assertion in this file still passed, because they all read the
    // notice SYNCHRONOUSLY inside that window. A browser probe showed it
    // present at 200ms and gone at 400ms, so the acknowledgment UX Review
    // asked for was unreadable in practice. This test reads it after the
    // debounce, which is when a human would.
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[{
          name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
          description: 'built-in', source: 'kirocrew',
        }]}
        defaultAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })
    await waitFor(() => expect(screen.getByTestId('jobform-agent-reset-note')).toBeInTheDocument())

    // Past the 250ms debounce, so the query-disabling re-run has happened.
    await new Promise(resolve => setTimeout(resolve, 600))

    const note = screen.getByTestId('jobform-agent-reset-note')
    expect(note).toHaveTextContent('repo-bot')
    expect(note).toHaveTextContent('project directory was cleared')
  })

  it('announces a SECOND clear too, rather than staying silent after the first', async () => {
    // The guard that fixed the test above is armed per empty field, so a bound
    // path has to re-arm it. Without that, the effect would suppress every
    // later reset for the lifetime of the form: the user binds another folder,
    // picks another project agent, clears again -- and that pick would vanish
    // with no acknowledgment at all, which is the exact silent-reset defect the
    // notice exists to prevent.
    vi.mocked(api.kirocrewAgents).mockResolvedValue({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[{
          name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
          description: 'built-in', source: 'kirocrew',
        }]}
        defaultAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    const field = screen.getByLabelText('Project directory')

    for (const round of [1, 2]) {
      fireEvent.change(field, { target: { value: `/Users/you/projects/repo-${round}` } })
      fireEvent.click(screen.getByLabelText('Switch agent'))
      await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
      fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
      await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

      fireEvent.change(field, { target: { value: '' } })
      await waitFor(
        () => expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent('repo-bot'),
        { timeout: 2000 },
      )
      await new Promise(resolve => setTimeout(resolve, 600))
      expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent(
        'project directory was cleared',
      )
    }
  })

  /**
   * The notice reports that the system discarded an agent the reader chose
   * deliberately. Rendered as a bare styled <span> it is invisible to
   * assistive tech, so a screen-reader user clears the directory, hears
   * nothing, and only discovers the substitution when a different agent
   * runs -- the exact silent substitution this notice exists against. Its
   * sibling `chat_folder_was_deleted` notice in the same file already
   * carries `role="status"` with a comment requiring it be "announced as a
   * status", so this asserts the parity rather than the attribute: the note
   * must be reachable BY ROLE, which is what an AT user actually depends on.
   */
  it('announces the reset to assistive tech, not only to sighted readers', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
    renderWithProviders(
      <JobForm
        job={messageJob()}
        agents={[{
          name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
          description: 'built-in', source: 'kirocrew',
        }]}
        defaultAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    const field = screen.getByLabelText('Project directory')
    fireEvent.change(field, { target: { value: '/Users/you/projects/repo' } })
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    fireEvent.change(field, { target: { value: '' } })
    const note = await waitFor(
      () => {
        const found = screen.getByTestId('jobform-agent-reset-note')
        expect(found).toHaveTextContent('repo-bot')
        return found
      },
      { timeout: 2000 },
    )
    // Reachable by role is the assertion that matters -- a reader on
    // assistive tech finds it this way or not at all.
    expect(note).toBe(screen.getByRole('status', { name: '' }))
  })
})
