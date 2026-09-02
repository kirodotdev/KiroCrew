/**
 * pierre/tree.tsx — the lazy boundary and its shimmer placeholder.
 *
 * The impl behind the boundary is covered by PierreWorkspaceTreeImpl.test.tsx;
 * what matters here is that the boundary hands off correctly: the same skeleton
 * covers both the chunk load and the first data load, and a mode switch REMOUNTS
 * the impl (initial expansion is fixed at model creation, so reusing the instance
 * would leave 'changed' mode collapsed).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
}))

import { PierreWorkspaceTree, TreeSkeleton } from '../pierre/tree'
import { api } from '../api/client'
import { treeMock } from './__mocks__/pierreTreesReact'

const ROOT = '/repo/project'

let qc: QueryClient

function wrap(children: ReactNode) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  treeMock.reset()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(api.projectTree).mockResolvedValue({ root: ROOT, paths: ['README.md'], repo: true })
  vi.mocked(api.projectGitStatus).mockResolvedValue({ repo: true, repoRoot: '/repo', files: [] })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('TreeSkeleton', () => {
  it('renders staggered tree-shaped shimmer rows announced as a loading status', () => {
    render(<TreeSkeleton />)

    const status = screen.getByRole('status', { name: 'Loading workspace…' })
    const rows = Array.from(status.children) as HTMLElement[]
    expect(rows).toHaveLength(8)
    // Varied indents and widths are what make it read as a file tree rather
    // than a generic list.
    expect(rows.map(r => r.style.marginLeft)).toEqual(
      ['0px', '14px', '28px', '28px', '14px', '28px', '0px', '14px'],
    )
    expect(new Set(rows.map(r => r.style.width)).size).toBeGreaterThan(1)
    expect(rows.map(r => r.style.animationDelay)).toEqual(
      ['0ms', '90ms', '180ms', '270ms', '360ms', '450ms', '540ms', '630ms'],
    )
    expect(rows.every(r => r.className.includes('animate-pulse'))).toBe(true)
  })
})

describe('PierreWorkspaceTree', () => {
  it('shows the skeleton while the trees chunk loads, then the tree', async () => {
    render(wrap(<PierreWorkspaceTree projectDir={ROOT} />))

    expect(screen.getByRole('status', { name: 'Loading workspace…' })).toBeInTheDocument()
    expect(treeMock.models).toHaveLength(0)

    await waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())
    expect(screen.queryByRole('status', { name: 'Loading workspace…' })).not.toBeInTheDocument()
  })

  it('forwards every prop through the boundary', async () => {
    const onFileOpen = vi.fn()
    render(wrap(
      <PierreWorkspaceTree
        projectDir={ROOT}
        onFileOpen={onFileOpen}
        searchQuery="rail"
        selectedPath={`${ROOT}/README.md`}
      />,
    ))
    await waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())

    expect(api.projectTree).toHaveBeenCalledWith(ROOT)
    const model = treeMock.last()
    expect(model.calls.search).toContain('rail')
    expect(model.calls.select).toEqual(['README.md'])
    // selectedPath is the file already open, so this selection is not an open —
    // proof the prop reached the impl rather than being dropped at the boundary.
    act(() => { model.simulateSelection('README.md') })
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('remounts the impl when the mode changes', async () => {
    const { rerender } = render(wrap(<PierreWorkspaceTree projectDir={ROOT} mode="all" />))
    await waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())
    expect(treeMock.models).toHaveLength(1)

    rerender(wrap(<PierreWorkspaceTree projectDir={ROOT} mode="changed" />))

    await waitFor(() => expect(treeMock.models).toHaveLength(2))
    expect(treeMock.models[0].options).toMatchObject({ initialExpansion: 'closed' })
    expect(treeMock.models[1].options).toMatchObject({ initialExpansion: 'open' })
  })

  it('defaults to the full-workspace mode', async () => {
    render(wrap(<PierreWorkspaceTree projectDir={ROOT} />))
    await waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())

    expect(treeMock.last().options).toMatchObject({ initialExpansion: 'closed' })
  })
})
