import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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
vi.mock('../api/client', () => ({ api: mockApi }))

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
