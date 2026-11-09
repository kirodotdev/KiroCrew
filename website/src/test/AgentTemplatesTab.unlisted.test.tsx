/**
 * AgentTemplatesTab: the files the list does not show.
 *
 * A spec file that does not read, or that declares a name another listed file
 * already holds, has no row, yet creating its name is refused. The tab lists
 * each one under "Files not shown" with why, offers Delete when the server can
 * reach the file by its stem alone (and the instruction to rename or delete it
 * by hand otherwise), routes a referenced refusal to the same reference dialog
 * a row's delete opens, and a refused create names the file that holds the name
 * (and the row it is listed as, when that row carries another name).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  agentTemplates: vi.fn(),
  agentDetail: vi.fn(),
  agentPatch: vi.fn(),
  agentTemplateCreate: vi.fn(),
  agentTemplateDelete: vi.fn(),
  createKirocrewAgent: vi.fn(),
  kirocrewAgents: vi.fn(),
  defaultAgent: vi.fn(),
  setDefaultAgent: vi.fn(),
  skillsCatalog: vi.fn(),
  skills: vi.fn(),
}))
/** Mirrors the real `ApiError`: `body` is the RAW response text. */
const StubApiError = vi.hoisted(() => class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body: unknown = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = typeof body === 'string' ? body : JSON.stringify(body)
  }
})

vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))
vi.mock('../store', () => ({ useAppDispatch: () => vi.fn(), useAppSelector: () => [] }))
vi.mock('../store/chatSlice', () => ({ createSlot: vi.fn() }))
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => vi.fn() }
})
vi.mock('../hooks/useAvailableModels', () => ({ useAvailableModels: () => [] }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../components/AgentSkillsEditor', () => ({ default: () => <div data-testid="skills-editor" /> }))

import AgentTemplatesTab from '../pages/overview/AgentTemplatesTab'
import type { TemplateRow } from '../pages/overview/AgentTemplatesTab'
import type { UnlistedSpec } from '../pages/overview/UnlistedSpecFiles'
import '../i18n/all'

const VISIBLE: TemplateRow = {
  name: 'atlas', filename: 'atlas.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'builtin', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, default_eligible: true, used_by: [],
}
const BROKEN: UnlistedSpec = { filename: 'broken.json', stem: 'broken', reason: 'unreadable', deletable: true }
const COPY: UnlistedSpec = {
  filename: 'atlas_v2.json', stem: 'atlas_v2', reason: 'duplicate_name',
  declared_name: 'atlas', listed_as: 'atlas.json', deletable: true,
}
const NOTES: UnlistedSpec = { filename: 'notes.md', stem: 'notes', reason: 'unreadable', deletable: false }

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><AgentTemplatesTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const section = () => screen.findByRole('region', { name: 'Files not shown' })

/** Press an entry's Delete file, then answer the in-app confirm it opens. */
const deleteFrom = async (region: HTMLElement, file: string, answer: 'Delete file' | 'Cancel') => {
  fireEvent.click(within(region).getByRole('button', { name: `Delete “${file}”` }))
  const ask = await screen.findByRole('dialog', { name: `Delete the file “${file}”?` })
  expect(ask).toHaveTextContent('a backup ending in .bak stays beside it')
  fireEvent.click(within(ask).getByRole('button', { name: answer }))
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.agentTemplates.mockResolvedValue({ templates: [VISIBLE], unlisted: [BROKEN, COPY, NOTES] })
  mockApi.agentDetail.mockResolvedValue({ name: 'atlas', description: '', prompt: '', tools: [], allowedTools: [] })
  mockApi.agentTemplateDelete.mockResolvedValue({ ok: true })
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: 'kirocrew' })
  mockApi.defaultAgent.mockResolvedValue({ default_agent: 'default', default_template: 'kirocrew', overridden: false })
})

describe('AgentTemplatesTab: files not shown', () => {
  it('lists each hidden file with why, outside the list of templates', async () => {
    renderTab()
    const region = await section()
    expect(within(region).getByText('broken.json')).toBeInTheDocument()
    // broken.json and notes.md
    expect(within(region).getAllByText(/^Cannot be read as an agent spec/)).toHaveLength(2)
    expect(within(region).getByText('Declares the name “atlas”, which “atlas.json” already uses, so only that file is listed.')).toBeInTheDocument()
    // A file the server cannot reach by its stem alone gets the instruction,
    // not a Delete that could only be refused.
    expect(within(region).getByText('Rename or delete this file in ~/.kiro/agents to free its name.')).toBeInTheDocument()
    expect(within(region).getAllByRole('button')).toHaveLength(2)
    expect(within(region).queryByRole('button', { name: 'Delete “notes.md”' })).toBeNull()
    // The entries are files, not options of the template listbox.
    expect(within(screen.getByRole('listbox')).queryByText('broken.json')).toBeNull()
  })

  it('deletes a hidden file by its stem after a confirm, then reloads the roster', async () => {
    renderTab()
    const region = await section()
    mockApi.agentTemplates.mockResolvedValue({ templates: [VISIBLE], unlisted: [COPY, NOTES] })
    await deleteFrom(region, 'broken.json', 'Delete file')
    await waitFor(() => expect(mockApi.agentTemplateDelete).toHaveBeenCalledWith('broken'))
    // Gone once the roster is read again, and the delete is reported in place.
    await waitFor(() => expect(within(region).queryByText('broken.json')).toBeNull())
    expect(within(region).getByRole('status')).toHaveTextContent('Deleted “broken.json”.')
  })

  it('sends nothing when the confirm is declined', async () => {
    renderTab()
    const region = await section()
    await deleteFrom(region, 'atlas_v2.json', 'Cancel')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Delete the file “atlas_v2.json”?' })).toBeNull())
    expect(mockApi.agentTemplateDelete).not.toHaveBeenCalled()
  })

  it('says a same-name copy goes alone: the listed file and its agent stay', async () => {
    renderTab()
    const region = await section()
    const press = within(region).getByRole('button', { name: 'Delete “atlas_v2.json”' })
    // A file, not an agent: the button says which.
    expect(press).toHaveTextContent(/^\s*Delete file$/)
    fireEvent.click(press)
    const ask = await screen.findByRole('dialog', { name: 'Delete the file “atlas_v2.json”?' })
    expect(ask).toHaveTextContent('“atlas.json”')
    expect(ask).toHaveTextContent('“atlas”')
    expect(within(ask).getByRole('button', { name: 'Delete file' })).toBeInTheDocument()
  })

  it('opens the reference list when the server refuses the delete as referenced', async () => {
    mockApi.agentTemplateDelete.mockRejectedValue(new StubApiError(409, 'referenced', {
      code: 'template_referenced',
      references: [{ kind: 'folder', id: 'f-1', label: 'Reviews' }],
    }))
    renderTab()
    const region = await section()
    await deleteFrom(region, 'broken.json', 'Delete file')
    const dialog = await screen.findByRole('dialog', { name: /Can’t delete broken yet/ })
    expect(within(dialog).getByText('Reviews')).toBeInTheDocument()
  })

  it('reports any other refused delete beside the entries', async () => {
    mockApi.agentTemplateDelete.mockRejectedValue(new StubApiError(409, "'broken' matches more than one template file", {
      code: 'ambiguous_template_name', error: "'broken' matches more than one template file",
    }))
    renderTab()
    const region = await section()
    await deleteFrom(region, 'broken.json', 'Delete file')
    expect(await within(region).findByRole('alert')).toHaveTextContent('matches more than one template file')
  })

  it('still lists hidden files when no template is listed at all', async () => {
    mockApi.agentTemplates.mockResolvedValue({ templates: [], unlisted: [BROKEN] })
    renderTab()
    const region = await section()
    expect(screen.getByText('No custom agents yet')).toBeInTheDocument()
    expect(within(region).getByRole('button', { name: 'Delete “broken.json”' })).toBeInTheDocument()
  })

  it('renders no section when nothing is hidden', async () => {
    mockApi.agentTemplates.mockResolvedValue({ templates: [VISIBLE], unlisted: [] })
    renderTab()
    await screen.findByRole('option', { name: /^atlas/ })
    expect(screen.queryByRole('region', { name: 'Files not shown' })).toBeNull()
  })

  it('names the file that holds a refused name in the create dialog', async () => {
    mockApi.agentTemplateCreate.mockRejectedValue(new StubApiError(409, 'taken', {
      code: 'name_taken', error: "'broken' is taken by ~/.kiro/agents/broken.json", unlisted: BROKEN,
    }))
    renderTab()
    await screen.findByRole('option', { name: /^atlas/ })
    fireEvent.click(screen.getByRole('button', { name: /New custom agent/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New custom agent' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'broken' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'That name is taken by the file “broken.json”, which the list does not show. Free it under Files not shown on this tab.',
    )
  })

  it('names the file and its row when a listed file holds the refused name by its stem', async () => {
    // A copy saved as src-custom.json that still declares "src" is the row
    // "src"; no row is labelled "src-custom", yet the copy takes that name.
    mockApi.agentTemplateCreate.mockRejectedValue(new StubApiError(409, 'taken', {
      code: 'name_taken',
      error: "'src-custom' is taken by ~/.kiro/agents/src-custom.json, which the list shows as 'src'",
      listed: { filename: 'src-custom.json', name: 'src' },
    }))
    renderTab()
    await screen.findByRole('option', { name: /^atlas/ })
    fireEvent.click(screen.getByRole('button', { name: /New custom agent/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New custom agent' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'src-custom' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'That name is taken by the file “src-custom.json”, which the list shows as “src” because that is the name it declares. Change the name inside the file, or choose another name.',
    )
  })

  it('keeps the plain refusal for a name a listed template holds', async () => {
    mockApi.agentTemplateCreate.mockRejectedValue(new StubApiError(409, 'taken', {
      code: 'name_taken', error: "A template named 'atlas' already exists",
    }))
    renderTab()
    await screen.findByRole('option', { name: /^atlas/ })
    fireEvent.click(screen.getByRole('button', { name: /New custom agent/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New custom agent' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'atlas' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('That name is already taken by a custom agent or a bot.')
  })
})
