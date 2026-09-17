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
        description: 'repo agent', source: 'project',
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
        description: 'repo agent', source: 'project',
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
})
