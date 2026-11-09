import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'
import JobForm, { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn(),
    agentCatalog: vi.fn(),
    chatFolders: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const GLOBAL_AGENT: KiroCrewAgent = {
  name: 'kirocrew',
  kiro_agent: 'kirocrew',
  workspace: 'default',
  memory_store: 'default',
  description: 'built-in',
  source: 'kirocrew',
  selection_kind: 'template',
  scope: 'global',
}

const PROJECT_AGENT: KiroCrewAgent = {
  name: 'repo-agent',
  kiro_agent: 'repo-agent',
  workspace: '',
  memory_store: 'default',
  description: 'Repository specialist',
  source: 'project',
  selection_kind: 'template',
  scope: 'project',
}

function job(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'job1',
    name: 'nightly',
    message: 'run',
    schedule: '',
    enabled: true,
    every_secs: 3600,
    ...overrides,
  } as CronJob
}

beforeEach(() => {
  vi.mocked(api.models).mockReset().mockResolvedValue([])
  vi.mocked(api.agentCatalog).mockReset().mockResolvedValue({
    agents: [GLOBAL_AGENT, PROJECT_AGENT],
    default_agent: 'kirocrew',
  })
  vi.mocked(api.chatFolders).mockReset().mockResolvedValue([])
  vi.mocked(api.kirocrewConfig).mockReset().mockResolvedValue({
    dashboard: { folder_sort: 'custom' },
  })
  vi.mocked(api.createCron).mockReset().mockResolvedValue({})
  vi.mocked(api.updateCron).mockReset().mockResolvedValue({})
})

describe('cron project-agent binding', () => {
  it('round-trips the stored project directory into the form', () => {
    const defaults = parseJobDefaults(job({ project_path: '/repo' }))
    expect(defaults.projectPath).toBe('/repo')
    expect(defaults.savedProjectPath).toBe('/repo')
  })

  it('sends a project path and the template namespace on create', () => {
    const body = buildBody(
      {
        ...parseJobDefaults(),
        name: 'nightly',
        message: 'run',
        agent: 'repo-agent',
        agentKind: 'template',
        projectPath: '/repo',
      },
      'UTC',
      vi.fn(),
    )

    expect(body).toEqual(expect.objectContaining({
      agent: 'repo-agent',
      agent_kind: 'template',
      project_path: '/repo',
    }))
  })

  it('sends only a changed path on edit, including an explicit clear', () => {
    const defaults = parseJobDefaults(job({ project_path: '/repo' }))
    const unchanged = buildBody(defaults, 'UTC', vi.fn(), true)
    const cleared = buildBody({ ...defaults, projectPath: '' }, 'UTC', vi.fn(), true)

    expect(unchanged).not.toHaveProperty('project_path')
    expect(cleared).toHaveProperty('project_path', '')
  })

  it('keeps the mounted project baseline across a refreshed job prop', async () => {
    const props = {
      agents: [GLOBAL_AGENT],
      defaultAgent: 'kirocrew',
      onSaved: () => {},
      layout: 'vertical' as const,
    }
    const view = renderWithProviders(
      <JobForm job={job({ project_path: '/repo' })} {...props} />,
    )
    await waitFor(() => {
      expect(api.agentCatalog).toHaveBeenCalledWith(undefined, '/repo')
    })

    view.rerender(
      <JobForm job={job({ name: 'saved elsewhere', project_path: '/other' })} {...props} />,
    )
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'local rename' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    expect(vi.mocked(api.updateCron).mock.calls.at(-1)![1]).not.toHaveProperty('project_path')
  })

  it('loads the selected project roster and offers its custom agents', async () => {
    renderWithProviders(
      <JobForm
        job={job({ project_path: '/repo' })}
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    await waitFor(() => {
      expect(api.agentCatalog).toHaveBeenCalledWith(undefined, '/repo')
    })
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const list = screen.getByRole('listbox')
    expect(await within(list).findByText('repo-agent')).toBeInTheDocument()
    expect(within(list).getByText('project')).toBeInTheDocument()
  })

  it('resets a project-only selection when the project roster cannot resolve it', async () => {
    vi.mocked(api.agentCatalog).mockResolvedValue({
      agents: [GLOBAL_AGENT],
      default_agent: 'kirocrew',
    })
    renderWithProviders(
      <JobForm
        job={job({ agent: 'removed-agent', project_path: '/repo' })}
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    expect(await screen.findByRole('status')).toHaveTextContent('reset to default')
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('kirocrew')
  })

  it('creates a locked Bot schedule in the member namespace', async () => {
    renderWithProviders(
      <JobForm
        agents={[]}
        defaultAgent=""
        lockedAgent="research-bot"
        memberId="research-bot"
        providerAgent="research-template"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'nightly' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'run' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({
        agent_kind: 'member',
        member_id: 'research-bot',
        agent: 'research-template',
      }))
    })
  })

  it('leaves the kind unstated for a schedule locked to the default crew', async () => {
    renderWithProviders(
      <JobForm
        agents={[]}
        defaultAgent=""
        lockedAgent="default"
        memberId="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'nightly' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'run' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(api.createCron).toHaveBeenCalled())
    const body = vi.mocked(api.createCron).mock.calls.at(-1)![0]
    expect(body).toEqual(expect.objectContaining({ agent: 'default' }))
    expect(body).not.toHaveProperty('agent_kind')
    expect(body).not.toHaveProperty('member_id')
  })

  it('creates an unlocked custom-agent pick in the template namespace', async () => {
    renderWithProviders(
      <JobForm
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'nightly' } })
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'run' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({
        agent_kind: 'template',
      }))
    })
  })

  it('leaves the kind unstated when editing a job locked to a configured alias', async () => {
    renderWithProviders(
      <JobForm
        job={job({ agent: 'default', project_path: '/repo' })}
        agents={[]}
        defaultAgent=""
        lockedAgent="default"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'renamed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    const body = vi.mocked(api.updateCron).mock.calls.at(-1)![1]
    expect(body).toEqual(expect.objectContaining({ agent: 'default', name: 'renamed' }))
    expect(body).not.toHaveProperty('agent_kind')
  })

  it('leaves the kind unstated when an unrelated edit keeps the stored agent', async () => {
    // The stored name is also listed as a template, so only the absence of a
    // pick in this form keeps the kind out of the save.
    const shared: KiroCrewAgent = { ...PROJECT_AGENT, name: 'shared-name', kiro_agent: 'shared-name' }
    vi.mocked(api.agentCatalog).mockResolvedValue({
      agents: [GLOBAL_AGENT, shared],
      default_agent: 'kirocrew',
    })
    renderWithProviders(
      <JobForm
        job={job({ agent: 'shared-name', project_path: '/repo' })}
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    // The project roster lists rows only once its catalog has loaded.
    fireEvent.click(screen.getByLabelText('Switch agent'))
    expect(await within(screen.getByRole('listbox')).findByText('project')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('listbox'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Switch agent')).toHaveTextContent('shared-name')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'renamed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    const body = vi.mocked(api.updateCron).mock.calls.at(-1)![1]
    expect(body).toEqual(expect.objectContaining({ agent: 'shared-name', name: 'renamed' }))
    expect(body).not.toHaveProperty('agent_kind')
  })

  it('states the template kind when the edit picks a catalog template', async () => {
    renderWithProviders(
      <JobForm
        job={job({ agent: 'kirocrew', project_path: '/repo' })}
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )
    await waitFor(() => expect(api.agentCatalog).toHaveBeenCalledWith(undefined, '/repo'))

    fireEvent.click(screen.getByLabelText('Switch agent'))
    fireEvent.click(await within(screen.getByRole('listbox')).findByText('repo-agent'))
    await waitFor(() => {
      expect(screen.getByLabelText('Switch agent')).toHaveTextContent('repo-agent')
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    expect(vi.mocked(api.updateCron).mock.calls.at(-1)![1]).toEqual(expect.objectContaining({
      agent: 'repo-agent',
      agent_kind: 'template',
    }))
  })

  it('does not offer project binding for a Bot-owned schedule', async () => {
    renderWithProviders(
      <JobForm
        job={job({ member_id: 'member-1' })}
        agents={[GLOBAL_AGENT]}
        defaultAgent="kirocrew"
        onSaved={() => {}}
        layout="vertical"
      />,
    )

    await screen.findByLabelText('Name')
    expect(screen.queryByLabelText('Project directory')).not.toBeInTheDocument()
    expect(api.agentCatalog).not.toHaveBeenCalled()
  })
})
