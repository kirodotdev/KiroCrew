/**
 * PierreWorkspaceTreeImpl — the wrapper's own logic around `@pierre/trees`.
 *
 * The trees runtime renders custom elements that never upgrade in the test DOM,
 * so `@pierre/trees/react` is replaced by a recording fake (see
 * `./__mocks__/pierreTreesReact`) and every assertion here is about what THIS
 * file does: how it maps props onto the model, how it turns two differently
 * anchored API payloads into one relative path set, and how it wires the
 * model's selection event back out as an open.
 *
 * Conventions follow ActivityViewerCoverage.test.tsx (locally-built
 * QueryClientProvider wrapper, an `api` module mock, small fixture makers).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
}))

import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { api } from '../api/client'
import { treeMock } from './__mocks__/pierreTreesReact'
import type { MenuItem, MenuContext } from './__mocks__/pierreTreesReact'

const ROOT = '/repo/project'
const PATHS = ['README.md', 'src/a/b.ts']

type TreePayload = Awaited<ReturnType<typeof api.projectTree>>
type StatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>
type StatusFile = StatusPayload['files'][number]

const mkTree = (over: Partial<TreePayload> = {}): TreePayload => ({
  root: ROOT,
  paths: PATHS,
  repo: true,
  ...over,
})

const mkStatus = (files: StatusFile[], over: Partial<StatusPayload> = {}): StatusPayload => ({
  repo: true,
  repoRoot: '/repo',
  files,
  ...over,
})

const mkFile = (path: string, status: string, staged = false): StatusFile => ({ path, status, staged })

type Props = Parameters<typeof PierreWorkspaceTreeImpl>[0]

function renderTree(props: Partial<Props> = {}) {
  // structuralSharing off: with it on, a poll that returns deep-equal data
  // reuses the previous `data` object, so the wrapper's own same-paths guard
  // would never be exercised — react-query would be doing the work.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, structuralSharing: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const view = render(<PierreWorkspaceTreeImpl projectDir={ROOT} {...props} />, { wrapper })
  return {
    qc,
    ...view,
    update: (next: Partial<Props> = {}) =>
      view.rerender(<PierreWorkspaceTreeImpl projectDir={ROOT} {...props} {...next} />),
  }
}

/** Resolve once the first payload has been folded into the model. */
const waitForTree = () => waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())

beforeEach(() => {
  treeMock.reset()
  vi.mocked(api.projectTree).mockResolvedValue(mkTree())
  vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([]))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('PierreWorkspaceTreeImpl — data loading', () => {
  it('shows the shimmer skeleton until the first payload decides empty vs populated', async () => {
    let resolveTree: (payload: TreePayload) => void = () => {}
    vi.mocked(api.projectTree).mockReturnValue(new Promise<TreePayload>(r => { resolveTree = r }))

    renderTree()

    expect(screen.getByRole('status', { name: 'Loading workspace…' })).toBeInTheDocument()
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
    // No path set may reach the model before the payload arrives, or the first
    // visible frame would be an authoritative-looking empty tree.
    expect(treeMock.last().calls.resetPaths).toEqual([])

    await act(async () => { resolveTree(mkTree()) })

    await waitForTree()
    expect(screen.queryByRole('status', { name: 'Loading workspace…' })).not.toBeInTheDocument()
    expect(treeMock.last().calls.resetPaths).toEqual([PATHS])
  })

  it('mounts the tree collapsed, with flattening on and the built-in search bar off', async () => {
    renderTree()
    await waitForTree()

    expect(treeMock.last().options).toMatchObject({
      initialExpansion: 'closed',
      flattenEmptyDirectories: true,
      search: false,
    })
    const props = treeMock.fileTreeProps.at(-1)!
    expect(props.model).toBe(treeMock.last())
    expect(props.className).toBe('pierre-tree')
    expect(props.style).toMatchObject({ height: '100%', flex: 1, minHeight: 0 })
  })

  it('does not reset the model when a refetch returns the same path set', async () => {
    const { qc } = renderTree()
    await waitForTree()
    expect(treeMock.last().calls.resetPaths).toHaveLength(1)

    // Same paths, different payload object: a reset here would throw away
    // expansion, focus and selection on every 10s poll.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: [...PATHS], truncated: false }))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })

    expect(treeMock.last().calls.resetPaths).toHaveLength(1)
  })

  it('resets the model when the path set actually changes', async () => {
    const { qc } = renderTree()
    await waitForTree()

    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['only.ts'] }))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })

    // waitFor, not a bare expect: the refetch resolving and the effect that
    // calls resetPaths are two separate ticks, so asserting straight after act()
    // races the second one and intermittently sees only the initial reset. The
    // sibling assertions above are safe because they check a COUNT that is
    // already final; this one waits for the second entry to land.
    await waitFor(() =>
      expect(treeMock.last().calls.resetPaths).toEqual([PATHS, ['only.ts']]),
    )
  })

  it('de-duplicates a workspace payload before it reaches the model', async () => {
    // Egress redaction can collapse two different paths to the same string, so
    // the API list may carry a duplicate. @pierre/trees throws 'Duplicate path'
    // on adjacent identical entries — uncaught inside the resetPaths layout
    // effect, it crashes the route — so the wrapper must de-dup, preserving
    // first occurrence, before handing the set to the model.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md', 'src/a.ts', 'README.md'] }))
    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([['README.md', 'src/a.ts']])
  })

  it('reports an empty workspace instead of an empty tree', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: [] }))
    renderTree()

    await waitFor(() => expect(screen.getByText('No files in this workspace yet')).toBeInTheDocument())
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
  })

  it('warns that a large workspace payload was truncated, in all mode only', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ truncated: true }))
    const { unmount } = renderTree()
    await waitForTree()
    expect(screen.getByText(/Large workspace/)).toBeInTheDocument()
    unmount()

    // Changed mode renders the git-status set, which is never truncated.
    vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([mkFile('project/a.ts', 'M')]))
    renderTree({ mode: 'changed' })
    await waitForTree()
    expect(screen.queryByText(/Large workspace/)).not.toBeInTheDocument()
  })
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
})

describe('PierreWorkspaceTreeImpl — selection wiring', () => {
  it('reports a selected file row as an open, with the absolute path', async () => {
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen })
    await waitForTree()

    act(() => { treeMock.last().simulateSelection('src/a/b.ts') })

    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`)
  })

  it('ignores a directory row, a multi-row selection, and a selection off the focused row', async () => {
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen })
    await waitForTree()
    const model = treeMock.last()

    act(() => { model.simulateSelection('src') })
    act(() => { model.simulateSelection('README.md', ['README.md', 'src/a/b.ts']) })
    act(() => { model.simulateSelection('README.md', ['src/a/b.ts']) })
    expect(onFileOpen).not.toHaveBeenCalled()

    act(() => { model.simulateSelection('README.md') })
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/README.md`)
  })

  it('picks up an onFileOpen handler swapped in after mount', async () => {
    const first = vi.fn()
    const second = vi.fn()
    const { update } = renderTree({ onFileOpen: first })
    await waitForTree()

    update({ onFileOpen: second })
    act(() => { treeMock.last().simulateSelection('README.md') })

    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledWith(`${ROOT}/README.md`)
  })

  it('stops listening on unmount', async () => {
    const { unmount } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    expect(model.subscriberCount()).toBe(1)

    unmount()

    expect(model.subscriberCount()).toBe(0)
    expect(model.calls.unsubscribes).toBe(1)
  })
})

describe('PierreWorkspaceTreeImpl — host selection echo', () => {
  it('focuses, selects and reveals the file the host has open', async () => {
    renderTree({ selectedPath: `${ROOT}/src/a/b.ts` })
    await waitForTree()
    const model = treeMock.last()

    expect(model.calls.focusPath).toEqual(['src/a/b.ts'])
    expect(model.calls.select).toEqual(['src/a/b.ts'])
    // A row under a collapsed ancestor is not on screen, so the highlight would
    // be invisible without expanding the chain root-down.
    expect(model.calls.expand).toEqual(['src', 'src/a'])
  })

  it('clears the previous row when the host switches files', async () => {
    const { update } = renderTree({ selectedPath: `${ROOT}/README.md` })
    await waitForTree()
    const model = treeMock.last()
    expect(model.getSelectedPaths()).toEqual(['README.md'])

    update({ selectedPath: `${ROOT}/src/a/b.ts` })

    expect(model.calls.deselect).toEqual(['README.md'])
    expect(model.getSelectedPaths()).toEqual(['src/a/b.ts'])
  })

  it('never re-reports the file the host already has open', async () => {
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen, selectedPath: `${ROOT}/README.md` })
    await waitForTree()
    const model = treeMock.last()

    // The echo above selects the row; clicking the already-open row lands here
    // too. Neither is a new open.
    act(() => { model.simulateSelection('README.md') })
    expect(onFileOpen).not.toHaveBeenCalled()

    act(() => { model.simulateSelection('src/a/b.ts') })
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`)
  })

  it('leaves the model alone when the open file is not in the rendered path set', async () => {
    // Changed mode renders only the git-status set, so the host's open file is
    // routinely absent — no ancestor rows exist to expand and no row to select.
    renderTree({ selectedPath: `${ROOT}/vendor/deep/x.ts` })
    await waitForTree()
    const model = treeMock.last()

    expect(model.calls.focusPath).toEqual(['vendor/deep/x.ts'])
    expect(model.calls.expand).toEqual([])
    expect(model.calls.select).toEqual([])
  })

  it('ignores a selectedPath that is not inside the project root', async () => {
    renderTree({ selectedPath: '/elsewhere/x.ts' })
    await waitForTree()

    expect(treeMock.last().calls.focusPath).toEqual([])
    expect(treeMock.last().calls.select).toEqual([])
  })

  it('ignores a null selectedPath', async () => {
    renderTree({ selectedPath: null })
    await waitForTree()

    expect(treeMock.last().calls.focusPath).toEqual([])
  })
})

describe('PierreWorkspaceTreeImpl — search forwarding', () => {
  it('forwards the rail search box into the tree search session and clears it when emptied', async () => {
    const { update } = renderTree()
    await waitForTree()

    update({ searchQuery: 'rail' })
    update({ searchQuery: '' })
    update({ searchQuery: null })

    // '' and null both mean "no search"; the model only accepts null for that.
    expect(treeMock.last().calls.search).toEqual([null, 'rail', null, null])
  })
})

describe('PierreWorkspaceTreeImpl — row context menu', () => {
  // Baseline composition the wrapper hands `useFileTree`: the `<FileTree>`
  // renderContextMenu wiring forces `enabled: true` on top of this, but the
  // trigger config here is what survives.
  it('enables both right-click and the hover button as context-menu triggers', async () => {
    renderTree()
    await waitForTree()
    expect(treeMock.last().options).toMatchObject({
      composition: { contextMenu: { triggerMode: 'both', buttonVisibility: 'when-needed' } },
    })
  })

  it('wires renderContextMenu only when a host is present to hand the row to', async () => {
    // `<FileTree>`'s own `renderContextMenu != null` check forces the menu
    // enabled unconditionally, so passing it with no `onAddToContext` would
    // open a menu whose only action closes itself and does nothing.
    const { update } = renderTree()
    await waitForTree()
    expect(treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBeUndefined()

    update({ onAddToContext: vi.fn() })
    expect(typeof treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBe('function')
  })

  const openMenu = (item: MenuItem) => {
    const close = vi.fn()
    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close,
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)
    return { close, ...render(<>{node}</>) }
  }

  it('offers a single Add to chat action on a file, wired to the absolute path', async () => {
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    const { close } = openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })

    // Row click already opens a file, so the menu carries no Open duplicate.
    expect(screen.queryByRole('menuitem', { name: 'Open' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('focuses the first item on open so the keyboard path can activate it', async () => {
    // Pierre focuses the tree ROW when it opens the menu, never this slotted
    // content, so without an explicit focus a Shift+F10 user would sit on the
    // row: the item's own Enter/Space handler could never fire.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })
    expect(menuitem).toHaveFocus()

    // And the focused item actually activates on Enter.
    fireEvent.keyDown(menuitem, { key: 'Enter' })
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
  })

  it('reports a directory as a dir add', async () => {
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    const { close } = openMenu({ kind: 'directory', name: 'a', path: 'src/a' })

    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a`, 'dir')
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('normalizes a native-Windows mixed-separator path to forward slashes', async () => {
    // On native Windows the tree root is backslash-separated while Pierre paths
    // are POSIX, so a raw join is `C:\repo\project/src/a/b.ts`. Left mixed, the
    // mention host's makeRelative cannot relativize it (absolute token +
    // duplicate attachment); the reported path must be forward-slash throughout.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ root: 'C:\\repo\\project' }))
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith('C:/repo/project/src/a/b.ts', 'file')
  })

  it('does not double a separator when the project root is a Windows drive root', async () => {
    // A drive-root project (`D:\`) normalizes to `D:/`, which ALREADY ends in
    // a separator. Left un-stripped, the join produces `D://item.path` -- a
    // double slash the mention host's makeRelative prefix-strip then consumes
    // only ONE of, leaving a stray leading `/` on the relativized result.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ root: 'D:\\' }))
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith('D:/src/a/b.ts', 'file')
  })

  it('leaves a POSIX filename containing a backslash untouched', async () => {
    // `\` is a legal character in a POSIX filename. Only a Windows-shaped ROOT
    // is separator-normalized; the row's own path must pass through verbatim,
    // or the staged mention would point at a nonexistent nested path.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'weird\\name.txt', path: 'src/weird\\name.txt' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/weird\\name.txt`, 'file')
  })
})

describe('PierreWorkspaceTreeImpl — row context menu keyboard contract (#6231)', () => {
  // The DEGENERATE case of the shared `role="menu"` contract: this menu hosts
  // exactly ONE menuitem, so every focus-move assertion is vacuously true —
  // "focus lands on the next item" and "focus does not move" are the same
  // observation. What the contract still owes a keyboard user is CONSUMPTION:
  // an arrow inside an open menu must not scroll the page behind it, and a Tab
  // must not drop the user out of a menu they were just told is open (#2533).
  // These tests therefore assert on `fireEvent`'s return value — false means
  // `preventDefault()` was called, i.e. the contract claimed the key — which is
  // the only signal that distinguishes wired from unwired on a one-item menu.
  const openMenu = (item: MenuItem) => {
    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close: vi.fn(),
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)
    return render(<>{node}</>)
  }

  /** Open the row menu and hand back its single item, focused (the
   *  component's own firstItemRef effect owns that focus entry). */
  const openSingleItemMenu = async () => {
    renderTree({ onAddToContext: vi.fn() })
    await waitForTree()
    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })
    expect(menuitem).toHaveFocus()
    return menuitem
  }

  it.each(['ArrowDown', 'ArrowUp', 'Home', 'End'])(
    'consumes %s rather than letting it scroll the page behind the open menu',
    async key => {
      const menuitem = await openSingleItemMenu()

      // false = preventDefault() was called: the menu contract claimed the key.
      expect(fireEvent.keyDown(menuitem, { key })).toBe(false)
      // One item, so navigation is a no-op — but it is a no-op that STAYS
      // inside the menu rather than moving focus nowhere useful.
      expect(menuitem).toHaveFocus()
    },
  )

  it.each([
    ['Tab', false],
    ['Shift+Tab', true],
  ] as const)('contains %s within the menu instead of dropping focus behind it', async (_label, shiftKey) => {
    const menuitem = await openSingleItemMenu()

    // The single item is simultaneously the first and the last item, so both
    // Tab directions sit on a containment boundary and must wrap onto it.
    expect(fireEvent.keyDown(menuitem, { key: 'Tab', shiftKey })).toBe(false)
    expect(menuitem).toHaveFocus()
  })

  it('leaves a key the menu contract does not own alone', async () => {
    // Regression PIN, not new behaviour: Enter still belongs to the item's own
    // activation handler, and the contract must not swallow it on the way.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()
    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })

    fireEvent.keyDown(menuitem, { key: 'Enter' })
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
  })
})
