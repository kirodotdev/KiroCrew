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

import SteeringTab, { steeringBody } from '../pages/overview/SteeringTab'
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
    { key: 'user/personal.md', name: 'personal.md', rel: 'personal.md', source: 'user', path: '~/.kiro/steering/personal.md', size: 12, description: 'Personal', inclusion: 'always', inclusion_declared: '', file_match_pattern: '', linked: false, editable: true, target: '' },
    { key: 'workspace/api.md', name: 'api.md', rel: 'api.md', source: 'workspace', path: '~/proj/.kiro/steering/api.md', size: 20, description: 'API standards', inclusion: 'always', inclusion_declared: '', file_match_pattern: '', linked: false, editable: true, target: '' },
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

  it('badges a linked entry and disables Edit and Delete on it', async () => {
    // A symlinked steering file loads into every session, but the write path
    // refuses its key — the tab must show the file while saying why it cannot
    // be edited here, naming the target file that can.
    const linked = {
      ...FILES.files[0],
      key: 'user/conventions.md', name: 'conventions.md', rel: 'conventions.md',
      path: '~/.kiro/steering/conventions.md', description: 'Conventions',
      linked: true, editable: false, target: '~/dotfiles/conventions.md',
    }
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, files: [linked, FILES.files[1]] })
    mockApi.steeringFile.mockResolvedValue({ key: 'user/conventions.md', content: '# Conventions\nbody', path: '~/dotfiles/conventions.md', source: 'user' })
    renderTab()
    await waitFor(() => expect(screen.getByText('conventions.md')).toBeInTheDocument())
    // Icon-only marker on the ROW only (chip text would truncate the filename
    // in the 240px column; the header needs none — the banner is right below).
    const chips = screen.getAllByTestId('steering-linked-chip')
    expect(chips.length).toBe(1)
    expect(chips[0]).toHaveAttribute(
      'title',
      'Linked from ~/dotfiles/conventions.md — read-only. Edit the target file instead.',
    )
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    expect(screen.getByText('Edit')).toBeDisabled()
    expect(screen.getByText('Delete')).toBeDisabled()
    // The reason is VISIBLE in the detail body, not tooltip-only: browsers
    // suppress hover on disabled buttons and touch has no hover at all.
    expect(screen.getByText(
      'Linked from ~/dotfiles/conventions.md — read-only. Edit the target file instead.',
    )).toBeInTheDocument()
    // Disabled is explained where it is seen, not only on the chip.
    expect(screen.getByText('Edit')).toHaveAttribute(
      'title',
      'Linked from ~/dotfiles/conventions.md — read-only. Edit the target file instead.',
    )
  })

  it('keeps Edit and Delete live on a regular entry beside a linked one', async () => {
    const linked = {
      ...FILES.files[0],
      key: 'user/conventions.md', name: 'conventions.md', rel: 'conventions.md',
      linked: true, editable: false, target: '~/dotfiles/conventions.md',
    }
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, files: [linked, FILES.files[0]] })
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Select personal.md' }))
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    expect(screen.getByText('Edit')).not.toBeDisabled()
    expect(screen.getByText('Delete')).not.toBeDisabled()
  })

  it('renders a listing that predates the linked fields as editable, unbadged', async () => {
    // `editable === false` is the read-only trigger, so an absent field fails
    // OPEN to today's behavior — a cached pre-upgrade listing must not lock
    // every row, and a missing `linked` must simply render no chip.
    const { linked: _l, editable: _e, target: _t, ...bare } = FILES.files[0]
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, files: [bare] })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    expect(screen.queryByTestId('steering-linked-chip')).not.toBeInTheDocument()
    expect(screen.getByText('Edit')).not.toBeDisabled()
  })

  it('hides the default-mode chip in the list but states it in the detail header', async () => {
    // Every document without front matter resolves to `always`, so a chip on
    // every row would bury the ones that mean something. In the header there is
    // room, and the mode is the document's own property.
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    const list = screen.getByRole('listbox', { name: 'Steering files' })
    expect(within(list).queryByText('always')).not.toBeInTheDocument()
    expect(screen.getAllByText('always').length).toBe(1)
  })

  it('chips a non-default mode with the token the file actually declares', async () => {
    // The mode is the author's own literal, not copy: a translated chip would
    // name a mode that does not appear in their front matter.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'manual', inclusion_declared: 'manual' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    const chips = screen.getAllByText('manual')
    expect(chips.length).toBeGreaterThan(0)
    expect(chips[0]).toHaveAttribute('title', 'Inclusion mode: manual')
  })

  it('names the pattern a fileMatch document is scoped to', async () => {
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'src/**/*.ts' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    // Describes the DECLARATION, not an outcome: whether a matching file
    // actually pulls the document in is the harness's call, not this tab's.
    expect(screen.getAllByText('fileMatch')[0]).toHaveAttribute('title', 'Declared for files matching src/**/*.ts')
  })

  it('shows a typo verbatim and says which mode it is actually read as', async () => {
    // Normalising the spelling away would hide the only thing that explains why
    // a document its author declared `manual` is loading into every session.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'always', inclusion_declared: 'manaul' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    const chips = screen.getAllByText('manaul')
    expect(chips[0]).toHaveAttribute('title', 'manaul is not a known inclusion mode — it is read as always')
  })

  it('renders a listing that carries no inclusion fields at all', async () => {
    // The chip sits inside the list pane, so throwing on a missing field would
    // take the whole pane down over the least important thing on the row.
    const { inclusion: _i, inclusion_declared: _d, file_match_pattern: _p, ...bare } = FILES.files[0]
    mockApi.steeringFiles.mockResolvedValue({ ...FILES, files: [bare] })
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
  })

  it('offers the four modes in the editor and sends the one picked', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    for (const mode of ['always', 'fileMatch', 'manual', 'auto']) {
      expect(screen.getByRole('radio', { name: mode })).toBeInTheDocument()
    }
    fireEvent.click(screen.getByRole('radio', { name: 'manual' }))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '# Personal\nbody', undefined, 'pk-listed',
      { inclusion: 'manual' },
    ))
  })

  it('asks for a pattern before it will save a fileMatch document', async () => {
    // A patternless fileMatch document can never match. The server refuses it;
    // refusing here states the requirement at the moment it applies rather than
    // after the user has committed.
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.click(screen.getByRole('radio', { name: 'fileMatch' }))
    expect(screen.getByText('Save')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: 'src/**/*.ts' } })
    expect(screen.getByText('Save')).not.toBeDisabled()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '# Personal\nbody', undefined, 'pk-listed',
      { inclusion: 'fileMatch', file_match_pattern: 'src/**/*.ts' },
    ))
  })

  it('seeds the editor from the mode the document already declares', async () => {
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'lib/**/*.py' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    expect(screen.getByRole('radio', { name: 'fileMatch' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('File pattern')).toHaveValue('lib/**/*.py')
  })

  it('renders the body without the front matter it declares', async () => {
    // The renderer draws an opening `---` as a rule and the declarations under
    // it as a heading, so a document that declares a mode was shown its own YAML
    // in the largest type on the pane, above its title. The chip already states
    // the mode.
    mockApi.steeringFile.mockResolvedValue({
      key: 'user/personal.md',
      content: '---\ninclusion: manual\ndescription: Outage recovery.\n---\n# Personal\nbody',
      path: '~/.kiro/steering/personal.md',
      source: 'user',
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    expect(screen.getByTestId('md')).toHaveTextContent('# Personal body')
    expect(screen.getByTestId('md')).not.toHaveTextContent('inclusion: manual')
  })

  it('still edits the RAW text, front matter included', async () => {
    // Stripping in the editor would delete the declaration on the next save.
    const raw = '---\ninclusion: manual\n---\n# Personal\nbody'
    mockApi.steeringFile.mockResolvedValue({
      key: 'user/personal.md', content: raw, path: '~/.kiro/steering/personal.md', source: 'user',
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    expect(screen.getByRole('textbox', { name: /personal\.md/ })).toHaveValue(raw)
  })

  it('sends no declaration when the mode controls were not touched', async () => {
    // The server applies a declaration ON TOP of the submitted text, so sending
    // the seeded values unconditionally would revert front matter the user had
    // just edited by hand in the textarea.
    mockApi.steeringFile.mockResolvedValue({
      key: 'user/personal.md',
      content: '---\ninclusion: manual\n---\n# Personal\nbody',
      path: '~/.kiro/steering/personal.md',
      source: 'user',
    })
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'manual', inclusion_declared: 'manual' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    const box = screen.getByRole('textbox', { name: /personal\.md/ })
    fireEvent.change(box, { target: { value: '---\ninclusion: always\n---\n# Personal\nbody' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '---\ninclusion: always\n---\n# Personal\nbody',
      undefined, 'pk-listed', undefined,
    ))
  })

  it('leaves an unrecognized declaration alone on a body-only save', async () => {
    // A typo resolves to `always` for DISPLAY. Seeding the draft mode from that
    // resolution made draft and base differ before the user touched anything, so
    // saving a text edit silently rewrote the author's declaration to `always` —
    // destroying the very spelling the warning chip exists to show.
    mockApi.steeringFile.mockResolvedValue({
      key: 'user/personal.md',
      content: '---\ninclusion: manaul\n---\n# Personal\nbody',
      path: '~/.kiro/steering/personal.md',
      source: 'user',
    })
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'always', inclusion_declared: 'manaul' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    const box = screen.getByRole('textbox', { name: /personal\.md/ })
    fireEvent.change(box, { target: { value: '---\ninclusion: manaul\n---\n# Personal\nedited' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '---\ninclusion: manaul\n---\n# Personal\nedited',
      undefined, 'pk-listed', undefined,
    ))
  })

  it('says why Save is disabled while the pattern is empty', async () => {
    // The server-side refusal carrying this sentence is unreachable in this flow
    // BECAUSE Save is disabled, so without the inline copy the button is dead
    // with no reason given.
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.click(screen.getByRole('radio', { name: 'fileMatch' }))
    expect(screen.getByText(/declares a file pattern/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: 'src/**/*.ts' } })
    await waitFor(() => expect(screen.queryByText(/declares a file pattern/)).not.toBeInTheDocument())
  })

  it('accepts a pattern pasted in the quoted form Kiro documents', async () => {
    // Kiro's docs spell it `fileMatchPattern: "src/**/*.ts"`, so the quoted form
    // is what gets copied; sent verbatim the writer refuses it.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'a/**/*.ts' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: '"src/**/*.ts"' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '# Personal\nbody', undefined, 'pk-listed',
      { file_match_pattern: 'src/**/*.ts' },
    ))
  })

  it('keeps the unrecognized-mode warning visible while editing', async () => {
    // The editor seeds an unrecognised declaration as UNSELECTED, so hiding the
    // warning on Edit leaves an empty radiogroup and no cue — at the exact moment
    // the user came to fix the typo it names.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'always', inclusion_declared: 'manaul' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    const banner = () => screen.getAllByText(/manaul/).filter(el => el.tagName === 'P')
    expect(banner()).toHaveLength(1)
    fireEvent.click(screen.getByText('Edit'))
    expect(banner()).toHaveLength(1)
  })

  it('shows a rejected pattern under the field, not only in the tab banner', async () => {
    // The tab-level banner sits above the search box, off-screen once the editor
    // is scrolled to, so a rejected pattern otherwise reads as a save that did
    // nothing.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'a/**/*.ts' }],
    })
    mockApi.updateSteering.mockRejectedValue(Object.assign(
      new Error('frontmatter value cannot be represented'),
      { body: JSON.stringify({ code: 'steering_field_unrepresentable' }) },
    ))
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: 'b\\c' } })
    fireEvent.click(screen.getByText('Save'))
    // Placement is the whole point, so assert WHERE it lands: inside the
    // declaration editor that owns the input, not merely somewhere on the page
    // (it is on the page either way — the tab-level banner is above the search
    // box, which is exactly the problem).
    const shown = await screen.findByText(/character this file format cannot store/i)
    const editor = screen.getByLabelText('File pattern').closest('div')?.parentElement
    expect(editor?.contains(shown)).toBe(true)
  })

  it('keeps a refusal on screen after the mode is switched away', async () => {
    // The field copy renders only while editing AND on `fileMatch`. Keying the
    // tab banner off the error CODE alone hid both the moment the user switched
    // mode: the save stayed refused with nothing on screen saying so.
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'a/**/*.ts' }],
    })
    mockApi.updateSteering.mockRejectedValue(Object.assign(
      new Error('frontmatter value cannot be represented'),
      { body: JSON.stringify({ code: 'steering_field_unrepresentable' }) },
    ))
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: 'b\\c' } })
    fireEvent.click(screen.getByText('Save'))
    await screen.findByText(/character this file format cannot store/i)
    // Switching away unmounts the field — the refusal must not vanish with it.
    fireEvent.click(screen.getByRole('radio', { name: 'manual' }))
    expect(screen.getByText(/character this file format cannot store/i)).toBeInTheDocument()
  })

  it('sends only the field whose control moved', async () => {
    mockApi.steeringFiles.mockResolvedValue({
      ...FILES,
      files: [{ ...FILES.files[0], inclusion: 'fileMatch', inclusion_declared: 'fileMatch', file_match_pattern: 'a/**/*.ts' }],
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByLabelText('File pattern'), { target: { value: 'b/**/*.ts' } })
    fireEvent.click(screen.getByText('Save'))
    // The mode did not move, so it is absent — and a pattern the author typed
    // is not cleared behind their back.
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith(
      'user/personal.md', '# Personal\nbody', undefined, 'pk-listed',
      { file_match_pattern: 'b/**/*.ts' },
    ))
  })

  it('seeds the next edit from the write, not from a stale cache', async () => {
    // A refetch can fail or lag. Re-seeding from the pre-save body would write
    // it back over what was just saved.
    mockApi.steeringFile.mockResolvedValueOnce({
      key: 'user/personal.md', content: '# before', path: '~/.kiro/steering/personal.md', source: 'user',
    })
    mockApi.updateSteering.mockResolvedValue({ ok: true, content: '# after' })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    fireEvent.change(screen.getByRole('textbox', { name: /personal\.md/ }), { target: { value: '# after' } })
    // From here the refetch FAILS — the scenario this guards: a successful save
    // whose cache refresh never lands.
    mockApi.steeringFile.mockRejectedValue(new Error('offline'))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText('Edit')).not.toBeDisabled())
    fireEvent.click(screen.getByText('Edit'))
    expect(screen.getByRole('textbox', { name: /personal\.md/ })).toHaveValue('# after')
  })

  it('seeds the next edit\'s MODE from the write response, not from a stale list', async () => {
    // Same hazard as the content cache above, but for the LIST row: Edit reads
    // its starting mode from `selected.inclusion`, which lives in the
    // `['steering', ...]` cache, not the per-file detail one. A list refetch
    // that fails after a mode change must not leave that row on the old mode.
    mockApi.updateSteering.mockResolvedValue({
      ok: true, content: '# Personal\nbody', inclusion: 'manual', inclusion_declared: 'manual', file_match_pattern: '',
    })
    renderTab()
    await waitFor(() => expect(screen.getByTestId('md')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Edit'))
    expect(screen.getByRole('radio', { name: 'always' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(screen.getByRole('radio', { name: 'manual' }))
    // From here the list refetch FAILS — the scenario this guards: a successful
    // mode save whose list refresh never lands.
    mockApi.steeringFiles.mockRejectedValue(new Error('offline'))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText('Edit')).not.toBeDisabled())
    fireEvent.click(screen.getByText('Edit'))
    expect(screen.getByRole('radio', { name: 'manual' })).toHaveAttribute('aria-checked', 'true')
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
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# Personal\nchanged', undefined, 'pk-listed', undefined))
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
    await waitFor(() => expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# Personal\nbody', 'dashboard:chat-2', 'pk-listed', undefined))
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
    expect(mockApi.updateSteering).toHaveBeenCalledWith('user/personal.md', '# draft from A', undefined, 'pk-A', undefined)
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


describe('steeringBody', () => {
  it('removes a leading front-matter block', () => {
    expect(steeringBody('---\ninclusion: manual\n---\n# Title\nbody\n')).toBe('# Title\nbody\n')
  })

  it('leaves a document without front matter alone', () => {
    expect(steeringBody('# Title\nbody\n')).toBe('# Title\nbody\n')
  })

  it('does not eat a horizontal rule later in the document', () => {
    // Anchored and non-greedy: a `---` used as a rule is not a fence.
    const doc = '# Title\n\ntext\n\n---\n\nmore\n'
    expect(steeringBody(doc)).toBe(doc)
  })

  it('stops at the FIRST closing fence', () => {
    expect(steeringBody('---\na: 1\n---\n# T\n\n---\n\ntail\n'))
      .toBe('# T\n\n---\n\ntail\n')
  })

  it('handles CRLF', () => {
    expect(steeringBody('---\r\ninclusion: manual\r\n---\r\n# T\r\n')).toBe('# T\r\n')
  })
})
