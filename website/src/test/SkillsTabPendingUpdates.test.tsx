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
  skillsAudit: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
  approvePendingSkill: vi.fn(),
  dismissPendingSkill: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

// DiffBlock is exercised by its own tests; here we only assert SkillsTab feeds
// it the server-computed unified diff.
vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="diff">{code}</pre>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

// The audit modal (pages/overview/SkillsAuditModal.tsx) mounts after its query
// settles. On a loaded CI shard that can outlast findBy*/waitFor's 1 s default,
// so every wait for the modal, its query or its contents names this timeout.
const AUDIT_MOUNT = { timeout: 5000 }

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // MemoryRouter: the pending-review panel reads (and clears) the `?review=<slug>`
  // deep link a skill notification points at, so the tab needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const UPDATE_ROW = {
  slug: 'deploy-helper-update',
  name: 'auto/deploy-helper-update',
  description: 'handles the new retry flag',
  has_scripts: false,
  kind: 'update',
  target: 'auto/deploy-helper',
  base_version: 2,
}

const NEW_ROW = {
  slug: 'fresh-skill',
  name: 'auto/fresh-skill',
  description: 'brand new procedure',
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
}

const DIFF = '--- live\n+++ proposed\n@@ -1,2 +1,2 @@\n-old step\n+new step\n'

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ pending: [] })
  mockApi.skillsAudit.mockResolvedValue({ clusters: [] })
})

describe('SkillsTab pending updates', () => {
  it('marks an update candidate with an Update badge and names its target', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    renderWithQuery()
    expect(await screen.findByText('Update')).toBeTruthy()
    expect(
      screen.getByText(/Adds new requirements to auto\/deploy-helper/),
    ).toBeTruthy()
    // An update changes a skill that already exists, so it is not marked New.
    expect(screen.queryByText('New')).toBeNull()
  })

  it('shows a related live skill and offers no way to change it', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'subsumed',
        score: 0.75,
        members: [
          { id: 'pending:fresh-skill', kind: 'pending', name: 'auto/fresh-skill', slug: 'fresh-skill' },
          { id: 'live:auto/deploy-helper', kind: 'live', name: 'auto/deploy-helper' },
        ],
        relations: [{
          classification: 'subsumed',
          score: 0.75,
          members: ['pending:fresh-skill', 'live:auto/deploy-helper'],
        }],
      }],
    })

    renderWithQuery()

    expect(await screen.findByRole('button', {
      name: 'auto/deploy-helper (Covers this candidate)',
    })).toBeTruthy()
    // The audit only reports: approval and dismissal stay the row's own buttons.
    expect(screen.queryByRole('button', { name: /Propose update/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeTruthy()
  })

  it('lists every related live skill, highest similarity first', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'duplicate',
        score: 0.9,
        members: [
          { id: 'pending:fresh-skill', kind: 'pending', name: 'auto/fresh-skill', slug: 'fresh-skill' },
          { id: 'live:auto/deploy-backup', kind: 'live', name: 'auto/deploy-backup' },
          { id: 'live:hand/deploy', kind: 'live', name: 'hand/deploy' },
          { id: 'live:auto/deploy-helper', kind: 'live', name: 'auto/deploy-helper' },
        ],
        relations: [
          { classification: 'subsumed', score: 0.55, members: ['pending:fresh-skill', 'live:auto/deploy-backup'] },
          { classification: 'duplicate', score: 0.9, members: ['pending:fresh-skill', 'live:hand/deploy'] },
          { classification: 'subsumed', score: 0.6, members: ['pending:fresh-skill', 'live:auto/deploy-helper'] },
        ],
      }],
    })

    renderWithQuery()

    await screen.findByRole('button', { name: 'hand/deploy (Duplicate)' })
    const related = screen.getAllByRole('button', { name: /^(auto|hand)\/deploy/ }).map(b => b.textContent)
    expect(related).toEqual([
      'hand/deploy (Duplicate)',
      'auto/deploy-helper (Covers this candidate)',
      'auto/deploy-backup (Covers this candidate)',
    ])
  })

  it('opens a related skill in a modal focused on its cluster', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [
        {
          classification: 'subsumed',
          score: 0.75,
          members: [
            { id: 'pending:fresh-skill', kind: 'pending', name: 'auto/fresh-skill', slug: 'fresh-skill' },
            { id: 'live:auto/deploy-helper', kind: 'live', name: 'auto/deploy-helper' },
          ],
          relations: [{ classification: 'subsumed', score: 0.75, members: ['pending:fresh-skill', 'live:auto/deploy-helper'] }],
        },
        {
          classification: 'duplicate',
          score: 1,
          members: [
            { id: 'pending:other', kind: 'pending', name: 'auto/other', slug: 'other' },
            { id: 'live:auto/unrelated', kind: 'live', name: 'auto/unrelated' },
          ],
          relations: [{ classification: 'duplicate', score: 1, members: ['pending:other', 'live:auto/unrelated'] }],
        },
      ],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', {
      name: 'auto/deploy-helper (Covers this candidate)',
    }))

    expect(await screen.findByTestId('skills-audit-modal', undefined, AUDIT_MOUNT)).toHaveTextContent('auto/deploy-helper')
    expect(screen.getByTestId('skills-audit-modal')).not.toHaveTextContent('auto/unrelated')
  })

  it('reports a failed audit instead of showing no related skills', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillsAudit.mockRejectedValue(new Error('audit unavailable'))

    renderWithQuery()

    const notice = await screen.findByTestId('skill-audit-related-failure')
    expect(notice).toHaveTextContent('Could not check related skills.')
    expect(notice).toHaveTextContent('audit unavailable')
  })

  it('refetches the audit when a clicked member is no longer listed', async () => {
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'overlapping',
        score: 0.6,
        members: [
          { id: 'live:auto/gone-one', kind: 'live', name: 'auto/gone-one' },
          { id: 'live:auto/gone-two', kind: 'live', name: 'auto/gone-two' },
        ],
        relations: [{
          classification: 'overlapping',
          score: 0.6,
          members: ['live:auto/gone-one', 'live:auto/gone-two'],
        }],
      }],
    })

    renderWithQuery()

    fireEvent.click(await screen.findByRole('button', { name: /Find overlapping skills/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'auto/gone-one' }, AUDIT_MOUNT))

    expect(await screen.findByTestId('skills-audit-selection-failure', undefined, AUDIT_MOUNT)).toBeTruthy()
    await waitFor(() => expect(mockApi.skillsAudit).toHaveBeenCalledTimes(2), AUDIT_MOUNT)
  })

  it('shows loading, then an empty state, in the related-skills modal', async () => {
    let resolveAudit: (value: { clusters: never[] }) => void = () => {}
    mockApi.skillsAudit.mockReturnValue(
      new Promise(resolve => { resolveAudit = resolve }),
    )
    mockApi.skills.mockResolvedValue([{
      key: 'auto/deploy-helper',
      name: 'auto/deploy-helper',
      description: 'd',
      source: 'kirocrew',
      loaded_by_agents: [],
    }])

    renderWithQuery()

    fireEvent.click(await screen.findByRole('button', { name: /Find overlapping skills/ }))
    expect(await screen.findByTestId('skills-audit-loading', undefined, AUDIT_MOUNT)).toBeTruthy()
    resolveAudit({ clusters: [] })
    expect(await screen.findByTestId('skills-audit-empty', undefined, AUDIT_MOUNT)).toHaveTextContent('No overlapping skills.')
  })

  it('shows the server-computed diff with the version transition on Review', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '## Steps\nnew\n',
      scripts: [], content_sha256: 'c'.repeat(64),
      diff: DIFF,
      live_body: 'old',
      proposed_body: 'new',
      from_version: 2,
      to_version: 3,
      stale_base: false,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByTestId('diff')).toBeTruthy())
    expect(screen.getByTestId('diff').textContent).toContain('+new step')
    expect(screen.getByText(/v2 → v3/)).toBeTruthy()
  })

  it('blocks approval when the live skill advanced past the update base version', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [], content_sha256: 'c'.repeat(64),
      diff: DIFF,
      from_version: 5,
      to_version: 6,
      stale_base: true,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    expect(
      await screen.findByText(/would undo those newer changes/),
    ).toBeTruthy()
    // The backend refuses a stale approval, so the button must not invite it.
    expect(screen.getByText('Approve').closest('button')!.disabled).toBe(true)
  })

  it('tells the user to dismiss an update whose target is gone', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '## Steps\nnew\n',
      scripts: [], content_sha256: 'c'.repeat(64),
      diff: null,
      live_body: null,
      stale_base: false,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    expect(
      await screen.findByText(/no longer exists, so there is nothing/),
    ).toBeTruthy()
    expect(screen.queryByTestId('diff')).toBeNull()
    // Approving an orphaned update would 409 — the button must stay disabled.
    expect(screen.getByText('Approve').closest('button')!.disabled).toBe(true)
  })

  it('marks a plain new candidate New and renders it as raw SKILL.md', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [], content_sha256: 'c'.repeat(64),
    })
    renderWithQuery()
    // The queue mounts only after the `skills` read settles, and then fetches
    // `skills-pending` itself, so this waits on two chained queries: give it an
    // explicit bound instead of findBy's 1 s default.
    expect(await screen.findByText('New', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText('Update')).toBeNull()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByText(/run it/)).toBeTruthy())
    expect(screen.queryByTestId('diff')).toBeNull()
  })

  it('approves an update through the same approve endpoint', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [], content_sha256: 'c'.repeat(64),
      diff: DIFF,
      from_version: 2,
      to_version: 3,
      stale_base: false,
    })
    mockApi.approvePendingSkill.mockResolvedValue({ ok: true })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByTestId('diff')).toBeTruthy())
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() =>
      expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('deploy-helper-update', 'c'.repeat(64)),
    )
  })
})
