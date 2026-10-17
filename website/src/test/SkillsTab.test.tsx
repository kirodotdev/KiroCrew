import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
}))
// A stub ApiError declared inside vi.hoisted so the mock factory (hoisted above
// the imports) can close over it: createSkill.onError branches on
// `instanceof ApiError` before reading the coded body, so the mock has to
// export something that branch recognizes. Same shape as PromptsTab.test.tsx.
const StubApiError = vi.hoisted(() => class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

// Controllable viewport: the narrow-viewport back control only renders under
// useIsMobile, and jsdom has no real matchMedia. Default is desktop; the
// mobile-specific tests flip it and restore it themselves.
const isMobileMock = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => isMobileMock.value }))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

// Skip the heavy SkillDirectoryBrowser internals in this tab-level test —
// other tests exercise that component directly.  Render the skill key +
// loaded_by_agents on the probe element so SkillsTab's wiring is testable.
vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: ({ skillKey, skill }: { skillKey: string; skill?: { loaded_by_agents?: string[] } }) => (
    <div
      data-testid="dir-browser"
      data-skill={skillKey}
      data-agents={(skill?.loaded_by_agents || []).join(',')}
    >browser</div>
  ),
}))

import SkillsTab from '../pages/overview/SkillsTab'
import { ERROR_HANDOFF_KEY, recordError, __resetErrorJournalForTests } from '../utils/errorReport'

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  // MemoryRouter: the pending-review panel reads (and clears) the `?review=<slug>`
  // deep link a skill notification points at, so the tab needs a router.
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><SkillsTab /></MemoryRouter>
      </QueryClientProvider>,
    ),
    qc,
  }
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody', hash: 'h-x' })
})

describe('SkillsTab', () => {
  it('renders a row per skill with its loaded_by_agents pill', async () => {
    mockApi.skills.mockResolvedValue([
      {
        key: 'foo', name: 'foo', description: 'a foo skill', source: 'kirocrew',
        loaded_by_agents: ['kirocrew', 'kirocrew-lite'],
      },
    ])
    renderWithQuery()

    // Row shows the humanized name and the key.
    await waitFor(() => expect(screen.getByText('Foo')).toBeInTheDocument())
    expect(screen.getByText('foo')).toBeInTheDocument()
    expect(screen.getByText(/Loaded by 2 agents/)).toBeInTheDocument()
  })

  it('shows singular form when exactly one agent loads the skill', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'solo', name: 'solo', description: 'lone', source: 'kirocrew', loaded_by_agents: ['only-one'] },
    ])
    renderWithQuery()
    await waitFor(() => expect(screen.getByText(/Loaded by 1 agent$/)).toBeInTheDocument())
  })

  it('selected row has no border (regression: selection should not draw a border)', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'a', name: 'a', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'b', name: 'b', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
    ])
    renderWithQuery()

    // First row auto-selects → aria-current="true".
    const selectedRow = await screen.findByRole('button', { name: 'Select A' })
    await waitFor(() => expect(selectedRow).toHaveAttribute('aria-current', 'true'))

    // No border-* utility on the selected row, and it carries the selected bg.
    const cls = selectedRow.className
    expect(cls).not.toMatch(/\bborder(-|\b)/)
    expect(cls).toContain('bg-accent-subtle')

    // The unselected row likewise has no border utility.
    const otherRow = screen.getByRole('button', { name: 'Select B' })
    expect(otherRow.className).not.toMatch(/\bborder(-|\b)/)
  })

  it('skill list uses the overlay (autohide, no-layout-shift) scrollbar', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'a', name: 'a', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
    ])
    renderWithQuery()

    const list = await screen.findByRole('listbox', { name: 'Skills' })
    // ``scrollbar-overlay`` keeps the scrollbar hidden until hover and
    // overlays it so the row width never shifts.
    expect(list.className).toContain('scrollbar-overlay')
    expect(list.className).toContain('overflow-y-auto')
  })

  it('omits the pill when loaded_by_agents is empty', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'unloaded', name: 'unloaded', description: 'no one', source: 'kirocrew', loaded_by_agents: [] },
    ])
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('Unloaded')).toBeInTheDocument())
    expect(screen.queryByText(/Loaded by/)).not.toBeInTheDocument()
  })

  it('groups package skills under their own section, kiro-user with local skills', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'kiro-user/x', name: 'x', description: 'kiro-x', source: 'kiro-user', loaded_by_agents: [] },
      { key: 'aim-only', name: 'aim-only', description: 'aim-pkg', source: 'package', loaded_by_agents: [] },
    ])
    renderWithQuery()
    // Both rows render; package skills have a section header.
    //
    // Query the ROW by its aria-label, not the bare name: the tab auto-selects the
    // first skill, so the detail pane renders the same display name in its header and
    // a getByText('X') has two matches as soon as both are mounted. It passed only
    // while the assertion happened to run in the gap between the list painting and
    // that effect firing -- a gap any change to catalog load timing closes.
    await waitFor(() => expect(screen.getByLabelText('Select X')).toBeInTheDocument())
    expect(screen.getByText('Aim Only')).toBeInTheDocument()
    expect(screen.getByText(/PACKAGES/)).toBeInTheDocument()
  })

  it('auto-selects the first skill and renders the directory browser (no modal)', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'demo', name: 'demo', description: 'demo skill', source: 'kirocrew', loaded_by_agents: [] },
    ])
    renderWithQuery()

    // No click needed — the first skill is selected on load and its browser shows.
    await waitFor(() => expect(screen.getByTestId('dir-browser')).toBeInTheDocument())
    expect(screen.getByTestId('dir-browser')).toHaveAttribute('data-skill', 'demo')
    // No dialog/modal in the master-detail layout.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('switches the browser when another skill row is clicked', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'first', name: 'first', description: 'one', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'second', name: 'second', description: 'two', source: 'kirocrew', loaded_by_agents: [] },
    ])
    renderWithQuery()

    // First auto-selected.
    await waitFor(() => expect(screen.getByTestId('dir-browser')).toHaveAttribute('data-skill', 'first'))

    fireEvent.click(screen.getByText('Second'))
    await waitFor(() => expect(screen.getByTestId('dir-browser')).toHaveAttribute('data-skill', 'second'))
  })

  it('passes loaded_by_agents through to the directory browser', async () => {
    mockApi.skills.mockResolvedValue([
      {
        key: 'agent-loaded', name: 'agent-loaded',
        description: 'has agents', source: 'kirocrew',
        loaded_by_agents: ['alpha-agent', 'beta-agent'],
      },
    ])
    renderWithQuery()

    // The browser receives the full Skill object so it can render the
    // frontmatter strip with the loaded_by_agents pills.
    await waitFor(() => expect(screen.getByTestId('dir-browser')).toBeInTheDocument())
    expect(screen.getByTestId('dir-browser')).toHaveAttribute('data-agents', 'alpha-agent,beta-agent')
  })

  it('Delete button confirms and dispatches the deleteSkill mutation', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'doomed', name: 'doomed', description: 'will go', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({ name: 'doomed', content: '---\nname: doomed\n---\n', hash: 'h-doomed' })
    mockApi.deleteSkill.mockResolvedValue({ ok: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    // Auto-selected → Delete appears in the detail header.
    const del = await screen.findByText('Delete')
    fireEvent.click(del)
    expect(confirmSpy).toHaveBeenCalled()
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('doomed'))
    confirmSpy.mockRestore()
  })

  it('Edit button enters edit mode for kirocrew-sourced skills', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'editable', name: 'editable', description: 'fixme', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({
      name: 'editable',
      content: '---\nname: editable\ndescription: fixme\n---\nbody text',
      hash: 'h-editable',
    })

    renderWithQuery()

    // Edit starts the content read when clicked.
    const editBtn = await screen.findByText('Edit')
    expect(editBtn).toBeEnabled()
    expect(mockApi.skill).not.toHaveBeenCalled()
    fireEvent.click(editBtn)

    // In edit mode, Save + Cancel surface.
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })

  it('preserves edit mode when the edited skill is filtered out (no data loss)', async () => {
    // Regression: entering edit mode then filtering the skill out of the list
    // must NOT auto-reselect another skill and discard unsaved form input.
    mockApi.skills.mockResolvedValue([
      { key: 'editable', name: 'editable', description: 'fixme', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'other', name: 'other', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({
      name: 'editable',
      content: '---\nname: editable\ndescription: fixme\n---\nbody text',
      hash: 'h-editable',
    })

    renderWithQuery()

    // Enter edit mode on the auto-selected first skill.
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    // Filter so the edited skill ("editable") is excluded but "other" remains.
    const filter = screen.getByPlaceholderText(/filter skills/i)
    fireEvent.change(filter, { target: { value: 'other' } })

    // Editor must stay mounted — Save/Cancel still present, no silent switch.
    await waitFor(() => {
      expect(screen.getByText('Save')).toBeInTheDocument()
      expect(screen.getByText('Cancel')).toBeInTheDocument()
    })
  })

  it('does NOT show Edit/Delete for kiro-user skills (read-only)', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'kiro-user/x', name: 'x', description: 'kiro-x', source: 'kiro-user', loaded_by_agents: [] },
    ])
    renderWithQuery()

    // Browser renders, but read-only sources lose Edit/Delete entirely.
    await waitFor(() => expect(screen.getByTestId('dir-browser')).toBeInTheDocument())
    expect(screen.queryByText('Edit')).not.toBeInTheDocument()
    expect(screen.queryByText('Delete')).not.toBeInTheDocument()
  })

  it('does NOT show Edit/Delete for a Kiro Crew row outside the skills folder (owned: false)', async () => {
    // A skill reached through skills.extra_paths lists as source 'kirocrew',
    // but Edit/Delete write into the skills folder, so the row is read-only.
    mockApi.skills.mockResolvedValue([
      { key: 'x', name: 'x', description: 'extra-x', source: 'kirocrew', owned: false, loaded_by_agents: [] },
    ])
    renderWithQuery()

    await waitFor(() => expect(screen.getByTestId('dir-browser')).toBeInTheDocument())
    expect(screen.queryByText('Edit')).not.toBeInTheDocument()
    expect(screen.queryByText('Delete')).not.toBeInTheDocument()
  })
})

describe('SkillsTab create gate and coded refusal', () => {
  // A non-empty list keeps the EmptyState (which has no Create button) out of the
  // DOM, and lets the tab settle out of its loading state -- while loading, the
  // header "Create New Skill" button is rendered DISABLED, so it must not be
  // clicked until a skill row is on screen.
  const ONE_SKILL = [
    { key: 'existing', name: 'existing', description: 'here already', source: 'kirocrew', loaded_by_agents: [] },
  ]

  // Katakana as code-point escapes: the repo forbids CJK literals in source, and
  // the sanitizer only cares that no character lands in [a-z0-9-/]. This is the
  // name that sanitizes to nothing -- the case the whole change is about.
  const NON_LATIN = '\u30b9\u30ad\u30eb'

  /** Render, wait for the list to load (the loading state disables Create), open
   *  the create modal, and return the dialog plus its footer Create button. */
  async function openCreateModal() {
    renderWithQuery()
    // The directory browser only mounts once `skills` has resolved; before that
    // the tab is in its loading branch, where Create is disabled.
    await screen.findByTestId('dir-browser')
    fireEvent.click(screen.getByText('Create New Skill'))
    // The footer Create button, scoped to the dialog: the header button that
    // opened it also reads "Create New Skill", and the modal title repeats it.
    const dialog = await screen.findByRole('dialog')
    return { dialog, create: within(dialog).getByText('Create') }
  }

  it('keeps Create disabled for a name that sanitizes to nothing, enabled for a valid one', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    // Empty field: still disabled (nothing to save yet), but no spurious refusal.
    expect(create).toBeDisabled()

    // A name that sanitizes away: gating on the raw name would have sent a
    // request that could only 400, so the gate reads the sanitized stem instead.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: NON_LATIN } })
    expect(create).toBeDisabled()
    // The form's own hint explains why, so the disable is not silent.
    expect(within(dialog).getByText(/has none of them/)).toBeInTheDocument()

    // One character in the allowed set produces a filename, so Create enables.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'My Skill!' } })
    expect(create).not.toBeDisabled()
    expect(within(dialog).getByText(/Saved as my-skill/)).toBeInTheDocument()
    // The gate never fired the mutation on its own.
    expect(mockApi.createSkill).not.toHaveBeenCalled()
  })

  it('drives the preview and gate off the COMBINED category/name the tab POSTs', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    // SkillsTab POSTs `category ? "{category}/{name}" : name`, so the modal's
    // preview and gate have to sanitize that same combined value -- not `name`
    // alone -- or the filename shown would disagree with what the server writes.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: 'Utils Code' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'My Skill' } })

    // category-then-name, each sanitized and joined by the surviving slash.
    expect(within(dialog).getByText(/Saved as utils-code\/my-skill/)).toBeInTheDocument()
    expect(create).not.toBeDisabled()
  })

  /* The three rows below are the states where gating on the COMBINED
     `category/name` gets the answer wrong. Category is an ordinary optional
     field, so each is reachable by typing into two inputs -- and in each one a
     combined check reports no problem, leaves Create enabled, and lets the server
     store something other than what the user described. */

  it('keeps Create disabled for a vanishing name even when the category survives', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: 'utils' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: NON_LATIN } })

    // `utils/<non-Latin>` sanitizes to a non-empty `utils`, so gating on the
    // combined value would enable Create and the server would store a skill
    // literally named `utils` with the typed name discarded -- no refusal, and
    // nothing for the coded-error path to translate.
    expect(create).toBeDisabled()
    expect(within(dialog).getByText(/has none of them/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/Saved as utils$/)).not.toBeInTheDocument()
  })

  it('keeps Create disabled for a separator-only name the category would carry', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: 'utils' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: '/' } })

    // The same discard as the non-Latin row, reached without leaving ASCII: every
    // segment of the Name is blank, so a per-segment check finds nothing to object
    // to, `utils//` sanitizes to a non-empty `utils`, and the server would store
    // the skill under the CATEGORY with the required Name gone.
    expect(create).toBeDisabled()
    expect(within(dialog).getByText(/has none of them/)).toBeInTheDocument()
    expect(mockApi.createSkill).not.toHaveBeenCalled()

    // And a blank segment the handler does NOT collapse: `a/ /b` is stored as the
    // unreadable `a/-/b`, which the identical `a/-/b` is already refused for.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: '' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'a/ /b' } })
    expect(create).toBeDisabled()
  })

  it('keeps Create disabled for a vanishing category, which would be dropped silently', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'code' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: NON_LATIN } })

    // The mirror image: the skill would land at the top level as `code`, with the
    // nesting the user asked for gone.
    expect(create).toBeDisabled()
  })

  it('keeps Create disabled for a vanishing segment nested inside the name', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    // Nesting is typed into the Name field directly, and the hint advertises it, so
    // this needs no category at all: `utils/<non-Latin>` sanitizes to a non-empty
    // `utils`, and checking the field whole would leave Create enabled.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: `utils/${NON_LATIN}` } })
    expect(create).toBeDisabled()
    expect(within(dialog).getByText(/has none of them/)).toBeInTheDocument()

    // A nested name whose every segment survives is still perfectly creatable.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'utils/code' } })
    expect(create).not.toBeDisabled()
    expect(within(dialog).getByText(/Saved as utils\/code/)).toBeInTheDocument()
  })

  it('keeps Create disabled for a whitespace-only name', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    // A truthy string, so a bare `!formData.name` gate passes it; and
    // skillPathProblem reports nothing, because an unfinished field is not a
    // mangled name. Only `.trim()` catches it -- otherwise Create sends a request
    // that can only earn the untranslated English `name is required`.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: '   ' } })
    expect(create).toBeDisabled()
    expect(mockApi.createSkill).not.toHaveBeenCalled()
  })

  it('accepts a category whose name is merely blank, which the server drops anyway', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    const { dialog, create } = await openCreateModal()

    // The gate must not over-refuse: a blank category sanitizes away server-side
    // too, so the stored name is the same with or without it.
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'code' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. utils, code'), { target: { value: '  ' } })
    expect(create).not.toBeDisabled()
  })

  it('blocks a second submit and the modal close while a create is in flight', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    // Never resolves: the mutation stays pending for the whole assertion.
    mockApi.createSkill.mockReturnValue(new Promise(() => {}))
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'ok-name' } })
    fireEvent.click(create)
    await waitFor(() => expect(mockApi.createSkill).toHaveBeenCalledTimes(1))

    // Re-clicking must not create the skill twice, and Cancel must not discard a
    // request already on the wire.
    await waitFor(() => expect(create).toBeDisabled())
    fireEvent.click(create)
    expect(mockApi.createSkill).toHaveBeenCalledTimes(1)
    expect(within(dialog).getByText('Cancel')).toBeDisabled()
    expect(screen.getByPlaceholderText('e.g. my-tool')).toBeInTheDocument()
  })

  it('translates a 400 invalid_name into the hint rather than echoing server English', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    // A name the client mirror accepts, so the request is actually sent: the
    // mapping has to hold for a name the preview and the server disagree on
    // (the safety net for any client the gate did not run in).
    mockApi.createSkill.mockRejectedValue(new StubApiError(
      400,
      'invalid skill name',
      JSON.stringify({ error: 'invalid skill name', code: 'invalid_name' }),
    ))
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'ok-name' } })
    fireEvent.click(create)

    await waitFor(() => expect(mockApi.createSkill).toHaveBeenCalled())
    // The translated hint, keyed on the coded body, not the raw server prose.
    await waitFor(() => expect(screen.getByText(/has none of them/)).toBeInTheDocument())
    expect(screen.queryByText('invalid skill name')).not.toBeInTheDocument()
  })

  it('surfaces an uncoded create failure with the server message, and keeps the modal open', async () => {
    mockApi.skills.mockResolvedValue(ONE_SKILL)
    mockApi.createSkill.mockRejectedValue(new Error("skill 'ok-name' already exists"))
    const { dialog, create } = await openCreateModal()

    fireEvent.change(within(dialog).getByPlaceholderText('e.g. my-tool'), { target: { value: 'ok-name' } })
    fireEvent.click(create)

    await waitFor(() => expect(screen.getByText(/already exists/)).toBeInTheDocument())
    // The form stays put so the typed work is not lost.
    expect(screen.getByPlaceholderText('e.g. my-tool')).toBeInTheDocument()
  })
})

describe('SkillsTab update/delete failure surfacing', () => {
  const SKILL = [
    { key: 'fragile', name: 'fragile', description: 'breaks on save', source: 'kirocrew', loaded_by_agents: [] },
  ]

  /** Render, wait for auto-select, and enter the detail editor. */
  async function enterEditor() {
    renderWithQuery()
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
  }

  beforeEach(() => {
    mockApi.skill.mockResolvedValue({ name: 'fragile', content: '---\nname: fragile\n---\nbody', hash: 'h-seeded' })
  })

  it('keeps the conflict notice in the form scroller and the header outside it', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockRejectedValue(new StubApiError(409, 'File changed', JSON.stringify({ code: 'content_conflict' })))

    await enterEditor()
    const body = screen.getByDisplayValue('body')
    fireEvent.change(body, { target: { value: 'unsaved draft' } })
    mockApi.skill.mockResolvedValue({ name: 'fragile', content: 'Current file\n'.repeat(80), hash: 'h-current' })
    const save = screen.getByRole('button', { name: 'Save' })
    fireEvent.click(save)

    const notice = await screen.findByTestId('skill-disk-conflict')
    const scroller = body.closest('.overflow-y-auto')
    expect(scroller).not.toBeNull()
    expect(scroller).toHaveClass('flex-1', 'min-h-0')
    expect(notice.closest('.overflow-y-auto')).toBe(scroller)
    expect(body).toHaveValue('unsaved draft')
    expect(scroller).not.toContainElement(save)
    expect(scroller).not.toContainElement(screen.getByRole('button', { name: 'Cancel' }))
    expect(save).toBeDisabled()
    const choices = within(notice).getAllByRole('button')
    expect(choices).toHaveLength(2)
    for (const choice of choices) expect(choice.closest('.overflow-y-auto')).toBe(scroller)
    // Neither choice is the wrong answer, so neither carries the destructive tint.
    for (const choice of choices) expect(choice).not.toHaveClass('text-danger')
  })

  it('keeps the save-failure notice in the fixed strip outside the form scroller', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockRejectedValue(new Error('disk full'))

    await enterEditor()
    const body = screen.getByDisplayValue('body')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    const notice = await screen.findByTestId('skill-update-failure')
    const scroller = body.closest('.overflow-y-auto')
    expect(scroller).not.toBeNull()
    expect(scroller).not.toContainElement(notice)
    expect(notice.closest('.shrink-0')).not.toBeNull()
  })

  it('surfaces an update failure next to the editor and keeps the draft', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockRejectedValue(new Error('disk full'))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())
    const notice = await screen.findByTestId('skill-update-failure')
    // Title lead + raw server message: the two halves of the same notice.
    expect(notice.textContent).toMatch(/Save failed/)
    expect(notice.textContent).toMatch(/disk full/)
    // The editor stays open: the form below still holds the only copy of the
    // unsaved edit, so a failed save must not close it.
    expect(screen.getByText('Save')).toBeInTheDocument()

    // The notice is dismissible, like its delete sibling.
    fireEvent.click(within(notice).getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByTestId('skill-update-failure')).not.toBeInTheDocument())
  })

  it('a successful save retires a retained delete failure instead of contradicting it', async () => {
    // The banner the user SAW on the list is retired when they enter the
    // editor (acknowledged-by-navigation); a stale "could not delete" must
    // not pop back up after the save succeeds.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    mockApi.updateSkill.mockResolvedValue({ ok: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await screen.findByTestId('skill-delete-failure')

    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(screen.queryByText('Save')).not.toBeInTheDocument())
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('a delete failure arriving DURING the edit survives a successful save and displays', async () => {
    // The failure was suppressed the whole time the editor was open — the
    // user never saw it, so the save's success must not clear it: the banner
    // displays once the editor closes, instead of the rolled-back row
    // sitting in the list unexplained.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    let rejectDelete: (e: Error) => void = () => {}
    mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
    mockApi.updateSkill.mockResolvedValue({ ok: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    // Delete A; it hangs; the survivor B is auto-selected. Open B's editor.
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    // The delete fails while the editor is open: suppressed, never seen.
    rejectDelete(new Error('permission denied'))
    await waitFor(() => expect(screen.getByText('fragile')).toBeInTheDocument())  // rollback
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()

    // A successful save closes the editor — the unseen failure must surface.
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(screen.queryByText('Save')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('skill-delete-failure')).toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('a row tap on the latched mobile list reopens the editor and keeps the draft AND the failure', async () => {
    // A latched session holds the only copy of a draft, and on a phone a row
    // tap is the only way back to the detail pane — so the tap reopens the
    // latched editor rather than ending the session (which would discard the
    // draft on the next Edit reseed). The retained failure is suppressed
    // while the editor shows and displays again after its explicit exits.
    isMobileMock.value = true
    try {
      const TWO = [
        ...SKILL,
        { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
      ]
      mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
      let rejectDelete: (e: Error) => void = () => {}
      mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

      renderWithQuery()
      // Delete A (hangs); the survivor is auto-selected; open its editor.
      fireEvent.click(await screen.findByText('fragile'))
      fireEvent.click(await screen.findByText('Delete'))
      await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

      // The failure arrives while editing (suppressed), then Back latches the
      // session: the retained banner displays on the list.
      rejectDelete(new Error('permission denied'))
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      await screen.findByTestId('skill-delete-failure')

      // A row tap REOPENS the latched editor: draft intact, banner suppressed
      // but not dropped.
      fireEvent.click(await screen.findByText('other'))
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
      expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()

      // Leaving through the editor's own exit surfaces the failure again.
      fireEvent.click(screen.getByText('Cancel'))
      await waitFor(() => expect(screen.getByTestId('skill-delete-failure')).toBeInTheDocument())
      confirmSpy.mockRestore()
    } finally {
      isMobileMock.value = false
    }
  })

  it('a latched row tap whose skill vanished falls through instead of trapping the user', async () => {
    // If the latched skill disappeared underneath the latch (an external
    // delete picked up by a refetch), reopening the pane would land on the
    // no-selection placeholder — the one detail branch with no Back — with
    // the list hidden: a dead-end on a phone. A dead latch must fall through
    // to the normal select path instead.
    isMobileMock.value = true
    try {
      const TWO = [
        ...SKILL,
        { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
      ]
      mockApi.skills.mockResolvedValue(TWO)
      const { qc } = renderWithQuery()

      // Latch an edit session on fragile.
      fireEvent.click(await screen.findByText('fragile'))
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      await waitFor(() => expect(screen.queryByText('Save')).not.toBeInTheDocument())

      // The latched skill vanishes underneath the latch.
      act(() => { qc.setQueryData(['skills'], TWO.filter(s => s.key !== 'fragile')) })

      // The tap must land on the tapped skill's detail, not the placeholder.
      fireEvent.click(await screen.findByText('other'))
      await waitFor(() => expect(screen.getByTestId('dir-browser')).toBeInTheDocument())
      expect(screen.queryByText(/Select a skill to view its files/i)).not.toBeInTheDocument()
    } finally {
      isMobileMock.value = false
    }
  })

  it('translates the coded read-only refusal rather than echoing server English', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockRejectedValue(new StubApiError(
      405,
      "skill 'fragile' is in a read-only territory",
      JSON.stringify({ error: "skill 'fragile' is in a read-only territory", code: 'readonly_skill_prefix' }),
    ))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))

    const notice = await screen.findByTestId('skill-update-failure')
    expect(notice.textContent).toMatch(/managed on disk/)
    expect(notice.textContent).not.toMatch(/read-only territory/)
  })

  it('Edit seeds the form from a fresh read, not the cached copy, and saves against it', async () => {
    // A cached copy can predate an agent's on-disk edit with no push.
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.skill.mockResolvedValue({ name: 'fragile', content: '---\nname: fragile\n---\nagent body', hash: 'h-agent' })
    mockApi.updateSkill.mockResolvedValue({ ok: true })

    const { qc } = renderWithQuery()
    qc.setQueryData(['skill-detail', 'fragile'], { content: '---\nname: fragile\n---\nold body', hash: 'h-old' })
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    expect(await screen.findByDisplayValue('agent body')).toBeInTheDocument()
    expect(mockApi.skill).toHaveBeenCalledExactlyOnceWith('fragile')
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(1))
    expect(mockApi.updateSkill.mock.calls[0][2]).toBe('h-agent')
  })

  it('keeps Edit enabled after an initial detail failure, reports a fresh read failure, and opens on retry', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    let resolveRead!: (detail: { content: string; hash: string }) => void
    const retryRead = new Promise<{ content: string; hash: string }>(resolve => { resolveRead = resolve })
    mockApi.skill
      .mockRejectedValueOnce(new Error('initial read failed'))
      .mockRejectedValueOnce(new Error('gateway restarting'))
      .mockReturnValueOnce(retryRead)
    const { qc } = renderWithQuery()
    const edit = await screen.findByRole('button', { name: 'Edit' })
    expect(edit).toBeEnabled()
    expect(mockApi.skill).not.toHaveBeenCalled()
    // Seed a failed initial GET in the shared cache; Edit owns its fresh read.
    await act(async () => {
      await expect(qc.fetchQuery({ queryKey: ['skill-detail', 'fragile'], queryFn: () => mockApi.skill('fragile') })).rejects.toThrow('initial read failed')
    })
    expect(qc.getQueryState(['skill-detail', 'fragile'])?.status).toBe('error')
    expect(edit).toBeEnabled()
    fireEvent.click(edit)

    const notice = await screen.findByTestId('skill-edit-read-failure')
    expect(mockApi.skill).toHaveBeenNthCalledWith(2, 'fragile')
    expect(notice).toHaveTextContent("Couldn't open the editor")
    expect(notice).toHaveTextContent('gateway restarting')
    // The raw error sits on its own line under the title, not run into it.
    const rawLine = [...notice.querySelectorAll('span')].find(el => el.textContent === 'gateway restarting')
    expect(rawLine).toBeTruthy()
    expect(rawLine!.className).toContain('block')
    expect(rawLine!.textContent).not.toContain("Couldn't open the editor")
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(edit).toBeEnabled()
    fireEvent.click(edit)
    await waitFor(() => expect(mockApi.skill).toHaveBeenCalledTimes(3))
    expect(screen.queryByTestId('skill-edit-read-failure')).not.toBeInTheDocument()
    expect(edit).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Create New Skill' })).toBeDisabled()

    await act(async () => { resolveRead({ content: '---\nname: fragile\n---\nfresh body', hash: 'h-fresh' }) })
    await screen.findByRole('button', { name: 'Save' })
    expect(screen.getByDisplayValue('fresh body')).toBeInTheDocument()
    expect(screen.queryByTestId('skill-edit-read-failure')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('skill-edit-read-failure')).not.toBeInTheDocument()
  })

  it.each(['dismiss', 'select row'] as const)('clears the Edit read failure on %s', async action => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.skill.mockRejectedValueOnce(new Error('gateway restarting'))
    renderWithQuery()
    const edit = await screen.findByRole('button', { name: 'Edit' })
    await waitFor(() => expect(edit).toBeEnabled())
    fireEvent.click(edit)
    const notice = await screen.findByTestId('skill-edit-read-failure')

    fireEvent.click(action === 'dismiss'
      ? within(notice).getByRole('button', { name: 'Dismiss' })
      : screen.getByRole('button', { name: 'Select Fragile' }))
    expect(screen.queryByTestId('skill-edit-read-failure')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  describe('a save refused because the file was edited after the form opened', () => {
    const ON_DISK = '---\nname: fragile\n---\nagent rewrote this'
    const conflict = () => new StubApiError(
      409,
      'the skill changed on disk after the edit was started',
      JSON.stringify({ error: 'the skill changed on disk after the edit was started', code: 'content_conflict' }),
    )

    async function refuseFirstSave() {
      mockApi.skills.mockResolvedValue(SKILL)
      mockApi.updateSkill.mockRejectedValueOnce(conflict()).mockResolvedValue({ ok: true, hash: 'h-saved' })
      await enterEditor()
      // The refusal re-reads the file; from here on the read serves the agent's edit.
      mockApi.skill.mockResolvedValue({ name: 'fragile', content: ON_DISK, hash: 'h-current' })
      fireEvent.change(screen.getByDisplayValue('body'), { target: { value: 'my draft' } })
      fireEvent.click(screen.getByText('Save'))
      return screen.findByTestId('skill-disk-conflict')
    }

    it('shows the current file, keeps the draft, and blocks Save until the user picks', async () => {
      const notice = await refuseFirstSave()
      expect(mockApi.updateSkill.mock.calls[0][2]).toBe('h-seeded')
      expect(notice.textContent).toMatch(/edited after you opened it/)
      expect(notice.textContent).toMatch(/An agent or another app changed this file\. Your edits are still in the form below\. Choose which version to keep; Save works again once you choose\./)
      expect(within(notice).getByText('The other version (now in the file)')).toBeInTheDocument()
      expect(within(notice).getByTestId('skill-disk-conflict-current').textContent).toBe(ON_DISK)
      expect(within(notice).getByText('Discard my edits and use the current file')).toBeInTheDocument()
      expect(within(notice).getByText('Keep my edits (the other version is replaced)')).toBeInTheDocument()
      // Nothing was written, so the editor and the draft stay.
      expect(screen.getByDisplayValue('my draft')).toBeInTheDocument()
      expect(screen.getByText('Save').closest('button')).toBeDisabled()
      expect(screen.queryByTestId('skill-update-failure')).not.toBeInTheDocument()
    })

    it('Discard reseeds the form from the current file and saves against its hash', async () => {
      const notice = await refuseFirstSave()
      fireEvent.click(within(notice).getByText('Discard my edits and use the current file'))

      expect(screen.queryByTestId('skill-disk-conflict')).not.toBeInTheDocument()
      expect(screen.getByDisplayValue('agent rewrote this')).toBeInTheDocument()
      fireEvent.click(screen.getByText('Save'))
      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(2))
      expect(mockApi.updateSkill.mock.calls[1][2]).toBe('h-current')
    })

    it('Discard from raw-markdown mode shows the current file, not an empty raw editor', async () => {
      mockApi.skills.mockResolvedValue(SKILL)
      mockApi.updateSkill.mockRejectedValueOnce(conflict()).mockResolvedValue({ ok: true })
      await enterEditor()
      mockApi.skill.mockResolvedValue({ name: 'fragile', content: ON_DISK, hash: 'h-current' })
      fireEvent.click(screen.getByText('Edit raw markdown'))
      const raw = screen.getByDisplayValue(/name: fragile/)
      fireEvent.change(raw, { target: { value: '---\nname: fragile\n---\nmy raw draft' } })
      fireEvent.click(screen.getByText('Save'))
      const notice = await screen.findByTestId('skill-disk-conflict')
      fireEvent.click(within(notice).getByText('Discard my edits and use the current file'))

      // The editor shows the current file's body; no blank raw textarea is
      // left to be typed into and saved over the file.
      expect(screen.getByDisplayValue('agent rewrote this')).toBeInTheDocument()
      expect(screen.queryByDisplayValue(/my raw draft/)).not.toBeInTheDocument()
      fireEvent.click(screen.getByText('Save'))
      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(2))
      expect(mockApi.updateSkill.mock.calls[1][1]).toMatch(/agent rewrote this/)
    })

    it('Keep writes the draft against the current file hash', async () => {
      const notice = await refuseFirstSave()
      fireEvent.click(within(notice).getByText('Keep my edits (the other version is replaced)'))

      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(2))
      const [key, content, base] = mockApi.updateSkill.mock.calls[1]
      expect(key).toBe('fragile')
      expect(content).toMatch(/my draft/)
      expect(base).toBe('h-current')
      await waitFor(() => expect(screen.queryByText('Save')).not.toBeInTheDocument())
    })

    it('reports the ordinary save failure when the current file cannot be read', async () => {
      mockApi.skills.mockResolvedValue(SKILL)
      mockApi.updateSkill.mockRejectedValueOnce(conflict())
      await enterEditor()
      mockApi.skill.mockRejectedValue(new Error('read failed'))
      fireEvent.change(screen.getByDisplayValue('body'), { target: { value: 'my draft' } })
      fireEvent.click(screen.getByText('Save'))

      const notice = await screen.findByTestId('skill-update-failure')
      expect(notice.textContent).toMatch(/Save failed/)
      expect(screen.queryByTestId('skill-disk-conflict')).not.toBeInTheDocument()
      expect(screen.getByDisplayValue('my draft')).toBeInTheDocument()
    })
  })

  it('every [\'skills\'] reader on the tab refetches on mount (none keeps a page-lifetime copy)', async () => {
    // The pending panel reads the same key to tell "approved" from "dismissed";
    // on the global staleTime: Infinity it would answer from the first fetch.
    mockApi.skills.mockResolvedValue(SKILL)
    const { qc } = renderWithQuery()
    await screen.findByText('Edit')
    const observers = qc.getQueryCache().find({ queryKey: ['skills'] })!.observers
    expect(observers.length).toBeGreaterThan(1)
    for (const o of observers) expect(o.options.staleTime).toBe(0)
  })

  it('clears the update failure when the editor is re-entered', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockRejectedValue(new Error('disk full'))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))
    await screen.findByTestId('skill-update-failure')

    // Leave and re-enter the editor: the stale notice must not survive.
    fireEvent.click(screen.getByText('Cancel'))
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
    expect(screen.queryByTestId('skill-update-failure')).not.toBeInTheDocument()
  })

  it('clears the update failure on a subsequent successful save', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill
      .mockRejectedValueOnce(new Error('disk full'))
      .mockResolvedValue({ ok: true })

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))
    await screen.findByTestId('skill-update-failure')

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(screen.queryByTestId('skill-update-failure')).not.toBeInTheDocument())
  })

  it('surfaces a delete failure and still restores the optimistically removed row', async () => {
    // The list resolves ONCE; every later (invalidation-driven) refetch hangs.
    // The row's visibility after the failure is then decided purely by the
    // optimistic cache write and its rollback — a refetch serving the original
    // list again would mask a broken rollback.
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    const del = await screen.findByText('Delete')
    fireEvent.click(del)
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

    // Rollback first: the optimistically removed row must come back…
    await waitFor(() => expect(screen.getByText('fragile')).toBeInTheDocument())
    // …and the user must be told why it came back.
    const notice = await screen.findByTestId('skill-delete-failure')
    expect(notice.textContent).toMatch(/permission denied/)
    // The banner names the skill the way the ROW does — its display name.
    expect(notice.textContent).toMatch(/Fragile/)
    confirmSpy.mockRestore()
  })

  it('clears the delete failure once a retry succeeds', async () => {
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill
      .mockRejectedValueOnce(new Error('permission denied'))
      .mockResolvedValue({ ok: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    const del = await screen.findByText('Delete')
    fireEvent.click(del)
    await screen.findByTestId('skill-delete-failure')

    // Retry from the restored row's detail header.
    const delAgain = await screen.findByText('Delete')
    fireEvent.click(delAgain)
    await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('retires the previous failure banner the moment a retry starts', async () => {
    // While a retry is in flight the old "could not delete" must not sit on
    // screen presenting a stale outcome as current; a fresh failure re-sets it.
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill
      .mockRejectedValueOnce(new Error('permission denied'))
      .mockImplementation(() => new Promise(() => {}))  // the retry hangs
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await screen.findByTestId('skill-delete-failure')

    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('hands the agent the ORIGINAL failure context, not the translated frame', async () => {
    // The visible notice wraps err.message in a translated frame, so the
    // journal lookup keyed on the visible text finds nothing — the report must
    // be recovered by the original message and passed through explicitly, or
    // the Ask-agent hand-off degrades to bare prose with no endpoint/status.
    __resetErrorJournalForTests()
    sessionStorage.clear()
    recordError({
      source: 'api',
      message: 'permission denied',
      status: 500,
      endpoint: '/api/skills/fragile',
      detail: '{"error": "permission denied"}',
    })
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    const notice = await screen.findByTestId('skill-delete-failure')

    fireEvent.click(within(notice).getByText('Ask the agent'))
    const staged = sessionStorage.getItem(ERROR_HANDOFF_KEY) || ''
    expect(staged).toMatch(/\/api\/skills\/fragile/)
    expect(staged).toMatch(/HTTP 500/)
    confirmSpy.mockRestore()
  })

  it('keeps the frame on a coded read-only delete refusal, so the banner names its referent', async () => {
    // A bare hint ("This skill is managed on disk…") next to several rows
    // names neither the failed action nor the skill; the frame must carry
    // both, with the hint riding in the {{error}} slot.
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new StubApiError(
      405,
      "skill 'fragile' is in a read-only territory",
      JSON.stringify({ error: "skill 'fragile' is in a read-only territory", code: 'readonly_skill_prefix' }),
    ))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    const notice = await screen.findByTestId('skill-delete-failure')
    expect(notice.textContent).toMatch(/Could not delete/)
    expect(notice.textContent).toMatch(/Fragile/)
    expect(notice.textContent).toMatch(/managed on disk/)
    expect(notice.textContent).not.toMatch(/read-only territory/)
    confirmSpy.mockRestore()
  })

  it('offers a dismiss on the delete notice, and dismiss retires it', async () => {    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    const notice = await screen.findByTestId('skill-delete-failure')

    fireEvent.click(within(notice).getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('retires the delete failure when the user moves to another skill', async () => {
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await screen.findByTestId('skill-delete-failure')

    // The failed attempt is finished; selecting another skill moves on.
    fireEvent.click(await screen.findByText('other'))
    await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('suppresses the delete banner while the editor is open, so its hand-off cannot outlive a draft', async () => {
    // The banner's Ask-agent hand-off navigates away and unmounts this tab. It
    // must therefore never co-render with the editor, whose form holds the
    // only copy of an unsaved edit — the render is gated on !detailEditing.
    mockApi.skills.mockResolvedValueOnce(SKILL).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await screen.findByTestId('skill-delete-failure')

    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('holds back a DELAYED delete failure while the editor is open, and shows it after', async () => {
    // The race the per-event clears cannot cover: the delete is still in
    // flight when the user opens another skill's editor, so the failure
    // arrives WHILE editing. The gate must suppress it then, and the retained
    // error must surface once the editor closes.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    let rejectDelete: (e: Error) => void = () => {}
    mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    // Delete the auto-selected first skill; the DELETE hangs, the optimistic
    // removal auto-selects the survivor.
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

    // Open the survivor's editor before the delete settles.
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    // NOW the delete fails — beside an open editor holding a draft.
    rejectDelete(new Error('permission denied'))
    await waitFor(() => expect(screen.getByText('fragile')).toBeInTheDocument())  // rollback landed
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()

    // Closing the editor reveals the retained failure.
    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(screen.getByTestId('skill-delete-failure')).toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('ignores the narrow-viewport back control while a save is in flight', async () => {
    isMobileMock.value = true
    try {
      mockApi.skills.mockResolvedValue(SKILL)
      mockApi.updateSkill.mockImplementation(() => new Promise(() => {}))

      renderWithQuery()
      // Mobile shows one pane at a time: enter the detail pane from the row.
      fireEvent.click(await screen.findByText('fragile'))
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

      fireEvent.click(screen.getByText('Save'))
      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())

      // Back is the editor's only other exit on a phone; taking it mid-save
      // would unmount the pane that renders the save's failure. (While the
      // save is in flight the button reads its pending label.)
      const back = screen.getByRole('button', { name: 'Skills' })
      // It must also read as unavailable, not look live and do nothing.
      expect(back).toBeDisabled()
      expect(back).toHaveAttribute('aria-disabled', 'true')
      fireEvent.click(back)
      expect(screen.getByText('Saving…')).toBeInTheDocument()
    } finally {
      isMobileMock.value = false
    }
  })

  it('narrow-viewport Back returns to the list and the retained delete failure displays', async () => {
    // Back hides the editor pane without ending the edit session; the delete
    // banner's gate keys on the editor being VISIBLE, so the retained failure
    // must display on the list rather than staying stranded behind a latched
    // detailEditing.
    isMobileMock.value = true
    try {
      const TWO = [
        ...SKILL,
        { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
      ]
      mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
      let rejectDelete: (e: Error) => void = () => {}
      mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

      renderWithQuery()
      // Enter fragile's detail pane and start a delete that hangs.
      fireEvent.click(await screen.findByText('fragile'))
      fireEvent.click(await screen.findByText('Delete'))
      await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

      // The optimistic removal auto-selects the survivor; open its editor.
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

      // The delayed failure arrives while editing: retained, suppressed.
      rejectDelete(new Error('permission denied'))
      await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())

      // Back to the list — the retained failure must now display.
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      await waitFor(() => expect(screen.getByTestId('skill-delete-failure')).toBeInTheDocument())
      confirmSpy.mockRestore()
    } finally {
      isMobileMock.value = false
    }
  })

  it('refuses a second delete while one is in flight', async () => {
    // Overlapping deletes cross their hook-level callbacks: the first
    // delete's onSuccess would clear the second's failure notice.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    mockApi.deleteSkill.mockImplementation(() => new Promise(() => {}))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledTimes(1))

    // The survivor is auto-selected; its Delete must refuse to overlap.
    const delAgain = await screen.findByText('Delete')
    expect(delAgain).toBeDisabled()
    fireEvent.click(delAgain)
    expect(mockApi.deleteSkill).toHaveBeenCalledTimes(1)
    confirmSpy.mockRestore()
  })

  it('disables Save while the save is in flight, so it cannot double-fire', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockImplementation(() => new Promise(() => {}))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(1))
    // The button announces the in-flight state and cannot double-fire.
    const saving = screen.getByText('Saving…')
    expect(saving.closest('button')).toBeDisabled()
    fireEvent.click(saving)
    expect(mockApi.updateSkill).toHaveBeenCalledTimes(1)
  })

  it('a row click from inside the editor reveals a suppressed delete failure instead of clearing it', async () => {
    // While the editor is open the !detailEditing gate suppresses the banner,
    // so a row click made from the editor is clearing a failure the user
    // never saw — the rolled-back row would be back with no explanation,
    // which is the issue's original symptom. Leaving the editor by row click
    // must therefore KEEP the retained failure, which then displays.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    let rejectDelete: (e: Error) => void = () => {}
    mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    rejectDelete(new Error('permission denied'))
    await waitFor(() => expect(screen.getByText('fragile')).toBeInTheDocument())  // rollback
    expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument()  // suppressed

    // Leave the editor via a list row click; the failure must survive and show.
    fireEvent.click(screen.getByText('fragile'))
    await waitFor(() => expect(screen.getByTestId('skill-delete-failure')).toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('a delete succeeding mid-save leaves the editor mounted so the save outcome can report', async () => {
    // A slow DELETE resolving while another skill's save is in flight must
    // not tear the editor down: the PUT's later rejection sets updateError,
    // which only the editing branch renders — unmounting it makes the save
    // fail silently and discards the draft.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    let resolveDelete: (v: unknown) => void = () => {}
    mockApi.deleteSkill.mockImplementation(() => new Promise(res => { resolveDelete = res }))
    let rejectUpdate: (e: Error) => void = () => {}
    mockApi.updateSkill.mockImplementation(() => new Promise((_, rej) => { rejectUpdate = rej }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    // Delete the auto-selected first skill; it hangs; the survivor is selected.
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

    // Open the survivor's editor and start a save that hangs.
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalledTimes(1))

    // The DELETE now succeeds — the editor must survive it…
    resolveDelete({ ok: true })
    await waitFor(() => expect(screen.getByText('Cancel')).toBeInTheDocument())

    // …so the PUT's failure has somewhere to report.
    rejectUpdate(new Error('disk full'))
    const notice = await screen.findByTestId('skill-update-failure')
    expect(notice.textContent).toMatch(/disk full/)
    confirmSpy.mockRestore()
  })

  it('a delete succeeding while another skill is merely being edited spares the editor', async () => {
    // The typed-but-unsaved case: no PUT is in flight, but the open editor
    // still holds the only copy of the draft — the delete's success teardown
    // must not unmount it.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
    let resolveDelete: (v: unknown) => void = () => {}
    mockApi.deleteSkill.mockImplementation(() => new Promise(res => { resolveDelete = res }))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderWithQuery()
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))

    // Open the survivor's editor; do NOT save.
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    resolveDelete({ ok: true })
    // The editor must remain mounted with the draft intact.
    await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())
    expect(screen.getByText('Save')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('list rows announce themselves disabled while a save is in flight', async () => {
    // selectSkill silently no-ops during the save window; the row must not
    // keep its live cursor/hover affordance while doing nothing.
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValue(TWO)
    mockApi.updateSkill.mockImplementation(() => new Promise(() => {}))

    await enterEditor()
    const row = screen.getByRole('button', { name: 'Select Other' })
    expect(row).not.toHaveAttribute('aria-disabled')

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Select Other' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('narrow-viewport Back keeps the edit session latched across a breakpoint crossing', async () => {
    // Back hides the pane; it must not END the edit session. Crossing back
    // over the desktop breakpoint re-renders the detail pane, and the latched
    // detailEditing is what restores the editor with the draft — exiting the
    // session on Back would hand the crossing the read-only browser instead,
    // and the Edit re-entry reseeds formData, discarding the draft.
    isMobileMock.value = true
    try {
      mockApi.skills.mockResolvedValue(SKILL)

      renderWithQuery()
      fireEvent.click(await screen.findByText('fragile'))
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

      // Leave via Back: the list shows, the session stays latched.
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      await waitFor(() => expect(screen.queryByText('Save')).not.toBeInTheDocument())

      // Cross to desktop; a state-bearing interaction re-renders the tab.
      isMobileMock.value = false
      fireEvent.change(screen.getByPlaceholderText(/filter skills/i), { target: { value: 'f' } })
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
    } finally {
      isMobileMock.value = false
    }
  })

  it('withholds the agent hand-off while an edit session is latched behind the list', async () => {
    // After mobile Back the banner may show above a LATCHED (hidden) editor
    // whose formData is the only copy of the draft — the hand-off's
    // navigation would unmount the tab and destroy it, so the button is
    // withheld until no session holds a draft. The failure arrives DURING
    // the edit (an already-visible banner would have been retired at entry).
    isMobileMock.value = true
    try {
      const TWO = [
        ...SKILL,
        { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
      ]
      mockApi.skills.mockResolvedValueOnce(TWO).mockImplementation(() => new Promise(() => {}))
      let rejectDelete: (e: Error) => void = () => {}
      mockApi.deleteSkill.mockImplementation(() => new Promise((_, rej) => { rejectDelete = rej }))
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

      renderWithQuery()
      // Delete A (hangs); open the survivor's editor; the failure arrives.
      fireEvent.click(await screen.findByText('fragile'))
      fireEvent.click(await screen.findByText('Delete'))
      await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('fragile'))
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())
      rejectDelete(new Error('permission denied'))

      // Back latches the session; the banner shows WITHOUT the hand-off.
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      const latched = await screen.findByTestId('skill-delete-failure')
      expect(within(latched).queryByText('Ask the agent')).not.toBeInTheDocument()

      // Dismiss it, end the latched session via its own exit, and fail a
      // FRESH delete on the unlatched list: the hand-off is offered (nothing
      // left to destroy).
      fireEvent.click(within(latched).getByLabelText('Dismiss'))
      await waitFor(() => expect(screen.queryByTestId('skill-delete-failure')).not.toBeInTheDocument())
      fireEvent.click(await screen.findByText('other'))          // reopens the latched editor
      await waitFor(() => expect(screen.getByText('Cancel')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Cancel'))                 // ends the session
      mockApi.deleteSkill.mockRejectedValue(new Error('still denied'))
      fireEvent.click(await screen.findByText('Delete'))
      const fresh = await screen.findByTestId('skill-delete-failure')
      expect(within(fresh).queryByText('Ask the agent')).toBeInTheDocument()
      confirmSpy.mockRestore()
    } finally {
      isMobileMock.value = false
    }
  })

  it('surfaces a save failure on the list when a viewport crossing hides the editor mid-save', async () => {
    // Desktop, detail never explicitly opened: the editor shows only because
    // the viewport is wide. Crossing to a phone mid-save hides it (the edit
    // session stays latched), so the PUT's later rejection must report on
    // the list instead of vanishing.
    try {
      mockApi.skills.mockResolvedValue(SKILL)
      let rejectUpdate: (e: Error) => void = () => {}
      mockApi.updateSkill.mockImplementation(() => new Promise((_, rej) => { rejectUpdate = rej }))

      renderWithQuery()
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      fireEvent.click(await screen.findByText('Save'))
      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())

      // Cross to a phone; a state-bearing interaction re-renders the tab.
      isMobileMock.value = true
      fireEvent.change(screen.getByPlaceholderText(/filter skills/i), { target: { value: 'f' } })
      await waitFor(() => expect(screen.queryByText('Saving…')).not.toBeInTheDocument())

      await act(async () => { rejectUpdate(new Error('disk full')) })
      const notice = await screen.findByTestId('skill-update-failure-list')
      expect(notice.textContent).toMatch(/disk full/)

      // A row tap reopens the latched editor, which shows the failure inline.
      fireEvent.click(screen.getByText('fragile'))
      await screen.findByTestId('skill-update-failure')
      expect(screen.queryByTestId('skill-update-failure-list')).not.toBeInTheDocument()

      // Back to the list: dismiss retires it.
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      const again = await screen.findByTestId('skill-update-failure-list')
      fireEvent.click(within(again).getByLabelText('Dismiss'))
      await waitFor(() => expect(screen.queryByTestId('skill-update-failure-list')).not.toBeInTheDocument())
    } finally {
      isMobileMock.value = false
    }
  })

  it('names a disk conflict on the list when a viewport crossing hides the editor mid-save', async () => {
    try {
      mockApi.skills.mockResolvedValue(SKILL)
      let rejectUpdate: (e: Error) => void = () => {}
      mockApi.updateSkill.mockImplementation(() => new Promise((_, rej) => { rejectUpdate = rej }))

      renderWithQuery()
      const editBtn = await screen.findByText('Edit')
      await waitFor(() => expect(editBtn).not.toBeDisabled())
      fireEvent.click(editBtn)
      fireEvent.click(await screen.findByText('Save'))
      await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())

      isMobileMock.value = true
      fireEvent.change(screen.getByPlaceholderText(/filter skills/i), { target: { value: 'f' } })
      await waitFor(() => expect(screen.queryByText('Saving…')).not.toBeInTheDocument())

      await act(async () => {
        rejectUpdate(new StubApiError(409, 'changed on disk', JSON.stringify({
          error: 'changed on disk', code: 'content_conflict',
        })))
      })
      const notice = await screen.findByTestId('skill-disk-conflict-list')
      expect(notice.textContent).toMatch(/edited after you opened it/)
      expect(notice.textContent).toContain('changed the file for “Fragile”')

      // A row tap reopens the latched editor, which carries the choices.
      fireEvent.click(screen.getByText('fragile'))
      await screen.findByTestId('skill-disk-conflict')
      expect(screen.queryByTestId('skill-disk-conflict-list')).not.toBeInTheDocument()
    } finally {
      isMobileMock.value = false
    }
  })

  it('keeps Create New Skill off while Edit re-reads the file, so a late seed cannot overwrite a new draft', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    renderWithQuery()
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    let resolveRead: (v: { name: string; content: string; hash: string }) => void = () => {}
    mockApi.skill.mockImplementation(() => new Promise(res => { resolveRead = res }))

    fireEvent.click(editBtn)
    const create = screen.getByText('Create New Skill').closest('button')!
    await waitFor(() => expect(create).toBeDisabled())

    await act(async () => { resolveRead({ name: 'fragile', content: '---\nname: fragile\n---\nbody', hash: 'h-seeded' }) })
    await screen.findByText('Save')
    expect(create).not.toBeDisabled()
  })

  it('disables Cancel while a save is in flight, so its outcome cannot report nowhere', async () => {
    mockApi.skills.mockResolvedValue(SKILL)
    mockApi.updateSkill.mockImplementation(() => new Promise(() => {}))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())
    expect(screen.getByText('Cancel')).toBeDisabled()
  })

  it('ignores row clicks while a save is in flight, keeping the editor mounted', async () => {
    const TWO = [
      ...SKILL,
      { key: 'other', name: 'other', description: 'still here', source: 'kirocrew', loaded_by_agents: [] },
    ]
    mockApi.skills.mockResolvedValue(TWO)
    mockApi.updateSkill.mockImplementation(() => new Promise(() => {}))

    await enterEditor()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())

    fireEvent.click(screen.getByText('other'))
    // The editor must survive: switching rows would discard the in-flight
    // save's outcome along with the draft. (Pending label while in flight.)
    expect(screen.getByText('Saving…')).toBeInTheDocument()
    expect(screen.getByText('Cancel')).toBeInTheDocument()
  })
})
