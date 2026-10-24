/**
 * PierreWorkspaceTreeImpl — expanding folders: the remembered expansion, and
 * the folders past the row cap that load on their own when opened.
 *
 * One topic of the wrapper suite whose conventions
 * `PierreWorkspaceTreeImpl.test.tsx` describes; the shared fixtures live in
 * `PierreWorkspaceTreeImplTestUtils.tsx`, and the `vi.mock` block is the
 * suite's own, word for word.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, fireEvent, within } from '@testing-library/react'

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
import { MAX_FOLDER_LISTINGS } from '../pierre/treeFolderListings'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import {
  recallExpandedPaths,
  rememberExpandedPaths,
  __resetTreeExpansionMemoryForTests,
} from '../pierre/treeExpansionMemory'
import { treeMock } from './__mocks__/pierreTreesReact'
import {
  ROOT,
  M,
  mkTree,
  mkStatus,
  mkFile,
  renderTree,
  waitForTree,
} from './PierreWorkspaceTreeImplTestUtils'
import type { TreePayload } from './PierreWorkspaceTreeImplTestUtils'

beforeEach(() => {
  treeMock.reset()
  vi.mocked(api.projectTree).mockResolvedValue(mkTree())
  vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([]))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('PierreWorkspaceTreeImpl — expansion persistence', () => {
  // The Files tab mounts only while active, so opening a file remounts the
  // whole tree; `persistExpansion` remembers the expanded directories (keyed
  // by projectDir) and restores them through `resetPaths`'
  // `initialExpandedPaths`. Off by default: the other hosts of this shared
  // tree keep their collapsed-by-default behavior.
  const TREE_PATHS = ['src/a.ts', 'src/lib/b.ts', 'README.md']
  // `@pierre/trees` materializes DIRECTORY row paths with a trailing slash
  // (`src/`, not `src`); the helper emits that canonical shape so these tests
  // exercise what the real library hands the capture. Shape verified against
  // @pierre/trees 1.0.0-beta.6 (path-store materializeNodePath appends `/`
  // to directory nodes) — re-verify on an upgrade.
  const expandedDir = (path: string) => ({ kind: 'directory' as const, path: path + '/', isExpanded: true })

  beforeEach(() => {
    __resetTreeExpansionMemoryForTests()
    localStorage.clear()
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: TREE_PATHS }))
  })

  it('passes no expansion options on a first mount with nothing remembered', async () => {
    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([TREE_PATHS])
    expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])
  })

  it('restores the expanded directories on a remount of the same projectDir', async () => {
    const { unmount } = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => {
      treeMock.last().simulateVisibleRows([
        expandedDir('src'),
        expandedDir('src/lib'),
        { kind: 'file', path: 'src/a.ts', isExpanded: false },
      ])
    })
    unmount()

    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src', 'src/lib'] },
    ])
  })

  it('gives a different projectDir nothing', async () => {
    const { unmount } = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src')]) })
    unmount()

    renderTree({ persistExpansion: true, projectDir: '/repo/other' })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])
  })

  it('drops remembered directories absent from the new payload', async () => {
    const { unmount } = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src'), expandedDir('src/lib')]) })
    unmount()

    // `src/lib` no longer exists in the new payload: a stale path must be
    // filtered out rather than handed to the controller.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['src/a.ts'] }))
    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
  })

  it('snapshots nothing while a search is active', async () => {
    // The search session expands matches transiently; persisting that would
    // restore an unrelated expansion after the filter is cleared.
    const { unmount } = renderTree({ persistExpansion: true, searchQuery: 'lib' })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src'), expandedDir('src/lib')]) })
    unmount()

    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])
  })

  it('neither reads nor writes the memory in changed mode', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/src/a.ts', 'M')]),
    )
    rememberExpandedPaths(ROOT, ['src'])

    const { unmount } = renderTree({ persistExpansion: true, mode: 'changed' })
    await waitForTree()
    // Changed mode starts fully open by design: the remembered set must not
    // narrow it.
    expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])

    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src'), expandedDir('src/x')]) })
    unmount()

    // And the changed-mode notification must not have overwritten the memory.
    renderTree({ persistExpansion: true })
    await waitForTree()
    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
  })

  it('carries a remembered directory through a payload that temporarily lacks it', async () => {
    // The backend truncates large payloads and a branch switch can drop a
    // subtree: a remembered directory absent from the current payload is not
    // evidence of a collapse, so a capture during that window must not erase
    // it, and it must restore once the payload carries it again.
    const first = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src'), expandedDir('src/lib')]) })
    first.unmount()

    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['src/a.ts'] }))
    const second = renderTree({ persistExpansion: true })
    await waitForTree()
    // Only the still-present directory is passed to the controller…
    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
    // …and a capture during this window keeps the absent one remembered.
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src')]) })
    second.unmount()

    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: TREE_PATHS }))
    renderTree({ persistExpansion: true })
    await waitForTree()
    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src', 'src/lib'] },
    ])
  })

  it('does not erase the memory when the model transiently holds no rows', async () => {
    // A `project-tree` poll can answer with an empty payload (re-indexing,
    // transient backend miss); the resulting empty visible set must not
    // overwrite the remembered expansion.
    const { unmount } = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src')]) })
    act(() => { treeMock.last().simulateVisibleRows([]) })
    unmount()

    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
  })

  it('evicts the least recently written project from storage past the dir cap', () => {
    // One storage key holds every project's entry; without eviction a long
    // session would grow the origin's quota use without bound.
    for (let i = 0; i <= 20; i++) rememberExpandedPaths(`/repo/p${i}`, ['src'])
    __resetTreeExpansionMemoryForTests()

    expect(recallExpandedPaths('/repo/p0')).toEqual([])
    expect(recallExpandedPaths('/repo/p1')).toEqual(['src'])
    expect(recallExpandedPaths('/repo/p20')).toEqual(['src'])
  })

  it('restores from the localStorage mirror after a page reload', async () => {
    const { unmount } = renderTree({ persistExpansion: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src')]) })
    unmount()

    // A page reload drops the module-scope session map; the localStorage
    // mirror is what carries the expansion across it.
    __resetTreeExpansionMemoryForTests()
    renderTree({ persistExpansion: true })
    await waitForTree()

    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
  })

  it('tolerates localStorage throwing on both write and read', async () => {
    // Private mode / quota: storage access is best-effort, the module-scope
    // session map still covers the remount case.
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage unavailable')
    })
    try {
      const first = renderTree({ persistExpansion: true })
      await waitForTree()
      act(() => { treeMock.last().simulateVisibleRows([expandedDir('src')]) })
      first.unmount()

      const second = renderTree({ persistExpansion: true })
      await waitForTree()
      expect(treeMock.last().calls.resetPathsOptions).toEqual([
        { initialExpandedPaths: ['src'] },
      ])
      second.unmount()

      // With the session map gone (a fresh page load) the throwing read falls
      // back to nothing remembered rather than crashing.
      __resetTreeExpansionMemoryForTests()
      const third = renderTree({ persistExpansion: true })
      await waitForTree()
      expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])
      third.unmount()
    } finally {
      setItem.mockRestore()
      getItem.mockRestore()
    }
  })

  it('leaves the memory alone when persistExpansion is off (the default)', async () => {
    rememberExpandedPaths(ROOT, ['src'])

    const { unmount } = renderTree()
    await waitForTree()
    expect(treeMock.last().calls.resetPathsOptions).toEqual([undefined])

    act(() => { treeMock.last().simulateVisibleRows([expandedDir('src'), expandedDir('src/x')]) })
    unmount()

    renderTree({ persistExpansion: true })
    await waitForTree()
    expect(treeMock.last().calls.resetPathsOptions).toEqual([
      { initialExpandedPaths: ['src'] },
    ])
  })
})

describe('PierreWorkspaceTreeImpl — folders past the row cap', () => {
  const cut = () => mkTree({
    paths: ['README.md'],
    directories: ['big'],
    truncated: true,
    truncatedDirectories: ['big'],
  })
  const bigListing = (): TreePayload => ({
    root: ROOT,
    dir: 'big',
    paths: ['big/late/leaf.ts'],
    directories: ['big/late'],
    repo: false,
    truncated: false,
    truncatedDirectories: [],
  })
  const answer = () => vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir === 'big' ? bigListing() : cut()))

  it('loads a cut folder on its own when it is expanded, and feeds its rows to the model', async () => {
    answer()
    renderTree()
    await waitForTree()
    const model = treeMock.last()
    expect(api.projectTree).not.toHaveBeenCalledWith(ROOT, 'big')
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'big'))
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts'))
  })

  it('loads a cut folder opened through its chain row, and keeps it open once split', async () => {
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir === 'big'
      ? bigListing()
      : mkTree({ paths: ['big/core/a.ts'], directories: ['big', 'big/core'], truncated: true, truncatedDirectories: ['big'] })))
    renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big/core'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/core/', isExpanded: true }])
    })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'big'))
    await waitFor(() => expect(model.calls.expand).toContain('big'))
  })

  describe('a nested repository', () => {
    // A submodule is a childless folder row (no state row holds it open), so the
    // reset that folds its own listing in would close it again. It is re-opened
    // once the listing has landed.
    const nestedRoot = () => mkTree({
      paths: ['README.md'],
      directories: ['libs', 'libs/sub'],
      nestedRepoDirectories: ['libs/sub'],
    })
    const subListing = (): TreePayload => ({
      root: ROOT,
      dir: 'libs/sub',
      paths: ['libs/sub/a.ts'],
      directories: [],
      repo: false,
      truncated: false,
      truncatedDirectories: [],
    })

    it('is re-opened once its own listing has landed', async () => {
      vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir === 'libs/sub' ? subListing() : nestedRoot()))
      renderTree()
      await waitForTree()
      const model = treeMock.last()
      act(() => {
        model.simulateExpanded(['libs/sub'])
        model.simulateVisibleRows([{ kind: 'directory', path: 'libs/sub/', isExpanded: true }])
      })
      await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'libs/sub'))
      await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('libs/sub/a.ts'))
      await waitFor(() => expect(model.calls.expand).toContain('libs/sub'))
    })

    it('is not re-opened when it was closed again before its listing landed', async () => {
      let release: (() => void) | undefined
      const gate = new Promise<void>(resolve => { release = resolve })
      vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
        if (dir !== 'libs/sub') return nestedRoot()
        await gate
        return subListing()
      })
      renderTree()
      await waitForTree()
      const model = treeMock.last()
      act(() => {
        model.simulateExpanded(['libs/sub'])
        model.simulateVisibleRows([{ kind: 'directory', path: 'libs/sub/', isExpanded: true }])
      })
      await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'libs/sub'))
      act(() => {
        model.simulateExpanded([])
        model.simulateVisibleRows([{ kind: 'directory', path: 'libs/sub/', isExpanded: false }])
      })
      await act(async () => { release?.(); await new Promise<void>(resolve => setTimeout(resolve, 0)) })
      await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('libs/sub/a.ts'))
      expect(model.calls.expand).not.toContain('libs/sub')
    })
  })

  it('does not reload a folder the listing bound dropped while it stays open', async () => {
    const folders = Array.from({ length: MAX_FOLDER_LISTINGS + 1 }, (_, i) => `f${String(i).padStart(2, '0')}`)
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir
      ? { root: ROOT, dir, paths: [`${dir}/x.ts`], directories: [], repo: false, truncated: false, truncatedDirectories: [] }
      : mkTree({ paths: ['README.md'], directories: folders, truncated: true, truncatedDirectories: folders })))
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(folders)
      model.simulateVisibleRows(folders.map(f => ({ kind: 'directory' as const, path: `${f}/`, isExpanded: true })))
    })
    const folderCalls = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1]).length
    await waitFor(() => expect(folderCalls()).toBeGreaterThanOrEqual(MAX_FOLDER_LISTINGS))
    const settled = folderCalls()
    // Every further store event finds the dropped folder still open and cut;
    // without the guard each one would load it again and drop the next.
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        model.simulateVisibleRows(folders.map(f => ({ kind: 'directory' as const, path: `${f}/`, isExpanded: true })))
        await new Promise<void>(resolve => queueMicrotask(resolve))
      })
    }
    await waitFor(() => expect(model.calls.resetPaths.length).toBeGreaterThan(0))
    expect(folderCalls()).toBe(settled)
    // The dropped folder's cached listing is gone too, so a host Refresh
    // (a prefix refetch, inactive queries included) cannot re-walk it.
    expect(qc.getQueryCache().find({ queryKey: ['project-tree', ROOT, 'dir', folders[0]], exact: true })).toBeUndefined()
  })

  it('names a cut folder whose own listing failed in a notice above the tree', async () => {
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'big') throw new Error('deadline exceeded')
      return cut()
    })
    renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    const notice = await screen.findByTestId('workspace-tree-folder-listing-retry')
    expect(notice).toHaveTextContent('big')
    expect(screen.queryByTestId('workspace-tree-folder-unavailable')).toBeNull()
  })

  it('retries a failed folder from the notice\'s own button and clears it on success', async () => {
    let fail = true
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'big') {
        if (fail) throw new Error('deadline exceeded')
        return bigListing()
      }
      return cut()
    })
    renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    const notice = await screen.findByTestId('workspace-tree-folder-listing-error')
    fail = false
    fireEvent.click(within(notice).getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.queryByTestId('workspace-tree-folder-listing-error')).toBeNull())
    expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts')
  })

  it('does not reload a gone (404) folder that stays open while the whole listing still names it', async () => {
    let gone = false
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'big') {
        if (gone) throw new ApiError(404, 'Folder cannot be listed')
        return bigListing()
      }
      return cut()
    })
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    const rows = [{ kind: 'directory' as const, path: 'big/', isExpanded: true }]
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows(rows)
    })
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts'))
    gone = true
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT, 'dir', 'big'], exact: true }) })
    const bigCalls = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1] === 'big').length
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).not.toContain('big/late/leaf.ts'))
    const settled = bigCalls()
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        model.simulateVisibleRows(rows)
        await new Promise<void>(resolve => queueMicrotask(resolve))
      })
    }
    expect(bigCalls()).toBe(settled)
  })

  it('re-reads at most three open loaded folders per whole-tree poll, in turn', async () => {
    const folders = ['f1', 'f2', 'f3', 'f4', 'f5']
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir
      ? { root: ROOT, dir, paths: [`${dir}/x.ts`], directories: [], repo: false, truncated: false, truncatedDirectories: [] }
      : mkTree({ paths: ['README.md'], directories: folders, truncated: true, truncatedDirectories: folders })))
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(folders)
      model.simulateVisibleRows(folders.map(f => ({ kind: 'directory' as const, path: `${f}/`, isExpanded: true })))
    })
    const dirCalls = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1]).length
    await waitFor(() => expect(dirCalls()).toBe(folders.length))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT], exact: true }) })
    await waitFor(() => expect(dirCalls()).toBe(folders.length + 3))
    await act(async () => { await new Promise<void>(resolve => queueMicrotask(resolve)) })
    expect(dirCalls()).toBe(folders.length + 3)
  })

  it('keeps the notice of a refused (403) folder until the user dismisses it, without re-fetching it', async () => {
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'link') throw new ApiError(403, 'Folder cannot be listed')
      return cut()
    })
    const { update } = renderTree()
    await waitForTree()
    update({ reveal: { path: 'link', nonce: 1 } })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'link'))
    const notice = await screen.findByTestId('workspace-tree-folder-listing-error')
    expect(notice).toHaveTextContent('link')
    // A refusal offers no retry: none can change it.
    expect(screen.getByTestId('workspace-tree-folder-refused')).toHaveTextContent("can't be opened here")
    expect(screen.queryByTestId('workspace-tree-folder-gone')).toBeNull()
    expect(screen.queryByTestId('workspace-tree-folder-listing-retry')).toBeNull()
    expect(within(notice).queryByRole('button', { name: /retry/i })).toBeNull()
    await act(async () => { await new Promise<void>(resolve => queueMicrotask(resolve)) })
    expect(vi.mocked(api.projectTree).mock.calls.filter(c => c[1] === 'link')).toHaveLength(1)
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(screen.queryByTestId('workspace-tree-folder-listing-error')).toBeNull())
  })

  it('keeps a retained folder failure on screen when the whole tree turns empty', async () => {
    let empty = false
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'only') throw new ApiError(404, 'Folder cannot be listed')
      return empty
        ? mkTree({ paths: [], directories: [], truncated: false, truncatedDirectories: [] })
        : mkTree({ paths: [], directories: ['only'], truncated: true, truncatedDirectories: ['only'] })
    })
    const { qc, update } = renderTree()
    await waitForTree()
    update({ reveal: { path: 'only', nonce: 1 } })
    expect(await screen.findByTestId('workspace-tree-folder-listing-error')).toHaveTextContent('only')
    expect(screen.getByTestId('workspace-tree-folder-gone')).toHaveTextContent('no longer exists')
    empty = true
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT], exact: true }) })
    expect(screen.getByTestId('workspace-tree-folder-listing-error')).toHaveTextContent('only')
  })

  it('drops the rows of a loaded folder that is gone (404) and keeps naming it', async () => {
    let gone = false
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'big') {
        if (gone) throw new ApiError(404, 'Folder cannot be listed')
        return bigListing()
      }
      return cut()
    })
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts'))
    gone = true
    act(() => model.simulateExpanded([]))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT, 'dir', 'big'], exact: true }) })
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).not.toContain('big/late/leaf.ts'))
    // The rows go; the notice that says why stays until dismissed.
    expect(screen.getByTestId('workspace-tree-folder-listing-error')).toHaveTextContent('big')
  })

  it('keeps naming a folder whose refresh failed after a good read', async () => {
    let fail = false
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'big') {
        if (fail) throw new Error('deadline exceeded')
        return bigListing()
      }
      return cut()
    })
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    await waitFor(() => expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts'))
    fail = true
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT, 'dir', 'big'], exact: true }) })
    expect(await screen.findByTestId('workspace-tree-folder-listing-error')).toHaveTextContent('big')
    expect(model.calls.resetPaths.at(-1)).toContain('big/late/leaf.ts')
  })

  it('re-reads an open loaded folder when the whole-tree poll lands, and not a closed one', async () => {
    answer()
    const { qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    act(() => {
      model.simulateExpanded(['big'])
      model.simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
    })
    const bigCalls = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1] === 'big').length
    await waitFor(() => expect(bigCalls()).toBe(1))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT], exact: true }) })
    await waitFor(() => expect(bigCalls()).toBe(2))
    act(() => model.simulateExpanded([]))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT], exact: true }) })
    await act(async () => { await new Promise<void>(resolve => queueMicrotask(resolve)) })
    expect(bigCalls()).toBe(2)
  })

  it('tells the host a reveal was consumed, so a remount cannot replay it', async () => {
    answer()
    const onRevealed = vi.fn()
    const { update } = renderTree({ onRevealed })
    await waitForTree()
    update({ onRevealed, reveal: { path: 'big', nonce: 1 } })
    await waitFor(() => expect(onRevealed).toHaveBeenCalledTimes(1))
  })

  it('loads and expands a folder a host reveals', async () => {
    answer()
    const { update } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    update({ reveal: { path: 'big', nonce: 1 } })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'big'))
    await waitFor(() => expect(model.calls.focusPath).toContain('big/'))
    expect(model.calls.expand).toContain('big')
    expect(model.calls.select).toContain('big')
  })

  it('re-reads the whole listing once when a folder answers 404, not when it answers 403', async () => {
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => {
      if (dir === 'gone') throw new ApiError(404, 'Folder cannot be listed')
      if (dir === 'link') throw new ApiError(403, 'Folder cannot be listed')
      return cut()
    })
    const { update } = renderTree()
    await waitForTree()
    const wholeReads = () => vi.mocked(api.projectTree).mock.calls.filter(c => !c[1]).length
    const before = wholeReads()
    update({ reveal: { path: 'link', nonce: 1 } })
    await screen.findByTestId('workspace-tree-folder-refused')
    await act(async () => { await new Promise<void>(resolve => queueMicrotask(resolve)) })
    expect(wholeReads()).toBe(before)
    update({ reveal: { path: 'gone', nonce: 2 } })
    await screen.findByTestId('workspace-tree-folder-gone')
    await waitFor(() => expect(wholeReads()).toBe(before + 1))
    await act(async () => { await new Promise<void>(resolve => queueMicrotask(resolve)) })
    expect(wholeReads()).toBe(before + 1)
  })

  it('reveals the nearest listed ancestor when a revealed folder settles with no row', async () => {
    // git lists an empty or ignored folder as nothing, so its listing adds no row.
    vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir === 'big/empty'
      ? { root: ROOT, dir, paths: [], directories: [], repo: true, truncated: false, truncatedDirectories: [] }
      : cut()))
    const { update } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    update({ reveal: { path: 'big/empty', nonce: 1 } })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(ROOT, 'big/empty'))
    await waitFor(() => expect(model.calls.focusPath).toContain('big/'))
    expect(model.calls.select).toContain('big')
    expect(model.calls.select).not.toContain('big/empty')
  })

  it('hands the cut folder to the host when its limit row is selected', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['big'],
      truncated: true,
      truncatedDirectories: ['big'],
    }))
    const onOpenFolder = vi.fn()
    renderTree({ onOpenFolder })
    await waitForTree()
    const model = treeMock.last()
    const row = model.calls.resetPaths.at(-1)!.find(p => p.startsWith('big/') && p.endsWith(M))!
    expect(row).toContain('Open in folder tab')
    act(() => model.simulateSelection(row))
    expect(onOpenFolder).toHaveBeenCalledWith('big')
  })
})
