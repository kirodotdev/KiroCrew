/**
 * PierreWorkspaceTreeImpl — what the tree reads while a host keeps it mounted but hidden.
 *
 * One topic of the wrapper suite whose conventions
 * `PierreWorkspaceTreeImpl.test.tsx` describes; the shared fixtures live in
 * `PierreWorkspaceTreeImplTestUtils.tsx`, and the `vi.mock` block is the
 * suite's own, word for word.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { focusManager } from '@tanstack/react-query'
import { refocusWindow } from './focusBarrier'

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

import { api } from '../api/client'
import { treeMock } from './__mocks__/pierreTreesReact'
import { ROOT, mkTree, mkStatus, renderTree, waitForTree, type TreePayload } from './PierreWorkspaceTreeImplTestUtils'
import { __resetTreeExpansionMemoryForTests, rememberExpandedPaths } from '../pierre/treeExpansionMemory'

/** A whole listing that cuts `big`, and `big`'s own listing once it is asked for. */
const cut = () => mkTree({ paths: ['README.md'], directories: ['big'], truncated: true, truncatedDirectories: ['big'] })
const bigListing = (): TreePayload => ({
  root: ROOT, dir: 'big', paths: ['big/late/leaf.ts'], directories: ['big/late'],
  repo: false, truncated: false, truncatedDirectories: [],
})

const treeReads = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1] === undefined).length
const folderReads = () => vi.mocked(api.projectTree).mock.calls.filter(c => c[1] === 'big').length
const statusReads = () => vi.mocked(api.projectGitStatus).mock.calls.length

beforeEach(() => {
  treeMock.reset()
  vi.mocked(api.projectTree).mockImplementation(async (_p, dir) => (dir === 'big' ? bigListing() : cut()))
  vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([]))
})

afterEach(() => {
  vi.useRealTimers()
  focusManager.setFocused(undefined)
  vi.clearAllMocks()
})

/** Mount active, expand the cut `big` so its own listing loads, and wait for all three reads. */
async function mountWithLoadedFolder() {
  const view = renderTree({ active: true })
  await waitForTree()
  act(() => {
    treeMock.last().simulateExpanded(['big'])
    treeMock.last().simulateVisibleRows([{ kind: 'directory', path: 'big/', isExpanded: true }])
  })
  await waitFor(() => expect(folderReads()).toBe(1))
  await waitFor(() => expect(statusReads()).toBe(1))
  return view
}

describe('PierreWorkspaceTreeImpl — reads under `active`', () => {
  it('reads nothing when mounted inactive, even on a window focus', async () => {
    const { qc } = renderTree({ active: false })
    await refocusWindow(qc)
    expect(api.projectTree).not.toHaveBeenCalled()
    expect(api.projectGitStatus).not.toHaveBeenCalled()
  })

  it('refetches on a window focus while active', async () => {
    const { qc } = await mountWithLoadedFolder()
    const [trees, statuses] = [treeReads(), statusReads()]
    await refocusWindow(qc)
    await waitFor(() => expect(treeReads()).toBeGreaterThan(trees))
    await waitFor(() => expect(statusReads()).toBeGreaterThan(statuses))
  })

  it('stops the tree, status and folder reads on a window focus once inactive', async () => {
    const { qc, update } = await mountWithLoadedFolder()
    const before = [treeReads(), statusReads(), folderReads()]
    update({ active: false })
    await refocusWindow(qc)
    expect([treeReads(), statusReads(), folderReads()]).toEqual(before)
  })

  it('stops the polls while inactive, and reads nothing on a focus once all of it is stale', async () => {
    const { qc, update } = await mountWithLoadedFolder()
    // After the render settles: `waitFor` never advances under fake timers. The
    // polls armed before this run on the real clock, so a hide and show re-arms
    // them on the fake one.
    vi.useFakeTimers()
    update({ active: false })
    update({ active: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    // The control: an active tree polls the listing every 10 s and the status every 5 s.
    const firstReads = treeReads()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_500) })
    expect(treeReads()).toBeGreaterThan(firstReads)
    update({ active: false })
    const before = [treeReads(), statusReads(), folderReads()]
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect([treeReads(), statusReads(), folderReads()]).toEqual(before)
    // Past every staleTime, the folder listing's 10 s included: an enabled
    // observer would refetch each of these on this focus. Still on the fake
    // clock, which is what the stale times were measured against.
    await refocusWindow(qc, { fakeTimers: true })
    expect([treeReads(), statusReads(), folderReads()]).toEqual(before)
  })

  it('re-reads the tree, its loaded folder and the status when it becomes active again', async () => {
    const { update } = await mountWithLoadedFolder()
    const before = [treeReads(), statusReads(), folderReads()]
    update({ active: false })
    update({ active: true })
    // A fresh whole listing re-reads the open loaded folders with it.
    await waitFor(() => expect(treeReads()).toBeGreaterThan(before[0]))
    await waitFor(() => expect(statusReads()).toBeGreaterThan(before[1]))
    await waitFor(() => expect(folderReads()).toBeGreaterThan(before[2]))
  })

  it('reads the new project, not the old one, when the project changed while hidden', async () => {
    const OTHER = '/other-project'
    vi.mocked(api.projectTree).mockImplementation(async p => mkTree({ root: p }))
    const { update } = renderTree({ active: true })
    await waitForTree()
    update({ active: false, projectDir: OTHER })
    expect(vi.mocked(api.projectTree).mock.calls.some(c => c[0] === OTHER)).toBe(false)
    update({ active: true, projectDir: OTHER })
    await waitFor(() => expect(api.projectTree).toHaveBeenCalledWith(OTHER))
    await waitFor(() => expect(api.projectGitStatus).toHaveBeenCalledWith(OTHER))
  })

  it('leaves the status unread on return outside a git repository', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ repo: false }))
    const { update } = renderTree({ active: true })
    await waitForTree()
    update({ active: false })
    update({ active: true })
    await waitFor(() => expect(treeReads()).toBe(2))
    expect(api.projectGitStatus).not.toHaveBeenCalled()
  })
})

describe('PierreWorkspaceTreeImpl — expansion on return under `active`', () => {
  // Kept mounted while hidden, the tree skips its remembered-expansion restore on
  // return (the payload is unchanged), while another tree of the project may have
  // changed that memory meanwhile.
  const PATHS = ['src/a.ts', 'src/lib/b.ts', 'docs/x.md']

  beforeEach(() => {
    __resetTreeExpansionMemoryForTests()
    localStorage.clear()
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: PATHS }))
  })

  it('matches the remembered expansion when shown again, moving only folders that differ', async () => {
    const { update } = renderTree({ persistExpansion: true, active: true })
    await waitForTree()
    const model = treeMock.last()
    act(() => { model.simulateExpanded(['src']) })
    update({ persistExpansion: true, active: false })
    // Another tree of this project opens `docs` and closes `src` meanwhile.
    rememberExpandedPaths(ROOT, ['docs'])
    update({ persistExpansion: true, active: true })
    expect(model.calls.expand).toEqual(['docs'])
    expect(model.calls.collapse).toEqual(['src'])
  })

  it('moves nothing when the memory still matches the tree', async () => {
    const { update } = renderTree({ persistExpansion: true, active: true })
    await waitForTree()
    const model = treeMock.last()
    act(() => { model.simulateExpanded(['src']) })
    rememberExpandedPaths(ROOT, ['src'])
    update({ persistExpansion: true, active: false })
    update({ persistExpansion: true, active: true })
    expect(model.calls.expand).toEqual([])
    expect(model.calls.collapse).toEqual([])
  })

  it('leaves a tree that does not remember expansion alone', async () => {
    const { update } = renderTree({ active: true })
    await waitForTree()
    const model = treeMock.last()
    act(() => { model.simulateExpanded(['src']) })
    update({ active: false })
    rememberExpandedPaths(ROOT, ['docs'])
    update({ active: true })
    expect(model.calls.expand).toEqual([])
    expect(model.calls.collapse).toEqual([])
  })
})

describe('PierreWorkspaceTreeImpl — a reveal while hidden', () => {
  it('holds a reveal received while inactive and applies it once shown again', async () => {
    const onRevealed = vi.fn()
    const { update } = renderTree({ active: true, onRevealed })
    await waitForTree()
    const model = treeMock.last()
    update({ active: false, onRevealed })
    // `big` already has a row, so only the gate keeps it from moving now.
    update({ active: false, onRevealed, reveal: { path: 'big', nonce: 1 } })
    expect(onRevealed).toHaveBeenCalledTimes(1)
    expect(model.calls.focusPath).toEqual([])
    expect(model.calls.select).toEqual([])
    expect(folderReads()).toBe(0)
    update({ active: true, onRevealed, reveal: null })
    expect(model.calls.focusPath).toEqual(['big/'])
    expect(model.calls.select).toEqual(['big'])
    await waitFor(() => expect(folderReads()).toBe(1))
  })
})
