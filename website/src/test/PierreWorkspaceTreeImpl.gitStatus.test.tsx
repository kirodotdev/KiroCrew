/**
 * PierreWorkspaceTreeImpl — the git status lanes and Changed mode.
 *
 * One topic of the wrapper suite whose conventions
 * `PierreWorkspaceTreeImpl.test.tsx` describes; the shared fixtures live in
 * `PierreWorkspaceTreeImplTestUtils.tsx`, and the `vi.mock` block is the
 * suite's own, word for word.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    // The contributed-row seam reads `['apps']` as a cache subscriber (never fetches)
    // and dispatches an activation through `invokeFileMenuItem`.
    listApps: vi.fn(),
    invokeFileMenuItem: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { api } from '../api/client'
import { treeMock } from './__mocks__/pierreTreesReact'
import {
  ROOT,
  mkTree,
  mkStatus,
  mkFile,
  renderTree,
  waitForTree,
} from './PierreWorkspaceTreeImplTestUtils'

beforeEach(() => {
  treeMock.reset()
  vi.mocked(api.projectTree).mockResolvedValue(mkTree())
  vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([]))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('PierreWorkspaceTreeImpl — git status lanes', () => {
  it('re-anchors repo-root-relative status paths onto the project root', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['a.ts'] }))
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/a.ts', 'M'), mkFile('other/b.ts', 'M')]),
    )
    renderTree()
    await waitForTree()

    // 'other/b.ts' lives in the repo but outside the project dir: it has no row
    // to paint, and left un-relativized it would land on an unrelated one.
    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([{ path: 'a.ts', status: 'modified' }]),
    )
  })

  it('anchors on the project root when the status payload has no repo root', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('README.md', 'M')], { repoRoot: undefined }),
    )
    renderTree()
    await waitForTree()

    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([{ path: 'README.md', status: 'modified' }]),
    )
  })

  it('prefers the payload root over the requested project dir', async () => {
    // The backend answers with the realpath; a symlinked project dir would
    // otherwise fail the `startsWith` containment check and lose every lane.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ root: ROOT, paths: ['a.ts'] }))
    vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([mkFile('project/a.ts', 'M')]))
    const onFileOpen = vi.fn()
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PierreWorkspaceTreeImpl projectDir="/link/project" onFileOpen={onFileOpen} />
      </QueryClientProvider>,
    )
    await waitForTree()

    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([{ path: 'a.ts', status: 'modified' }]),
    )
    act(() => { treeMock.last().simulateSelection('a.ts') })
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/a.ts`)
  })

  it('maps each porcelain letter onto a lane and drops the ones it has none for', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['m', 'a', 'd', 'r', 'c', 'u', 'x'] }))
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([
        mkFile('project/m', 'M'), mkFile('project/a', 'A'), mkFile('project/d', 'D'),
        mkFile('project/r', 'R'), mkFile('project/c', 'C'), mkFile('project/u', '?'),
        mkFile('project/x', 'U'),
      ]),
    )
    renderTree()
    await waitForTree()

    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([
        { path: 'm', status: 'modified' },
        { path: 'a', status: 'added' },
        { path: 'd', status: 'deleted' },
        { path: 'r', status: 'renamed' },
        { path: 'c', status: 'added' },
        { path: 'u', status: 'untracked' },
      ]),
    )
  })

  it('keeps the staged lane when a file is listed both staged and unstaged', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/README.md', 'A', true), mkFile('project/README.md', 'M')]),
    )
    renderTree()
    await waitForTree()

    // One row can only show one state; a second entry for it would overwrite
    // the staged lane with the unstaged one.
    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([{ path: 'README.md', status: 'added' }]),
    )
  })

  it('leaves the lanes empty when the working tree is clean', async () => {
    renderTree()
    await waitForTree()
    expect(treeMock.last().calls.gitStatus.every(entries => entries.length === 0)).toBe(true)
  })
})

describe('PierreWorkspaceTreeImpl — changed mode', () => {
  it('renders only the changed files, expanded, gated on the status payload', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/src/a/b.ts', 'M'), mkFile('project/README.md', '?')]),
    )
    renderTree({ mode: 'changed' })
    await waitForTree()

    expect(treeMock.last().options).toMatchObject({ initialExpansion: 'open' })
    expect(treeMock.last().calls.resetPaths).toEqual([['src/a/b.ts', 'README.md']])
  })

  it('reports a clean working tree instead of an empty tree', async () => {
    renderTree({ mode: 'changed' })

    await waitFor(() => expect(screen.getByText('Working tree clean')).toBeInTheDocument())
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
  })

  it('reports it without waiting for the unrelated project-tree query', async () => {
    // In `changed` mode both the paths and the readiness signal come from the
    // status query; the tree walk is far slower on a large workspace, and
    // holding the notice until it lands renders an empty FileTree in its place.
    vi.mocked(api.projectTree).mockReturnValue(new Promise(() => {}) as never)

    renderTree({ mode: 'changed' })

    await waitFor(() => expect(screen.getByText('Working tree clean')).toBeInTheDocument())
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
  })

  it('names a filter refusal instead of wearing the generic failed copy', async () => {
    // Turning the refusal into a 503 made these two surfaces render their
    // generic "failed" notice on every LFS-configured repository, forever, with
    // no cause and no "retry won't help" -- the outage spelling this change
    // exists to end, one panel over. Both must recognise the refusal code.
    const errorReport = await import('../utils/errorReport')
    const i18n = await import('../i18n/t')
    const refusal = Object.assign(new Error('CHECKS-OFF'), {
      status: 503,
      code: 'git_status_filter_refused',
      body: JSON.stringify({
        error: 'CHECKS-OFF',
        code: 'git_status_filter_refused',
        cause: 'declared',
      }),
    })
    errorReport.__resetErrorJournalForTests()
    vi.mocked(api.projectGitStatus).mockRejectedValue(refusal)

    const direct = renderTree({ mode: 'changed' })
    const notice = await screen.findByTestId('workspace-tree-status-error')
    expect(notice).toHaveTextContent(i18n.i18nT('components.gitPanel.filter_refused'))
    expect(notice).not.toHaveTextContent(
      i18n.i18nT('components.workspaceTree.status_failed'),
    )
    // And NO title. `inline` lays a title out as a flex sibling of the message,
    // so at this width it stacks into two-word fragments -- the capture is what
    // showed that. The panel needs the title to separate two coexisting notices;
    // nothing renders beside this one.
    expect(notice.querySelector('strong')).toBeNull()

    direct.unmount()
    const { default: FileBrowserRail } = await import('../pages/chat/FileBrowserRail')
    const railClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={railClient}>
        <FileBrowserRail projectDir={ROOT} onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    // All mode, which is where the rail owns the notice.
    const railNotice = await screen.findByText(
      i18n.i18nT('components.gitPanel.filter_refused'),
    )
    expect(railNotice).toBeInTheDocument()
    expect(
      screen.queryByText(i18n.i18nT('pages.chat.fileBrowserRail.git_status_failed')),
    ).toBeNull()
    errorReport.__resetErrorJournalForTests()
  })

  it('reports a changed-mode 503 once with its own copy and structured agent handoff', async () => {
    const errorReport = await import('../utils/errorReport')
    const serverMessage = 'server-only workspace status detail'
    errorReport.__resetErrorJournalForTests()
    errorReport.__resetNavSeamForTests()
    sessionStorage.clear()
    errorReport.installSoftNavigate(() => {})

    try {
      errorReport.recordError({
        source: 'api',
        message: serverMessage,
        status: 503,
        code: 'git_status_unavailable',
        endpoint: '/api/project/git/status',
      })
      vi.mocked(api.projectGitStatus).mockRejectedValue(
        Object.assign(new Error(serverMessage), {
          status: 503,
          code: 'git_status_unavailable',
        }),
      )

      const direct = renderTree({ mode: 'changed' })

      const notice = await screen.findByTestId('workspace-tree-status-error')
      expect(notice).toHaveTextContent('Couldn’t read the repository status.')
      expect(notice).not.toHaveTextContent('Commit history may be out of date.')
      expect(screen.queryByRole('status', { name: 'Loading workspace…' })).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
      const prompt = errorReport.consumeChatHandoff()
      expect(prompt).toContain('- Request: /api/project/git/status -> HTTP 503')
      expect(prompt).toContain('- Code: git_status_unavailable')
      expect(prompt).toContain(`- Message: ${serverMessage}`)

      direct.unmount()
      const { default: FileBrowserRail } = await import('../pages/chat/FileBrowserRail')
      const railClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(
        <QueryClientProvider client={railClient}>
          <FileBrowserRail projectDir={ROOT} onFileOpen={vi.fn()} />
        </QueryClientProvider>,
      )
      fireEvent.click(screen.getByRole('button', { name: 'Changed' }))

      const hostedNotice = await screen.findByTestId('workspace-tree-status-error')
      expect(hostedNotice).toHaveTextContent('Couldn’t read the repository status.')
      expect(screen.getAllByRole('alert')).toHaveLength(1)
    } finally {
      errorReport.__resetErrorJournalForTests()
      errorReport.__resetNavSeamForTests()
      sessionStorage.clear()
    }
  })
})
