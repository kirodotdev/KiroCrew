import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
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
  },
}))

const globalRoster: KiroCrewAgent[] = [
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
const projectRoster = {
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

function messageJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *', ...overrides,
  } as CronJob
}

async function openWithFolder(): Promise<HTMLElement> {
  vi.mocked(api.kirocrewAgents).mockResolvedValueOnce(projectRoster)
  renderWithProviders(
    <JobForm
      job={messageJob()}
      agents={globalRoster}
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
    const listbox = await openWithFolder()
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
    const listbox = await openWithFolder()
    const untouched = within(listbox).getByRole('option', { name: /default/ })
    expect(untouched).not.toHaveTextContent('overrides global')
  })

  it('marks the surviving row as overriding the configured agent', async () => {
    const listbox = await openWithFolder()
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
    const listbox = await openWithFolder()
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
