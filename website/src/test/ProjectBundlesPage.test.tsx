import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../api/client'
import ProjectBundlesPage from '../pages/ProjectBundlesPage'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    projectBundles: vi.fn(),
    createProjectBundle: vi.fn(),
    addProjectBundle: vi.fn(),
    syncProjectBundle: vi.fn(),
    activateProjectBundle: vi.fn(),
    deactivateProjectBundle: vi.fn(),
    removeProjectBundle: vi.fn(),
    createChatSlot: vi.fn(),
    chatSlotProject: vi.fn(),
    setSlotColor: vi.fn(),
    setSlotColorHex: vi.fn(),
    deleteChatSlot: vi.fn(),
  },
}))

const localProject = {
  id: '018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e',
  name: 'Payments Platform',
  description: 'Payments services and operational context.',
  workspace_source: 'payments-api',
  sources: [{
    id: 'payments-api',
    type: 'repo',
    url: 'https://github.com/acme/payments-api',
    default_branch: 'main',
  }],
  context: {
    agents: ['agents/*.json'],
    skills: ['skills/'],
    mcp: 'mcp.json',
  },
  registrations: [{ origin: 'local' as const, path: '/work/payments', syncable: false }],
  health: { status: 'healthy' as const, code: 'project_healthy' },
  sessions: [{
    key: 'payments-chat',
    title: 'Investigate refunds',
    messages: 4,
    running: false,
    live: true,
  }],
  capabilities: {
    active: false,
    review_key: '/work/payments',
    agents: 2,
    skills: 3,
    mcp_servers: 1,
    repos: 1,
    repositories: [],
    agent_names: ['reviewer', 'release-manager'],
    mcp_server_details: [{ name: 'docs', command: 'uvx', args: ['docs-mcp', '--readonly'] }],
  },
}

const managedProject = {
  ...localProject,
  id: '018f4f4a-760f-7a8b-a5d4-5a7e0f130d5f',
  name: 'Shared Payments',
  registrations: [{
    origin: 'managed_git' as const,
    path: '/data/projects/shared-payments',
    syncable: true,
  }],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.projectBundles).mockResolvedValue({ projects: [localProject] })
  vi.mocked(api.createProjectBundle).mockResolvedValue(localProject)
  vi.mocked(api.addProjectBundle).mockResolvedValue(localProject)
  vi.mocked(api.syncProjectBundle).mockResolvedValue(managedProject)
  vi.mocked(api.activateProjectBundle).mockResolvedValue({
    ...localProject.capabilities,
    active: true,
  })
  vi.mocked(api.deactivateProjectBundle).mockResolvedValue(localProject.capabilities)
  vi.mocked(api.removeProjectBundle).mockResolvedValue({ ok: true, id: localProject.id })
  vi.mocked(api.createChatSlot).mockResolvedValue({
    key: 'new-project-chat',
    title: 'New Session',
    messages: 0,
    running: false,
    project: '/work/payments',
    project_id: localProject.id,
  })
})

describe('Project bundles portal', () => {
  it('opens a Project from a single-column list into a focused detail view', async () => {
    renderWithProviders(<ProjectBundlesPage />)

    const project = await screen.findByRole('button', { name: /Open project Payments Platform/ })
    expect(screen.queryByRole('button', { name: 'New session' })).not.toBeInTheDocument()

    fireEvent.click(project)

    expect(await screen.findByRole('heading', { name: 'Payments Platform' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeInTheDocument()
    expect(screen.getByText('Payments services and operational context.')).toBeInTheDocument()
    expect(screen.getAllByText('payments-api')).toHaveLength(2)
    expect(screen.getByText('https://github.com/acme/payments-api')).toBeInTheDocument()
    expect(screen.getByText('/work/payments')).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit project' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Trust and activate' })).toBeInTheDocument()
    expect(screen.getByText('Investigate refunds')).toBeInTheDocument()
    expect(screen.getByText('/work/payments')).toBeInTheDocument()
  })

  it('renders only repository sources in detail when provider data contains objects', async () => {
    const projectWithExtensionSource = {
      ...localProject,
      sources: [
        ...localProject.sources,
        { id: 'pay-board', type: 'jira', url: { board: 'PAY' } },
      ],
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [projectWithExtensionSource] })
    renderWithProviders(<ProjectBundlesPage />)

    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    expect(screen.getByText('https://github.com/acme/payments-api')).toBeInTheDocument()
    expect(screen.queryByText('pay-board')).not.toBeInTheDocument()
  })

  it('starts a session with the Project identity in the create request', async () => {
    renderWithProviders(<ProjectBundlesPage />)

    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))
    fireEvent.click(screen.getByRole('button', { name: 'New session' }))

    await waitFor(() => {
      expect(api.createChatSlot).toHaveBeenCalledWith(
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        localProject.id,
      )
    })
  })

  it('explains how to populate an empty registry', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [] })

    renderWithProviders(<ProjectBundlesPage />)

    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
    expect(screen.getByText('Create a local Project or add one from a folder or Git URL.')).toBeInTheDocument()
  })

  it('creates a local bundle and refreshes the portal list', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [] })
      .mockResolvedValue({ projects: [localProject] })

    renderWithProviders(<ProjectBundlesPage />)
    await screen.findByText('No projects yet')
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    fireEvent.change(screen.getByLabelText('Project name'), {
      target: { value: 'Payments Platform' },
    })
    const projectFolder = screen.getByLabelText('Project folder')
    fireEvent.change(projectFolder, {
      target: { value: '/work/payments' },
    })
    fireEvent.click(within(projectFolder.closest('form')!).getByRole('button', { name: 'Create project' }))

    expect(await screen.findByRole('button', { name: /Open project Payments Platform/ })).toBeInTheDocument()
  })

  it('adds an existing folder or Git URL and refreshes the portal list', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [] })
      .mockResolvedValue({ projects: [localProject] })

    renderWithProviders(<ProjectBundlesPage />)
    await screen.findByText('No projects yet')
    fireEvent.click(screen.getByRole('button', { name: 'Add project' }))
    const projectSource = screen.getByLabelText('Folder or Git URL')
    fireEvent.change(projectSource, {
      target: { value: '/work/payments' },
    })
    fireEvent.click(within(projectSource.closest('form')!).getByRole('button', { name: 'Add project' }))

    expect(await screen.findByRole('button', { name: /Open project Payments Platform/ })).toBeInTheDocument()
  })

  it('syncs managed Git projects and confirms completion', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Sync project' }))

    expect(await screen.findByText('Project synced.')).toBeInTheDocument()
  })

  it('offers recovery for an unavailable Git Project and explains why sessions are blocked', async () => {
    const unavailable = {
      ...managedProject,
      health: { status: 'unavailable' as const, code: 'project_manifest_unavailable' },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [unavailable] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    expect(screen.getByRole('alert')).toHaveTextContent('Project files are unavailable')
    expect(screen.getByRole('button', { name: 'Retry sync' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New session' })).toBeDisabled()
  })

  it('explains which files removal preserves', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [localProject] })
      .mockResolvedValue({ projects: [] })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove from Kiro Crew' }))

    expect(await screen.findByText(
      'Folders you added stay on disk. Kiro Crew removes only storage it created for this project.',
    )).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove project' }))

    await waitFor(() => expect(api.removeProjectBundle).toHaveBeenCalledWith(localProject.id))
    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
  })

  it('activates bundled capabilities after an explicit trust action', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [localProject] })
      .mockResolvedValue({
        projects: [{
          ...localProject,
          capabilities: { ...localProject.capabilities, active: true },
        }],
      })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))
    expect(screen.getByText('reviewer')).toBeInTheDocument()
    expect(screen.getByText('release-manager')).toBeInTheDocument()
    expect(screen.getByText('docs')).toBeInTheDocument()
    expect(screen.getByText('"uvx" "docs-mcp" "--readonly"')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Trust and activate' }))

    expect(api.activateProjectBundle).not.toHaveBeenCalled()
    const trustDialog = await screen.findByRole('dialog', { name: 'Trust and activate this project?' })
    expect(trustDialog).toHaveTextContent('shown on this page')
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Trust and activate' }))

    await waitFor(() => {
      expect(api.activateProjectBundle).toHaveBeenCalledWith(localProject.id, '/work/payments')
    })
    expect(await screen.findByRole('button', { name: 'Deactivate capabilities' })).toBeInTheDocument()
  })

  it('shows resolved capability counts and materialized repository paths', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({
      projects: [{
        ...localProject,
        capabilities: {
          ...localProject.capabilities,
          active: true,
          repositories: [{ source_id: 'payments-api', path: '/managed/projects/payments-api' }],
        },
      }],
    })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    expect(screen.getByText('Repository checkouts')).toBeInTheDocument()
    expect(screen.getAllByText('Repositories')).toHaveLength(1)
    expect(screen.getByText('/managed/projects/payments-api')).toBeInTheDocument()
    expect(screen.getAllByText('payments-api', { selector: '.font-medium' })).toHaveLength(2)
    expect(screen.getAllByText('2')).not.toHaveLength(0)
    expect(screen.getAllByText('3')).not.toHaveLength(0)
  })

  it('explains how to populate a newly created empty Project', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({
      projects: [{
        ...localProject,
        sources: [],
        context: { agents: [], skills: [], mcp: null },
        capabilities: {
          ...localProject.capabilities,
          agents: 0,
          skills: 0,
          mcp_servers: 0,
          repos: 0,
          repositories: [],
          agent_names: [],
          mcp_server_details: [],
        },
      }],
    })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    expect(screen.getByText(/Edit project\.yaml in the Project folder/)).toBeInTheDocument()
  })
})
