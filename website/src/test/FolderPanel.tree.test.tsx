/**
 * FolderPanel's SECOND body: the workspace tree it renders when the tab is
 * rooted at the current chat's project directory (#6077).
 *
 * The behaviours pinned here are the ones that decide WHICH body renders, plus
 * the contract the tree body owes the tab. They are separated from
 * `FolderPanel.test.tsx` because the Pierre tree is replaced by a probe that
 * echoes the props it was handed — that is what makes "what the panel tells the
 * tree" assertable without loading the trees runtime, and it must not weaken the
 * listing suite next door.
 *
 * The gate is deliberately string-only and platform-aware: `/api/project/tree`
 * answers for server-known project roots ONLY, so a tab on any other directory
 * must keep the one-level listing rather than render a panel the backend will
 * refuse with 403.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const H = vi.hoisted(() => ({ OPENED: '/repo/src/a.ts' }))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: (p: {
    projectDir: string
    searchQuery?: string | null
    onFileOpen?: (abs: string) => void
    onAddToContext?: (abs: string, kind: 'file' | 'dir') => void
  }) => (
    <button
      data-testid="tree"
      data-dir={p.projectDir}
      data-query={p.searchQuery ?? ''}
      data-has-add-to-context={p.onAddToContext ? '1' : '0'}
      onClick={() => p.onFileOpen?.(H.OPENED)}
      onContextMenu={() => p.onAddToContext?.(H.OPENED, 'file')}
    >
      tree
    </button>
  ),
}))

import FolderPanel from '../pages/chat/FolderPanel'
import { api } from '../api/client'

const ROOT = '/repo'

function listing(path = ROOT, parent: string | null = '/') {
  return {
    path,
    parent,
    dirs: [{ name: 'src', path: `${path}/src` }],
    files: [{ name: 'README.md', path: `${path}/README.md` }],
  }
}

function renderPanel(
  props: {
    path: string
    projectDir?: string
    onFileOpen?: (p: string) => void
    onPathChange?: (p: string) => void
    onAddToContext?: (p: string, kind: 'file' | 'dir') => void
  },
  platform = 'linux',
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // `useGatewayPlatform` is a pure reader over this cache key (the prerequisite
  // gate owns the fetch), so seeding it is how a test says "the gateway is
  // Windows" without a request.
  client.setQueryData(['kiro-prerequisite'], { platform })
  return render(
    <QueryClientProvider client={client}>
      <FolderPanel onClose={() => {}} {...props} />
    </QueryClientProvider>,
  )
}

const tree = () => screen.getByTestId('tree')

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'browseFiles').mockImplementation(async (p: string) => listing(p) as never)
  vi.spyOn(api, 'projectTree').mockResolvedValue({ root: ROOT, paths: ['README.md'], repo: true } as never)
  vi.spyOn(api, 'projectGitStatus').mockResolvedValue({ repo: true, files: [] } as never)
  vi.spyOn(api, 'fileSearch').mockResolvedValue({ root: ROOT, results: [] } as never)
})

describe('FolderPanel — project-root workspace tree', () => {
  it('renders the workspace tree instead of the one-level listing at the project root', async () => {
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(tree()).toBeTruthy())
    expect(tree().getAttribute('data-dir')).toBe(ROOT)
    // The listing's own rows are what the tree replaces: seeing either of these
    // would mean both bodies rendered.
    expect(screen.queryByText('src')).toBeNull()
    expect(screen.queryByText('README.md')).toBeNull()
  })

  it('keeps the one-level listing for a tab that is not the project root', async () => {
    renderPanel({ path: `${ROOT}/src`, projectDir: ROOT })
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('tree')).toBeNull()
  })

  it('matches the project root across trailing slashes, and separator flavour only on Windows', async () => {
    const { unmount } = renderPanel({ path: `${ROOT}/`, projectDir: ROOT })
    await waitFor(() => expect(screen.getByTestId('tree')).toBeTruthy())
    unmount()

    renderPanel({ path: 'C:\\repo\\', projectDir: 'C:/repo' }, 'win32')
    await waitFor(() => expect(screen.getByTestId('tree')).toBeTruthy())
  })

  it('never treats a POSIX backslash as a separator', async () => {
    // On Linux `\` is an ordinary filename character, so `/srv/a\b` and
    // `/srv/a/b` are two different real directories. Folding the separator here
    // would render one project's tree under the other's path, and a file opened
    // from it would be the wrong file on disk.
    vi.spyOn(api, 'browseFiles').mockResolvedValue(listing('/srv/a\\b') as never)
    renderPanel({ path: '/srv/a\\b', projectDir: '/srv/a/b' }, 'linux')
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('tree')).toBeNull()
  })

  it('never folds case, on either platform', async () => {
    // Windows is case-insensitive only by DEFAULT: NTFS carries a per-directory
    // case-sensitivity flag, so two siblings differing only in case can both
    // exist. Aliasing them would render the wrong directory's tree; declining to
    // match merely keeps today's listing, which is the safe direction.
    vi.spyOn(api, 'browseFiles').mockResolvedValue(listing('C:\\Repo') as never)
    const { unmount } = renderPanel({ path: 'C:\\Repo', projectDir: 'C:\\repo' }, 'win32')
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('tree')).toBeNull()
    unmount()

    vi.spyOn(api, 'browseFiles').mockResolvedValue(listing('/Repo') as never)
    renderPanel({ path: '/Repo', projectDir: '/repo' }, 'linux')
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('tree')).toBeNull()
  })

  it('falls back to the listing when the tree endpoint refuses the directory', async () => {
    vi.spyOn(api, 'projectTree').mockRejectedValue(new Error('unknown_project_dir'))
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(screen.queryByTestId('tree')).toBeNull()
  })

  it('refreshes the visible listing while the project tree is unavailable', async () => {
    vi.spyOn(api, 'projectTree').mockRejectedValue(new Error('unknown_project_dir'))
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(screen.getByText('src')).toBeTruthy())
    expect(api.browseFiles).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(api.browseFiles).toHaveBeenCalledTimes(2))
  })

  it('opens a file through the normal file tab without re-targeting the folder tab', async () => {
    const onFileOpen = vi.fn()
    const onPathChange = vi.fn()
    renderPanel({ path: ROOT, projectDir: ROOT, onFileOpen, onPathChange })
    fireEvent.click(await waitFor(() => tree()))
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED)
    // The whole point of the tree body: browsing descendants never moves the tab.
    expect(onPathChange).not.toHaveBeenCalled()
  })

  it('feeds the search box into the tree instead of the recursive file search', async () => {
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(tree()).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Search files'), { target: { value: 'read' } })
    await waitFor(() => expect(tree().getAttribute('data-query')).toBe('read'))
    // Past the 200ms debounce the listing body would have dispatched a walk; the
    // tree already holds the path set, so nothing is requested.
    await new Promise(resolve => setTimeout(resolve, 260))
    expect(api.fileSearch).not.toHaveBeenCalled()
  })

  it('refreshes the queries the tree reads, not the directory listing', async () => {
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledTimes(2))
  })

  it('hands the tree the same add-to-context action the Files rail gets', async () => {
    const onAddToContext = vi.fn()
    renderPanel({ path: ROOT, projectDir: ROOT, onAddToContext })
    await waitFor(() => expect(tree()).toBeTruthy())
    // Pinned because the SAME tree renders in two surfaces now: a row that offers
    // the action in the rail and not here is exactly how the two would diverge.
    expect(tree().getAttribute('data-has-add-to-context')).toBe('1')
    fireEvent.contextMenu(tree())
    expect(onAddToContext).toHaveBeenCalledWith(H.OPENED, 'file')
  })

  it('names the search shape while filtering the tree, and not before', async () => {
    renderPanel({ path: ROOT, projectDir: ROOT })
    await waitFor(() => expect(tree()).toBeTruthy())
    expect(screen.queryByText('includes subfolders')).toBeNull()
    fireEvent.change(screen.getByLabelText('Search files'), { target: { value: 'ord' } })
    const hint = await waitFor(() => screen.getByText('includes subfolders'))
    expect(hint).toHaveClass('min-w-0', 'truncate')
  })

  it('still offers the parent row, which is what leaves tree mode', async () => {
    const onPathChange = vi.fn()
    renderPanel({ path: ROOT, projectDir: ROOT, onPathChange })
    fireEvent.click(await waitFor(() => screen.getByText('Parent folder')))
    expect(onPathChange).toHaveBeenCalledWith('/')
    // Stepping out of the project root drops the tab back to the listing.
    await waitFor(() => expect(screen.queryByTestId('tree')).toBeNull())
  })
})
