import type { ComponentProps } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import type { KiroCrewAgent } from '../components/AgentSelector'
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
    // is any file in this group's subject; unstubbed they throw or render an
    // unrelated error notice into the form under test.
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

const builtInAgent: KiroCrewAgent = {
  name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
  description: 'built-in', source: 'kirocrew',
}
const eaDevAgent: KiroCrewAgent = {
  name: 'ea-dev', kiro_agent: 'ea-dev', workspace: 'ea', memory_store: 'ea',
  description: 'ea agent', source: 'kirocrew',
}
const withDefaultAgent = { agents: [builtInAgent], defaultAgent: 'default' }

/** Renders JobForm with the props every test shares; `overrides` varies any one. */
function renderJobForm(overrides: Partial<ComponentProps<typeof JobForm>> = {}) {
  return renderWithProviders(
    <JobForm
      job={messageJob()}
      agents={[]}
      defaultAgent=""
      onSaved={() => {}}
      layout="vertical"
      {...overrides}
    />,
  )
}

beforeEach(() => {
  vi.mocked(api.kirocrewAgents).mockReset()
  vi.mocked(api.kirocrewAgents).mockResolvedValue({ agents: [], default_agent: '' })
})

afterEach(() => { vi.useRealTimers() })

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
          description: 'repo A agent', source: 'project', scope: 'project',
        }],
        default_agent: '',
      })
      .mockResolvedValueOnce({
        agents: [{
          name: 'repo-b-bot', kiro_agent: 'repo-b-bot', workspace: 'repo-b', memory_store: 'repo-b',
          description: 'repo B agent', source: 'project', scope: 'project',
        }],
        default_agent: '',
      })

    renderJobForm(withDefaultAgent)

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

    renderJobForm(withDefaultAgent)

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

    renderJobForm({ agents: [builtInAgent, eaDevAgent], defaultAgent: 'default' })

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

  it('clears a project-A agent when the global catalog fails but project B loads', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({
        agents: [{
          name: 'repo-a-bot', kiro_agent: 'repo-a-bot', workspace: 'repo-a', memory_store: 'repo-a',
          description: 'repo A agent', source: 'project', scope: 'project',
        }],
        default_agent: '',
      })
      .mockResolvedValueOnce({
        agents: [{
          name: 'repo-b-bot', kiro_agent: 'repo-b-bot', workspace: 'repo-b', memory_store: 'repo-b',
          description: 'repo B agent', source: 'project', scope: 'project',
        }],
        default_agent: '',
      })

    renderJobForm()

    const projectDirectory = screen.getByLabelText('Project directory')
    fireEvent.change(projectDirectory, { target: { value: '/Users/you/projects/repo-a' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-a-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-a-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot'))

    fireEvent.change(projectDirectory, { target: { value: '/Users/you/projects/repo-b' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))

    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-a-bot'),
    )
    expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent('repo-a-bot')
  })

  /** The project roster for `/Users/you/projects/repo`: one folder agent, and no
   *  installed shared template, which the project endpoint never lists. */
  function mockRepoRoster() {
    vi.mocked(api.kirocrewAgents).mockResolvedValue({
      agents: [{
        name: 'repo-bot', kiro_agent: 'repo-bot', workspace: 'repo', memory_store: 'repo',
        description: 'repo agent', source: 'project', scope: 'project',
      }],
      default_agent: '',
    })
  }

  /** Waits until the project roster has reached the picker, which happens in the
   *  same effect run that reconciles the saved pick against it. */
  async function waitForProjectRoster() {
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.keyDown(document.activeElement || document.body, { key: 'Escape' })
  }

  const sharedTemplate: KiroCrewAgent = {
    name: 'shared-tpl', kiro_agent: 'shared-tpl', workspace: 'default', memory_store: 'default',
    description: 'installed template', source: 'kirocrew',
  }
  const boundJob = () => messageJob({ agent: 'shared-tpl', project_path: '/Users/you/projects/repo' })
  const formProps = (overrides: Partial<ComponentProps<typeof JobForm>>) => ({
    job: boundJob(), defaultAgent: '', onSaved: () => {}, layout: 'vertical' as const, ...overrides,
  })

  it('keeps a saved template while the global catalog is pending, and after it then fails', async () => {
    // GPT 6.1 Review: the project roster settled before the global catalog,
    // omitted the template, and the pick was cleared; a later catalog failure
    // left it cleared, so saving any unrelated edit stored the default agent.
    mockRepoRoster()
    const { rerender } = renderWithProviders(<JobForm {...formProps({ agents: [] })} />)
    await waitForProjectRoster()

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-tpl')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()

    const failure = { reloading: false, onReload: () => {} }
    rerender(<JobForm {...formProps({ agents: [], rosterFailure: failure })} />)
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-tpl')

    rerender(<JobForm {...formProps({ agents: [sharedTemplate] })} />)
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-tpl')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
  })

  it('keeps and saves a runtime-owned template that no loaded catalog lists', async () => {
    // GPT 6.1 Review: an installed runtime-owned template (`kirocrew-lite`) is
    // accepted by the backend resolver but omitted by both pickers' catalogs,
    // so catalog absence cleared it and an unrelated save stored `agent_id: ''`.
    // Catalog absence is not a resolution failure; only a pick the previous
    // folder defined is cleared.
    mockRepoRoster()
    vi.mocked(api.updateCron).mockResolvedValue({} as never)
    const job = messageJob({ agent: 'kirocrew-lite', project_path: '/Users/you/projects/repo' })
    renderWithProviders(<JobForm {...formProps({ job, agents: [builtInAgent] })} />)
    await waitForProjectRoster()

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('kirocrew-lite')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    expect(api.updateCron).toHaveBeenCalledWith('j1', expect.objectContaining({ agent: 'kirocrew-lite' }))
  })

  it('keeps a globally valid pick when the global catalog fails but project B loads it', async () => {
    const globalFromProjectRoster = {
      name: 'ea-dev', kiro_agent: 'ea-dev', workspace: 'ea', memory_store: 'ea',
      description: 'global agent', source: 'kirocrew', scope: 'global',
    }
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({
        agents: [
          globalFromProjectRoster,
          {
            name: 'repo-a-bot', kiro_agent: 'repo-a-bot', workspace: 'repo-a', memory_store: 'repo-a',
            description: 'repo A agent', source: 'project', scope: 'project',
          },
        ],
        default_agent: '',
      })
      .mockResolvedValueOnce({ agents: [globalFromProjectRoster], default_agent: '' })

    renderJobForm()

    const projectDirectory = screen.getByLabelText('Project directory')
    fireEvent.change(projectDirectory, { target: { value: '/Users/you/projects/repo-a' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /ea-dev/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /ea-dev/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev'))

    fireEvent.change(projectDirectory, { target: { value: '/Users/you/projects/repo-b' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))

    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('ea-dev')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
  })

  it('keeps a project-only pick while a longer directory path is typed', async () => {
    const projectAgent = {
      name: 'repo-a-bot', kiro_agent: 'repo-a-bot', workspace: 'repo-a', memory_store: 'repo-a',
      description: 'repo A agent', source: 'project', scope: 'project',
    }
    const initialPath = '/Users/you/projects/repo-a'
    const finalPath = `${initialPath}-longer`
    vi.mocked(api.kirocrewAgents).mockImplementation(async (_sessionKey, path) => ({
      agents: path === initialPath || path === finalPath ? [projectAgent] : [],
      default_agent: '',
    }))

    renderJobForm()

    const projectDirectory = screen.getByLabelText('Project directory')
    fireEvent.change(projectDirectory, { target: { value: initialPath } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-a-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-a-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot'))

    for (let end = initialPath.length + 1; end <= finalPath.length; end += 1) {
      fireEvent.change(projectDirectory, { target: { value: finalPath.slice(0, end) } })
      await Promise.resolve()
    }

    // No half-typed path is queried, so its empty roster cannot reset the pick.
    expect(api.kirocrewAgents).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()

    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))
    expect(api.kirocrewAgents).toHaveBeenLastCalledWith(undefined, finalPath)
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot')
  })

  it('restores a pick an intermediate path cleared once the finished path defines it', async () => {
    // UX Review: the debounce above only protects a path typed FASTER than
    // 250ms. Hand-editing a bound job's directory pauses longer than that, so an
    // intermediate path becomes a roster identity of its own -- and
    // `GET /api/agents?project_path=` answers an unusable path with HTTP 200 and
    // the globals alone, so that roster legitimately lacks the picked project
    // agent and the reconcile clears it. Clearing on what it knows is right; the
    // defect was that the pick never came BACK once the finished path loaded a
    // roster that does define the name, so every path edit on a bound job forced
    // a re-pick, and the acknowledgment for a reset still in force was erased by
    // the next intermediate settle.
    const projectAgent = {
      name: 'repo-a-bot', kiro_agent: 'repo-a-bot', workspace: 'repo-a', memory_store: 'repo-a',
      description: 'repo A agent', source: 'project', scope: 'project',
    }
    const initialPath = '/Users/you/projects/repo-a'
    const halfTyped = `${initialPath}-v`
    const finalPath = `${initialPath}-v2`
    vi.mocked(api.kirocrewAgents).mockImplementation(async (_sessionKey, path) => ({
      agents: path === initialPath || path === finalPath ? [projectAgent] : [],
      default_agent: '',
    }))

    renderJobForm(withDefaultAgent)

    const projectDirectory = screen.getByLabelText('Project directory')
    fireEvent.change(projectDirectory, { target: { value: initialPath } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-a-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-a-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot'))

    // Pause on a half-typed path long enough for it to settle and be queried.
    fireEvent.change(projectDirectory, { target: { value: halfTyped } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2), { timeout: 2000 })
    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).not.toHaveTextContent('repo-a-bot'),
    )
    // The reset IS announced, and stays announced while it is still in force.
    expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent('repo-a-bot')

    // Finish the path. Its roster defines the name again, so the operator's own
    // pick comes back rather than leaving them to re-pick it.
    fireEvent.change(projectDirectory, { target: { value: finalPath } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(3), { timeout: 2000 })

    await waitFor(() =>
      expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-a-bot'),
    )
    // Nothing was lost after all, so the acknowledgment must not linger.
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
  })
})

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
    renderJobForm(withDefaultAgent)

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
    renderJobForm({ agents: [builtInAgent, eaDevAgent], defaultAgent: 'default' })

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
    renderJobForm()

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
    renderJobForm({ job: messageJob({ agent: 'ea-dev' }) })

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
    renderJobForm()

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
    renderJobForm(withDefaultAgent)

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

    vi.useFakeTimers()
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })
    expect(screen.getByTestId('jobform-agent-reset-note')).toBeInTheDocument()

    // Settle the 250ms debounce and flush the query-disabling re-render.
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })

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
    renderJobForm(withDefaultAgent)
    const field = screen.getByLabelText('Project directory')

    for (const round of [1, 2]) {
      fireEvent.change(field, { target: { value: `/Users/you/projects/repo-${round}` } })
      fireEvent.click(screen.getByLabelText('Switch agent'))
      await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
      fireEvent.click(screen.getByRole('option', { name: /repo-bot/ }))
      await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-bot'))

      vi.useFakeTimers()
      fireEvent.change(field, { target: { value: '' } })
      expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent('repo-bot')
      await act(async () => { await vi.advanceTimersByTimeAsync(250) })
      expect(screen.getByTestId('jobform-agent-reset-note')).toHaveTextContent(
        'project directory was cleared',
      )
      vi.useRealTimers()
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
    renderJobForm(withDefaultAgent)
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

/**
 * The project-scoped roster fetch (`api.kirocrewAgents` keyed by working
 * directory) had its `.catch()` write into the SAME `error` state as
 * Save-time field validation. Two real bugs from that: (1) `setError` also
 * auto-scrolls the page to the bottom-of-form notice on every set, so a
 * background fetch failing mid-typing would yank the user's scroll position
 * for a failure unrelated to what they were doing; (2) the two failure kinds
 * share one string with no independent clear, so a stale roster error could
 * survive an otherwise-successful save (only the Save handler's own
 * `setError('')` clears it) or a validation error could be silently
 * clobbered by a late-resolving roster retry. Fixed by giving the roster
 * fetch its own state, rendered beside the project-directory field via the
 * same `ErrorNotice` component the sibling `AgentSelector` roster-failure UI
 * already uses, instead of the form-wide notice.
 */
describe('JobForm project roster fetch failure stays out of the form-wide error', () => {
  it('shows the roster failure beside the project-directory field, not in the form-wide notice', async () => {
    vi.mocked(api.kirocrewAgents).mockRejectedValueOnce(new Error('network unreachable'))
    renderJobForm()

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })

    await waitFor(() =>
      expect(screen.getByTestId('jobform-project-roster-error')).toHaveTextContent('network unreachable'),
    )
    // The form-wide notice (Save-time validation, no testId) must stay
    // absent -- a background roster failure is not a reason to show it or
    // scroll the page. Several notices in this form share `role="alert"`, so a
    // bare `queryByRole` would match one of the tagged ones; the form-wide
    // notice is the one carrying no `data-testid`, so assert on its absence
    // rather than on a total alert count, which any new tagged sibling breaks.
    const formWide = screen
      .queryAllByRole('alert')
      .filter(el => !el.getAttribute('data-testid'))
    expect(formWide).toHaveLength(0)
  })

  it('keeps the roster failure visible after an unrelated successful save -- it describes the folder, not the save outcome', async () => {
    vi.mocked(api.kirocrewAgents).mockRejectedValueOnce(new Error('network unreachable'))
    vi.mocked(api.updateCron).mockResolvedValue({ ok: true })
    const onSaved = vi.fn()
    renderJobForm({ onSaved })

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    // A prior implementation shared one `error` string between the roster
    // fetch and Save-time validation, whose only clear point was the Save
    // handler's own `setError('')` -- so a successful save happened to also
    // wipe the (unrelated) roster message as a side effect, and the reverse
    // held too: a real validation error could be clobbered by a late roster
    // retry. The roster notice is now independent state describing the
    // CURRENT folder, so it is unaffected by an unrelated save outcome --
    // it still needs its own retry/folder-change to clear, checked below.
    expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument()
  })

  it('clears the roster failure when the folder is changed to one that resolves', async () => {
    vi.mocked(api.kirocrewAgents)
      .mockRejectedValueOnce(new Error('network unreachable'))
      .mockResolvedValueOnce({ agents: [], default_agent: '' })
    renderJobForm()

    const input = screen.getByLabelText('Project directory')
    fireEvent.change(input, { target: { value: '/Users/you/projects/myrepo' } })
    await waitFor(() => expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument())

    fireEvent.change(input, { target: { value: '/Users/you/projects/other' } })
    await waitFor(() =>
      expect(screen.queryByTestId('jobform-project-roster-error')).not.toBeInTheDocument(),
    )
  })

  it('drops the detail clause when the error carries no prose message', async () => {
    // UX Review: `friendlyErrText` unwraps error/detail/message when present and
    // otherwise hands back the RAW body, so a response with no message field (a
    // bare `{}`) was interpolated straight into the sentence and rendered
    // "Couldn't load this project's agents: {}." Bare punctuation is worse than
    // no clause, so the non-prose shapes take the detail-less sentence instead.
    vi.mocked(api.kirocrewAgents).mockRejectedValue(new Error('{}'))
    renderJobForm()

    const input = screen.getByLabelText('Project directory')
    fireEvent.change(input, { target: { value: '/Users/you/projects/myrepo' } })

    const notice = await screen.findByTestId('jobform-project-roster-error')
    expect(notice.textContent).not.toContain('{}')
    expect(notice.textContent).not.toContain('agents: .')
    // The two facts still both land: what failed, and what it fell back to.
    expect(notice.textContent).toContain("Couldn't load this project's agents.")
    expect(notice.textContent).toContain('Showing the global agent list instead.')
  })

  it('keeps the detail clause when the error IS prose', async () => {
    vi.mocked(api.kirocrewAgents).mockRejectedValue(new Error('network unreachable'))
    renderJobForm()

    const input = screen.getByLabelText('Project directory')
    fireEvent.change(input, { target: { value: '/Users/you/projects/myrepo' } })

    const notice = await screen.findByTestId('jobform-project-roster-error')
    expect(notice.textContent).toContain('network unreachable')
  })

  it('offers a retry in place and clears the failure on a successful refetch', async () => {
    // The failure "need[s] different next actions (retry vs. pick a different
    // folder)" -- before this it offered only the latter (re-type the path to
    // re-key the query). Now a retry sits beside the notice, the same shape as
    // the chat-folder and agent-roster retries (UX Review span=6409bbff1088).
    vi.mocked(api.kirocrewAgents)
      .mockRejectedValueOnce(new Error('network unreachable'))
      .mockResolvedValueOnce({ agents: [], default_agent: '' })
    renderJobForm()

    fireEvent.change(screen.getByLabelText('Project directory'), {
      target: { value: '/Users/you/projects/myrepo' },
    })
    await waitFor(() => expect(screen.getByTestId('jobform-project-roster-error')).toBeInTheDocument())

    // Re-key by re-typing was the ONLY recovery before; the button is the fix.
    const before = vi.mocked(api.kirocrewAgents).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() =>
      expect(screen.queryByTestId('jobform-project-roster-error')).not.toBeInTheDocument(),
    )
    // The retry refetched the same query in place rather than asking the user
    // to change the folder: at least one more fetch, no folder change.
    expect(vi.mocked(api.kirocrewAgents).mock.calls.length).toBeGreaterThan(before)
  })
})

const collisionGlobalRoster: KiroCrewAgent[] = [
  {
    name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
    description: 'built-in', source: 'kirocrew', scope: 'global',
  },
  {
    name: 'reviewer', kiro_agent: 'reviewer-tpl', workspace: 'ws', memory_store: 'reviewer-kb',
    description: 'the configured reviewer', source: 'package', scope: 'global',
  },
]

/**
 * The folder declares its OWN `reviewer` -- the collision under test.
 *
 * Shaped as `GET /api/agents?project_path=` actually answers, which matters in
 * three ways a hand-made fixture gets wrong:
 *
 *  - It is the WHOLE roster, not the folder's half: the configured agents are
 *    in it too. A fixture holding only project rows hides the bug where every
 *    configured agent gets marked as overridden.
 *  - The shadowed `reviewer` global row is ABSENT -- the backend suppresses the
 *    losing side rather than sending both.
 *  - A project row carries `source: 'kirocrew'` and an EMPTY description. Every
 *    project row is one shared default record under a project tag, because
 *    `project_agent_names` returns names only; there is no per-agent config on
 *    disk to read without a second scan. So `scope` is the only field that says
 *    "project", and a test may not identify a project row by a description the
 *    backend never populates.
 */
const collisionProjectRoster = {
  agents: [
    {
      name: 'default', kiro_agent: 'default', workspace: 'default', memory_store: 'default',
      description: 'built-in', source: 'kirocrew', scope: 'global',
    },
    {
      name: 'reviewer', kiro_agent: '', workspace: 'default', memory_store: 'default',
      description: '', source: 'kirocrew', scope: 'project',
    },
    {
      name: 'repo-bot', kiro_agent: '', workspace: 'default', memory_store: 'default',
      description: '', source: 'kirocrew', scope: 'project',
    },
  ],
  default_agent: 'default',
}

async function openWithCollisionFolder(): Promise<HTMLElement> {
  vi.mocked(api.kirocrewAgents).mockResolvedValueOnce(collisionProjectRoster)
  renderJobForm({ agents: collisionGlobalRoster, defaultAgent: 'default' })
  fireEvent.change(screen.getByLabelText('Project directory'), {
    target: { value: '/Users/you/projects/myrepo' },
  })
  await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
  fireEvent.click(screen.getByLabelText('Switch agent'))
  // The roster arrives one render hop past the fetch call, so wait for a
  // project-only row rather than reading the listbox immediately.
  await waitFor(() => expect(screen.getByRole('option', { name: /repo-bot/ })).toBeInTheDocument())
  return screen.getByRole('listbox')
}

/**
 * Inside a bound folder a project definition outranks a same-named configured
 * agent, so the picker must offer the PROJECT row: it is the one the fire
 * resolves. Keeping the global row instead advertised an agent that could not
 * answer -- the form named the configured agent while dispatch ran the project
 * file, the advertised-vs-answering mismatch `resolved_alias` exists to
 * prevent.
 *
 * One row per name, because `agent` is a bare string and could not record
 * which of two same-named rows was chosen. The surviving row therefore states
 * the override outright: "this is a project agent" and "this project agent took
 * over a configured agent's name" are different facts, and only the second
 * explains why the configured one is no longer listed.
 */
describe('JobForm agent picker on a project/global name collision', () => {
  it('offers the project agent, not the same-named configured one', async () => {
    const listbox = await openWithCollisionFolder()
    const options = within(listbox).getAllByRole('option').map(o => o.textContent || '')

    // One row for the colliding name, not two: a second row would be an option
    // the form cannot persist.
    expect(options.filter(t => t.includes('reviewer'))).toHaveLength(1)

    // And it is the PROJECT one. Identified by the override marker plus the
    // ABSENCE of the configured agent's description: a project row carries no
    // description of its own, so there is no positive text to match on -- which
    // is exactly why the marker has to exist.
    const row = within(listbox).getByRole('option', { name: /reviewer/ })
    expect(row).toHaveTextContent('overrides global')
    expect(row).not.toHaveTextContent('the configured reviewer')

    // Non-colliding rows from both sides survive untouched.
    expect(options.some(t => t.includes('repo-bot'))).toBe(true)
    expect(options.some(t => t.includes('default'))).toBe(true)
  })

  it('does not mark a configured agent the folder never declared', async () => {
    // The project-scoped response carries the configured agents as well as the
    // folder's, so a name-only match marks EVERY configured agent as overridden
    // the moment a folder is bound. `default` is in both rosters and is not
    // declared by the folder, so it must stay unmarked.
    const listbox = await openWithCollisionFolder()
    const untouched = within(listbox).getByRole('option', { name: /default/ })
    expect(untouched).not.toHaveTextContent('overrides global')
  })

  it('marks the surviving row as overriding the configured agent', async () => {
    const listbox = await openWithCollisionFolder()
    const row = within(listbox).getByRole('option', { name: /reviewer/ })
    expect(row).toHaveTextContent('overrides global')

    // The explanation is VISIBLE, not hover-only. The badge's `title` needs a
    // mouse resting on a 10px chip and does not exist on touch at all, and this
    // marker is the only signal in the feature that tells a user why their
    // configured agent vanished from the list. It no longer REPEATS the agent
    // name — the row title directly above already shows it, and repeating it
    // pushed the meaning past this line's own truncation on a narrow popover
    // (UX Review span=7ddb24260c79).
    expect(row).toHaveTextContent(
      "This job's project directory defines its own agent with this name, so it runs "
      + 'instead of your global one. Your global agent is unchanged elsewhere.',
    )

    // The marker is specific to the collision, not to being a project agent:
    // a project-only row has nothing to override and must not claim otherwise.
    const projectOnly = within(listbox).getByRole('option', { name: /repo-bot/ })
    expect(projectOnly).not.toHaveTextContent('overrides global')
    expect(projectOnly).not.toHaveTextContent('Runs instead of')
  })

  it('drops the marker when the project directory is cleared', async () => {
    const listbox = await openWithCollisionFolder()
    expect(within(listbox).getByRole('option', { name: /reviewer/ })).toHaveTextContent('overrides global')

    fireEvent.keyDown(listbox, { key: 'Escape' })
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '' } })

    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() => {
      const reopened = screen.getByRole('listbox')
      expect(within(reopened).queryByText('overrides global')).not.toBeInTheDocument()
    })
    // With no folder there is no project scope, so the configured agent is the
    // one offered again.
    const reopened = screen.getByRole('listbox')
    expect(within(reopened).getByRole('option', { name: /reviewer/ })).toHaveTextContent('the configured reviewer')
  })
})
