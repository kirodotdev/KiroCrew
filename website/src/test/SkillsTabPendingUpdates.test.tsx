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
})

describe('SkillsTab pending updates', () => {
  it('marks an update candidate with an Update badge and names its target', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    renderWithQuery()
    expect(await screen.findByText('Update')).toBeTruthy()
    expect(
      screen.getByText(/Adds new requirements to auto\/deploy-helper/),
    ).toBeTruthy()
  })

  it('shows the server-computed diff with the version transition on Review', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '## Steps\nnew\n',
      scripts: [],
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
      scripts: [],
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
      scripts: [],
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

  it('still renders a plain new candidate as raw SKILL.md, with no badge', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    renderWithQuery()
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
      scripts: [],
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
      expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('deploy-helper-update'),
    )
  })
})
