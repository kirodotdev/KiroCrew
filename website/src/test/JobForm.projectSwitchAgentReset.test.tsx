import { describe, it, expect, vi, beforeEach } from 'vitest'
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

beforeEach(() => {
  vi.mocked(api.kirocrewAgents).mockReset()
  vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [], default_agent: '' })
})

/**
 * GPT 5.6 Review F2: switching the project directory from project A to
 * project B (WITHOUT ever clearing it in between) left the picker's `agent`
 * state untouched -- only the `!projectPath` early-return branch reconciled
 * a stale selection, so the A-project agent stayed selected after B's
 * roster loaded even though B's roster does not contain that name. Save
 * would then persist an agent name B's project cannot resolve, and the
 * scheduled fire silently falls back to the default agent's prompt, tools,
 * and permissions with no error surfaced anywhere. Fixed by reconciling the
 * selection against the union of the newly-fetched project roster and the
 * global roster inside the success branch of the folder-change effect
 * itself, not just the cleared-folder branch.
 */
describe('JobForm reconciles the agent picker when the project directory switches project', () => {
  it('clears a project-A-only agent that does not exist under project B', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({
        agents: [{
          name: 'repo-a-bot', kiro_agent: 'repo-a-bot', workspace: 'repo-a', memory_store: 'repo-a',
          description: 'repo A agent', source: 'project',
        }],
        default_agent: '',
      })
      .mockResolvedValueOnce({
        agents: [{
          name: 'repo-b-bot', kiro_agent: 'repo-b-bot', workspace: 'repo-b', memory_store: 'repo-b',
          description: 'repo B agent', source: 'project',
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
      target: { value: '/Users/you/projects/repo-a' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    // The resolved roster now propagates through useQuery (an extra render
    // hop past the raw fetch call above), so wait for the option to actually
    // be in the DOM before clicking it -- a bare `getByRole` can race that
    // hop and see an empty listbox.
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-a-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-a-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot'))

    // Switch straight to project B, never clearing the field in between.
    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/repo-b' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))

    // repo-a-bot exists in neither B's project roster nor the global
    // roster, so the selection must be cleared back to default.
    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).toHaveTextContent('default'),
    )
    expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-a-bot')
  })

  it('keeps a project-only agent selected when it also exists under the new project', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({
        agents: [{
          name: 'shared-bot', kiro_agent: 'shared-bot', workspace: 'repo-a', memory_store: 'repo-a',
          description: 'shared agent', source: 'project',
        }],
        default_agent: '',
      })
      .mockResolvedValueOnce({
        agents: [{
          name: 'shared-bot', kiro_agent: 'shared-bot', workspace: 'repo-b', memory_store: 'repo-b',
          description: 'shared agent', source: 'project',
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
      target: { value: '/Users/you/projects/repo-a' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    // See the sibling test above -- wait for the roster to have propagated
    // through useQuery before clicking the option.
    await waitFor(() => expect(screen.getByRole('option', { name: /shared-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /shared-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-bot'))

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/repo-b' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))

    // shared-bot exists under project B too, so it must survive.
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-bot')
  })

  it('keeps a global agent selected across a project A-to-B switch', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({ agents: [], default_agent: '' })
      .mockResolvedValueOnce({ agents: [], default_agent: '' })

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
      target: { value: '/Users/you/projects/repo-a' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/repo-b' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev')
  })
})
