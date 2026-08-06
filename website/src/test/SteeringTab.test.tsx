/**
 * SteeringTab — the Steering tab under Agent Capabilities.
 *
 * Pins: both steering sources are listed with provenance badges, selecting a
 * file renders its markdown, Edit round-trips the raw content through the
 * update endpoint, Delete confirms first, and the create dialog forwards the
 * chosen scope.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  steeringFiles: vi.fn(),
  steeringFile: vi.fn(),
  createSteering: vi.fn(),
  updateSteering: vi.fn(),
  deleteSteering: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

import SteeringTab from '../pages/overview/SteeringTab'

const FILES = {
  files: [
    { key: 'user/personal.md', name: 'personal.md', rel: 'personal.md', source: 'user', path: '~/.kiro/steering/personal.md', size: 12, description: 'Personal' },
    { key: 'workspace/api.md', name: 'api.md', rel: 'api.md', source: 'workspace', path: '~/proj/.kiro/steering/api.md', size: 20, description: 'API standards' },
  ],
  roots: [
    { source: 'user', path: '~/.kiro/steering', exists: true },
    { source: 'workspace', path: '~/proj/.kiro/steering', exists: true },
  ],
  project: '~/proj',
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><SteeringTab /></QueryClientProvider>)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.steeringFiles.mockResolvedValue(FILES)
  mockApi.steeringFile.mockResolvedValue({ key: 'user/personal.md', content: '# Personal\nbody', path: '~/.kiro/steering/personal.md', source: 'user' })
  mockApi.createSteering.mockResolvedValue({ ok: true, key: 'workspace/new.md' })
  mockApi.updateSteering.mockResolvedValue({ ok: true })
  mockApi.deleteSteering.mockResolvedValue({ ok: true })
})

describe('SteeringTab', () => {
  it('lists files from both sources with scope badges', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    expect(screen.getByText('api.md')).toBeInTheDocument()
    // Each scope badge appears on its row; the selected file repeats it in the
    // detail header, so assert on presence rather than a single match.
    expect(screen.getAllByText('Global').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Workspace').length).toBeGreaterThan(0)
    expect(screen.getByText('Steering (2)')).toBeInTheDocument()
  })

  it('auto-selects the first file and renders its markdown', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.steeringFile).toHaveBeenCalledWith('user/personal.md'))
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('# Personal body'))
  })

  it('shows an empty state naming both search roots when nothing is found', async () => {
    mockApi.steeringFiles.mockResolvedValue({ files: [], roots: FILES.roots, project: '~/proj' })
    renderTab()
    await waitFor(() => expect(screen.getByText('No steering files yet')).toBeInTheDocument())
    expect(screen.getByText(/~\/\.kiro\/steering/)).toBeInTheDocument()
  })

  it('Edit loads the raw content into a textarea and Save posts it back', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Edit')).toBeEnabled())
    fireEvent.click(screen.getByText('Edit'))
    const editor = screen.getByLabelText('Edit personal.md') as HTMLTextAreaElement
    expect(editor.value).toBe('# Personal\nbody')
    fireEvent.change(editor, { target: { value: '# Personal\nchanged' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# Personal\nchanged'))
  })

  it('Delete confirms before calling the API', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(mockApi.deleteSteering).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSteering).toHaveBeenCalledWith('user/personal.md'))
    confirmSpy.mockRestore()
  })

  it('create dialog forwards name, content and scope', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'new.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('new.md', expect.stringContaining('# Title'), 'workspace'))
  })

  it('defaults the create scope to global when no project is set', async () => {
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project: '' })
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'g.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('g.md', expect.any(String), 'user'))
  })

  // ---- Scope dropdown (migrated off a native <select> with a disabled option) ----

  it('keeps the id its visible "Scope" label points at, and is named by it', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))
    // The htmlFor/id pair survived the migration (SearchableSelect takes an id),
    // so clicking the visible "Scope" text still reaches the trigger.
    expect(screen.getByRole('button', { name: 'Scope' })).toHaveAttribute('id', 'steering-new-scope')
  })

  it('offers the workspace scope but refuses to select it when no project is set', async () => {
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project: '' })
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))

    const trigger = screen.getByRole('button', { name: 'Scope' })
    fireEvent.click(trigger)
    // Named, not bare: the file list on the page behind the dialog is a listbox too.
    await screen.findByRole('listbox', { name: 'Scope' })

    // Still LISTED — the point of a per-option disabled over dropping the row is
    // that "workspace scope exists, you just have no project set" stays visible.
    const workspace = screen.getByRole('option', { name: /Workspace/ })
    // `aria-disabled`, NOT the `disabled` attribute: a disabled button cannot take
    // focus, which would strand ArrowDown in the filter box and make the rows
    // below this one keyboard-unreachable. The row stays focusable and announced
    // as disabled, and the commit path refuses it.
    expect(workspace).toHaveAttribute('aria-disabled', 'true')
    expect(workspace).not.toBeDisabled()
    expect(workspace).toHaveTextContent('(no project set)')

    fireEvent.click(workspace)
    expect(trigger).toHaveTextContent(/Global/)

    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'g.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('g.md', expect.any(String), 'user'))
  })

  it('commits a scope change through the dropdown', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))

    const trigger = screen.getByRole('button', { name: 'Scope' })
    expect(trigger).toHaveTextContent(/Workspace/)
    fireEvent.click(trigger)
    await screen.findByRole('listbox', { name: 'Scope' })
    fireEvent.click(screen.getByRole('option', { name: /Global/ }))
    await waitFor(() => expect(trigger).toHaveTextContent(/Global/))

    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'new.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('new.md', expect.any(String), 'user'))
  })

  it('surfaces mutation errors inline', async () => {
    mockApi.deleteSteering.mockRejectedValue(new Error('restricted session cannot modify steering files'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(screen.getByText('restricted session cannot modify steering files')).toBeInTheDocument())
  })

  it('does not serve a deleted file\'s cached content to a recreated file', async () => {
    // A delete that only invalidates ['steering'] leaves the old detail in
    // cache under the same key (gcTime retains it, and it is served stale on
    // re-select), so the editor would load the deleted file's body and a save
    // would overwrite the new file.
    mockApi.steeringFile
      .mockResolvedValueOnce({ key: 'user/personal.md', content: 'OLD deleted body', path: '~/x', source: 'user' })
      .mockResolvedValue({ key: 'user/personal.md', content: 'NEW recreated body', path: '~/x', source: 'user' })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('OLD deleted body'))

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSteering).toHaveBeenCalled())

    fireEvent.click(screen.getByText('New Steering File'))
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'personal.md' } })
    mockApi.createSteering.mockResolvedValue({ ok: true, key: 'user/personal.md' })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('NEW recreated body'))
  })

  it('filters the list', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('api.md')).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText('Filter steering files…'), { target: { value: 'api' } })
    await waitFor(() => expect(screen.queryByText('personal.md')).not.toBeInTheDocument())
    // Selection follows the filter, so api.md shows in both the row and header.
    expect(screen.getAllByText('api.md').length).toBeGreaterThan(0)
  })
})
