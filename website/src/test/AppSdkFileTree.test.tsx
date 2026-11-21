/**
 * The App SDK's `FileTree` reaches only what the hosting app declares.
 *
 * An external app's tree is backed by its scoped transport: an undeclared tree
 * read is a refusal with no request, a declared one lists the project through
 * the app's own transport, and the git status read is its own declaration. A
 * builtin page keeps the dashboard's client, and a surface with no app identity
 * fails closed. An external tree's row menu carries none of the dashboard's file
 * actions, whose requests sit outside the declared reads.
 *
 * The tree sits behind `pierre/tree.tsx`'s lazy `import()`, so the first render
 * of anything it draws waits on that chunk (LAZY_TREE).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))
const mockApi = vi.hoisted(() => ({
  projectTree: vi.fn(),
  projectGitStatus: vi.fn(),
  // The contributed-row seam reads `['apps']` as a cache subscriber and never fetches.
  listApps: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { AppApiProvider, FileTree, type AppApi } from '../app-sdk/index'
import { AppIdentityProvider } from '../app-sdk/identity'
import { appQueryClient } from '../app-sdk/appQueryClient'
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import { FILE_TREE_PATHS, scopedWorkspaceTreeClient } from '../app-sdk/FileTree'
import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { WorkspaceTreeClientProvider } from '../pierre/workspaceTreeClient'
import { treeMock, type MenuContext, type MenuItem } from './__mocks__/pierreTreesReact'

const ROOT = '/repo/project'
const TREE = { root: ROOT, paths: ['README.md', 'src/a.ts'], directories: [], repo: true }
const STATUS = { repo: true, repoRoot: '/repo', files: [] }

// The wait for the tree's lazy chunk to load and commit (docs/testing.md).
const LAZY_TREE = { timeout: 5000 }

let fetchMock: ReturnType<typeof vi.fn>
let treeStatus = 200
let statusStatus = 200
let treeBody: object = TREE
let appSeq = 0

function pathOf(call: unknown[]): string {
  return new URL(String(call[0]), 'http://localhost').pathname
}

beforeEach(() => {
  treeMock.reset()
  for (const fn of Object.values(mockApi)) fn.mockReset()
  mockApi.projectTree.mockResolvedValue(TREE)
  mockApi.projectGitStatus.mockResolvedValue(STATUS)
  treeStatus = 200
  statusStatus = 200
  treeBody = TREE
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname
    if (path === FILE_TREE_PATHS.tree && treeStatus !== 200) {
      return new Response(JSON.stringify({ error: 'boom' }), { status: treeStatus })
    }
    if (path === FILE_TREE_PATHS.gitStatus && statusStatus !== 200) {
      return new Response(JSON.stringify({ error: 'refused', code: 'git_status_failed' }), { status: statusStatus })
    }
    const body = path === FILE_TREE_PATHS.tree ? treeBody : path === FILE_TREE_PATHS.gitStatus ? STATUS : {}
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  // One app id per mount: an external app's query client is held per id.
  appSeq += 1
  return (
    <AppApiProvider appName={`file-tree-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

function openMenu(item: MenuItem) {
  const context: MenuContext = {
    anchorElement: document.createElement('div'),
    anchorRect: document.createElement('div').getBoundingClientRect(),
    close: vi.fn(),
    restoreFocus: vi.fn(),
  }
  return render(<>{treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)}</>)
}

const README: MenuItem = { kind: 'file', name: 'README.md', path: 'README.md' }

/** An installed app contributing a 'tree-context' row, as the `['apps']` cache holds it. */
const CONTRIBUTING_APPS = [{
  name: 'doc-store',
  enabled: true,
  manifest: { contributes: { fileMenuItems: [{ id: 'send', label: 'Send to store', icon: 'Package', endpoint: '/api/apps/doc-store/send', surfaces: ['tree-context'] }] } },
}]

describe('scopedWorkspaceTreeClient', () => {
  const fakeApi = () => ({ get: vi.fn(async () => ({ ...TREE, lazyDirectories: ['src'] })) }) as unknown as AppApi & { get: ReturnType<typeof vi.fn> }

  it('is null without the tree read, whatever else is declared', () => {
    const api = fakeApi()
    expect(scopedWorkspaceTreeClient(api, [])).toBeNull()
    expect(scopedWorkspaceTreeClient(api, ['/api/apps/my-app/*'])).toBeNull()
    expect(scopedWorkspaceTreeClient(api, [FILE_TREE_PATHS.gitStatus, '/api/project/session-files'])).toBeNull()
    expect(scopedWorkspaceTreeClient(api, ['/api/project/tre'])).toBeNull()
  })

  it('is a client under each declaration that covers the read, and sends nothing yet', () => {
    const api = fakeApi()
    for (const declared of [[FILE_TREE_PATHS.tree], ['=' + FILE_TREE_PATHS.tree], ['/api/project/*'], ['/api/project']]) {
      expect(scopedWorkspaceTreeClient(api, declared), declared.join()).not.toBeNull()
    }
    expect(api.get).not.toHaveBeenCalled()
  })

  it('reads git status only when that path is declared too', () => {
    const api = fakeApi()
    expect(scopedWorkspaceTreeClient(api, [FILE_TREE_PATHS.tree])!.gitStatus).toBeNull()
    expect(scopedWorkspaceTreeClient(api, [FILE_TREE_PATHS.tree, FILE_TREE_PATHS.gitStatus])!.gitStatus).not.toBeNull()
    expect(scopedWorkspaceTreeClient(api, ['/api/project'])!.gitStatus).not.toBeNull()
  })

  it('sends the requests the dashboard client sends, and offers no host file actions', async () => {
    const api = fakeApi()
    const c = scopedWorkspaceTreeClient(api, ['/api/project'])!
    expect(c.hostFileActions).toBe(false)
    const listing = await c.tree(ROOT)
    await c.tree(ROOT, 'src/a b')
    await c.gitStatus!(ROOT)
    expect(api.get.mock.calls.map(call => call[0])).toEqual([
      `/api/project/tree?shallow=1&path=${encodeURIComponent(ROOT)}`,
      `/api/project/tree?shallow=1&path=${encodeURIComponent(ROOT)}&dir=${encodeURIComponent('src/a b')}`,
      `/api/project/git/status?path=${encodeURIComponent(ROOT)}`,
    ])
    // A shallow read's unread folders join the set the tree lists with no state row.
    expect(listing.nestedRepoDirectories).toEqual(['src'])
  })
})

describe('the App SDK FileTree', () => {
  it('refuses an external app that declares no tree read, and sends nothing', async () => {
    render(externalApp(['/api/apps/file-tree-app', FILE_TREE_PATHS.gitStatus], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('app-file-tree-not-declared')).toHaveTextContent(FILE_TREE_PATHS.tree)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.projectTree).not.toHaveBeenCalled()
  })

  it('fails closed with no app identity and no SDK provider', async () => {
    const client = new QueryClient()
    render(<QueryClientProvider client={client}><FileTree projectDir={ROOT} /></QueryClientProvider>)
    expect(await screen.findByTestId('app-file-tree-not-declared')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.projectTree).not.toHaveBeenCalled()
  })

  it('fails closed with no app identity even under a scoped provider that declares both reads', async () => {
    const client = new QueryClient()
    render(
      <QueryClientProvider client={client}>
        <AppScopedApiProvider appName="scoped-no-identity" allowedApiPaths={[FILE_TREE_PATHS.tree, FILE_TREE_PATHS.gitStatus]} navigateFn={() => {}}>
          <FileTree projectDir={ROOT} />
        </AppScopedApiProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('app-file-tree-not-declared')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.projectTree).not.toHaveBeenCalled()
  })

  it('lists the project through the app transport and hands an opened file to onOpenFile', async () => {
    const onOpenFile = vi.fn()
    render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} onOpenFile={onOpenFile} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.at(-1)).toEqual(TREE.paths))
    expect(fetchMock.mock.calls.map(pathOf)).toContain(FILE_TREE_PATHS.tree)
    // Undeclared, the status read is never sent; the rows are drawn uncoloured.
    expect(fetchMock.mock.calls.map(pathOf)).not.toContain(FILE_TREE_PATHS.gitStatus)
    expect(Object.values(mockApi).every(fn => fn.mock.calls.length === 0)).toBe(true)
    act(() => { treeMock.last().simulateSelection('README.md') })
    expect(onOpenFile).toHaveBeenCalledWith(`${ROOT}/README.md`)
  })

  it('opens a file once when a listing refresh reports the same selection again', async () => {
    const onOpenFile = vi.fn()
    render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} onOpenFile={onOpenFile} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    // A click on a filtered row notifies twice before React re-renders (the
    // click also closes the filter), so both land in one act.
    act(() => {
      treeMock.last().simulateSelection('README.md')
      treeMock.last().simulateSelection('README.md')
    })
    expect(onOpenFile).toHaveBeenCalledTimes(1)
    // A later refresh that keeps README.md selected notifies it once more.
    act(() => { treeMock.last().simulateSelection('README.md') })
    expect(onOpenFile).toHaveBeenCalledTimes(1)
    // Another file is still a new open, and so is the first one after it.
    act(() => { treeMock.last().simulateSelection('src/a.ts') })
    act(() => { treeMock.last().simulateSelection('README.md') })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`, `${ROOT}/src/a.ts`, `${ROOT}/README.md`])
  })

  it('reopens a file once the page has moved its selectedPath elsewhere', async () => {
    const onOpenFile = vi.fn()
    // One app id across the rerenders, so the same tree stays mounted.
    const page = (selected: string | null) => (
      <AppApiProvider appName="file-tree-reopen-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} />
      </AppApiProvider>
    )
    const view = render(page(null))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    act(() => { treeMock.last().simulateSelection('README.md') })
    expect(onOpenFile).toHaveBeenCalledTimes(1)
    // The page closes the file itself; picking it again is a new open.
    view.rerender(page(`${ROOT}/src/a.ts`))
    view.rerender(page(null))
    act(() => { treeMock.last().simulateSelection('README.md') })
    expect(onOpenFile).toHaveBeenCalledTimes(2)
  })

  it('does not reopen a file the page closed when a listing refresh keeps its row', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null) => (
      <AppApiProvider appName="file-tree-close-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} />
      </AppApiProvider>
    )
    const view = render(page(null))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    const model = treeMock.last()
    act(() => { model.simulateSelection('README.md') })
    view.rerender(page(`${ROOT}/README.md`))
    // The page closes the file; the tree keeps the row selected, as the Files rail does.
    view.rerender(page(null))
    // A refresh notifies with the kept selection: README.md is still the opened file.
    act(() => { model.simulateVisibleRows([]) })
    expect(onOpenFile).toHaveBeenCalledTimes(1)
    // Documented limit: re-picking the file the page just closed is no change.
    act(() => { model.simulateSelection('README.md') })
    expect(onOpenFile).toHaveBeenCalledTimes(1)
    // Another file is; and after it, README.md is again.
    act(() => { model.simulateSelection('src/a.ts') })
    act(() => { model.simulateSelection('README.md') })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`, `${ROOT}/src/a.ts`, `${ROOT}/README.md`])
  })

  it('does not reopen a closed file when the same render clears the filter', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null, query: string | null) => (
      <AppApiProvider appName="file-tree-close-filter-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} searchQuery={query} />
      </AppApiProvider>
    )
    const view = render(page(null, 'READ'))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    act(() => { treeMock.last().simulateSelection('README.md') })
    view.rerender(page(`${ROOT}/README.md`, 'READ'))
    // The tree's search change notifies while the row is still selected.
    const model = treeMock.last()
    const setSearch = model.setSearch.bind(model)
    model.setSearch = (value: string | null) => { setSearch(value); model.simulateVisibleRows([]) }
    view.rerender(page(null, null))
    expect(onOpenFile).toHaveBeenCalledTimes(1)
  })

  it('does not reopen the previous file when the page switches files in a render that also notifies', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null, query: string | null) => (
      <AppApiProvider appName="file-tree-switch-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} searchQuery={query} />
      </AppApiProvider>
    )
    const view = render(page(null, 'R'))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    act(() => { treeMock.last().simulateSelection('README.md') })
    view.rerender(page(`${ROOT}/README.md`, 'R'))
    const model = treeMock.last()
    const setSearch = model.setSearch.bind(model)
    model.setSearch = (value: string | null) => { setSearch(value); model.simulateVisibleRows([]) }
    // The page shows another file itself; an earlier effect notifies while README.md is still selected.
    view.rerender(page(`${ROOT}/src/a.ts`, null))
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`])
    expect(model.getSelectedPaths()).toEqual(['src/a.ts'])
    // The user can open README.md again.
    act(() => { model.simulateSelection('README.md') })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`, `${ROOT}/README.md`])
  })

  it('reports no open when the page itself leaves a file and returns to it', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null) => (
      <AppApiProvider appName="file-tree-return-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} />
      </AppApiProvider>
    )
    const view = render(page(`${ROOT}/README.md`))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    const model = treeMock.last()
    await waitFor(() => expect(model.getSelectedPaths()).toEqual(['README.md']))
    // Out to a file outside the project and back: the page's own moves, no user open.
    view.rerender(page('/elsewhere/notes.md'))
    act(() => { model.simulateVisibleRows([]) })
    view.rerender(page(`${ROOT}/README.md`))
    act(() => { model.simulateVisibleRows([]) })
    expect(model.getSelectedPaths()).toEqual(['README.md'])
    // Re-clicking the file the page shows is no change, so no open.
    act(() => { model.simulateSelection('README.md') })
    expect(onOpenFile).not.toHaveBeenCalled()
  })

  it('opens no other selected row when the page closes its file', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null) => (
      <AppApiProvider appName="file-tree-multi-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} />
      </AppApiProvider>
    )
    const view = render(page(null))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    const model = treeMock.last()
    act(() => { model.simulateSelection('README.md') })
    view.rerender(page(`${ROOT}/README.md`))
    // Ctrl/Cmd-click adds src/a.ts: two rows selected, so no open.
    act(() => { model.simulateSelection('src/a.ts', ['README.md', 'src/a.ts']) })
    view.rerender(page(null))
    act(() => { model.simulateVisibleRows([]) })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`])
    // Documented limit: a refresh that leaves one file of the multi-selection
    // (README.md deleted) is reported as that file's selection, so it is an open,
    // as the Files rail opens it. Picking src/a.ts alone is the same report.
    act(() => { model.simulateSelection('src/a.ts') })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`, `${ROOT}/src/a.ts`])
  })

  it('opens no row the tree leaves selected while it reconciles a page-driven switch', async () => {
    const onOpenFile = vi.fn()
    const page = (selected: string | null) => (
      <AppApiProvider appName="file-tree-reconcile-app" allowedApiPaths={[FILE_TREE_PATHS.tree]} navigateFn={() => {}}>
        <FileTree projectDir={ROOT} onOpenFile={onOpenFile} selectedPath={selected} />
      </AppApiProvider>
    )
    const view = render(page(null))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    const model = treeMock.last()
    act(() => { model.simulateSelection('README.md') })
    view.rerender(page(`${ROOT}/README.md`))
    // Ctrl/Cmd-click adds src/a.ts: two rows selected, so no open.
    act(() => { model.simulateSelection('src/a.ts', ['README.md', 'src/a.ts']) })
    // As the real model does, each deselect notifies, the first leaving src/a.ts alone.
    const getItem = model.getItem.bind(model)
    model.getItem = (path: string) => {
      const item = getItem(path)
      if (!item) return item
      return { ...item, deselect: () => { item.deselect(); model.simulateVisibleRows([]) } }
    }
    // As the real model does, focusing a path it does not hold keeps the focus where it is.
    const focusPath = model.focusPath.bind(model)
    model.focusPath = (path: string) => { if (getItem(path)) focusPath(path) }
    // The page shows a file whose folder is not loaded: the tree drops README.md's selection.
    view.rerender(page(`${ROOT}/sub/B.md`))
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`])
    // A user open afterwards still fires.
    act(() => { model.simulateSelection('src/a.ts') })
    expect(onOpenFile.mock.calls.map(c => c[0])).toEqual([`${ROOT}/README.md`, `${ROOT}/src/a.ts`])
  })

  it('sends no tree read while the host has the page hidden', async () => {
    const client = new QueryClient()
    const hidden = (active: boolean) => (
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="hidden-tree-app" origin="external">
          <AppScopedApiProvider appName="hidden-tree-app" allowedApiPaths={[FILE_TREE_PATHS.tree, FILE_TREE_PATHS.gitStatus]} navigateFn={() => {}} active={active}>
            <FileTree projectDir={ROOT} />
          </AppScopedApiProvider>
        </AppIdentityProvider>
      </QueryClientProvider>
    )
    const view = render(hidden(false))
    // Declared, so no refusal; hidden, so nothing is sent. The mount's effects
    // (where a query would start its fetch) have run once render's act returns.
    expect(screen.queryByTestId('app-file-tree-not-declared')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
    // Shown again, the same mount reads: the gate, not timing, held it back.
    view.rerender(hidden(true))
    await waitFor(() => expect(fetchMock.mock.calls.map(pathOf)).toContain(FILE_TREE_PATHS.tree))
  })

  it('starts a fresh tree when the page switches project, so no folder state carries over', async () => {
    const view = render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    const first = treeMock.last()
    // Same app (same query client) on another project whose listing is already
    // cached, so the tree has no loading pause that would remount it anyway.
    appQueryClient(`file-tree-app-${appSeq}`).setQueryData(['project-tree', '/repo/other'], { ...TREE, root: '/repo/other' })
    appSeq -= 1
    view.rerender(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir="/repo/other" />))
    await waitFor(() => expect(treeMock.last()).not.toBe(first))
  })

  it('reads git status through the app transport once that path is declared', async () => {
    render(externalApp([FILE_TREE_PATHS.tree, FILE_TREE_PATHS.gitStatus], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.map(pathOf)).toContain(FILE_TREE_PATHS.gitStatus))
    expect(mockApi.projectGitStatus).not.toHaveBeenCalled()
  })

  it('says why the changed-file colours are missing when the declared status read fails', async () => {
    statusStatus = 503
    render(externalApp([FILE_TREE_PATHS.tree, FILE_TREE_PATHS.gitStatus], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    // The tree stays usable; the notice sits above it.
    expect(await screen.findByTestId('app-file-tree-status-error', {}, { timeout: 15000 })).toBeInTheDocument()
    expect(screen.getByTestId('file-tree')).toBeInTheDocument()
  }, 20000)

  it('draws no dashboard file action on an external tree row, contributed rows included', async () => {
    // Even with a contributing app in the cache the tree reads, the row offers nothing.
    appQueryClient(`file-tree-app-${appSeq + 1}`).setQueryData(['apps'], CONTRIBUTING_APPS)
    render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    // No row can hold an action, so the tree wires no menu at all: no "..." on a
    // row, and no empty menu state for a keyboard user to be stranded in.
    expect(treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBeUndefined()
  })

  it('keeps Download and contributed rows off a menu a client without host file actions wires', async () => {
    // The tree client's contract, reached here through a host row handler.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['apps'], CONTRIBUTING_APPS)
    const client = { tree: vi.fn(async () => TREE), gitStatus: null, hostFileActions: false }
    render(
      <QueryClientProvider client={qc}>
        <WorkspaceTreeClientProvider client={client}>
          <PierreWorkspaceTreeImpl projectDir={ROOT} onAddToContext={() => {}} />
        </WorkspaceTreeClientProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(treeMock.last().calls.resetPaths.length).toBeGreaterThan(0))
    expect(client.tree).toHaveBeenCalledWith(ROOT)
    openMenu(README)
    expect(screen.getByText('Add to chat')).toBeInTheDocument()
    expect(screen.queryByText(/Download/)).toBeNull()
    expect(screen.queryByText('Send to store')).toBeNull()
    expect(Object.values(mockApi).every(fn => fn.mock.calls.length === 0)).toBe(true)
  })

  it("offers no agent hand-off from the shared tree's own notices on an external app's page", async () => {
    // The hand-off navigates to the chat and would unmount the app page with its
    // unsaved state. A builtin page keeps it (the last assertion is the control).
    const unreadable = { ...TREE, directories: ['vault'], unreadableDirectories: ['vault'] }
    treeBody = unreadable
    const { unmount } = render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('workspace-tree-unreadable-notice', {}, LAZY_TREE)).toHaveTextContent('vault')
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    unmount()

    mockApi.projectTree.mockResolvedValue(unreadable)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="files-app" origin="builtin"><FileTree projectDir={ROOT} /></AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('workspace-tree-unreadable-notice', {}, LAZY_TREE)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('names a failed read with a retry that re-reads through the app transport', async () => {
    treeStatus = 500
    render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('app-file-tree-error')).toBeInTheDocument()
    const reads = () => fetchMock.mock.calls.map(pathOf).filter(p => p === FILE_TREE_PATHS.tree).length
    expect(reads()).toBe(1)
    treeStatus = 200
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    expect(reads()).toBeGreaterThanOrEqual(2)
  })

  it('keeps a listed tree mounted under an inline notice when a refresh fails', async () => {
    render(externalApp([FILE_TREE_PATHS.tree], <FileTree projectDir={ROOT} />))
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    const listed = treeMock.last()
    await waitFor(() => expect(listed.calls.resetPaths.at(-1)).toEqual(TREE.paths))
    // The next read of the listed tree fails (a poll, a focus refetch, a gateway restart).
    treeStatus = 500
    await act(async () => { await appQueryClient(`file-tree-app-${appSeq}`).refetchQueries({ queryKey: ['project-tree', ROOT], exact: true }) })
    expect(await screen.findByTestId('app-file-tree-error')).toBeInTheDocument()
    // Same mount, same model: its loaded folders and selection are kept.
    expect(screen.getByTestId('file-tree')).toBeInTheDocument()
    expect(treeMock.last()).toBe(listed)
  })

  it('keeps the dashboard client and its row actions for a builtin page', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], CONTRIBUTING_APPS)
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="files-app" origin="builtin"><FileTree projectDir={ROOT} /></AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('file-tree', {}, LAZY_TREE)).toBeInTheDocument()
    expect(mockApi.projectTree).toHaveBeenCalledWith(ROOT)
    await waitFor(() => expect(mockApi.projectGitStatus).toHaveBeenCalledWith(ROOT))
    expect(fetchMock).not.toHaveBeenCalled()
    openMenu(README)
    expect(screen.getByText(/Download/)).toBeInTheDocument()
    expect(screen.getByText('Send to store')).toBeInTheDocument()
  })
})
