import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import { api } from '../api/client'
import JobForm from '../components/JobForm'
import type { KiroCrewAgent } from '../components/AgentSelector'
import type { CronJob } from '../types'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn(),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn(),
    createCron: vi.fn(),
  },
}))

const row = (name: string, scope: string): KiroCrewAgent => ({
  name, scope, kiro_agent: name, workspace: '', memory_store: '',
  description: '', source: 'kirocrew',
})
const configured = row('configured', 'global')
const projectOnly = row('repo-agent', 'project')
const rosterFailure = { reloading: false, onReload: () => {} }
const job = (agent: string): CronJob => ({
  id: 'saved-job', name: 'nightly', message: 'summarize changes', schedule: '',
  enabled: true, every_secs: 3600, project_path: '/repo-a', agent,
} as CronJob)

beforeEach(() => {
  vi.mocked(api.kirocrewAgents).mockReset().mockResolvedValue({ agents: [configured], default_agent: '' })
  vi.mocked(api.updateCron).mockReset().mockResolvedValue({})
})

function mount(agent: string, overrides: Partial<ComponentProps<typeof JobForm>> = {}) {
  const props: ComponentProps<typeof JobForm> = {
    job: job(agent), agents: [], defaultAgent: '', rosterFailure,
    layout: 'vertical', onSaved: () => {}, ...overrides,
  }
  return { ...renderWithProviders(<JobForm {...props} />), props }
}

async function saveAndExpectAgent(agent: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(api.updateCron).toHaveBeenCalledWith(
    'saved-job', expect.objectContaining({ agent }),
  ))
}

describe('JobForm preserves saved template picks when the global catalog is unavailable', () => {
  it('keeps an installed template absent from the configured-member project endpoint on an unrelated save', async () => {
    const { queryClient } = mount('installed-template')
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-a'])?.status).toBe('success'))
    await act(async () => {})
    fireEvent.change(screen.getByLabelText('Name', { exact: true }), { target: { value: 'renamed' } })
    await saveAndExpectAgent('installed-template')
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
  })

  it('keeps an installed template on a folder switch and fresh failure-prop objects', async () => {
    const { queryClient, rerender, props } = mount('installed-template')
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-a'])?.status).toBe('success'))
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-b' } })
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-b'])?.status).toBe('success'))
    rerender(<JobForm {...props} rosterFailure={{ reloading: true, onReload: () => {} }} />)
    await act(async () => {})
    await saveAndExpectAgent('installed-template')
  })

  it('clears a positively known project-only pick after a loading gap', async () => {
    let finish!: (value: { agents: KiroCrewAgent[]; default_agent: string }) => void
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce({ agents: [configured, projectOnly], default_agent: '' })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const { queryClient } = mount(projectOnly.name)
    await waitFor(() => expect(screen.getByLabelText('Switch agent')).toHaveTextContent(projectOnly.name))
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-a'])?.status).toBe('success'))
    await act(async () => {})
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-b' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
    await act(async () => { finish({ agents: [configured], default_agent: '' }) })
    const notice = await screen.findByTestId('jobform-agent-reset-note')
    expect(notice).toHaveTextContent(projectOnly.name)
    await saveAndExpectAgent('')
  })

  it('retains a matching global name even when a project row defined it', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce({ agents: [projectOnly], default_agent: '' })
    const { queryClient } = mount(projectOnly.name, { agents: [row(projectOnly.name, 'global')] })
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-a'])?.status).toBe('success'))
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-b' } })
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-b'])?.status).toBe('success'))
    await act(async () => {})
    await saveAndExpectAgent(projectOnly.name)
  })

  it('keeps a pick the recovered global catalog does not list', async () => {
    // A loaded catalog is not the resolver: it omits runtime-owned templates the
    // backend still runs, so its silence is no reason to clear a saved binding.
    const { queryClient, rerender, props } = mount('installed-template')
    await waitFor(() => expect(queryClient.getQueryState(['project-agents', '/repo-a'])?.status).toBe('success'))
    await act(async () => {})
    rerender(<JobForm {...props} agents={[configured]} rosterFailure={undefined} />)
    await act(async () => {})
    expect(screen.queryByTestId('jobform-agent-reset-note')).not.toBeInTheDocument()
    await saveAndExpectAgent('installed-template')
  })
})
