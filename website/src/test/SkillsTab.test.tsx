import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
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

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  // MemoryRouter: the pending-review panel reads (and clears) the `?review=<slug>`
  // deep link a skill notification points at, so the tab needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
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
    mockApi.skill.mockResolvedValue({ name: 'doomed', content: '---\nname: doomed\n---\n' })
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
    })

    renderWithQuery()

    // The Edit button is disabled while content loads.  Wait for it to enable.
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
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
