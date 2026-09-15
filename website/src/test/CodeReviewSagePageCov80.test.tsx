import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * The page entry point. It is deliberately thin, and the two things it does are
 * both load-bearing: a finished-review notification deep-links `?run=<id>`, and
 * the add-repos dialog has to mount INSIDE the provider (it reads it) while the
 * workspace blurs behind it.
 */
vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    runs: vi.fn(async () => ({ runs: [], pool: null, reviewer: null })),
    run: vi.fn(),
    runReport: vi.fn(async () => null),
    cancelRun: vi.fn(),
    deleteRun: vi.fn(),
    archiveRun: vi.fn(),
    postComments: vi.fn(),
    postCommentGroups: vi.fn(),
    review: vi.fn(),
    reviewLinks: vi.fn(),
    reviewRepo: vi.fn(),
    recentRepos: vi.fn(async () => ({ repos: [], pinned: [] })),
    myRepos: vi.fn(async () => ({ repos: [], pinned: [] })),
    pinnedRepos: vi.fn(async () => ({ repos: [] })),
    pinRepo: vi.fn(),
    pinRepoUrl: vi.fn(),
    unpinRepo: vi.fn(),
    repoPrs: vi.fn(async () => ({ repo: '', prs: [], count: 0 })),
    settings: vi.fn(async () => ({
      settings: { model: null, effort: 'medium', active_namespaces: ['default'], max_concurrent: 5 },
      models: [], efforts: [], namespaces: ['default'],
    })),
    putSettings: vi.fn(),
    namespaces: vi.fn(async () => ({ namespaces: [], active: [] })),
    createNamespace: vi.fn(),
    deleteNamespace: vi.fn(),
    learnings: vi.fn(),
  },
}))

// The shell and the dialog have their own suites; here they are probes so the
// page's own wiring (deep link in, dialog layer out) is what is asserted.
vi.mock('../apps/code-review-sage/Workspace', async () => {
  const { useSage } = await import('../apps/code-review-sage/context')
  function WorkspaceProbe() {
    const { selectedRunId, addingRepos, openAddRepos } = useSage()
    return (
      <div>
        <span data-testid="run">{selectedRunId ?? 'zzz-none'}</span>
        <span data-testid="adding">{addingRepos ? 'zzz-open' : 'zzz-shut'}</span>
        <button onClick={openAddRepos}>zzz-add-repos</button>
      </div>
    )
  }
  return { default: WorkspaceProbe }
})

vi.mock('../apps/code-review-sage/components/AddReposModal', () => ({
  default: ({ onClose }: { onClose: () => void }) => (
    <div>
      <span data-testid="modal">zzz-modal</span>
      <button onClick={onClose}>zzz-close</button>
    </div>
  ),
}))

import CodeReviewSagePage from '../apps/code-review-sage/CodeReviewSagePage'

function mount(entry: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={qc}>
        <CodeReviewSagePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

describe('CodeReviewSagePage', () => {
  it('seeds the initial selection from the ?run= deep link', async () => {
    mount('/apps/code-review-sage?run=zzz-run-7')
    await waitFor(() => expect(screen.getByTestId('run')).toHaveTextContent('zzz-run-7'))
  })

  it('selects nothing when there is no deep link', async () => {
    mount('/apps/code-review-sage')
    await waitFor(() => expect(screen.getByTestId('run')).toHaveTextContent('zzz-none'))
  })

  it('mounts the add-repos dialog only while the rail asks for it, and closes it again', async () => {
    mount('/apps/code-review-sage')
    expect(screen.queryByTestId('modal')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'zzz-add-repos' }))
    expect(screen.getByTestId('modal')).toBeInTheDocument()
    expect(screen.getByTestId('adding')).toHaveTextContent('zzz-open')

    await userEvent.click(screen.getByRole('button', { name: 'zzz-close' }))
    expect(screen.queryByTestId('modal')).not.toBeInTheDocument()
  })
})
