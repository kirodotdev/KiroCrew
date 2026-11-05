/**
 * ProjectList: the paper landing view's create, clone, delete and compiler-install actions.
 *
 * Each action must do what the operator sees: Create and Clone open the new paper with its server
 * name, a refused create or clone shows the reason in the banner and keeps the typed name, a failed
 * compiler install shows its message, and a failed delete leaves the paper in the list. The Papyrus
 * API is mocked at the network boundary; the page, its mutations and its banner are real.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ProjectList from './ProjectList'
import { renderWithProviders } from '../../test/helpers'
import { papyrusApi, type Project } from './api'

vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  papyrusApi: {
    health: vi.fn(),
    listProjects: vi.fn(),
    createProject: vi.fn(),
    cloneProject: vi.fn(),
    deleteProject: vi.fn(),
    provisionCompiler: vi.fn(),
  },
}))

const mocked = vi.mocked(papyrusApi as unknown as Record<string, ReturnType<typeof vi.fn>>)

function project(name: string): Project {
  return { name, title: name, modified: 1_700_000_000, has_pdf: false, generation: `gen-${name}` }
}

function mount(projects: Project[], onOpenProject = vi.fn()) {
  mocked.listProjects.mockResolvedValue({ projects })
  mocked.health.mockResolvedValue({ compiler: 'tectonic' })
  renderWithProviders(<ProjectList onOpenProject={onOpenProject} />)
  return onOpenProject
}

beforeEach(() => {
  for (const fn of Object.values(mocked)) fn.mockReset()
})

describe('ProjectList create', () => {
  it('creates the paper under the typed name and opens it', async () => {
    mocked.createProject.mockResolvedValue({ name: 'thesis', main_file: 'main.tex' })
    const onOpen = mount([])
    await userEvent.type(await screen.findByLabelText('New paper name'), '  thesis  ')
    await userEvent.click(screen.getByRole('button', { name: /Create/ }))
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith('thesis'))
    expect(mocked.createProject).toHaveBeenCalledWith('thesis')
  })

  it('shows the refusal in the banner and keeps the typed name', async () => {
    mocked.createProject.mockRejectedValue(new Error('disk full'))
    const onOpen = mount([])
    const field = await screen.findByLabelText('New paper name')
    await userEvent.type(field, 'thesis')
    await userEvent.click(screen.getByRole('button', { name: /Create/ }))
    expect(await screen.findByText('disk full')).toBeTruthy()
    expect(onOpen).not.toHaveBeenCalled()
    expect(field).toHaveValue('thesis')
  })
})

describe('ProjectList clone', () => {
  it('clones the repository and opens the paper the server named', async () => {
    mocked.cloneProject.mockResolvedValue({ name: 'paper-copy', main_file: 'main.tex' })
    const onOpen = mount([])
    await userEvent.type(await screen.findByLabelText('Repository URL'), 'https://git.example/paper.git')
    await userEvent.click(screen.getByRole('button', { name: 'Clone' }))
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith('paper-copy'))
    expect(mocked.cloneProject).toHaveBeenCalledWith('https://git.example/paper.git')
  })

  it('reports a failed clone and does not open anything', async () => {
    mocked.cloneProject.mockRejectedValue(new Error('repository not found'))
    const onOpen = mount([])
    await userEvent.type(await screen.findByLabelText('Repository URL'), 'https://git.example/none.git')
    await userEvent.click(screen.getByRole('button', { name: 'Clone' }))
    expect(await screen.findByText('repository not found')).toBeTruthy()
    expect(onOpen).not.toHaveBeenCalled()
  })
})

describe('ProjectList delete', () => {
  it('keeps the paper in the list and shows the reason when the delete is refused', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocked.deleteProject.mockRejectedValue(new Error('paper is locked'))
    mount([project('draft')])
    await userEvent.click(await screen.findByRole('button', { name: 'Delete draft' }))
    expect(mocked.deleteProject).toHaveBeenCalledWith('draft')
    expect(await screen.findByText('paper is locked')).toBeTruthy()
    expect(screen.getByText('draft')).toBeTruthy()
  })
})

describe('ProjectList compiler install', () => {
  it('shows the install reason when the compiler cannot be provisioned', async () => {
    mocked.listProjects.mockResolvedValue({ projects: [] })
    mocked.health.mockResolvedValue({
      compiler: '',
      managed: { supported: true, job: { state: 'idle', bytes_downloaded: 0, bytes_total: 0 } },
    })
    mocked.provisionCompiler.mockRejectedValue(new Error('network unreachable'))
    renderWithProviders(<ProjectList onOpenProject={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Install compiler' }))
    expect(mocked.provisionCompiler).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('network unreachable')).toBeTruthy()
  })
})
