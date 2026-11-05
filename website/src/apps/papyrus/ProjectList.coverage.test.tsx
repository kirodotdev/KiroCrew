/**
 * ProjectList: the paper landing view's create, clone, delete and compiler-install actions.
 *
 * Each action must do what the operator sees: Create and Clone open the new paper with its server
 * name, a refused create or clone shows the reason in the banner and keeps the typed name, a failed
 * compiler install shows its message, and a failed delete leaves the paper in the list. The Papyrus
 * API is mocked at the network boundary; the page, its mutations and its banner are real.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ProjectList from './ProjectList'
import { renderWithProviders } from '../../test/helpers'
import { confirmAction } from '../../test/confirmAction'
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
    mocked.deleteProject.mockRejectedValue(new Error('paper is locked'))
    mount([project('draft')])
    await userEvent.click(await screen.findByRole('button', { name: 'Delete draft' }))
    await confirmAction('Delete')
    await waitFor(() => expect(mocked.deleteProject).toHaveBeenCalledWith('draft'))
    expect(await screen.findByText('paper is locked')).toBeTruthy()
    expect(screen.getByText('draft')).toBeTruthy()
  })
})

describe('ProjectList confirmation safety', () => {
  it('keeps the paper when deletion is cancelled', async () => {
    mount([project('draft')])
    fireEvent.click(await screen.findByRole('button', { name: 'Delete draft' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.querySelector('p')).toHaveTextContent('Delete “draft” and all of its files? This cannot be undone.')
    expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)).toHaveTextContent(/^Delete$/)
    await confirmAction('Cancel')
    expect(mocked.deleteProject).not.toHaveBeenCalled()
    expect(screen.getByText('draft')).toBeInTheDocument()
  })

  it('keeps an unbroken paper name in the body with a plain Delete action', async () => {
    const name = 'p'.repeat(64)
    mount([project(name)])
    fireEvent.click(await screen.findByRole('button', { name: `Delete ${name}` }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog.querySelector('p')).toHaveTextContent(`Delete “${name}” and all of its files? This cannot be undone.`)
    expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)).toHaveTextContent(/^Delete$/)
    expect(within(dialog).getByRole('button', { name: 'Delete', exact: true })).toHaveTextContent(/^Delete$/)
    expect(within(dialog).getByRole('button', { name: 'Cancel', exact: true })).toBeInTheDocument()
    await confirmAction('Cancel')
    expect(mocked.deleteProject).not.toHaveBeenCalled()
  })

  it('deletes the captured paper after the project list becomes empty', async () => {
    mocked.deleteProject.mockResolvedValue({ ok: true })
    mocked.listProjects.mockResolvedValue({ projects: [project('draft')] })
    mocked.health.mockResolvedValue({ compiler: 'tectonic' })
    const { queryClient } = renderWithProviders(<ProjectList onOpenProject={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete draft' }))
    await screen.findByRole('dialog')
    act(() => queryClient.setQueryData(['papyrus', 'projects'], { projects: [] }))
    await waitFor(() => expect(screen.queryByText('draft')).not.toBeInTheDocument())
    await confirmAction('Delete')
    await waitFor(() => expect(mocked.deleteProject).toHaveBeenCalledWith('draft'))
  })

  it('ignores another delete while the paper mutation is pending', async () => {
    let finish!: (value: { ok: boolean }) => void
    mocked.deleteProject.mockReturnValue(new Promise(resolve => { finish = resolve }))
    mount([project('draft'), project('other')])
    fireEvent.click(await screen.findByRole('button', { name: 'Delete draft' }))
    await confirmAction('Delete')
    const other = screen.getByRole('button', { name: 'Delete other' })
    await waitFor(() => expect(other).toBeDisabled())
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    fireEvent.click(other)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(mocked.deleteProject).toHaveBeenCalledTimes(1)
    await act(async () => finish({ ok: true }))
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
