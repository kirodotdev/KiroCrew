import { describe, it, expect, vi, beforeEach } from 'vitest'

import { repoScopeKey } from '../apps/issue-radar/lib/links'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The Tagging dashboard's whole contract is "nothing reaches GitHub until you
// confirm", so every test here asserts on what the api layer was ASKED to do.
const api = {
  tagging: vi.fn(),
  generateTagging: vi.fn(),
  applyLabels: vi.fn(),
  applyLabelsBulk: vi.fn(),
  labels: vi.fn(),
  getRecommendations: vi.fn(),
  generateRecommendations: vi.fn(),
  createLabel: vi.fn(),
  putSettings: vi.fn(),
  getSettings: vi.fn(),
  addSettingLabel: vi.fn(),
}
vi.mock('../apps/issue-radar/api', () => ({
  issueRadarApi: api,
  DEFAULT_REPO_SETTINGS: {
    triage_labels: [], unlabeled_is_untriaged: true,
    good_first_issue_labels: [], notify_on_new_issue: false,
  },
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))

const TaggingView = (await import('../apps/issue-radar/views/TaggingView')).default

const LABELS = [
  { name: 'bug', color: 'ee0000', description: 'Something is broken' },
  { name: 'docs', color: '0000ee', description: 'Documentation' },
]
const TITLES: Record<string, string> = { '7': 'Crash on start', '8': 'Typo in readme' }
const ISSUES = [
  { number: 7, title: 'Crash on start', url: 'https://github.com/o/r/issues/7', labels: [], comments: 0, updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z', author: 'alice' },
  { number: 8, title: 'Typo in readme', url: 'https://github.com/o/r/issues/8', labels: [], comments: 1, updated_at: '2026-07-02T00:00:00Z', created_at: '2026-07-02T00:00:00Z', author: 'bob' },
]

function setCtx(over: Record<string, unknown> = {}) {
  ctx.value = {
    active: { owner: 'o', repo: 'r' },
    repoLabels: LABELS,
    canWrite: true,
    issues: ISSUES,
    labelsLoading: false,
    labelsError: null,
    toggleLabel: vi.fn(),
    openIssues: vi.fn(),
    repoSettings: {
      triage_labels: [], unlabeled_is_untriaged: true,
      good_first_issue_labels: [], notify_on_new_issue: false,
    },
    ...over,
  }
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><TaggingView /></QueryClientProvider>,
  )
}

/** The row (card) for one issue, located by its #number link. */
function card(n: number): HTMLElement {
  const link = screen.getByText(`#${n}`)
  // The row container: #number link → the single flex line → the row
  return link.closest('div.rounded-md') as HTMLElement
}

beforeEach(() => {
  vi.clearAllMocks()
  setCtx()
  api.tagging.mockResolvedValue({
    owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4,
    suggestions: {}, generated_at: null, batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
  })
  api.getRecommendations.mockResolvedValue({
    owner: 'o', repo: 'r', recommendations: null, generated_at: null, from_cache: false,
  })
})

describe('TaggingView', () => {
  it('lists every untagged issue in the order the server returned', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    expect(screen.getByText('Typo in readme')).toBeTruthy()
    // Untagged KPI reflects the queue, not the whole open set.
    expect(screen.getByText('Untagged')).toBeTruthy()
  })

  it('says so plainly when nothing is untagged', async () => {
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [], untagged: [], open_count: 4,
      suggestions: {}, generated_at: null, batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
    })
    renderView()
    await waitFor(() => expect(screen.getByText(/carries at least one label/)).toBeTruthy())
  })

  it('does not offer to apply anything before suggestions exist', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    const applyAll = screen.getByRole('button', { name: /Apply 0 suggestions/ })
    expect(applyAll).toHaveProperty('disabled', true)
  })

  it('stages the suggestions a generate returns without writing them', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: { '7': [{ name: 'bug', reason: 'reports a crash' }] },
      analyzed: [7, 8], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    // The row is one line high, so a proposal's reason lives in its chip tooltip.
    await waitFor(() => expect(
      within(card(7)).getByTitle(/reports a crash/),
    ).toBeTruthy())
    // Staged, NOT applied.
    expect(api.applyLabels).not.toHaveBeenCalled()
    expect(api.applyLabelsBulk).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy()
  })

  it('sends only the staged labels when applying the whole batch', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: {
        '7': [{ name: 'bug', reason: 'crash' }],
        '8': [{ name: 'docs', reason: 'typo' }],
      },
      analyzed: [7, 8], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    api.applyLabelsBulk.mockResolvedValue({
      owner: 'o', repo: 'r',
      applied: [{ number: 7, labels: [] }, { number: 8, labels: [] }], failed: [],
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: /Apply 2 suggestions/ }))
    await waitFor(() => expect(api.applyLabelsBulk).toHaveBeenCalledTimes(1))
    expect(api.applyLabelsBulk).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [
      { number: 8, add: ['docs'] },
      { number: 7, add: ['bug'] },
    ])
    // Applied rows STAY, frozen and marked — the list must not jump.
    await waitFor(() => expect(screen.getAllByText('Added')).toHaveLength(2))
    expect(screen.getByText('Crash on start')).toBeTruthy()
    expect(screen.getByText(/Labelled 2 issues/)).toBeTruthy()
    // …and they drop out of what a later Apply would write.
    expect(screen.getByRole('button', { name: /Apply 0 suggestions/ })).toHaveProperty('disabled', true)
  })

  it('unstaging a suggested chip removes it from what would be applied', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      analyzed: [7], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())

    await userEvent.click(within(card(7)).getByRole('button', { name: /^bug/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Apply 0 suggestions/ })).toHaveProperty('disabled', true))
  })

  it('narrows the bulk apply to the selection when one exists', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: {
        '7': [{ name: 'bug', reason: 'crash' }],
        '8': [{ name: 'docs', reason: 'typo' }],
      },
      analyzed: [7, 8], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    api.applyLabelsBulk.mockResolvedValue({
      owner: 'o', repo: 'r', applied: [{ number: 7, labels: [] }], failed: [],
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toBeTruthy())

    await userEvent.click(screen.getByLabelText('Select issue #7'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 1 suggestion$/ }))
    await waitFor(() => expect(api.applyLabelsBulk).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [{ number: 7, add: ['bug'] }]))
  })

  it('applies one issue on its own', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      analyzed: [7], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    api.applyLabels.mockResolvedValue({ owner: 'o', repo: 'r', number: 7, labels: [] })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())

    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #7' }))
    await waitFor(() => expect(api.applyLabels).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), 7, ['bug'], []))
    await waitFor(() => expect(screen.getByText('Added')).toBeTruthy())
    expect(screen.getByText('Crash on start')).toBeTruthy()
  })

  it('reports a partial bulk failure instead of claiming success', async () => {
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r',
      suggestions: {
        '7': [{ name: 'bug', reason: 'crash' }],
        '8': [{ name: 'docs', reason: 'typo' }],
      },
      analyzed: [7, 8], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    })
    api.applyLabelsBulk.mockResolvedValue({
      owner: 'o', repo: 'r',
      applied: [{ number: 7, labels: [] }],
      failed: [{ number: 8, error: 'issue is locked' }],
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 2 suggestions/ }))

    await waitFor(() => expect(screen.getByText(/1 could not be updated/)).toBeTruthy())
    // The failed issue stays in the queue, carrying its reason.
    expect(screen.getByText('Typo in readme')).toBeTruthy()
    expect(screen.getByText('issue is locked')).toBeTruthy()
  })

  it('disables every write on a read-only repo but still suggests', async () => {
    setCtx({ canWrite: false })
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toHaveProperty('disabled', true)
    expect(screen.getByRole('button', { name: 'Add labels to #7' })).toHaveProperty('disabled', true)
    // This fixture is fully analysed, so the batch button reads "Suggest again".
    expect(screen.getByRole('button', { name: 'Suggest again (1)' })).toHaveProperty('disabled', false)
  })

  it('blocks suggesting when the repo defines no labels at all', async () => {
    setCtx({ repoLabels: [] })
    renderView()
    // Both the Labels panel and the queue banner say so — assert on the count,
    // not a unique match, so the copy can live in both places.
    await waitFor(() => expect(screen.getAllByText(/defines no labels yet/).length).toBeGreaterThan(0))
    expect(screen.getByRole('button', { name: /^Suggest labels \(next/ })).toHaveProperty('disabled', true)
  })

  it('surfaces a generate failure rather than failing silently', async () => {
    api.generateTagging.mockRejectedValue(new Error('gateway is down'))
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /^Suggest labels \(next/ }))
    await waitFor(() => expect(screen.getByText('gateway is down')).toBeTruthy())
  })

  it('ranks the repo\'s existing labels by open-issue count', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    const chips = screen.getAllByRole('button').filter((b) => /^(bug|docs)3?0?$/.test(b.textContent ?? ''))
    // bug (3 open) outranks docs (0 open), regardless of alphabetical order.
    expect(chips[0].textContent).toContain('bug')
  })

  it('hosts the new-label suggestions that used to live in settings', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest new labels' }))
    await waitFor(() => expect(api.generateRecommendations).toHaveBeenCalledWith(
      expect.objectContaining({ owner: 'o', repo: 'r' }), '',
    ))
  })

  it('shows a short reason on each suggested new label', async () => {
    api.generateRecommendations.mockResolvedValue({
      owner: 'o', repo: 'r', generated_at: '2026-07-26T00:00:00Z', from_cache: false,
      recommendations: [{
        name: 'area: auth', category: 'area', color: 'cccccc',
        description: 'Auth surface', rationale: 'six open issues touch login', examples: [7],
      }],
    })
    renderView()
    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest new labels' }))
    await waitFor(() => expect(screen.getByText('six open issues touch login')).toBeTruthy())
    expect(screen.getByText('area: auth')).toBeTruthy()

    // The evidence is the repo's own issues, by title, linked by number — the
    // abstract category tag is deliberately NOT shown.
    // \s* — the accname algorithm inserts a separator at the <span>/text
    // boundary, so the computed name is "#7 : Crash on start".
    const example = screen.getByRole('link', { name: /#7\s*: Crash on start/ })
    expect(example.getAttribute('href')).toBe('https://github.com/o/r/issues/7')
    expect(screen.queryByText('area', { exact: true })).toBeNull()
  })

  it('offers a re-run, not an empty pass, once every issue is analysed', async () => {
    // Regression: `Math.min(batchSize, 0) || queue.length` fell through to the
    // whole queue, so the button advertised "next 2" with nothing left to do —
    // and the request would have selected an empty slice and no-opped.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4, batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
      generated_at: '2026-07-26T00:00:00Z',
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }], '8': [] },
    })
    api.generateTagging.mockResolvedValue({
      owner: 'o', repo: 'r', suggestions: {}, analyzed: [8, 7], remaining: 0, generated_at: null,
    })
    renderView()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Suggest again (2)' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /next/ })).toBeNull()

    // And it re-analyses by explicit number rather than asking for a new slice.
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [8, 7]))
  })

  it('counts only un-analysed issues in the next slice', async () => {
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4, batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
      generated_at: '2026-07-26T00:00:00Z',
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
    })
    renderView()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Suggest labels (next 1)' })).toBeTruthy())
  })

  it('re-fetches the repo labels on demand', async () => {
    // Labels are created and renamed on GitHub itself, so the panel needs a way
    // to bypass the local cache — otherwise it shows whatever was cached at
    // connect time no matter what the repo now looks like.
    api.labels.mockResolvedValue({ owner: 'o', repo: 'r', labels: LABELS, from_cache: false })
    renderView()
    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: "Re-fetch this repo's labels from GitHub" }))
    await waitFor(() => expect(api.labels).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), { refresh: true }))
  })

  it('gives each row exactly one action, which becomes Added', async () => {
    // Suggesting is a queue-level operation and an unwanted label is dropped by
    // clicking its chip, so the row carries no controls beyond Add.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4, batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z',
    })
    api.applyLabels.mockResolvedValue({ owner: 'o', repo: 'r', number: 7, labels: [] })
    renderView()

    const row = () => card(7)
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    // Add, plus the chip (click-to-drop). Nothing else.
    expect(within(row()).getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? b.textContent))
      .toEqual(['bug', 'Add labels to #7'])

    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #7' }))
    await waitFor(() => expect(screen.getByText('Added')).toBeTruthy())
    // Frozen: the chip stops being clickable and Add is gone.
    expect(within(row()).queryAllByRole('button')).toHaveLength(0)
  })

  it('does not carry review state across a repo switch', async () => {
    // Every piece of review state is keyed by ISSUE NUMBER, and numbers collide
    // across repos — so a long-lived mount would let `bug`, staged for #7 in one
    // repo, be written to a different #7 in the next.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}><TaggingView /></QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())
    // Drop the staged label, so the override is non-empty for #7.
    await userEvent.click(within(card(7)).getByRole('button', { name: /^bug/ }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Apply 0 suggestions/ })).toBeTruthy())

    // Switch repo. The new repo has its own #7, still carrying a proposal.
    setCtx({ active: { owner: 'o', repo: 'other' } })
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'other', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
    })
    rerender(<QueryClientProvider client={qc}><TaggingView /></QueryClientProvider>)

    // The stale "unstaged" override must NOT follow the number across repos.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())
  })

  it('splits a bulk apply into requests the backend will accept', async () => {
    // The backend caps one bulk request at 25 (it fans out to that many
    // sequential gh calls), so a bigger queue must be chunked rather than sent
    // whole and rejected with a 400.
    const many = Array.from({ length: 30 }, (_, i) => ({
      number: 100 + i,
      title: `issue ${100 + i}`,
      url: `https://github.com/o/r/issues/${100 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 40, batch_size: 50, label_counts: { bug: 3, docs: 0 }, titles: TITLES,
      bulk_max: 12, generated_at: '2026-07-26T00:00:00Z',
      suggestions: Object.fromEntries(many.map((i) => [String(i.number), [{ name: 'bug', reason: 'x' }]])),
    })
    api.applyLabelsBulk.mockImplementation((_ref, changes) =>
      Promise.resolve({ owner: 'o', repo: 'r', applied: changes.map((c: { number: number }) => ({ number: c.number, labels: [] })), failed: [] }))
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 30 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 30 suggestions/ }))
    // 12, not 25: the cap is SERVED, so a hardcoded client copy would fail here.
    await waitFor(() => expect(api.applyLabelsBulk).toHaveBeenCalledTimes(3))
    expect(api.applyLabelsBulk.mock.calls[0][1]).toHaveLength(12)
    expect(api.applyLabelsBulk.mock.calls[1][1]).toHaveLength(12)
    expect(api.applyLabelsBulk.mock.calls[2][1]).toHaveLength(6)
    // Every issue from both chunks is reported as applied.
    await waitFor(() => expect(screen.getByText(/Labelled 30 issues/)).toBeTruthy())
  })

  it('reloads the queue from GitHub rather than the local cache', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Reload the untagged queue' }))
    // Labels get added on GitHub itself; a cache-first reload never notices.
    await waitFor(() =>
      expect(api.tagging).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), { refresh: true }))
  })

  it('keeps chunks that already landed when a later chunk rejects', async () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      number: 100 + i, title: `issue ${100 + i}`,
      url: `https://github.com/o/r/issues/${100 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 40, batch_size: 50, label_counts: {}, bulk_max: 25, titles: {},
      generated_at: '2026-07-26T00:00:00Z',
      suggestions: Object.fromEntries(many.map((i) => [String(i.number), [{ name: 'bug', reason: 'x' }]])),
    })
    api.applyLabelsBulk
      .mockImplementationOnce((_ref, changes) => Promise.resolve({
        owner: 'o', repo: 'r', failed: [],
        applied: changes.map((c: { number: number }) => ({ number: c.number, labels: [] })),
      }))
      .mockImplementationOnce(() => Promise.reject(new Error('gateway went away')))
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 30 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 30 suggestions/ }))

    // The error is reported…
    await waitFor(() => expect(screen.getByText(/gateway went away/)).toBeTruthy())
    // …but the 25 already written to GitHub are marked, not offered again.
    expect(screen.getAllByText('Added')).toHaveLength(25)
    expect(screen.getByRole('button', { name: /Apply 5 suggestions/ })).toBeTruthy()
  })

  it('blocks every apply control while one apply is in flight', async () => {
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }], '8': [{ name: 'docs', reason: 'typo' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    // Applies patch the same server-side cache and are not serialized there, so
    // only one may be in flight at a time.
    api.applyLabels.mockImplementation(() => new Promise(() => {}))
    renderView()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add labels to #7' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #7' }))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Add labels to #8' })).toHaveProperty('disabled', true))
    expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toHaveProperty('disabled', true)
  })

  it('takes label counts and example titles from the queue response', async () => {
    // Not from the shared issue list: that follows the open/closed filter, so
    // entering from Closed reported closed counts as open ones.
    renderView()
    // Wait for the queue response — the panel renders before it resolves.
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    await waitFor(() => expect(
      screen.getAllByRole('button').some((b) => b.textContent === 'bug3'),
    ).toBe(true))
    // The title came from the response's `titles`, not the shared issue list.
    expect(screen.getByText('Crash on start')).toBeTruthy()
  })

  it('drops selection for issues a reload removed', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Typo in readme')).toBeTruthy())
    await userEvent.click(screen.getByLabelText('Select issue #8'))
    await waitFor(() =>
      expect(screen.getByLabelText('Select issue #8')).toHaveProperty('checked', true))

    // #8 picked up a label on GitHub, so the refreshed queue no longer has it.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: {}, generated_at: '2026-07-26T00:00:00Z',
      batch_size: 50, label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    await userEvent.click(screen.getByRole('button', { name: 'Reload the untagged queue' }))

    // A stale selection would keep "selection mode" on while matching no row,
    // so Apply would silently cover nothing.
    await waitFor(() => expect(screen.queryByText('Typo in readme')).toBeNull())
    expect(screen.getByText('Select all')).toBeTruthy()
  })

  it('advances through the queue on repeated re-runs', async () => {
    const many = Array.from({ length: 5 }, (_, i) => ({
      number: 200 + i, title: `issue ${200 + i}`,
      url: `https://github.com/o/r/issues/${200 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 10, batch_size: 2, label_counts: {}, bulk_max: 25, titles: {},
      generated_at: '2026-07-26T00:00:00Z',
      suggestions: Object.fromEntries(many.map((i) => [String(i.number), []])),
    })
    // The real endpoint returns the MERGED map, so a re-run leaves every issue
    // still analysed — which is what keeps the button in "Suggest again" mode.
    const merged = Object.fromEntries(many.map((i) => [String(i.number), []]))
    api.generateTagging.mockImplementation((_ref, numbers) => Promise.resolve({
      owner: 'o', repo: 'r', suggestions: merged,
      analyzed: numbers ?? [], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    }))
    renderView()

    // Fully analysed, so the button re-runs. Each press must cover the NEXT
    // slice — re-running the first two forever left issues 202..204 unreachable.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Suggest again (2)' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [200, 201]))
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenLastCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [202, 203]))
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (1)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenLastCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [204]))
    // …then wraps.
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenLastCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [200, 201]))
  })

  it('does not skip a slice whose re-run failed', async () => {
    const many = Array.from({ length: 4 }, (_, i) => ({
      number: 300 + i, title: `issue ${300 + i}`,
      url: `https://github.com/o/r/issues/${300 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    const merged = Object.fromEntries(many.map((i) => [String(i.number), []]))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 8, batch_size: 2, label_counts: {}, bulk_max: 25, titles: {},
      generated_at: '2026-07-26T00:00:00Z', suggestions: merged,
    })
    // First re-run fails, second succeeds.
    api.generateTagging
      .mockRejectedValueOnce(new Error('model unavailable'))
      .mockImplementation((_ref, numbers) => Promise.resolve({
        owner: 'o', repo: 'r', suggestions: merged,
        analyzed: numbers ?? [], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
      }))
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Suggest again (2)' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(screen.getByText(/model unavailable/)).toBeTruthy())

    // The cursor must NOT have moved past a slice that was never analysed —
    // advancing on click left those issues unreachable until a full wrap.
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    await waitFor(() => expect(api.generateTagging).toHaveBeenLastCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [300, 301]))
  })

  it('keeps per-issue errors reported by an earlier chunk', async () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      number: 400 + i, title: `issue ${400 + i}`,
      url: `https://github.com/o/r/issues/${400 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 40, batch_size: 50, label_counts: {}, bulk_max: 25, titles: {},
      generated_at: '2026-07-26T00:00:00Z',
      suggestions: Object.fromEntries(many.map((i) => [String(i.number), [{ name: 'bug', reason: 'x' }]])),
    })
    api.applyLabelsBulk
      .mockImplementationOnce((_ref, changes) => Promise.resolve({
        owner: 'o', repo: 'r',
        applied: changes.slice(1).map((c: { number: number }) => ({ number: c.number, labels: [] })),
        failed: [{ number: changes[0].number, error: 'issue is locked' }],
      }))
      .mockImplementationOnce(() => Promise.reject(new Error('gateway went away')))
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 30 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 30 suggestions/ }))

    // onSuccess never runs when a later chunk rejects, so the first chunk's
    // per-issue failure has to be published as it happens or it disappears.
    await waitFor(() => expect(screen.getByText('issue is locked')).toBeTruthy())
    expect(screen.getByText(/gateway went away/)).toBeTruthy()
  })

  it('drops staged labels the repo no longer defines', async () => {
    // A label renamed or deleted on GitHub leaves an obsolete proposal staged,
    // and the backend rejects an unknown label for the WHOLE bulk chunk — so one
    // stale name would block every issue in the batch.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }, { name: 'renamed-away', reason: 'y' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50,
      label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    api.applyLabelsBulk.mockResolvedValue({
      owner: 'o', repo: 'r', applied: [{ number: 7, labels: [] }], failed: [],
    })
    renderView()

    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    // `renamed-away` is not in the repo's label set, so it is never staged.
    expect(screen.queryByText('renamed-away')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /Apply 1 suggestion$/ }))
    await waitFor(() => expect(api.applyLabelsBulk).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [{ number: 7, add: ['bug'] }]))
  })

  it('reconciles queue state on a background refetch, not just a manual reload', async () => {
    // react-query refetches on focus/reconnect too. Those paths never touched
    // queue-keyed state, so a selection could survive its issue leaving the queue
    // and keep "selection mode" on while matching no current row.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><TaggingView /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByText('Typo in readme')).toBeTruthy())
    await userEvent.click(screen.getByLabelText('Select issue #8'))
    await waitFor(() =>
      expect(screen.getByLabelText('Select issue #8')).toHaveProperty('checked', true))

    // Simulate a background refetch that drops #8 — no reload button involved.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50,
      label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    // Built from the real helper, not spelled out: cache keys are scoped by
    // provider + host now, so a hand-written key would silently match nothing
    // and this test would pass by refetching zero queries.
    await qc.refetchQueries({
      queryKey: ['issue-radar', 'tagging', repoScopeKey({ owner: 'o', repo: 'r' })],
    })

    await waitFor(() => expect(screen.queryByText('Typo in readme')).toBeNull())
    // Selection was pruned, so Apply covers the surviving row rather than nothing.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())
    expect(screen.getByText('Select all')).toBeTruthy()
  })

  it('blocks applying and says why when the labels query failed', async () => {
    // `knownLabels.size === 0` cannot tell "no labels" from "query failed", and
    // treating the second as the first bypassed the staleness filter entirely.
    setCtx({ repoLabels: [], labelsError: new Error('gh rate limited') })
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50,
      label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    renderView()

    // Wait for the QUEUE — the banner comes from context and renders first.
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    expect(screen.getByText(/gh rate limited/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Add labels to #7' })).toHaveProperty('disabled', true)
    // And it must NOT claim the repo simply has no labels.
    expect(screen.queryByText(/defines no labels yet/)).toBeNull()
  })

  it('clears a stale bulk banner when a single row is retried', async () => {
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }], '8': [{ name: 'docs', reason: 'typo' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    api.applyLabelsBulk.mockResolvedValue({
      owner: 'o', repo: 'r',
      applied: [{ number: 8, labels: [] }],
      failed: [{ number: 7, error: 'issue is locked' }],
    })
    api.applyLabels.mockResolvedValue({ owner: 'o', repo: 'r', number: 7, labels: [] })
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 2 suggestions/ }))
    await waitFor(() => expect(screen.getByText(/1 could not be updated/)).toBeTruthy())

    // Retrying the failed row must not leave a banner asserting a failure that
    // has since been fixed.
    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #7' }))
    await waitFor(() => expect(screen.queryByText(/could not be updated/)).toBeNull())
  })

  it('appends a created label to its role server-side, not by read-modify-write', async () => {
    // The settings PUT replaces the WHOLE document, so any client-side
    // read-modify-write only serializes itself — two tabs would each read the same
    // settings and the later full replacement would drop the other's label. The
    // append therefore happens on the server, under the config lock.
    api.generateRecommendations.mockResolvedValue({
      owner: 'o', repo: 'r', generated_at: '2026-07-26T00:00:00Z', from_cache: false,
      recommendations: [{
        name: 'needs-triage', category: 'triage', color: 'cccccc',
        description: 'Needs triage', rationale: 'lots of unsorted issues', examples: [7],
      }],
    })
    api.createLabel.mockResolvedValue({
      owner: 'o', repo: 'r', created: true,
      label: { name: 'needs-triage', color: 'cccccc', description: '' },
    })
    api.addSettingLabel.mockResolvedValue({
      owner: 'o', repo: 'r',
      settings: {
        triage_labels: ['needs-triage'], unlabeled_is_untriaged: true,
        good_first_issue_labels: [], notify_on_new_issue: false,
      },
    })
    renderView()

    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest new labels' }))
    await waitFor(() => expect(screen.getByText('needs-triage')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Create the needs-triage label/ }))

    await waitFor(() =>
      expect(api.addSettingLabel).toHaveBeenCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), 'triage_labels', 'needs-triage'))
    // The whole-document write must NOT be used for this.
    expect(api.putSettings).not.toHaveBeenCalled()
  })

  it('reports a failure to update local settings instead of hiding it', async () => {
    api.generateRecommendations.mockResolvedValue({
      owner: 'o', repo: 'r', generated_at: '2026-07-26T00:00:00Z', from_cache: false,
      recommendations: [{
        name: 'needs-triage', category: 'triage', color: 'cccccc',
        description: 'Needs triage', rationale: 'lots of unsorted issues', examples: [7],
      }],
    })
    api.createLabel.mockResolvedValue({
      owner: 'o', repo: 'r', created: true,
      label: { name: 'needs-triage', color: 'cccccc', description: '' },
    })
    api.addSettingLabel.mockRejectedValue(new Error('settings API unavailable'))
    renderView()

    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest new labels' }))
    await waitFor(() => expect(screen.getByText('needs-triage')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Create the needs-triage label/ }))

    // And it must say the label DID reach GitHub — that half succeeded.
    await waitFor(() => expect(screen.getByText(/settings API unavailable/)).toBeTruthy())
    expect(screen.getByText(/was created on GitHub/)).toBeTruthy()
  })

  it('allows only one label creation at a time', async () => {
    // Each create patches the same local label cache; two overlapping patches
    // lose one of the new labels.
    api.generateRecommendations.mockResolvedValue({
      owner: 'o', repo: 'r', generated_at: '2026-07-26T00:00:00Z', from_cache: false,
      recommendations: [
        { name: 'needs-triage', category: 'triage', color: 'cccccc', description: 'd', rationale: 'r', examples: [] },
        { name: 'starter', category: 'first-issue', color: 'dddddd', description: 'd', rationale: 'r', examples: [] },
      ],
    })
    api.createLabel.mockImplementation(() => new Promise(() => {}))
    renderView()

    await waitFor(() => expect(screen.getByText('Labels')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest new labels' }))
    await waitFor(() => expect(screen.getByText('needs-triage')).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Create the needs-triage label/ }))

    await waitFor(() => expect(
      screen.getByRole('button', { name: /Create the starter label/ }),
    ).toHaveProperty('disabled', true))
  })

  it('re-runs over rows that are still pending, skipping applied ones', async () => {
    // A slice made only of applied issues came back with `analyzed: []`, so the
    // cursor never advanced and every row past it was unreachable.
    const many = Array.from({ length: 4 }, (_, i) => ({
      number: 500 + i, title: `issue ${500 + i}`,
      url: `https://github.com/o/r/issues/${500 + i}`,
      labels: [], comments: 0,
      updated_at: '2026-07-01T00:00:00Z', created_at: '2026-07-01T00:00:00Z',
    }))
    const merged = Object.fromEntries(many.map((i) => [String(i.number), [{ name: 'bug', reason: 'x' }]]))
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: many, untagged: many.map((i) => i.number),
      open_count: 8, batch_size: 2, label_counts: {}, bulk_max: 25, titles: {},
      generated_at: '2026-07-26T00:00:00Z', suggestions: merged,
    })
    api.applyLabels.mockResolvedValue({ owner: 'o', repo: 'r', number: 500, labels: [] })
    api.generateTagging.mockImplementation((_ref, numbers) => Promise.resolve({
      owner: 'o', repo: 'r', suggestions: merged,
      analyzed: numbers ?? [], remaining: 0, generated_at: '2026-07-26T00:00:00Z',
    }))
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Add labels to #500' })).toBeTruthy())
    // #500 and #501 are the first slice; apply #500 so it drops out of `pending`.
    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #500' }))
    await waitFor(() => expect(screen.getByText('Added')).toBeTruthy())

    // Untagged now counts only what is left.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Suggest again (2)' })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: 'Suggest again (2)' }))
    // #500 is applied, so the slice starts at #501 — not at the applied row.
    await waitFor(() => expect(api.generateTagging).toHaveBeenLastCalledWith(expect.objectContaining({ owner: 'o', repo: 'r' }), [501, 502]))
  })

  it('clears only the row errors the current apply covers', async () => {
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[1], ISSUES[0]], untagged: [8, 7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }], '8': [{ name: 'docs', reason: 'typo' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50, label_counts: {}, bulk_max: 25, titles: TITLES,
    })
    // First bulk apply fails BOTH rows.
    api.applyLabelsBulk.mockResolvedValueOnce({
      owner: 'o', repo: 'r', applied: [],
      failed: [{ number: 7, error: 'seven is locked' }, { number: 8, error: 'eight is locked' }],
    })
    renderView()
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 2 suggestions/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 2 suggestions/ }))
    await waitFor(() => expect(screen.getByText('seven is locked')).toBeTruthy())
    expect(screen.getByText('eight is locked')).toBeTruthy()

    // Now retry ONLY #7 by selecting it. #8's unresolved error must survive —
    // wiping the whole map hid a failure the user had not addressed.
    api.applyLabelsBulk.mockResolvedValueOnce({
      owner: 'o', repo: 'r', applied: [{ number: 7, labels: [] }], failed: [],
    })
    await userEvent.click(screen.getByLabelText('Select issue #7'))
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply 1 suggestion$/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Apply 1 suggestion$/ }))

    await waitFor(() => expect(screen.queryByText('seven is locked')).toBeNull())
    expect(screen.getByText('eight is locked')).toBeTruthy()
  })

  it('says so when the queue reload fails', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Crash on start')).toBeTruthy())
    api.tagging.mockRejectedValueOnce(new Error('gh exploded'))
    await userEvent.click(screen.getByRole('button', { name: 'Reload the untagged queue' }))
    // Silently keeping the old rows made stale data look current.
    await waitFor(() => expect(screen.getByText(/gh exploded/)).toBeTruthy())
    expect(screen.getByText(/previous result/)).toBeTruthy()
  })

  it('updates the label counts after an apply', async () => {
    // The queue rows deliberately stay put after an apply, so nothing else
    // refreshes the served counts — the Labels panel would keep showing usage
    // from before the write.
    api.tagging.mockResolvedValue({
      owner: 'o', repo: 'r', issues: [ISSUES[0]], untagged: [7], open_count: 4,
      suggestions: { '7': [{ name: 'bug', reason: 'crash' }] },
      generated_at: '2026-07-26T00:00:00Z', batch_size: 50,
      label_counts: { bug: 3, docs: 0 }, bulk_max: 25, titles: TITLES,
    })
    api.applyLabels.mockResolvedValue({
      owner: 'o', repo: 'r', number: 7,
      labels: [{ name: 'bug', color: 'ee0000', description: '' }],
    })
    renderView()

    await waitFor(() => expect(
      screen.getAllByRole('button').some((b) => b.textContent === 'bug3'),
    ).toBe(true))
    await userEvent.click(screen.getByRole('button', { name: 'Add labels to #7' }))

    await waitFor(() => expect(
      screen.getAllByRole('button').some((b) => b.textContent === 'bug4'),
    ).toBe(true))
  })

  it('starts at the numbers, with no page title above them', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Untagged')).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Tagging' })).toBeNull()
  })
})
