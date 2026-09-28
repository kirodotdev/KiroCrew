/**
 * AgentTemplatesTab — the Agent templates tab under Agent Capabilities.
 *
 * Pins what a management page must not get wrong: the roster groups by origin
 * (Mine / Private copies / From packages / Built-in), a read-only row explains
 * why and offers "Duplicate to edit" instead of Delete, an owned row saves the
 * definition keys through the detail PATCH and refuses to leave a dirty draft
 * silently (including under a background refetch), a delete that the server
 * refuses as referenced opens the reference list instead of a bare error, create
 * sends `from` only for a duplicate, and "Chat with this template" creates a
 * slot in the TEMPLATE namespace. The secondary actions live in one overflow
 * menu (the row holds two controls), so the tests open it the way Radix lets
 * jsdom: keyboard activation of the trigger.
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
  skillsCatalog: vi.fn(),
  skills: vi.fn(),
}))
/** Mirrors the real `ApiError`: `body` is the RAW response text, so a test that
 *  hands it an object would pass where the real client fails. */
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
const mockDispatch = vi.hoisted(() => vi.fn())
const mockNavigate = vi.hoisted(() => vi.fn())
const mockCreateSlot = vi.hoisted(() => vi.fn((opts: unknown) => ({ type: 'chat/createSlot', payload: opts })))

vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))
vi.mock('../store', () => ({ useAppDispatch: () => mockDispatch, useAppSelector: () => [] }))
vi.mock('../store/chatSlice', () => ({ createSlot: mockCreateSlot }))
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})
vi.mock('../hooks/useAvailableModels', () => ({ useAvailableModels: () => [{ name: 'claude-x' }, { name: 'gpt-y' }] }))
let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))
// The shell's leave gate: every in-app link this tab owns must route through it
// (with its target, so a link to the current page skips the ask).
const mockLeave = vi.fn((perform: () => void, _to?: string) => perform())
vi.mock('../components/NavigationLeaveGuard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../components/NavigationLeaveGuard')>()),
  useGuardedLeave: () => mockLeave,
}))
// The stub exposes the editor's `onChange(name, skills)` as a button, so a
// test can play a completed skill save without the real popover.
vi.mock('../components/AgentSkillsEditor', () => ({
  default: (p: { skills: string[]; onChange: (name: string, skills: string[]) => void }) => (
    <div data-testid="skills-editor" data-skills={p.skills.join(',')}>
      <button onClick={() => p.onChange('reviewer', [...p.skills, 'added'])}>stub-save-skill</button>
    </div>
  ),
}))

import AgentTemplatesTab from '../pages/overview/AgentTemplatesTab'
import type { TemplateRow } from '../pages/overview/AgentTemplatesTab'

const row = (over: Partial<TemplateRow>): TemplateRow => ({
  name: 'x', filename: 'x.json', description: '', model: '', skills: [], mcp_servers: [],
  source: 'builtin', package: '', scope: 'global', kirocrew_owned: false, forked_from: '', private_to: '',
  read_only: null, used_by: [], ...over,
})
const MINE = row({ name: 'reviewer', filename: 'reviewer.json', description: 'Careful reviewer', model: 'claude-x', used_by: [{ kind: 'crew', id: 'pr-bot', label: 'pr-bot' }, { kind: 'private_copy', id: 'pr-bot', label: 'pr-bot' }] })
// The same template with nothing pointing at it: the row a delete can go through for.
const FREE = { ...MINE, used_by: [] }
const PKG = row({ name: 'atlas', filename: 'Pkg-atlas.json', source: 'package', package: 'Pkg', read_only: 'package' })
const RUNTIME = row({ name: 'kirocrew-worker', filename: 'kirocrew-worker.json', kirocrew_owned: true, read_only: 'runtime' })
const COPY = row({ name: 'pr-bot', filename: 'pr-bot.json', forked_from: 'reviewer', private_to: 'pr-bot', read_only: 'private_copy' })

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><AgentTemplatesTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const option = (name: string) => screen.getByRole('option', { name: new RegExp(`^${name}\\b`) })

/** Open the detail pane's overflow menu. Radix opens on keyboard activation, a
 *  path jsdom handles, unlike the PointerEvent-driven mouse open. */
const openMore = () => {
  fireEvent.keyDown(screen.getByRole('button', { name: 'More actions' }), { key: 'Enter' })
  return screen.findAllByRole('menuitem')
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockDispatch.mockReset(); mockNavigate.mockReset(); mockCreateSlot.mockClear()
  mockApi.agentTemplates.mockResolvedValue({ templates: [MINE, PKG, RUNTIME, COPY] })
  mockApi.agentDetail.mockImplementation(async (name: string) => ({
    name, description: name === 'reviewer' ? 'Careful reviewer' : 'shipped', model: 'claude-x',
    prompt: `prompt of ${name}`, tools: ['fs_read', '@docs/search'], allowedTools: ['@docs/search'],
    resources: ['file://AGENTS.md', 'skill://x'], mcpServers: { docs: { command: 'docs-mcp' } }, skills: ['adversarial-review'],
  }))
  mockApi.agentPatch.mockResolvedValue({ ok: true })
  mockApi.agentTemplateCreate.mockResolvedValue({ ok: true, name: 'pr-summarizer', filename: 'pr-summarizer.json' })
  mockApi.agentTemplateDelete.mockResolvedValue({ ok: true })
  mockApi.createKirocrewAgent.mockResolvedValue({ ok: true })
  mockDispatch.mockImplementation(() => ({ unwrap: () => Promise.resolve({ key: 'chat-1' }) }))
})

describe('AgentTemplatesTab roster', () => {
  it('groups rows by origin and marks read-only rows', async () => {
    renderTab()
    await screen.findByRole('option', { name: /^reviewer/ })
    const groups = screen.getAllByRole('group').map(g => g.getAttribute('aria-label'))
    expect(groups).toEqual(['Mine', 'Crewmate overrides', 'From packages', 'Built-in'])
    expect(within(screen.getByRole('group', { name: 'Mine' })).getByRole('option', { name: /^reviewer/ })).toBeInTheDocument()
    // The overrides group is glossed, and its row is described in the tab's
    // own word rather than the fork-written "private copy" sentence.
    const overrides = screen.getByRole('group', { name: 'Crewmate overrides' })
    expect(within(overrides).getByText(/One crewmate’s edited version/)).toBeInTheDocument()
    expect(within(overrides).getByRole('option', { name: /^pr-bot/ })).toHaveTextContent('Crewmate pr-bot’s override of reviewer')
    // One fact, one word: the count badge on the source row says "override"
    // the way the group heading and the banner do, not "Overridden by".
    expect(within(screen.getByRole('group', { name: 'Mine' })).getByText('1 crewmate override')).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'From packages' })).getByRole('option', { name: /atlas/ })).toBeInTheDocument()
    // The first row is auto-selected and its detail read fires.
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
  })

  it('offers Delete on an owned template and Duplicate to edit on a package one', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    // The row itself holds two controls: the primary action and the menu.
    const row = screen.getByRole('button', { name: 'More actions' }).parentElement!
    expect(within(row).getAllByRole('button')).toHaveLength(2)
    let items = await openMore()
    expect(items.map(i => i.textContent)).toEqual([
      expect.stringContaining('Enroll as crewmate'), 'Duplicate', 'Delete',
    ])
    // The enroll item says what enrolling starts. `pr-bot` runs this template
    // under its own name, so a crewmate NAMED reviewer can still be enrolled.
    expect(items[0]).toHaveTextContent('Adds a crewmate with its own memory')
    expect(items[0]).not.toHaveAttribute('data-disabled')
    fireEvent.keyDown(items[0], { key: 'Escape' })

    fireEvent.click(option('atlas'))
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('atlas'))
    items = await openMore()
    expect(items.map(i => i.textContent)).toEqual([
      expect.stringContaining('Adds a crewmate with its own memory'), 'Duplicate to edit',
    ])
    fireEvent.keyDown(items[0], { key: 'Escape' })
    // The read-only banner carries the reason once and the remedy beside it;
    // the header subtitle only says "Read-only".
    expect(screen.getAllByText(/Installed by a package/)).toHaveLength(1)
    expect(screen.getByText((_, el) => el?.tagName === 'DIV' && el.textContent === 'Read-only · ~/.kiro/agents/Pkg-atlas.json')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Duplicate to edit/ })).toBeInTheDocument()
    // The banner leads with a bold two-word reason so the four read-only
    // states can be told apart at a glance.
    expect(screen.getByText('From a package').tagName).toBe('STRONG')
    // Read-only tags are inert, so the Tools caption must not tell the reader
    // to click one; it says read-only, like Resources.
    expect(screen.queryByText(/Click a tool’s tag/)).toBeNull()
    expect(screen.getAllByText('read-only here').length).toBeGreaterThanOrEqual(2)
    // A read-only prompt is a visibly locked block, not a disabled textbox
    // that looks like a normal editor.
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Prompt' })).toBeNull())
    const locked = screen.getByLabelText('Prompt')
    expect(locked.tagName).toBe('PRE')
    expect(locked).toHaveTextContent('prompt of atlas')
  })

  it('names a chat folder among the references, and an enrolled template says so', async () => {
    mockApi.agentTemplates.mockResolvedValue({
      templates: [row({ name: 'reviewer', filename: 'reviewer.json', used_by: [
        { kind: 'folder', id: 'f-1', label: 'Reviews' }, { kind: 'crew', id: 'reviewer', label: 'reviewer' },
        { kind: 'webhook', id: 'wht_1', label: 'ci-hook' }, { kind: 'private_copy', id: 'pr-bot-copy', label: 'pr-bot' },
      ] })],
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    confirm.mockClear()
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    // The usage line names every holder kind the guard counts, up front, and
    // each is a drill-in to where that holder lives. One fact, one phrasing:
    // the line says "Runs 1 crewmate" exactly as the list badge does.
    expect(screen.getAllByText(/Runs 1 crewmate/).length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(/Runs as/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '1 chat folder' }))
    expect(mockNavigate).toHaveBeenLastCalledWith('/chat')
    // ...and each one asks the shell's leave gate first, naming its target.
    expect(mockLeave).toHaveBeenLastCalledWith(expect.any(Function), '/chat')
    fireEvent.click(screen.getByRole('button', { name: '1 webhook' }))
    expect(mockNavigate).toHaveBeenLastCalledWith('/webhooks')
    const items = await openMore()
    expect(items[0]).toHaveTextContent('Already enrolled as a crewmate.')
    expect(items[0]).toHaveAttribute('data-disabled')
    fireEvent.click(items.find(i => i.textContent === 'Delete')!)
    // The row already shows holders, so the "nothing points at it" confirm is
    // never asked and no request is sent: the reference list opens directly.
    const dialog = await screen.findByRole('dialog', { name: /Can’t delete reviewer yet/ })
    expect(confirm).not.toHaveBeenCalled()
    expect(mockApi.agentTemplateDelete).not.toHaveBeenCalled()
    expect(within(dialog).getByText('Chat folder')).toBeInTheDocument()
    expect(within(dialog).getByText('Reviews')).toBeInTheDocument()
    // A private copy is named by the copy with its crew as a gloss, and links to
    // that crew's pane -- it is not a dead end.
    expect(within(dialog).getByText('pr-bot-copy')).toBeInTheDocument()
    expect(within(dialog).getByText('crewmate pr-bot')).toBeInTheDocument()
    fireEvent.click(within(dialog).getAllByRole('button', { name: /Open/ }).at(-1)!)
    expect(mockNavigate).toHaveBeenCalledWith('/members?member=pr-bot')
  })

  it('sends a private copy to its crewmate instead of offering Duplicate to edit', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(option('pr-bot'))
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('pr-bot'))
    expect(screen.queryByRole('button', { name: /Duplicate to edit/ })).toBeNull()
    // Said as what it is, not as a template nobody enrolled -- in the list row
    // and again in the detail's usage line.
    expect(screen.getAllByText('Crewmate pr-bot’s override of reviewer')).toHaveLength(2)
    expect(screen.queryByText('Not enrolled as a crewmate')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Open crewmate/ }))
    expect(mockNavigate).toHaveBeenCalledWith('/members?member=pr-bot')
    expect(mockLeave).toHaveBeenLastCalledWith(expect.any(Function), '/members?member=pr-bot')
  })
})

describe('AgentTemplatesTab editing', () => {
  it('saves the definition keys through the detail PATCH and clears the dirty bar', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'Review carefully.' } })
    expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument()
    // Toggle auto-approval on fs_read: the worded state tag is the toggle, the
    // remove control is a separate button.
    fireEvent.click(screen.getByRole('button', { name: 'fs_read: asks first', pressed: false }))
    expect(screen.getByRole('button', { name: 'fs_read: auto-approve ✓', pressed: true })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove fs_read' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save template' }))
    // Only the keys that changed travel: tools and their marks together, the
    // prompt, and NOT the untouched description or model.
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledWith('reviewer', {
      prompt: 'Review carefully.',
      tools: ['fs_read', '@docs/search'],
      allowedTools: ['@docs/search', 'fs_read'],
    }))
    await waitFor(() => expect(screen.queryByText(/Unsaved changes/)).toBeNull())
    expect(screen.getByRole('status')).toHaveTextContent('Template saved.')
  })

  it('never resends an unchanged model, which the server would read as a pin', async () => {
    // A managed template carries a concrete model string on disk; the server
    // treats any non-empty `model` in a PATCH as an explicit pin and flips
    // model_managed off. A prompt-only save must therefore not mention it.
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'prompt only' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save template' }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))
    expect(mockApi.agentPatch).toHaveBeenCalledWith('reviewer', { prompt: 'prompt only' })
    expect(mockApi.agentPatch.mock.calls[0][1]).not.toHaveProperty('model')
  })

  it('writes a saved skill list into the detail cache before the refetch lands', async () => {
    // The editor reads `skills` from the detail query; if a save only
    // invalidated, a second toggle inside the invalidate-to-refetch window
    // would start from the stale list and PATCH the first edit away.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><AgentTemplatesTab /></MemoryRouter></QueryClientProvider>)
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    await waitFor(() => expect(screen.getByTestId('skills-editor')).toHaveAttribute('data-skills', 'adversarial-review'))
    // Hold the refetch open so the window is observable.
    let release!: () => void
    mockApi.agentDetail.mockImplementation(() => new Promise(resolve => { release = () => resolve({
      name: 'reviewer', prompt: 'prompt of reviewer', tools: ['fs_read'], allowedTools: [], resources: [], skills: ['adversarial-review', 'added'],
    }) }))
    fireEvent.click(screen.getByText('stub-save-skill'))
    // Synchronously after the save the cache holds the new list, and the
    // editor reads it on its next render -- while the refetch is still open.
    expect(qc.getQueryData<{ skills: string[] }>(['agent-templates', 'detail', 'reviewer'])?.skills).toEqual(['adversarial-review', 'added'])
    await waitFor(() => expect(screen.getByTestId('skills-editor')).toHaveAttribute('data-skills', 'adversarial-review,added'))
    expect(qc.isFetching()).toBeGreaterThan(0)
    release()
    await waitFor(() => expect(qc.isFetching()).toBe(0))
    expect(screen.getByTestId('skills-editor')).toHaveAttribute('data-skills', 'adversarial-review,added')
  })

  it('keeps a dirty draft through a background refetch of the detail', async () => {
    // The skills editor saves on its own and invalidates ['agent-templates'],
    // a prefix the detail key shares; the refetch must not reseed the editor
    // over what the user is still typing.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><AgentTemplatesTab /></MemoryRouter></QueryClientProvider>)
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'still typing' } })
    mockApi.agentDetail.mockImplementation(async (name: string) => ({
      name, prompt: `prompt of ${name}`, tools: ['fs_read'], allowedTools: [], resources: ['skill://y'], skills: ['y'],
    }))
    await qc.invalidateQueries({ queryKey: ['agent-templates'] })
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('textbox', { name: 'Prompt' })).toHaveValue('still typing')
    expect(screen.getByText(/Unsaved changes · affects 1 crewmate/)).toBeInTheDocument()
    expect(screen.getByText(/new chats use them at once; chats already running pick them up after Apply & Restart/)).toBeInTheDocument()
  })

  it('asks before a row switch discards a dirty draft', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'changed' } })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(option('atlas'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(mockApi.agentDetail).not.toHaveBeenCalledWith('atlas')
    confirmSpy.mockRestore()
  })

  it('reports a refused save inside the save bar, beside the Save button', async () => {
    mockApi.agentPatch.mockRejectedValue(new StubApiError(409, 'read only', { code: 'template_read_only' }))
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'x' } })
    const saveBtn = screen.getByRole('button', { name: 'Save template' })
    fireEvent.click(saveBtn)
    // The pane above scrolls and the bar does not: the error must share the
    // bar with the button that produced it, not sit at the top of the pane.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/read-only here\. Duplicate it/)
    expect(alert.closest('div')).toBe(saveBtn.closest('div'))
    // Discard asks first (one click must not erase a long edit for good), then
    // clears the error along with the draft. Declined, both stay.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(confirm).toHaveBeenLastCalledWith('Discard unsaved changes?')
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(prompt).toHaveValue('x')
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(prompt).toHaveValue('prompt of reviewer')
  })

  it('lists resources as plain rows, not chips', async () => {
    mockApi.agentDetail.mockImplementation(async (name: string) => ({
      name, prompt: 'p', tools: [], allowedTools: [], skills: [],
      resources: ['file://~/.kiro/steering/**/*.md', 'skill://x'],
    }))
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const item = await screen.findByText('file://~/.kiro/steering/**/*.md')
    expect(item.tagName).toBe('LI')
    expect(screen.queryByText('skill://x')).toBeNull()
  })
})

describe('AgentTemplatesTab detail robustness', () => {
  it('shows the detail error instead of Loading when the detail read is rejected', async () => {
    mockApi.agentDetail.mockRejectedValue(new StubApiError(500, 'boom'))
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    await screen.findByText('This template could not be read.')
    expect(screen.queryByText('Loading…')).toBeNull()
  })

  it('renders only string-valued MCP fields from a hand-edited spec', async () => {
    mockApi.agentDetail.mockImplementation(async (name: string) => ({
      name, prompt: 'p', tools: [], allowedTools: [], resources: [], skills: [],
      mcpServers: {
        docs: { command: { bin: 'docs-mcp' }, url: 42, type: 'stdio' },
        broken: 'not an object',
        remote: { url: 'https://mcp.example.test' },
      },
    }))
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const rows = await screen.findAllByRole('row')
    expect(rows.map(r => r.textContent)).toEqual(['docsstdio', 'broken', 'remotehttps://mcp.example.test'])
  })

  it('says why a read-only template has no prompt instead of showing an empty editor', async () => {
    mockApi.agentDetail.mockImplementation(async (name: string) => ({
      name, prompt: '', tools: [], allowedTools: [], resources: [], skills: [],
    }))
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(option('kirocrew-worker'))
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('kirocrew-worker'))
    await screen.findByText(/prompt is not stored in its file/)
    expect(screen.queryByRole('textbox', { name: 'Prompt' })).toBeNull()
    // No "0 characters" beside a note saying the prompt exists elsewhere.
    expect(screen.queryByText(/0 characters/)).toBeNull()
  })

  it('shows one Skills heading on an owned template', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    await screen.findByTestId('skills-editor')
    expect(screen.queryByRole('heading', { name: 'Skills' })).toBeNull()
  })
})

describe('AgentTemplatesTab actions', () => {
  it('starts a chat in the template namespace', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(screen.getByRole('button', { name: /Chat with this template/ }))
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/chat'))
    expect(mockCreateSlot).toHaveBeenCalledWith({ agent: 'reviewer', agent_kind: 'template' })
  })

  it('keeps Chat enabled while dirty and asks first, instead of a disabled button', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'dirty' } })
    const chat = screen.getByRole('button', { name: /Chat with this template/ })
    expect(chat).toBeEnabled()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(chat)
    expect(confirmSpy).toHaveBeenCalled()
    expect(mockCreateSlot).not.toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(chat)
    await waitFor(() => expect(mockCreateSlot).toHaveBeenCalledWith({ agent: 'reviewer', agent_kind: 'template' }))
  })

  it('shows the reference list when the server refuses a delete as referenced', async () => {
    // The roster showed no holder (so the confirm IS asked), but one landed
    // before the request: the server's refusal opens the same list.
    mockApi.agentTemplates.mockResolvedValue({ templates: [FREE, PKG, RUNTIME, COPY] })
    mockApi.agentTemplateDelete.mockRejectedValue(new StubApiError(409, 'referenced', {
      code: 'template_referenced',
      references: [{ kind: 'crew', id: 'pr-bot', label: 'pr-bot' }, { kind: 'schedule', id: 'job-1', label: 'nightly triage' }],
    }))
    // The spy is shared across this file's tests; count from here.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    confirm.mockClear()
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const items = await openMore()
    fireEvent.click(items.find(i => i.textContent === 'Delete')!)
    const dialog = await screen.findByRole('dialog', { name: /Can’t delete reviewer yet/ })
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByText('nightly triage')).toBeInTheDocument()
    expect(within(dialog).getByText('pr-bot')).toBeInTheDocument()
    // The roster is untouched: the row is still listed.
    expect(option('reviewer')).toBeInTheDocument()
  })

  it('names a reserved template id in the create dialog when the server refuses it', async () => {
    // The KAS engine keeps `default`, `vibe`, `plan`, ... for itself; the
    // server refuses them as 400 template_name_reserved_by_engine and the dialog must
    // say which word to change rather than relay the server's English text.
    mockApi.agentTemplateCreate.mockRejectedValue(
      new StubApiError(400, "'plan' is reserved", { error: "'plan' is reserved", code: 'template_name_reserved_by_engine' }),
    )
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New template' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'plan' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    await waitFor(() => expect(mockApi.agentTemplateCreate).toHaveBeenCalledWith({ name: 'plan', description: '' }))
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('“plan” is reserved for a built-in agent. Pick another name.')
    // Still open with the typed name: nothing was created, nothing to reopen.
    expect(within(dialog).getByRole('textbox', { name: 'Name' })).toHaveValue('plan')
    // Editing the name clears the refusal: the live "Create and edit" button then
    // reads as a fresh attempt, not as a press that would fail the same way.
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'planner' } })
    await waitFor(() => expect(within(dialog).queryByRole('alert')).toBeNull())
    expect(within(dialog).getByRole('button', { name: 'Create and edit' })).toBeEnabled()
  })

  it('does not blame the engine for a runtime-owned reserved name', async () => {
    // `kirocrew` and the Windows device names answer the plain
    // template_name_reserved code; that refusal keeps the server's own text
    // rather than the engine sentence, which would name the wrong owner.
    mockApi.agentTemplateCreate.mockRejectedValue(
      new StubApiError(400, "'kirocrew' is reserved", { error: "'kirocrew' is reserved", code: 'template_name_reserved' }),
    )
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New template' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'kirocrew' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent("'kirocrew' is reserved")
    expect(alert).not.toHaveTextContent('built-in agent')
  })

  it('creates a blank template, and a duplicate sends `from`', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    // After the create the roster lists the new row; the tab must open THAT
    // row, not fall back to the first one because the stale roster lacked it.
    const CREATED = row({ name: 'pr-summarizer', filename: 'pr-summarizer.json', description: 'Sums up PRs' })
    mockApi.agentTemplateCreate.mockImplementation(async () => {
      mockApi.agentTemplates.mockResolvedValue({ templates: [MINE, CREATED, PKG, RUNTIME, COPY] })
      return { ok: true, name: 'pr-summarizer', filename: 'pr-summarizer.json' }
    })
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New template' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'pr-summarizer' } })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Description' }), { target: { value: 'Sums up PRs' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    await waitFor(() => expect(mockApi.agentTemplateCreate).toHaveBeenCalledWith({ name: 'pr-summarizer', description: 'Sums up PRs' }))
    await waitFor(() => expect(option('pr-summarizer')).toHaveAttribute('aria-selected', 'true'))
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('pr-summarizer'))
    // Now the duplicate path, seeded from the selected template.
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const items = await openMore()
    fireEvent.click(items.find(i => i.textContent === 'Duplicate')!)
    const dup = await screen.findByRole('dialog', { name: 'Duplicate pr-summarizer' })
    // Opened from Duplicate: no blank/duplicate choice, the source is preselected.
    expect(within(dup).queryByRole('radiogroup')).toBeNull()
    expect(within(dup).getByRole('combobox', { name: 'Duplicate of' })).toHaveTextContent('pr-summarizer')
    fireEvent.change(within(dup).getByRole('textbox', { name: 'Name' }), { target: { value: 'reviewer-2' } })
    fireEvent.click(within(dup).getByRole('button', { name: 'Create and edit' }))
    await waitFor(() => expect(mockApi.agentTemplateCreate).toHaveBeenLastCalledWith({ name: 'reviewer-2', description: '', from: 'pr-summarizer' }))
  })

  it('does not carry a dirty draft into a newly created template', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'edited reviewer prompt' } })
    // New template is a row switch: the dirty guard asks first.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(confirmSpy).toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    const CREATED = row({ name: 'fresh', filename: 'fresh.json' })
    mockApi.agentTemplateCreate.mockImplementation(async () => {
      mockApi.agentTemplates.mockResolvedValue({ templates: [MINE, CREATED, PKG, RUNTIME, COPY] })
      return { ok: true, name: 'fresh', filename: 'fresh.json' }
    })
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New template' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'fresh' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create and edit' }))
    await waitFor(() => expect(option('fresh')).toHaveAttribute('aria-selected', 'true'))
    // The editor reseeds from the NEW template; the abandoned draft is gone and
    // nothing is dirty, so a Save here could not write reviewer's edits as fresh.
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Prompt' })).toHaveValue('prompt of fresh'))
    expect(screen.queryByRole('button', { name: 'Save template' })).toBeNull()
  })

  it('confirms a save where the bar stood, and the Add tool label stays visible while typing', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    fireEvent.change(prompt, { target: { value: 'edited' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save template' }))
    // The bar unmounts on success; the confirmation takes its place rather
    // than landing at the top of the scrolling pane.
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Template saved.')
    expect(status.className).toContain('bottom-4')
    // Opening the Add tool input keeps its words on screen.
    fireEvent.click(screen.getByRole('button', { name: /Add tool/ }))
    const input = screen.getByRole('combobox', { name: 'Add tool' })
    expect(input.closest('label')).toHaveTextContent('Add tool')
    expect(input).toHaveAttribute('placeholder', 'e.g. fs_write or @github/…')
  })

  it('names the deleted template in the closure line over the next row', async () => {
    mockApi.agentTemplates.mockResolvedValue({ templates: [FREE, PKG, RUNTIME, COPY] })
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockApi.agentTemplateDelete.mockImplementation(async () => {
      mockApi.agentTemplates.mockResolvedValue({ templates: [PKG, RUNTIME, COPY] })
      return { ok: true }
    })
    const items = await openMore()
    fireEvent.click(items.find(i => i.textContent === 'Delete')!)
    await waitFor(() => expect(mockApi.agentTemplateDelete).toHaveBeenCalledWith('reviewer'))
    // The list picks another row and the line rides over its detail, so it
    // names what went rather than reading as a verdict on the row on screen.
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Deleted “reviewer”.'))
    expect(screen.queryByRole('option', { name: /^reviewer/ })).toBeNull()
  })

  it('returns to the list after deleting a dirty template on a narrow viewport', async () => {
    mobile = true
    mockApi.agentTemplates.mockResolvedValue({ templates: [FREE, PKG, RUNTIME, COPY] })
    try {
      renderTab()
      // Narrow: the list shows first; pick the owned row to open its detail.
      fireEvent.click(await screen.findByRole('option', { name: /^reviewer/ }))
      await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
      const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
      await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
      fireEvent.change(prompt, { target: { value: 'dirty' } })
      expect(screen.queryByRole('listbox')).toBeNull()
      vi.spyOn(window, 'confirm').mockReturnValue(true)
      mockApi.agentTemplateDelete.mockImplementation(async () => {
        mockApi.agentTemplates.mockResolvedValue({ templates: [PKG, RUNTIME, COPY] })
        return { ok: true }
      })
      const items = await openMore()
      fireEvent.click(items.find(i => i.textContent === 'Delete')!)
      await waitFor(() => expect(mockApi.agentTemplateDelete).toHaveBeenCalledWith('reviewer'))
      // Back on the list, with the deleted template's draft gone -- not a
      // detail pane over a hidden list with nothing selected.
      await waitFor(() => expect(screen.getByRole('listbox')).toBeInTheDocument())
      expect(screen.queryByRole('textbox', { name: 'Prompt' })).toBeNull()
    } finally {
      mobile = false
    }
  })

  it('arms beforeunload only while the draft is dirty', async () => {
    const added = vi.spyOn(window, 'addEventListener')
    const removed = vi.spyOn(window, 'removeEventListener')
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    const prompt = await screen.findByRole('textbox', { name: 'Prompt' })
    await waitFor(() => expect(prompt).toHaveValue('prompt of reviewer'))
    const armedBefore = added.mock.calls.filter(c => c[0] === 'beforeunload').length
    expect(armedBefore).toBe(0)
    fireEvent.change(prompt, { target: { value: 'dirty' } })
    await waitFor(() => expect(added.mock.calls.filter(c => c[0] === 'beforeunload').length).toBe(1))
    // Discard (confirmed) cleans the draft; the reload warning goes with it.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(removed.mock.calls.filter(c => c[0] === 'beforeunload').length).toBe(1))
    added.mockRestore(); removed.mockRestore()
  })

  it('refuses a name the server would refuse before sending it', async () => {
    renderTab()
    await waitFor(() => expect(mockApi.agentDetail).toHaveBeenCalledWith('reviewer'))
    fireEvent.click(screen.getByRole('button', { name: /New template/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New template' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'has space' } })
    expect(within(dialog).getByRole('button', { name: 'Create and edit' })).toBeDisabled()
  })
})
