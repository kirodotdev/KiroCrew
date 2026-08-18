/**
 * SteeringTab — the Steering tab under Agent Capabilities.
 *
 * Pins: both steering sources are listed with provenance badges, selecting a
 * file renders its markdown, Edit round-trips the raw content through the
 * update endpoint, Delete confirms first, and the create dialog forwards the
 * chosen scope.
 *
 * Also pins the two things a shared `dashboard:ui` placeholder used to hide:
 * every verb carries the ACTIVE CHAT SLOT's session key so the server resolves
 * `workspace/` against that chat's project, and the Scope row distinguishes
 * "no project set" from "open chats disagree" — three whole catalog labels plus
 * a hint line, because the two states look identical from this tab and only one
 * of them is fixed by picking a folder.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  steeringFiles: vi.fn(),
  steeringFile: vi.fn(),
  createSteering: vi.fn(),
  updateSteering: vi.fn(),
  deleteSteering: vi.fn(),
}))
/** Stand-in for the real `ApiError`, hoisted because `vi.mock`'s factory is
 *  hoisted with it — a class declared at normal top level is still uninitialized
 *  when the factory runs. The component branches on `status` and reads the machine
 *  code out of `body`, so those two fields are what a fake has to carry. */
const FakeApiError = vi.hoisted(() => class FakeApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: FakeApiError }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

import SteeringTab from '../pages/overview/SteeringTab'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'

/** Catalog text asserted verbatim: these three labels are the whole point of the
 *  change, so a test that matched them loosely would pass on the very bug being
 *  fixed (two states collapsing onto one string). */
const WORKSPACE_LABEL = {
  set: 'Workspace — this project only',
  none: 'Workspace — this project only (no project set for this chat)',
  ambiguous: 'Workspace — this project only (open chats use different projects)',
}
const SCOPE_HINT = {
  set: 'Writes to ~/proj/.kiro/steering.',
  none: 'Workspace scope needs a project. Open a chat, choose a folder with the project button beside the composer, then reopen this dialog.',
  ambiguous: 'Your open chats are on different projects, so no single project applies. Close or re-point one, then reopen this dialog.',
}

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
  project_key: 'pk-listed',
  project_state: 'set' as const,
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><SteeringTab /></QueryClientProvider>)
}

/** Open the create dialog on a freshly-rendered tab.
 *
 *  The visible "New Steering File" button and the dialog's own "New steering
 *  file" title differ in case, so the button stays unambiguous once the dialog
 *  is up. */
async function openCreateDialog() {
  await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
  fireEvent.click(screen.getByText('New Steering File'))
  return screen.getByRole('dialog')
}

/** Render with one project state, read the Scope row and the hint under it, then
 *  unmount — so two states can be compared inside one test without `screen`
 *  matching both renders at once. */
async function scopeRowFor(list: object) {
  mockApi.steeringFiles.mockResolvedValue(list)
  const view = renderTab()
  await openCreateDialog()
  const trigger = screen.getByRole('button', { name: 'Scope' })
  fireEvent.click(trigger)
  await screen.findByRole('listbox', { name: 'Scope' })
  const option = screen.getByRole('option', { name: /Workspace/ })
  const row = {
    label: option.textContent ?? '',
    ariaDisabled: option.getAttribute('aria-disabled'),
    hint: screen.getByTestId('steering-scope-hint').textContent ?? '',
  }
  view.unmount()
  return row
}

beforeEach(() => {
  // The tab reads `chat.activeSlot` off the real store at mount, so every test
  // starts from "no chat open" and opts in explicitly.
  store.dispatch(setActiveSlot(null))
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
    await waitFor(() => expect(mockApi.steeringFile).toHaveBeenCalledWith('user/personal.md', undefined))
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
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# Personal\nchanged', undefined, 'pk-listed'))
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
    await waitFor(() => expect(mockApi.deleteSteering).toHaveBeenCalledWith('user/personal.md', undefined, 'pk-listed'))
    confirmSpy.mockRestore()
  })

  it('create dialog forwards name, content and scope', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'new.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('new.md', expect.stringContaining('# Title'), 'workspace', undefined, 'pk-listed'))
  })

  it('defaults the create scope to global when no project is set', async () => {
    mockApi.steeringFiles.mockResolvedValue(
      { ...FILES, project: '', project_key: '', project_state: 'none' as const })
    renderTab()
    await waitFor(() => expect(screen.getByText('New Steering File')).toBeInTheDocument())
    fireEvent.click(screen.getByText('New Steering File'))
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'g.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('g.md', expect.any(String), 'user', undefined, ''))
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
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project: '', project_state: 'none' })
    renderTab()
    await openCreateDialog()

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
    // One whole catalog label, not a translated base plus a raw-English suffix:
    // the qualifier used to be concatenated on, so every non-English catalog
    // rendered a half-translated row.
    expect(workspace).toHaveTextContent(WORKSPACE_LABEL.none)

    fireEvent.click(workspace)
    expect(trigger).toHaveTextContent(/Global/)

    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'g.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('g.md', expect.any(String), 'user', undefined, 'pk-listed'))
  })

  // ---- Project state: three distinct scope rows, three distinct hints ----

  it('leaves the workspace row selectable and names the target directory when a project is set', async () => {
    const row = await scopeRowFor({ ...FILES, project: '~/proj', project_state: 'set' })
    expect(row.ariaDisabled).not.toBe('true')
    // The plain label: no parenthetical, because there is nothing to qualify.
    expect(row.label).toBe(WORKSPACE_LABEL.set)
    // The dialog always states where the file lands, so "workspace" is never an
    // unnamed destination.
    expect(row.hint).toContain(SCOPE_HINT.set)
    expect(row.hint).toContain('~/proj')
  })

  it('disables the workspace row and points at the project button when no chat names a project', async () => {
    const row = await scopeRowFor({ ...FILES, project: '', project_state: 'none' })
    expect(row.ariaDisabled).toBe('true')
    expect(row.label).toBe(WORKSPACE_LABEL.none)
    // Names the control that fixes it — the row used to say "(no project set)"
    // and stop there, naming a scope with no route to reaching it.
    expect(row.hint).toContain(SCOPE_HINT.none)
    // The destination is stated too, so a Global create is never unaddressed.
    expect(row.hint).toContain('Writes to ~/.kiro/steering.')
  })

  it('distinguishes chats-disagree from no-project instead of collapsing them', async () => {
    const conflict = await scopeRowFor({ ...FILES, project: '', project_state: 'ambiguous' })
    const none = await scopeRowFor({ ...FILES, project: '', project_state: 'none' })

    expect(conflict.ariaDisabled).toBe('true')
    expect(conflict.label).toBe(WORKSPACE_LABEL.ambiguous)
    expect(conflict.hint).toContain(SCOPE_HINT.ambiguous)

    // The bug this closes: both states arrive with an empty `project`, so a UI
    // keyed on that alone told an operator whose chats disagree to go set a
    // project — advice that cannot work, since each chat already has one. The
    // two must not render the same text.
    expect(conflict.label).not.toBe(none.label)
    expect(conflict.hint).not.toBe(none.hint)
  })


  // ---- Session key: which chat's project `workspace/` resolves against ----

  it('sends the active chat slot as the session key on every verb', async () => {
    store.dispatch(setActiveSlot('chat-7'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()

    // The list and the detail both carry it, and both query keys include it, so
    // switching chats cannot serve another project's files from cache.
    await waitFor(() => expect(mockApi.steeringFiles).toHaveBeenCalledWith('dashboard:chat-7'))
    await waitFor(() => expect(mockApi.steeringFile).toHaveBeenCalledWith('user/personal.md', 'dashboard:chat-7'))

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSteering).toHaveBeenCalledWith('user/personal.md', 'dashboard:chat-7', 'pk-listed'))
  })

  it('sends no session key when no chat is open, leaving the client to fall back', async () => {
    // `activeSlot` is null (see beforeEach): the tab must pass `undefined`
    // rather than invent a slot name, so `client.ts` applies its own
    // `dashboard:ui` placeholder.
    renderTab()
    await waitFor(() => expect(mockApi.steeringFiles).toHaveBeenCalledWith(undefined))
    expect(mockApi.steeringFiles.mock.calls[0]).toEqual([undefined])
  })

  it('carries the session key into a create', async () => {
    store.dispatch(setActiveSlot('chat-2'))
    renderTab()
    await openCreateDialog()
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'new.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('new.md', expect.any(String), 'workspace', 'dashboard:chat-2', 'pk-listed'))
  })

  it('carries the session key into an update', async () => {
    store.dispatch(setActiveSlot('chat-2'))
    renderTab()
    await waitFor(() => expect(screen.getByText('Edit')).toBeEnabled())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# Personal\nbody', 'dashboard:chat-2', 'pk-listed'))
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
    await waitFor(() => expect(mockApi.createSteering).toHaveBeenCalledWith('new.md', expect.any(String), 'user', undefined, 'pk-listed'))
  })

  it('surfaces mutation errors inline', async () => {
    mockApi.deleteSteering.mockRejectedValue(new Error('restricted session cannot modify steering files'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(screen.getByText('restricted session cannot modify steering files')).toBeInTheDocument())
  })

  it('renders a failed create INSIDE the still-open dialog', async () => {
    // A failed create leaves the dialog open, so the page-level banner behind it
    // is invisible and the Create button just looks inert. The refusal has to
    // render within the dialog itself.
    mockApi.createSteering.mockRejectedValue(new Error('steering name must end in .md'))
    renderTab()
    const dialog = await openCreateDialog()
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'bad' } })
    fireEvent.click(screen.getByText('Create'))

    const alert = await waitFor(() => within(dialog).getByRole('alert'))
    expect(alert).toHaveTextContent('steering name must end in .md')
    // Still open: the operator can correct the name in place rather than losing
    // the body they typed.
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(screen.getByPlaceholderText('api-standards.md')).toBeInTheDocument()
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

  // ---- Superseded project: the slot can move after the list is drawn ----

  it('echoes the listed project key on every workspace write', async () => {
    // The session key names a slot and the slot's project is MUTABLE, so it
    // cannot answer "which project did the user think they were editing". The
    // listing's fingerprint can, and the server refuses on a mismatch.
    store.dispatch(setActiveSlot('chat-9'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSteering)
      .toHaveBeenCalledWith('user/personal.md', 'dashboard:chat-9', 'pk-listed'))
  })

  it('re-lists when a write is refused because the project moved', async () => {
    // The rows on screen ARE the stale input that produced the 409, so leaving
    // them up would let the user hit the same refusal again.
    mockApi.deleteSteering.mockRejectedValue(
      new FakeApiError(409, 'the project is no longer active', JSON.stringify({ code: 'steering_project_changed' })))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.steeringFiles.mock.calls.length).toBeGreaterThan(listCalls))
  })

  it('does not re-list on an ordinary write failure', async () => {
    // Only a superseded-project refusal is fixed by refetching; a permission
    // error would just spin the list.
    mockApi.deleteSteering.mockRejectedValue(new Error('restricted session cannot modify steering files'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(screen.getByText(/restricted session/)).toBeInTheDocument())
    expect(mockApi.steeringFiles.mock.calls.length).toBe(listCalls)
  })

  it('does not carry a failed create\'s error into the next open dialog', async () => {
    // react-query holds mutation state until the next mutate()/reset(), so
    // Cancel + reopen used to render the previous attempt's banner over a form
    // the user had not submitted.
    mockApi.createSteering.mockRejectedValue(new Error('boom from the last attempt'))
    renderTab()
    const dialog = await openCreateDialog()
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'x.md' } })
    fireEvent.click(screen.getByText('Create'))
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('boom from the last attempt'))

    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.click(screen.getByText('New Steering File'))
    const reopened = screen.getByRole('dialog')
    expect(within(reopened).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('saves a draft under the project it was loaded from, not the one the list moved to', async () => {
    // GPT round 2: a background refetch re-syncs `projectKey`, so sending the
    // LIVE value would let a draft typed against project A satisfy the server's
    // precondition for B and overwrite B's same-named file. The captured value
    // fails the precondition instead, with the draft still on screen.
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-A' })
    renderTab()
    await waitFor(() => expect(screen.getByText('Edit')).toBeEnabled())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('Edit personal.md'), { target: { value: '# draft from A' } })

    // The slot is re-pointed: the next listing resolves a DIFFERENT project.
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-B' })
    fireEvent.click(screen.getByLabelText('Refresh steering files'))
    await waitFor(() => expect(mockApi.steeringFiles.mock.calls.length).toBeGreaterThan(1))

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalled())
    // pk-A — the project the CONTENT came from — never pk-B.
    expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# draft from A', undefined, 'pk-A')
  })

  it('does not serve one project\'s cached body as another project\'s same-named file', async () => {
    // `workspace/api.md` names a different file per project, so the detail query
    // key must carry the project or the cache answers for the wrong one.
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-A' })
    mockApi.steeringFile.mockResolvedValue({ key: 'user/personal.md', content: 'BODY FROM A', path: '~/x', source: 'user' })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('BODY FROM A'))
    const before = mockApi.steeringFile.mock.calls.length

    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-B' })
    mockApi.steeringFile.mockResolvedValue({ key: 'user/personal.md', content: 'BODY FROM B', path: '~/x', source: 'user' })
    fireEvent.click(screen.getByLabelText('Refresh steering files'))

    // A refetch under the new project key, not a cache hit on the old one.
    await waitFor(() => expect(mockApi.steeringFile.mock.calls.length).toBeGreaterThan(before))
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('BODY FROM B'))
  })

  it('keeps a live draft reachable when an update is refused for a moved project', async () => {
    // The 409 exists to preserve the draft, so refetching the list — which lists
    // the NEW project, where this file may not exist — must not drop the row the
    // editor is attached to and hide it.
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-A' })
    mockApi.updateSteering.mockRejectedValue(
      new FakeApiError(409, 'the project is no longer active', JSON.stringify({ code: 'steering_project_changed' })))
    renderTab()
    await waitFor(() => expect(screen.getByText('Edit')).toBeEnabled())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('Edit personal.md'), { target: { value: '# precious draft' } })
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(screen.getByText(/Copy your changes somewhere safe/)).toBeInTheDocument())

    // No refetch, and the draft is still on screen and still editable.
    expect(mockApi.steeringFiles.mock.calls.length).toBe(listCalls)
    expect((screen.getByLabelText('Edit personal.md') as HTMLTextAreaElement).value).toBe('# precious draft')
  })

  it('still refreshes when a DELETE is refused, where there is no draft to lose', async () => {
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, project_key: 'pk-A' })
    mockApi.deleteSteering.mockRejectedValue(
      new FakeApiError(409, 'the project is no longer active', JSON.stringify({ code: 'steering_project_changed' })))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(mockApi.steeringFiles.mock.calls.length).toBeGreaterThan(listCalls))
  })

  // ---- Conflict copy: detected by status+code, worded per verb ----

  it('detects the conflict from status and code, not from the message wording', async () => {
    // Design review: matching the human sentence means a copy edit silently
    // disables the recovery path the 409 exists to trigger. This failure carries
    // wording that shares NOTHING with the old regex.
    mockApi.deleteSteering.mockRejectedValue(
      new FakeApiError(409, 'totally reworded server text', JSON.stringify({ code: 'steering_project_changed' })))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Delete'))
    // Still recognised: refetched, and shown the localized conflict copy rather
    // than the server's prose.
    await waitFor(() => expect(mockApi.steeringFiles.mock.calls.length).toBeGreaterThan(listCalls))
    expect(screen.getByText('The active project changed, so this list has been refreshed.')).toBeInTheDocument()
    expect(screen.queryByText('totally reworded server text')).not.toBeInTheDocument()
  })

  it('tells a mid-edit user to preserve their draft, not to retry', async () => {
    // UX review: "refresh and try again" cannot succeed here — the save carries
    // the project the draft was loaded from — so retrying 409s forever and Cancel
    // destroys the draft. The copy must name the way out that works.
    mockApi.updateSteering.mockRejectedValue(new FakeApiError(409, 'the project this steering file belongs to is no longer the active project', JSON.stringify({ code: 'steering_project_changed' })))
    renderTab()
    await waitFor(() => expect(screen.getByText('Edit')).toBeEnabled())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('Edit personal.md'), { target: { value: '# draft' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(screen.getByText(/Copy your changes somewhere safe/)).toBeInTheDocument())
    // Not the create/delete wording, and not a "try again" instruction.
    expect(screen.queryByText(/this list has been refreshed/)).not.toBeInTheDocument()
    expect(screen.queryByText(/try again/i)).not.toBeInTheDocument()
    // And the draft survives, which is the point of refusing rather than retrying.
    expect((screen.getByLabelText('Edit personal.md') as HTMLTextAreaElement).value).toBe('# draft')
  })

  it('does not claim a project conflict for a 409 carrying a different code', async () => {
    // The status alone is not the identity. A future 409 on this route with its own
    // code must keep its own message, or the tab would tell the user the project
    // moved when something else entirely happened.
    mockApi.deleteSteering.mockRejectedValue(
      new FakeApiError(409, 'that file is locked by another writer', JSON.stringify({ code: 'steering_locked' })))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    const listCalls = mockApi.steeringFiles.mock.calls.length

    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(screen.getByText(/locked by another writer/)).toBeInTheDocument())
    expect(screen.queryByText(/this list has been refreshed/)).not.toBeInTheDocument()
    // And no refetch, because nothing says the listing is stale.
    expect(mockApi.steeringFiles.mock.calls.length).toBe(listCalls)
  })

  it('leaves a non-conflict error rendering the server message verbatim', async () => {
    // Only the 409 gets substituted copy; everything else must still surface the
    // server's own reason, which is often the only actionable detail.
    mockApi.deleteSteering.mockRejectedValue(
      new FakeApiError(403, 'restricted session cannot modify steering files', '{}'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await waitFor(() => expect(screen.getByText('Delete')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Delete'))
    await waitFor(() => expect(screen.getByText(/restricted session cannot modify/)).toBeInTheDocument())
  })

  it('stops naming the project directory once Global is selected', async () => {
    // UX round 5: the hint was keyed on project state alone, so with a project set
    // it kept claiming "Writes to ~/proj/.kiro/steering." directly under a select
    // the user had switched to Global — the wrong destination, which is the exact
    // failure the hint exists to prevent.
    renderTab()
    const dialog = await openCreateDialog()
    expect(within(dialog).getByTestId('steering-scope-hint')).toHaveTextContent('Writes to ~/proj/.kiro/steering.')

    const trigger = within(dialog).getByRole('button', { name: 'Scope' })
    fireEvent.click(trigger)
    await screen.findByRole('listbox', { name: 'Scope' })
    fireEvent.click(screen.getByRole('option', { name: /Global/ }))
    await waitFor(() => expect(trigger).toHaveTextContent(/Global/))

    const hint = within(screen.getByRole('dialog')).getByTestId('steering-scope-hint')
    expect(hint).toHaveTextContent('Writes to ~/.kiro/steering.')
    expect(hint).not.toHaveTextContent('~/proj')
  })

  it('keeps the how-to-bind guidance when workspace scope is unavailable', async () => {
    // The select defaults to Global with no project, so keying the hint purely on
    // the selection would delete the affordance this dialog was missing.
    mockApi.steeringFiles.mockResolvedValue(
      { ...FILES, project: '', project_key: '', project_state: 'none' as const })
    renderTab()
    const dialog = await openCreateDialog()
    expect(within(dialog).getByTestId('steering-scope-hint'))
      .toHaveTextContent('Workspace scope needs a project')
  })

  it('shows localized copy for a create conflict, not the server diagnostic', async () => {
    // UX round 6: the modal alert printed the raw message, so a 409 during create
    // rendered the backend's English diagnostic in every non-English locale —
    // while update and delete already got the code-keyed catalog string. Create is
    // the only verb with its own alert surface, so it was the one that missed it.
    mockApi.createSteering.mockRejectedValue(new FakeApiError(
      409,
      'the project this steering file belongs to is no longer the active project',
      JSON.stringify({ code: 'steering_project_changed' }),
    ))
    renderTab()
    const dialog = await openCreateDialog()
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'x.md' } })
    fireEvent.click(screen.getByText('Create'))

    const alert = await waitFor(() => within(dialog).getByRole('alert'))
    expect(alert).toHaveTextContent('The active project changed, so this list has been refreshed.')
    expect(alert).not.toHaveTextContent('no longer the active project')
  })

  it('still shows the server message in the modal for a non-conflict failure', async () => {
    // Substitution is scoped to the conflict code; anything else keeps the
    // server's own reason, which is usually the only actionable detail.
    mockApi.createSteering.mockRejectedValue(
      new FakeApiError(400, 'name is required', JSON.stringify({ code: 'bad_name' })))
    renderTab()
    const dialog = await openCreateDialog()
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'x.md' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('name is required'))
  })

  it('treats a code-less 409 as its own error, not as a project conflict', async () => {
    // Opus: `api_steering_create` answers 409 for a name collision with no `code`,
    // so failing open on a bare 409 showed "the active project changed" over
    // "'x.md' already exists" — and re-listed for nothing.
    mockApi.createSteering.mockRejectedValue(
      new FakeApiError(409, "'x.md' already exists", JSON.stringify({ error: "'x.md' already exists" })))
    renderTab()
    const dialog = await openCreateDialog()
    const listCalls = mockApi.steeringFiles.mock.calls.length
    fireEvent.change(screen.getByPlaceholderText('api-standards.md'), { target: { value: 'x.md' } })
    fireEvent.click(screen.getByText('Create'))

    const alert = await waitFor(() => within(dialog).getByRole('alert'))
    expect(alert).toHaveTextContent('already exists')
    expect(alert).not.toHaveTextContent('The active project changed')
    // And no refetch: nothing said the listing was stale.
    expect(mockApi.steeringFiles.mock.calls.length).toBe(listCalls)
  })
})
