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
 * directory was set), then clearing that working directory, previously left
 * the picker's `agent` state untouched: `effectiveAgents` correctly fell
 * back to the global roster once `projectAgents` cleared, but the SELECTED
 * name was never reset, so save sent that now-unresolvable name with an
 * empty `project_path`. Kiro cannot resolve a project agent without its
 * project scope and silently falls back to the default agent's prompt,
 * tools, and permissions -- with no error surfaced anywhere to say so.
 * Fixed by clearing the selection back to the global default whenever it is
 * not a name the global roster itself recognizes.
 */
describe('JobForm clears a project-only agent selection when the working directory is cleared', () => {
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

    fireEvent.change(screen.getByLabelText('Working directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())

    // Select the project-only agent via the AgentSelector's listbox.
    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    // Now clear the working directory -- the project-only agent must no
    // longer be shown as selected.
    fireEvent.change(screen.getByLabelText('Working directory'), { target: { value: '' } })

    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).toHaveTextContent('default'),
    )
    expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-bot')
  })

  it('leaves a global agent selection untouched when the working directory clears', async () => {
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

    fireEvent.change(screen.getByLabelText('Working directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Working directory'), { target: { value: '' } })

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev')
  })
})
