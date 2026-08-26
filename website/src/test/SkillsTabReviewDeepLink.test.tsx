/**
 * Candidate-level deep link from a skill notification.
 *
 * A skill notification's action navigates to
 * `/capabilities?tab=skills&review=<slug>`. Landing on the tab is not enough --
 * the queue can hold several rows, and "go find it" is the failure mode the
 * notification exists to prevent. So the panel must open THAT candidate, and say
 * something useful when it is already gone.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'

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
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div>browser</div>,
}))
vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="diff">{code}</pre>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

/** Surfaces the live URL so the "param is consumed once" assertion can read it. */
function UrlProbe() {
  const loc = useLocation()
  return <div data-testid="url">{loc.pathname + loc.search}</div>
}

function renderAt(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <UrlProbe />
        <SkillsTab />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const ROW_A = {
  slug: 'alpha-skill',
  name: 'auto/alpha-skill',
  description: 'first candidate',
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
}
const ROW_B = { ...ROW_A, slug: 'beta-skill', name: 'auto/beta-skill', description: 'second' }
/** Deep link at ROW_B, used by the tests that assert latch behaviour. */
const REVIEW_URL_B = '/capabilities?tab=skills&review=beta-skill'

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTree.mockResolvedValue({ entries: [] })
  mockApi.skillsPending.mockResolvedValue({ pending: [ROW_A, ROW_B] })
  mockApi.skillPendingDetail.mockImplementation((slug: string) =>
    Promise.resolve({ name: `auto/${slug}`, content: `BODY OF ${slug}`, scripts: [] }),
  )
})

describe('?review=<slug> deep link', () => {
  it('expands the linked candidate and only that one', async () => {
    renderAt('/capabilities?tab=skills&review=beta-skill')
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())
    expect(screen.queryByText('BODY OF alpha-skill')).not.toBeInTheDocument()
    // Detail is fetched for the linked slug only -- an expand-everything
    // implementation would fan out a request per row.
    expect(mockApi.skillPendingDetail).toHaveBeenCalledWith('beta-skill')
    expect(mockApi.skillPendingDetail).not.toHaveBeenCalledWith('alpha-skill')
  })

  it('strips the param so the highlight does not outlive the visit', async () => {
    renderAt('/capabilities?tab=skills&review=beta-skill')
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())
    await waitFor(() =>
      expect(screen.getByTestId('url').textContent).toBe('/capabilities?tab=skills'),
    )
  })

  it('keeps the row collapsible after the deep link opened it', async () => {
    renderAt('/capabilities?tab=skills&review=beta-skill')
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())
    // The auto-open effect must not fight a user who closes the row.
    fireEvent.click(screen.getByRole('button', { name: 'Hide' }))
    await waitFor(() =>
      expect(screen.queryByText('BODY OF beta-skill')).not.toBeInTheDocument(),
    )
  })

  it('explains a candidate that is no longer pending', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    renderAt('/capabilities?tab=skills&review=gone-skill')
    await waitFor(() =>
      expect(
        screen.getByText(/no longer awaiting review/i),
      ).toBeInTheDocument(),
    )
  })

  it('does not flash the gone notice before the queue has been read', async () => {
    let resolve!: (v: { pending: typeof ROW_A[] }) => void
    mockApi.skillsPending.mockReturnValue(new Promise(r => { resolve = r }))
    renderAt('/capabilities?tab=skills&review=beta-skill')
    // In flight: `pending` is [] but nothing is known yet.
    expect(screen.queryByText(/no longer awaiting review/i)).not.toBeInTheDocument()
    resolve({ pending: [ROW_B] })
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())
    expect(screen.queryByText(/no longer awaiting review/i)).not.toBeInTheDocument()
  })

  it('does not report the candidate gone when the USER approves it', async () => {
    // Regression: the deep-link latch outlived the user's own action, so
    // approving the row you arrived at flipped reviewMissing and claimed the
    // candidate "was approved or dismissed" one click after you approved it --
    // and when it was the only row, that sentence was the whole panel.
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [ROW_B] })
    mockApi.approvePendingSkill.mockResolvedValue({ ok: true })
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    renderAt('/capabilities?tab=skills&review=beta-skill')
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('beta-skill'))
    // The queue is now empty AND the latch is cleared, so the panel unmounts
    // rather than accusing someone else of resolving the user's own candidate.
    await waitFor(() =>
      expect(screen.queryByText('BODY OF beta-skill')).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/no longer awaiting review/i)).not.toBeInTheDocument()
  })

  it('does not report the candidate gone when the USER dismisses it', async () => {
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [ROW_B] })
    mockApi.dismissPendingSkill.mockResolvedValue({ ok: true })
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderAt('/capabilities?tab=skills&review=beta-skill')
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('beta-skill'))
    // Waiting for the row to GO is what makes this pin the guard: the mock being
    // called only proves the request fired, which precedes the refetch, so the
    // assertion below would otherwise be checking a moment when the list is not
    // yet empty and reviewMissing could not be true either way.
    await waitFor(() =>
      expect(screen.queryByText('BODY OF beta-skill')).not.toBeInTheDocument(),
    )
    expect(screen.queryByText(/no longer awaiting review/i)).not.toBeInTheDocument()
  })

  it('does not serve a PRIOR candidate’s cached detail for a reused slug', async () => {
    // Regression: the latch auto-expands the row, so the detail query served a
    // cache entry left by an earlier candidate that reused the same slug (30s
    // staleTime, 5min gcTime) while `Approve` is enabled on `!!detail` -- the
    // user could approve content they never saw. Seed the cache with the OLD
    // body and assert the deep link shows the NEW one.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    })
    qc.setQueryData(['skills-pending-detail', 'beta-skill'], {
      name: 'auto/beta-skill',
      content: 'STALE BODY FROM A PRIOR CANDIDATE',
      scripts: [],
    })
    mockApi.skillsPending.mockResolvedValue({ pending: [ROW_B] })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[REVIEW_URL_B]}>
          <UrlProbe />
          <SkillsTab />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByText('BODY OF beta-skill')).toBeInTheDocument())
    expect(screen.queryByText('STALE BODY FROM A PRIOR CANDIDATE')).not.toBeInTheDocument()
  })

  it('renders the queue normally with no deep link', async () => {
    renderAt('/capabilities?tab=skills')
    await waitFor(() => expect(screen.getByText('auto/alpha-skill')).toBeInTheDocument())
    expect(screen.queryByText('BODY OF alpha-skill')).not.toBeInTheDocument()
    expect(screen.queryByText(/no longer awaiting review/i)).not.toBeInTheDocument()
  })
})
