/**
 * PierreWorkspaceTreeImpl — scroll position memory (`rememberScroll`).
 *
 * The pinned Files tab unmounts whenever another panel tab is active, which
 * destroys the tree's scroller. `rememberScroll` keeps the top visible row
 * while mounted and scrolls back to it on the next mount (see
 * `../pierre/treeScrollAnchor`). Runs against the recording fake in
 * `./__mocks__/pierreTreesReact`: these tests pin the wrapper's policy (what
 * is recorded, when, and what is restored), not the library's scrolling.
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
    listApps: vi.fn(),
    invokeFileMenuItem: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { __recallTreeScrollAnchorForTests, __resetTreeScrollAnchorsForTests } from '../pierre/treeScrollAnchor'
import { api } from '../api/client'
import { treeMock, ITEM_HEIGHT } from './__mocks__/pierreTreesReact'
import type { VisibleRow } from './__mocks__/pierreTreesReact'

const ROOT = '/repo/project'
const FILES = Array.from({ length: 30 }, (_, i) => `src/f${String(i).padStart(2, '0')}.ts`)
const ROWS: VisibleRow[] = [
  { kind: 'directory', path: 'src/', isExpanded: true },
  ...FILES.map(path => ({ kind: 'file' as const, path, isExpanded: false })),
]
/** A scrollTop inside row 10, which sits at 10 * ITEM_HEIGHT. */
const ROW_10_TOP = 10 * ITEM_HEIGHT + 5

type Props = Parameters<typeof PierreWorkspaceTreeImpl>[0]

function renderTree(props: Partial<Props> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, structuralSharing: false } } })
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

const waitForTree = () => waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())

/** Mount with `rows` visible from the first projection, as the real model has
 *  them right after its first `resetPaths`, scroll as the user, unmount. */
async function leaveScrolledAt(scrollTop: number, props: Partial<Props> = { rememberScroll: true }, rows = ROWS) {
  treeMock.initialVisibleRows = rows
  const { unmount } = renderTree(props)
  await waitForTree()
  act(() => { treeMock.last().simulateScroll(scrollTop) })
  unmount()
}

/** Remount with `rows` already visible, as the restored projection is. */
async function comeBack(props: Partial<Props> = { rememberScroll: true }, rows = ROWS) {
  treeMock.initialVisibleRows = rows
  const view = renderTree(props)
  await waitForTree()
  return { ...view, calls: treeMock.last().calls.scrollToPath }
}

/** Animation frames run only when a test flushes them, so the restore's
 *  resend after the view measures happens at a point the test chooses. */
let frames = new Map<number, FrameRequestCallback>()
let nextFrame = 1
const flushFrames = () => act(() => {
  for (let pass = 0; pass < 4 && frames.size > 0; pass++) {
    const due = frames
    frames = new Map()
    for (const cb of due.values()) cb(0)
  }
})

/** ResizeObservers the hook creates; `measure` delivers a size to each. */
let observers: ResizeObserverCallback[] = []
const measure = (height: number) => act(() => {
  for (const cb of observers) {
    cb([{ contentRect: { height } } as unknown as ResizeObserverEntry], {} as ResizeObserver)
  }
})

beforeEach(() => {
  observers = []
  vi.stubGlobal('ResizeObserver', class {
    constructor(cb: ResizeObserverCallback) { observers.push(cb) }
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  frames = new Map()
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const id = nextFrame++
    frames.set(id, cb)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { frames.delete(id) })
  treeMock.reset()
  __resetTreeScrollAnchorsForTests()
  vi.mocked(api.projectTree).mockResolvedValue({ root: ROOT, paths: FILES, repo: true })
  vi.mocked(api.projectGitStatus).mockResolvedValue({ repo: true, repoRoot: '/repo', files: [] })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('PierreWorkspaceTreeImpl — scroll position memory', () => {
  it('scrolls back to the top visible row on a remount', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[10].path)

    const { calls } = await comeBack()
    expect(calls).toEqual([{ path: ROWS[10].path, options: { focus: false, offset: 'top' } }])
  })

  it('sends the restore again once the view has measured, unless the user scrolled first', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const settled = await comeBack()
    expect(settled.calls.map(c => c.path)).toEqual([ROWS[10].path])
    flushFrames()
    // Nothing is sent again before the view has a measured size.
    expect(settled.calls.map(c => c.path)).toEqual([ROWS[10].path])
    measure(0)
    flushFrames()
    expect(settled.calls.map(c => c.path)).toEqual([ROWS[10].path])
    measure(300)
    flushFrames()
    expect(settled.calls.map(c => c.path)).toEqual([ROWS[10].path, ROWS[10].path])
    settled.unmount()

    const interrupted = await comeBack()
    act(() => { treeMock.last().simulateScroll(ITEM_HEIGHT * 4) })
    measure(300)
    flushFrames()
    expect(interrupted.calls.map(c => c.path)).toEqual([ROWS[10].path])
  })

  it('restores only once per mount', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const { calls, update } = await comeBack()
    update({ rememberScroll: true, searchQuery: 'f1' })
    update({ rememberScroll: true, searchQuery: null })
    expect(calls).toHaveLength(1)
  })

  it('records a scroll the tree made itself, once the restore is decided', async () => {
    // A clamp when rows shrink, or focus brought into view, leaves the tree
    // where the user sees it: that is where the next visit should start.
    treeMock.initialVisibleRows = ROWS
    const { unmount } = renderTree({ rememberScroll: true })
    await waitForTree()
    unmount()
    const { unmount: unmountAgain } = await comeBack()
    act(() => { treeMock.last().simulateScroll(ROW_10_TOP, { byUser: false }) })
    unmountAgain()
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[10].path)
  })

  it('records nothing before the restore is decided', async () => {
    // A fresh mount's scroller sits at the top until the restore lands; a
    // scroll seen then must not erase the anchor.
    await leaveScrolledAt(ROW_10_TOP)
    const { update } = renderTree({ rememberScroll: true, scrollRestorePaused: true })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows(ROWS) })
    act(() => { treeMock.last().simulateScroll(0) })
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[10].path)
    update({ scrollRestorePaused: false })
    expect(treeMock.last().calls.scrollToPath.map(c => c.path)).toEqual([ROWS[10].path])
  })

  it('forgets the anchor when the tree is scrolled back to the top', async () => {
    treeMock.initialVisibleRows = ROWS
    const { unmount } = renderTree({ rememberScroll: true })
    await waitForTree()
    act(() => { treeMock.last().simulateScroll(ROW_10_TOP) })
    act(() => { treeMock.last().simulateScroll(0) })
    unmount()

    expect((await comeBack()).calls).toEqual([])
  })

  it('does not capture while a filter is active, and holds the restore until it closes', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const { unmount } = renderTree({ rememberScroll: true, searchQuery: 'f2' })
    await waitForTree()
    act(() => { treeMock.last().simulateVisibleRows(ROWS.slice(20)) })
    act(() => { treeMock.last().simulateScroll(ITEM_HEIGHT * 3) })
    expect(treeMock.last().calls.scrollToPath).toEqual([])
    unmount()
    // The filtered scroll did not replace the anchor.
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[10].path)

    const { calls, update } = await comeBack({ rememberScroll: true, searchQuery: 'f2' })
    expect(calls).toEqual([])
    update({ searchQuery: null })
    expect(calls.map(c => c.path)).toEqual([ROWS[10].path])
  })

  it('does not capture a hidden tree, and holds the restore while paused', async () => {
    treeMock.initialVisibleRows = ROWS
    const { unmount } = renderTree({ rememberScroll: true })
    await waitForTree()
    act(() => { treeMock.last().simulateScroll(ROW_10_TOP, { clientHeight: 0 }) })
    unmount()
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBeNull()

    await leaveScrolledAt(ROW_10_TOP)
    const { calls, update } = await comeBack({ rememberScroll: true, scrollRestorePaused: true })
    expect(calls).toEqual([])
    update({ scrollRestorePaused: false })
    expect(calls.map(c => c.path)).toEqual([ROWS[10].path])
  })

  it('falls back to the nearest visible ancestor when the anchor row is gone', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const collapsed: VisibleRow[] = [{ kind: 'directory', path: 'src/', isExpanded: false }]
    expect((await comeBack({ rememberScroll: true }, collapsed)).calls.map(c => c.path)).toEqual(['src/'])
  })

  it('moves on to the exact row when a later payload brings it, and keeps the anchor meanwhile', async () => {
    // A folder past the listing's row cap has its rows only once its own
    // listing lands, after the first projection the restore runs against.
    await leaveScrolledAt(ROW_10_TOP)
    const cut: VisibleRow[] = [{ kind: 'directory', path: 'src/', isExpanded: true }]
    const { qc, calls } = await comeBack({ rememberScroll: true }, cut)
    expect(calls.map(c => c.path)).toEqual(['src/'])
    // The ancestor's own scroll is not where the user left the tree.
    act(() => { treeMock.last().simulateScroll(ITEM_HEIGHT * 2, { byUser: false }) })
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[10].path)

    act(() => { treeMock.last().simulateVisibleRows(ROWS) })
    vi.mocked(api.projectTree).mockResolvedValue({ root: ROOT, paths: [...FILES, 'src/new.ts'], repo: true })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['project-tree', ROOT] }) })
    await waitFor(() => expect(calls.map(c => c.path)).toEqual(['src/', ROWS[10].path]))
  })

  it('stops waiting for the exact row once the user takes the tree over', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const cut: VisibleRow[] = [{ kind: 'directory', path: 'src/', isExpanded: true }]
    const { qc, calls } = await comeBack({ rememberScroll: true }, cut)
    act(() => { treeMock.last().simulateVisibleRows(ROWS) })
    // The user scrolls: that position is now the one to keep.
    act(() => { treeMock.last().simulateScroll(ITEM_HEIGHT * 4) })
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBe(ROWS[4].path)

    vi.mocked(api.projectTree).mockResolvedValue({ root: ROOT, paths: [...FILES, 'src/new.ts'], repo: true })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['project-tree', ROOT] }) })
    await waitFor(() => expect(treeMock.last().calls.resetPaths).toHaveLength(2))
    expect(calls.map(c => c.path)).toEqual(['src/'])
  })

  it('does nothing when neither the anchor nor an ancestor is visible', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    const other: VisibleRow[] = [{ kind: 'file', path: 'README.md', isExpanded: false }]
    expect((await comeBack({ rememberScroll: true }, other)).calls).toEqual([])
  })

  it('keeps each project its own anchor', async () => {
    await leaveScrolledAt(ROW_10_TOP)
    expect((await comeBack({ rememberScroll: true, projectDir: '/repo/other' })).calls).toEqual([])
  })

  it('is inert without the opt-in and in changed mode', async () => {
    await leaveScrolledAt(ROW_10_TOP, {})
    expect(__recallTreeScrollAnchorForTests(ROOT, 'all')).toBeNull()
    expect((await comeBack({})).calls).toEqual([])

    vi.mocked(api.projectGitStatus).mockResolvedValue({
      repo: true, repoRoot: '/repo', files: [{ path: 'project/src/f10.ts', status: 'M', staged: false }],
    })
    await leaveScrolledAt(ROW_10_TOP, { rememberScroll: true, mode: 'changed' })
    expect(__recallTreeScrollAnchorForTests(ROOT, 'changed')).toBeNull()
  })
})
