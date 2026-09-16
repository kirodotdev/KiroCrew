import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
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
vi.mock('../api/client', () => ({
  api: mockApi,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

let qcRef: QueryClient | null = null

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qcRef = qc
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The redacted report shape the backend serves for a flagged candidate. */
const REPORT = { 'evil.py': ["dynamic exec/import: eval()"] }

const FLAGGED_ROW = {
  slug: 'evil-helper',
  name: 'auto/evil-helper',
  description: 'does bad things',
  has_scripts: true,
  kind: 'new',
  target: null,
  base_version: null,
  script_validation: { ok: false, report: REPORT },
}

const CLEAN_ROW = {
  slug: 'good-helper',
  name: 'auto/good-helper',
  description: 'does good things',
  has_scripts: true,
  kind: 'new',
  target: null,
  base_version: null,
  script_validation: { ok: true, report: {} },
}

const FLAGGED_DETAIL = {
  name: 'auto/evil-helper',
  content: '## Steps\n1. go\n',
  scripts: [{ filename: 'evil.py', content: "x = eval('1+1')\n" }],
  script_validation: { ok: false, report: REPORT },
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTree.mockResolvedValue({ tree: [] })
  mockApi.skillPendingDetail.mockResolvedValue(FLAGGED_DETAIL)
})

describe('pending-card script-validation verdict (issue #10861)', () => {
  it('renders a fails-validation badge from the list entry, before any click', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW, CLEAN_ROW] })
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    // Flagged card carries the badge; the clean card does not.
    expect(screen.getAllByText('fails validation')).toHaveLength(1)
  })

  it('shows the expandable findings warning in the expanded review panel', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    await waitFor(() =>
      expect(
        screen.getByText(/Bundled scripts fail validation/),
      ).toBeInTheDocument(),
    )
    // The findings themselves are inside the <details> body.
    expect(screen.getByText("dynamic exec/import: eval()")).toBeInTheDocument()
    // Approve stays clickable — the server is the authority.
    const approve = screen.getByText('Approve').closest('button')
    await waitFor(() => expect(approve).not.toBeDisabled())
  })

  it('renders the refusal reason and findings after a failed approve', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    const body = JSON.stringify({
      error: 'script validation failed',
      code: 'script_validation_failed',
      report: REPORT,
    })
    // Duck-typed ApiError shape (status + body), per api/apiError.ts.
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('script validation failed'), { status: 422, body }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText('Approval refused: the bundled scripts failed validation.'),
      ).toBeInTheDocument(),
    )
    // The per-file findings render next to the card (post-click evidence),
    // in addition to the pre-approval copy inside the expanded panel.
    expect(screen.getAllByText("dynamic exec/import: eval()").length).toBeGreaterThanOrEqual(1)
  })

  it('maps a live-exists refusal to its own message', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [CLEAN_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/good-helper',
      content: 'body',
      scripts: [],
      script_validation: { ok: true, report: {} },
    })
    const body = JSON.stringify({ error: 'a live skill with this name already exists', code: 'live_skill_exists' })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('conflict'), { status: 409, body }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText('Approval refused: a live skill with this name already exists.'),
      ).toBeInTheDocument(),
    )
    // No findings list for a non-validation refusal.
    expect(screen.queryByText("dynamic exec/import: eval()")).not.toBeInTheDocument()
  })

  it('dismissing the candidate clears its refusal, so a re-staged slug starts clean', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    const body = JSON.stringify({
      error: 'script validation failed',
      code: 'script_validation_failed',
      report: REPORT,
    })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('script validation failed'), { status: 422, body }),
    )
    mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Review'))
      const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
      await waitFor(() => expect(approve).not.toBeDisabled())
      fireEvent.click(approve)
      await waitFor(() =>
        expect(
          screen.getByText('Approval refused: the bundled scripts failed validation.'),
        ).toBeInTheDocument(),
      )
      // Dismiss the candidate; the row (still rendered from the stale list
      // mock) must no longer carry the previous candidate's refusal notice.
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('evil-helper'))
      await waitFor(() =>
        expect(
          screen.queryByText('Approval refused: the bundled scripts failed validation.'),
        ).not.toBeInTheDocument(),
      )
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a failed dismiss surfaces a named error notice instead of failing silently', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    mockApi.dismissPendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('evil-helper'))
      // The mutation's rejection must not vanish: the panel renders the
      // localized failure through ErrorNotice (errors-use-error-notice —
      // the value originates in a rejected mutation, so the shared error
      // surface with its Ask-agent hand-off is mandatory), NAMING the
      // candidate and mapping the coded reason to catalog text.
      await waitFor(() =>
        expect(
          screen.getByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending).'),
        ).toBeInTheDocument(),
      )
      expect(screen.getByText('Ask the agent')).toBeInTheDocument()
      // And the queue is re-read so the stale row cannot contradict the
      // notice (same recovery as the approve path's not-found branch).
      expect(mockApi.skillsPending.mock.calls.length).toBeGreaterThan(1)
      // A retry starts clean: the next dismiss click clears the stale notice
      // before the new mutation settles.
      mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() =>
        expect(
          screen.queryByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending).'),
        ).not.toBeInTheDocument(),
      )
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a not-found approve refusal survives the refetch that removes its row', async () => {
    // First fetch renders the card; the refetch the refusal triggers returns
    // an empty queue, unmounting the row and any per-row notice with it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    // The row disappears (queue re-read as empty) …
    await waitFor(() => expect(screen.queryByText('auto/evil-helper')).not.toBeInTheDocument())
    // … but the refusal does NOT: it moved to the panel, so the user never
    // mistakes the removal for a successful approve.
    expect(
      screen.getByText('Approval failed: this candidate is no longer pending.'),
    ).toBeInTheDocument()
  })

  it('the panel notice is evicted when its subject slug is restaged', async () => {
    // Fetch 1 renders the card; fetch 2 (after the refusal) is empty; fetch 3
    // brings a RESTAGED candidate under the same slug with a new created_at —
    // the stale "no longer pending" notice must not sit above it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValueOnce({ pending: [] })
      .mockResolvedValue({ pending: [{ ...FLAGGED_ROW, created_at: '2026-09-15T21:00:00Z' }] })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText('Approval failed: this candidate is no longer pending.'),
      ).toBeInTheDocument(),
    )
    // The restage poll returns the slug with a fresh created_at.
    await act(() => qcRef!.invalidateQueries({ queryKey: ['skills-pending'] }))
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    await waitFor(() =>
      expect(
        screen.queryByText('Approval failed: this candidate is no longer pending.'),
      ).not.toBeInTheDocument(),
    )
  })
})
