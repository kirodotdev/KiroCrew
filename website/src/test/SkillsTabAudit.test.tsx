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
  skillsBudget: vi.fn(),
  skillsAudit: vi.fn(),
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

// The audit modal (pages/overview/SkillsAuditModal.tsx) mounts after its query
// settles. On a loaded CI shard that can outlast findBy*/waitFor's 1 s default,
// so every wait for the modal, its query or its contents names this timeout.
const AUDIT_MOUNT = { timeout: 5000 }

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

describe('SkillsTab overlap audit', () => {
  it('opens the audit modal in the same render as the click', async () => {
    // The modal ships in this tab's chunk: no second chunk request can fail or
    // hold the dialog back, so it is in the document before any await.
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockReturnValue(new Promise(() => {}))
    renderWithQuery()
    await screen.findByText('Deploy One')

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))

    expect(screen.getByRole('dialog', { name: 'Overlapping skills' })).toBeInTheDocument()
  })

  it('lists queue-wide audit clusters and links each member to its skill row', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'deploy-two', name: 'deploy-two', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'overlapping',
        score: 0.5,
        members: [
          { id: 'live:deploy-one', kind: 'live', name: 'deploy-one' },
          { id: 'live:deploy-two', kind: 'live', name: 'deploy-two' },
        ],
        relations: [],
      }],
    })
    renderWithQuery()
    await screen.findByText('Deploy One')

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))

    await waitFor(() => expect(mockApi.skillsAudit).toHaveBeenCalledTimes(1), AUDIT_MOUNT)
    // Await an element that only exists once audit results render. /Overlapping/
    // also matches the modal title, which renders before the results arrive.
    expect(await screen.findByTestId('skills-audit-similarity', undefined, AUDIT_MOUNT)).toHaveTextContent('50% similar')
    expect(screen.getByRole('button', { name: 'deploy-one' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'deploy-two' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Select Deploy Two' }))
        .toHaveAttribute('aria-current', 'true'),
    )
  })

  it('keeps an open draft when a live audit member is clicked', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'deploy-two', name: 'deploy-two', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({
      name: 'deploy-one',
      content: '---\nname: deploy-one\ndescription: first\n---\nbody text',
    })
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'overlapping',
        score: 0.5,
        members: [
          { id: 'live:deploy-one', kind: 'live', name: 'deploy-one' },
          { id: 'live:deploy-two', kind: 'live', name: 'deploy-two' },
        ],
        relations: [],
      }],
    })
    renderWithQuery()
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'deploy-two' }, AUDIT_MOUNT))

    expect(await screen.findByTestId('skills-audit-selection-failure', undefined, AUDIT_MOUNT)).toHaveTextContent(
      'Save or cancel your open skill draft first, then pick another skill.',
    )
    const draftNotice = screen.getByTestId('skills-audit-selection-failure')
    expect(within(draftNotice).queryByRole('button', { name: /agent/i })).toBeNull()
    expect(within(draftNotice).queryByRole('link', { name: /agent/i })).toBeNull()
    expect(screen.getByTestId('skills-audit-modal')).toBeInTheDocument()
    expect(screen.getByText('Save')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Select Deploy One' }))
      .toHaveAttribute('aria-current', 'true')
  })

  it('offers no Ask-agent hand-off for an audit failure while a draft is open', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({
      name: 'deploy-one',
      content: '---\nname: deploy-one\ndescription: first\n---\nbody text',
    })
    mockApi.skillsAudit.mockRejectedValue(new Error('audit exploded'))
    renderWithQuery()
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    await waitFor(() => expect(screen.getByText('Save')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))

    const failures = await screen.findAllByTestId(/audit.*failure/, undefined, AUDIT_MOUNT)
    for (const failure of failures) {
      expect(within(failure).queryByRole('button', { name: /agent/i })).toBeNull()
      expect(within(failure).queryByRole('link', { name: /agent/i })).toBeNull()
    }
  })

  it('announces relationships the server trimmed inside a cluster', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'deploy-two', name: 'deploy-two', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      omitted_relations: 1,
      clusters: [{
        classification: 'overlapping',
        score: 0.5,
        members: [
          { id: 'live:deploy-one', kind: 'live', name: 'deploy-one' },
          { id: 'live:deploy-two', kind: 'live', name: 'deploy-two' },
        ],
        relations: [],
        omitted_relations: 3,
      }],
    })
    renderWithQuery()
    await screen.findByText('Deploy One')

    fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))

    expect(await screen.findByTestId('skills-audit-omitted-relations', undefined, AUDIT_MOUNT)).toHaveTextContent('+4')
  })

})

describe('SkillsTab audit result bounds', () => {
  it('caps member rows and reports omitted members and clusters', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'known', name: 'known', description: 'known', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      total_clusters: 20,
      omitted_entries: 4,
      omitted_relations: 7,
      clusters: [{
        classification: 'duplicate',
        score: 0.91,
        members: Array.from({ length: 10 }, (_, index) => ({
          id: `live:member-${index}`,
          kind: 'live',
          name: `member-${index}`,
        })),
        relations: [],
      }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))

    expect(await screen.findByTestId('skills-audit-similarity', undefined, AUDIT_MOUNT)).toHaveTextContent('91% similar')
    expect(screen.getByRole('button', { name: 'member-7' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'member-8' })).not.toBeInTheDocument()
    expect(screen.getByText('+2 more')).toBeInTheDocument()
    expect(screen.getByTestId('skills-audit-omitted-clusters')).toHaveTextContent('+19 more')
    expect(screen.getByTestId('skills-audit-omitted-entries')).toHaveTextContent('+4 skills not compared')
    expect(screen.getByTestId('skills-audit-omitted-relations')).toHaveTextContent('+7 relationships not shown')
  })

  it('uses the singular form when one skill or relationship is omitted', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'known', name: 'known', description: 'known', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      total_clusters: 1,
      omitted_entries: 1,
      omitted_relations: 1,
      clusters: [{
        classification: 'duplicate',
        score: 0.91,
        members: [
          { id: 'live:a', kind: 'live', name: 'a' },
          { id: 'live:b', kind: 'live', name: 'b' },
        ],
        relations: [],
      }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))

    const entries = await screen.findByTestId('skills-audit-omitted-entries', undefined, AUDIT_MOUNT)
    // Without _one keys i18next falls back to the base key: "+1 skills not compared".
    expect(entries).toHaveTextContent(/^\+1 skill not compared$/)
    expect(screen.getByTestId('skills-audit-omitted-relations')).toHaveTextContent(/^\+1 relationship not shown$/)
  })

  it('keeps the modal open and explains when a live member vanished', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'known', name: 'known', description: 'known', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'overlapping',
        score: 0.5,
        members: [{ id: 'live:missing', kind: 'live', name: 'missing' }],
        relations: [],
      }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'missing' }, AUDIT_MOUNT))

    expect(screen.getByTestId('skills-audit-modal')).toBeInTheDocument()
    expect(screen.getByTestId('skills-audit-selection-failure')).toHaveTextContent(
      'That skill is no longer in this list. Refresh and try again.',
    )
  })

  it('keeps the modal open when the list filter hides a live member', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'known', name: 'known', description: 'known', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'other', name: 'other', description: 'other', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'overlapping',
        score: 0.5,
        members: [{ id: 'live:known', kind: 'live', name: 'known' }],
        relations: [],
      }],
    })

    renderWithQuery()
    const filter = await screen.findByPlaceholderText(/filter/i)
    fireEvent.change(filter, { target: { value: 'other' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'known' }, AUDIT_MOUNT))

    expect(screen.getByTestId('skills-audit-modal')).toBeInTheDocument()
    expect(screen.getByTestId('skills-audit-selection-failure')).toHaveTextContent(
      'That skill is no longer in this list. Refresh and try again.',
    )
  })

  it('badges pending members and keeps the modal open when one vanished', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'known', name: 'known', description: 'known', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'subsumed',
        score: 0.9,
        members: [
          { id: 'pending:gone', kind: 'pending', name: 'auto/gone', slug: 'gone' },
          { id: 'live:known', kind: 'live', name: 'known' },
        ],
        relations: [],
      }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))

    expect(await screen.findByText('Overlapping skills', undefined, AUDIT_MOUNT)).toBeInTheDocument()
    expect(await screen.findAllByTestId('skills-audit-pending-badge', undefined, AUDIT_MOUNT)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'auto/gone' }))

    expect(screen.getByTestId('skills-audit-modal')).toBeInTheDocument()
    expect(screen.getByTestId('skills-audit-selection-failure')).toHaveTextContent(
      'That skill is no longer in this list. Refresh and try again.',
    )
  })
})
