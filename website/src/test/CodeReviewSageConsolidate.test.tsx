// The Consolidate control: the step that makes staged learnings reach reviews.
//
// Reviews read the consolidated ruleset only, so a candidate that is never merged
// does nothing. The view previously described consolidation it could not perform,
// which left staged learnings inert with no way to act on them.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import LearningRail from '../apps/code-review-sage/components/LearningRail'
import LearningView, { ConsolidateControl } from '../apps/code-review-sage/views/LearningView'
import { SageProvider } from '../apps/code-review-sage/context'
import { sageApi } from '../apps/code-review-sage/api'

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    namespaces: vi.fn(),
    learnings: vi.fn(),
    consolidateLearnings: vi.fn(),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
    settings: vi.fn(),
    putSettings: vi.fn(),
    runs: vi.fn(),
    pinnedRepos: vi.fn(),
    recentRepos: vi.fn(),
    myRepos: vi.fn(),
    repoPrs: vi.fn(),
  },
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

function pattern(title: string, id = title) {
  return { id, title, guidance: `Guidance for ${title}`, scope: 'common', impact: 'high' }
}

/** Namespace picking lives in the rail now, the learnings in the detail pane, so
 *  both are mounted together — that is how they appear in the app. */
function mount() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={null}>
          <LearningRail />
          <LearningView />
        </SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** The default namespace is selected on mount, so this only waits for its
 *  learnings to arrive. */
async function openNamespace() {
  await waitFor(() => expect(mockApi.learnings).toHaveBeenCalledWith('default'))
}

describe('consolidating staged learnings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.namespaces.mockResolvedValue({
      namespaces: [{ name: 'default', patterns: 1, candidate: 2, active: true }],
      active: ['default'],
    })
    mockApi.settings.mockResolvedValue({
      settings: { active_namespaces: ['default'] }, pool: null, reviewer: null,
    })
    mockApi.learnings.mockResolvedValue({
      namespace: 'default',
      patterns: [pattern('Existing rule')],
      candidate: [pattern('Staged one', 'c1'), pattern('Staged two', 'c2')],
      consolidating: false,
      consolidate_error: null,
    })
    mockApi.consolidateLearnings.mockResolvedValue({
      ok: true, namespace: 'default', staged: 2, running: true,
    })
    mockApi.runs.mockResolvedValue({ runs: [] })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: '', prs: [], count: 0 })
  })

  it('offers to consolidate when learnings are pending', async () => {
    mount()
    await openNamespace()
    expect(await screen.findByRole('button', {
      name: /Consolidate 2 pending learnings in default/,
    })).toBeTruthy()
  })

  it('asks before replacing the ruleset', async () => {
    mount()
    await openNamespace()
    await userEvent.click(await screen.findByRole('button', {
      name: /Consolidate 2 pending learnings/,
    }))
    // Merging replaces the ruleset and clears the candidate; neither is
    // recoverable from here, so the first click only asks.
    expect(mockApi.consolidateLearnings).not.toHaveBeenCalled()
    // One key now carries the whole question, with the count interpolated and
    // the noun pluralized, so the copy reads "Merge 2 learnings…".
    expect(screen.getByText(/Merge 2 learnings into the ruleset\?/)).toBeTruthy()
  })

  it('merges on confirm', async () => {
    mount()
    await openNamespace()
    await userEvent.click(await screen.findByRole('button', {
      name: /Consolidate 2 pending learnings/,
    }))
    await userEvent.click(screen.getByRole('button', { name: /^Consolidate$/ }))
    await waitFor(() => expect(mockApi.consolidateLearnings)
      .toHaveBeenCalledWith('default'))
  })

  it('backs out cleanly', async () => {
    mount()
    await openNamespace()
    await userEvent.click(await screen.findByRole('button', {
      name: /Consolidate 2 pending learnings/,
    }))
    await userEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(mockApi.consolidateLearnings).not.toHaveBeenCalled()
  })

  it('shows a merge already in flight instead of offering another', async () => {
    mockApi.learnings.mockResolvedValue({
      namespace: 'default',
      patterns: [pattern('Existing rule')],
      candidate: [pattern('Staged one', 'c1')],
      consolidating: true,
      consolidate_error: null,
    })
    mount()
    await openNamespace()
    expect(await screen.findByText(/Consolidating…/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Consolidate \d+ pending/ })).toBeNull()
  })

  it('says the ruleset survived a failed merge', async () => {
    mockApi.learnings.mockResolvedValue({
      namespace: 'default',
      patterns: [pattern('Existing rule')],
      candidate: [pattern('Staged one', 'c1')],
      consolidating: false,
      consolidate_error: 'the merge produced no file; the ruleset is unchanged',
    })
    mount()
    await openNamespace()
    // A failed merge that looked like a success would be read as "learning
    // applied" when nothing changed.
    expect(await screen.findByText(/Merge failed — ruleset unchanged/)).toBeTruthy()
  })

  it('offers nothing when there is nothing staged', async () => {
    mockApi.namespaces.mockResolvedValue({
      namespaces: [{ name: 'default', patterns: 1, candidate: 0, active: true }],
      active: ['default'],
    })
    mockApi.learnings.mockResolvedValue({
      namespace: 'default',
      patterns: [pattern('Existing rule')],
      candidate: [],
      consolidating: false,
      consolidate_error: null,
    })
    mount()
    await openNamespace()
    await waitFor(() => expect(screen.getAllByText(/Existing rule/).length)
      .toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /Consolidate/ })).toBeNull()
  })

  it('still lists the pending learnings themselves', async () => {
    mount()
    await openNamespace()
    await screen.findByText(/Pending consolidation/)
    expect(screen.getAllByText(/Staged one/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Staged two/).length).toBeGreaterThan(0)
  })
})

describe('the rail while Learning is open', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.namespaces.mockResolvedValue({
      namespaces: [
        { name: 'default', patterns: 2, candidate: 0, active: true },
        { name: 'kirocrew', patterns: 5, candidate: 1, active: false },
      ],
      active: ['default'],
    })
    mockApi.settings.mockResolvedValue({
      settings: { active_namespaces: ['default'] }, pool: null, reviewer: null,
    })
    mockApi.learnings.mockResolvedValue({
      namespace: 'default', patterns: [pattern('Rule one')], candidate: [],
      consolidating: false, consolidate_error: null,
    })
    mockApi.runs.mockResolvedValue({ runs: [] })
    mockApi.pinnedRepos.mockResolvedValue({ repos: [] })
    mockApi.repoPrs.mockResolvedValue({ repo: '', prs: [], count: 0 })
  })

  it('lists the namespaces with their pattern counts', async () => {
    mount()
    expect(await screen.findByRole('button', { name: 'Read namespace kirocrew' }))
      .toBeTruthy()
    expect(screen.getByRole('button', { name: 'Read namespace default' })).toBeTruthy()
    expect(screen.getByText(/5 patterns/)).toBeTruthy()
    expect(screen.getByText(/1 pending/)).toBeTruthy()
  })

  it('reads the namespace you pick', async () => {
    mockApi.learnings.mockImplementation((ns: string) => Promise.resolve({
      namespace: ns, patterns: [pattern(`Rule in ${ns}`)], candidate: [],
      consolidating: false, consolidate_error: null,
    }))
    mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Read namespace kirocrew' }))
    await waitFor(() => expect(mockApi.learnings).toHaveBeenCalledWith('kirocrew'))
    // Title and guidance both carry it.
    expect((await screen.findAllByText(/Rule in kirocrew/)).length).toBeGreaterThan(0)
  })

  it('says whether the namespace being read is actually loaded', async () => {
    mockApi.learnings.mockImplementation((ns: string) => Promise.resolve({
      namespace: ns, patterns: [], candidate: [],
      consolidating: false, consolidate_error: null,
    }))
    mount()
    // A ruleset you are reading may be switched off; that is the first thing
    // worth knowing about it.
    await screen.findByRole('button', { name: 'Read namespace kirocrew' })
    expect(await screen.findByText(/^loaded during reviews$/)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Read namespace kirocrew' }))
    expect(await screen.findByText(/not loaded/)).toBeTruthy()
  })

  it('keeps active independent of what you are reading', async () => {
    mount()
    await screen.findByRole('button', { name: 'Read namespace kirocrew' })
    // Selecting a namespace to read must not change which ones reviews load.
    await userEvent.click(screen.getByRole('button', { name: 'Read namespace kirocrew' }))
    expect(mockApi.putSettings).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('checkbox', {
      name: /Load namespace kirocrew during reviews/,
    }))
    await waitFor(() => expect(mockApi.putSettings).toHaveBeenCalledWith({
      active_namespaces: ['default', 'kirocrew'],
    }))
  })

  it('offers no delete for the default namespace', async () => {
    mount()
    await screen.findByRole('button', { name: 'Read namespace kirocrew' })
    expect(screen.getByRole('button', { name: /Delete namespace kirocrew/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Delete namespace default/ })).toBeNull()
  })

  it('disarms when the namespace changes under an armed consolidation', async () => {
    // Rendered directly: routed through the rail, the parent unmounts this control while
    // the next namespace loads and resets the state for free, which hides the guard.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(
      <QueryClientProvider client={qc}>
        <ConsolidateControl namespace="default" count={2} running={false} error={null} />
      </QueryClientProvider>,
    )

    await userEvent.click(await screen.findByRole('button',
      { name: /Consolidate \d+ pending/ }))
    expect(screen.getByRole('button', { name: /^Consolidate$/ })).toBeTruthy()

    view.rerender(
      <QueryClientProvider client={qc}>
        <ConsolidateControl namespace="other" count={2} running={false} error={null} />
      </QueryClientProvider>,
    )

    // The armed confirmation belonged to 'default'; it must not carry to 'other'.
    expect(screen.queryByRole('button', { name: /^Consolidate$/ })).toBeNull()
    expect(mockApi.consolidateLearnings).not.toHaveBeenCalled()
  })
})
