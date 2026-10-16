/**
 * PierreWorkspaceTreeImpl — the host wiring: a selected row reported out as an
 * open, the host's open file echoed back in, and the rail search forwarded.
 *
 * One topic of the wrapper suite whose conventions
 * `PierreWorkspaceTreeImpl.test.tsx` describes; the shared fixtures live in
 * `PierreWorkspaceTreeImplTestUtils.tsx`, and the `vi.mock` block is the
 * suite's own, word for word.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor, act } from '@testing-library/react'

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
import type { MenuItem, VisibleRow } from './__mocks__/pierreTreesReact'
import {
  ROOT,
  M,
  mkTree,
  mkStatus,
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

  it('feeds only the state rows of the folders the filter matches, and all of them back when it clears', async () => {
    // A state row is a status line, not a name: typing "empty" must not list
    // every empty folder's row as a result, so a row is never fed on a match of
    // its label alone. But Pierre force-expands every folder the query matches
    // (hide-non-matches re-applies that on every store event, so the folder
    // cannot be kept closed), and a childless folder open over nothing is a
    // down-chevron with no row beneath it -- the shape the row exists to
    // prevent. So the row is fed exactly when its folder matches; a truncated
    // childless folder that does not match keeps its badge, since no row says it.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['empty', 'big'],
      truncatedDirectories: ['big'],
    }))
    const { update } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    const bigRow = `big/Some items not shown: limit of 10,000 items reached${M}`
    expect(model.calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/', 'big/', `empty/Empty folder${M}`, bigRow])
    const decorate = model.options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => { text: string } | null
    const big = { kind: 'directory', name: 'big', path: 'big' } as const

    // "emp" matches the folder `empty/`: its row rides along; `big/` does not match.
    update({ searchQuery: 'emp' })
    expect(model.calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/', 'big/', `empty/Empty folder${M}`])
    expect(model.calls.search.at(-1)).toBe('emp')
    expect(decorate({ item: big, row: { ...big, isExpanded: true } })).toMatchObject({ text: 'some items not shown' })

    // "folder" matches every label and no folder: no row is fed.
    update({ searchQuery: 'folder' })
    expect(model.calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/', 'big/'])

    // "big" matches the truncated folder: its row is fed and the badge yields
    // to it while the folder is open, as outside a filter.
    update({ searchQuery: 'big' })
    expect(model.calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/', 'big/', bigRow])
    expect(decorate({ item: big, row: { ...big, isExpanded: true } })).toBeNull()
    expect(decorate({ item: big, row: { ...big, isExpanded: false } })).toMatchObject({ text: 'some items not shown' })

    update({ searchQuery: null })
    expect(model.calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/', 'big/', `empty/Empty folder${M}`, bigRow])
    expect(model.calls.search.at(-1)).toBeNull()
  })

  it('moves the search focus off a state row onto its folder, so the first result is never a status line', async () => {
    // Pierre focuses the first match when a query is set, and a fed state row
    // sorts right under its folder -- so for "emp" the first match is the
    // `empty/Empty folder` line. The focus ring on an inert status line reads
    // as a selectable result; the wrapper hands the focus to the folder the
    // row qualifies. A focused real row is left alone.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['empty'],
    }))
    const { update } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    expect(model.calls.focusPath).toEqual([])

    // Pierre's search session lands the focus on the row (the stand-in only
    // records `setSearch`, so the focus is installed the way the tree would).
    model.simulateSelection(`empty/Empty folder${M}`, [])
    update({ searchQuery: 'emp' })
    expect(model.calls.focusPath).toEqual(['empty/'])

    model.simulateSelection('README.md', [])
    update({ searchQuery: 'read' })
    expect(model.calls.focusPath).toEqual(['empty/'])
  })

  it('carries the expansion across the filter toggle in a host that does not persist it', async () => {
    // Filter on and filter off are each a path reset. Without an
    // `initialExpandedPaths` the reset collapses the tree -- and Pierre's own
    // filter snapshot, taken right after, would then restore a collapsed tree
    // as the pre-filter state. A payload change is not a filter toggle and
    // keeps the host's plain reset.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['notes/a.md', 'src/b.ts'],
      directories: ['notes', 'src', 'empty'],
    }))
    const { update, qc } = renderTree()
    await waitForTree()
    const model = treeMock.last()
    expect(model.calls.resetPathsOptions.at(-1)).toBeUndefined()

    model.simulateExpanded(['notes'])
    update({ searchQuery: 'a' })
    expect(model.calls.resetPathsOptions.at(-1)).toEqual({ initialExpandedPaths: ['notes'] })

    model.simulateExpanded(['notes', 'src'])
    update({ searchQuery: null })
    expect(model.calls.resetPathsOptions.at(-1)).toEqual({ initialExpandedPaths: ['notes', 'src'] })

    // A poll that changes the path set with no filter toggle: plain reset, as before.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['notes/a.md', 'src/b.ts', 'src/c.ts'],
      directories: ['notes', 'src', 'empty'],
    }))
    const before = model.calls.resetPaths.length
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
    await waitFor(() => expect(model.calls.resetPaths.length).toBe(before + 1))
    expect(model.calls.resetPathsOptions.at(-1)).toBeUndefined()
  })
})
