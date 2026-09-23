/**
 * The chat side panel's file-browser rail.
 *
 * Pins the parts of the rail that no other suite owns: the All/Changed segment
 * (whose mode is MODULE-level session state, so it must survive a remount), the
 * always-open search field feeding the tree's search session, the Name/Content
 * toggle and the content-search results list, the refresh escape hatch, and the
 * grip's clamp + persistence. The Pierre tree is replaced by a probe that echoes
 * the props it was handed, so "what the rail tells the tree" is assertable
 * without loading the trees runtime, and `/api/file-grep` is stubbed so the
 * results list is asserted against a known payload rather than a live walk.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  render, screen, waitFor, fireEvent, within, cleanup, renderHook,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { consumeChatHandoff, recordError } from '../utils/errorReport'

const H = vi.hoisted(() => ({
  OPENED: '/repo/src/a.ts',
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
  fileGrep: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: H.api }))

vi.mock('../api/fileGrep', () => ({ fileGrep: H.fileGrep }))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: (p: {
    mode?: string
    projectDir: string
    searchQuery?: string | null
    selectedPath?: string | null
    onFileOpen?: (abs: string) => void
  }) => (
    <button
      data-testid="tree"
      data-mode={p.mode}
      data-dir={p.projectDir}
      data-query={p.searchQuery ?? ''}
      data-selected={p.selectedPath ?? ''}
      onClick={() => p.onFileOpen?.(H.OPENED)}
    >
      tree
    </button>
  ),
}))

import FileBrowserRail, { useTreeState } from '../pages/chat/FileBrowserRail'
import { ApiError } from '../api/apiError'

const RAIL_W_KEY = 'mc-files-rail-w'
const DIR = '/repo'

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function mount(props: { onFileOpen?: (p: string, d: boolean) => void; selectedPath?: string | null; projectDir?: string } = {}) {
  const qc = newClient()
  const onFileOpen = props.onFileOpen ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <FileBrowserRail projectDir={props.projectDir ?? DIR} onFileOpen={onFileOpen} selectedPath={props.selectedPath} />
    </QueryClientProvider>,
  )
  return { qc, onFileOpen, ...utils }
}

const rail = () => screen.getByRole('separator').nextElementSibling as HTMLElement
const tree = () => screen.getByTestId('tree')

beforeEach(() => {
  localStorage.clear()
  H.api.projectTree.mockReset().mockResolvedValue({ root: DIR, paths: [], repo: true })
  H.api.projectGitStatus.mockReset().mockResolvedValue({ repo: true, files: [] })
  H.fileGrep.mockReset().mockResolvedValue({
    results: [], truncated: false, engine: 'rg', skipped_docs: 0, root: DIR,
  })
  // Three pieces of the rail's state are MODULE-level so that in-place tab
  // navigation (which remounts the rail) keeps them — which also means a prior
  // test's toggle or typed filter survives into this one. Drive all three back
  // to their defaults on one mount, search MODE first: the field's label is
  // mode-dependent, so the filter cannot be found until Name is showing again.
  mount()
  fireEvent.click(screen.getByLabelText('Search file names'))
  fireEvent.click(screen.getByLabelText('All files'))
  fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: '' } })
  cleanup()
  H.api.projectTree.mockClear()
  H.api.projectGitStatus.mockClear()
  H.fileGrep.mockClear()
})

describe('FileBrowserRail mode segment', () => {
  it('starts in All files mode and runs the tree in all mode', () => {
    mount()
    expect(screen.getByLabelText('All files')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'false')
    expect(tree()).toHaveAttribute('data-mode', 'all')
    expect(tree()).toHaveAttribute('data-dir', DIR)
  })

  it('switches the tree to changed mode', () => {
    mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('All files')).toHaveAttribute('aria-pressed', 'false')
    expect(tree()).toHaveAttribute('data-mode', 'changed')
  })

  it('keeps Changed mode across a remount', () => {
    mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    cleanup()
    mount()
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
    expect(tree()).toHaveAttribute('data-mode', 'changed')
  })

  it('badges the Changed segment with the working-tree file count', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      files: [
        { path: 'a.ts', status: 'M', staged: false },
        { path: 'b.ts', status: 'M', staged: false },
        { path: 'c.ts', status: '?', staged: false },
      ],
    })
    mount()
    expect(await within(screen.getByLabelText('Changed')).findByText('3')).toBeInTheDocument()
  })

  it('omits the badge when the working tree is clean', async () => {
    mount()
    await waitFor(() => expect(H.api.projectGitStatus).toHaveBeenCalledWith(DIR))
    expect(within(screen.getByLabelText('Changed')).queryByText('0')).toBeNull()
  })

  // The listing is capped server-side at 500 and the response says so. Read as a
  // total it is a wrong number, not a rounded one: a repo with 900 changed files
  // reported "500". These two pin the floor marker in both directions, so the
  // badge cannot go back to stating the cap as the count.
  it('reads the badge as a floor when the status listing was capped', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      truncated: true,
      files: Array.from({ length: 500 }, (_, i) => ({ path: `f${i}.ts`, status: 'M', staged: false })),
    })
    mount()
    const badge = await screen.findByTestId('file-browser-rail-changed-count')
    expect(badge).toHaveTextContent('500+')
    // The qualifier has to reach a non-hovering reader too, or `500+` is the
    // only explanation of itself.
    expect(screen.getByLabelText('Changed').getAttribute('title')).toContain('500+ uncommitted')
  })

  it('leaves the badge a bare total when the listing was complete', async () => {
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      truncated: false,
      files: [
        { path: 'a.ts', status: 'M', staged: false },
        { path: 'b.ts', status: 'M', staged: false },
      ],
    })
    mount()
    const badge = await screen.findByTestId('file-browser-rail-changed-count')
    expect(badge).toHaveTextContent('2')
    expect(badge.textContent).not.toContain('+')
    expect(screen.getByLabelText('Changed').getAttribute('title')).toContain('2 uncommitted')
  })
})

describe('FileBrowserRail search field', () => {
  it('feeds the typed query to the tree and clears it from the X button', () => {
    mount()
    expect(screen.queryByLabelText('Close search')).toBeNull()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    expect(tree()).toHaveAttribute('data-query', 'rail')
    fireEvent.click(screen.getByLabelText('Close search'))
    expect(tree()).toHaveAttribute('data-query', '')
    expect(screen.queryByLabelText('Close search')).toBeNull()
  })

  it('clears the query on Escape', () => {
    mount()
    const input = screen.getByLabelText('Filter files…')
    fireEvent.change(input, { target: { value: 'rail' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')
  })

  it('leaves the query alone on any other key', () => {
    mount()
    const input = screen.getByLabelText('Filter files…')
    fireEvent.change(input, { target: { value: 'rail' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('rail')
  })

  it('keeps the typed query across a remount of the same project directory', () => {
    // In-place tab navigation (opening a file focuses its tab) remounts the
    // rail; the filter survives through the module-level session map, the
    // same way the All/Changed mode does.
    mount()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    cleanup()
    mount()
    expect(screen.getByLabelText('Filter files…')).toHaveValue('rail')
    expect(tree()).toHaveAttribute('data-query', 'rail')
  })

  it('rehydrates the query when the mounted rail switches project directory', () => {
    // An in-place projectDir change (switching chat slots) must not carry the
    // previous project's filter into the new one — the seed read happens only
    // on first mount, so the rail rehydrates from the session map on change.
    const qc = newClient()
    const view = render(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir={DIR} onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    view.rerender(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir="/elsewhere-switched" onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByLabelText('Filter files…')).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')

    // And switching back restores the first project's filter from the map.
    view.rerender(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir={DIR} onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    expect(screen.getByLabelText('Filter files…')).toHaveValue('rail')
  })

  it('starts a different project directory with an empty query', () => {
    mount()
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'rail' } })
    cleanup()
    mount({ projectDir: '/elsewhere' })
    expect(screen.getByLabelText('Filter files…')).toHaveValue('')
    expect(tree()).toHaveAttribute('data-query', '')
  })
})

describe('FileBrowserRail file opens', () => {
  it('opens a plain file in All mode and keeps the query', () => {
    // Deliberate contract: opening a file keeps the filter, so a second file
    // can be opened from the same filtered result without re-typing it.
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'a.ts' } })
    fireEvent.click(tree())
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, false)
    expect(tree()).toHaveAttribute('data-query', 'a.ts')
  })

  it('opens in diff mode from Changed mode', () => {
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    fireEvent.click(screen.getByLabelText('Changed'))
    fireEvent.click(tree())
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, true)
  })

  it('echoes the host selection into the tree', () => {
    mount({ selectedPath: '/repo/src/b.ts' })
    expect(tree()).toHaveAttribute('data-selected', '/repo/src/b.ts')
  })

  it('passes an empty selection when no file is open', () => {
    mount()
    expect(tree()).toHaveAttribute('data-selected', '')
  })
})

describe('FileBrowserRail refresh', () => {
  it('hands the agent the journaled server report, not the translated sentence', async () => {
    // The notice shows a composed line ("Couldn't load the file tree — Refresh to retry"), which is
    // NOT the journal key. The API layer journals the failure under the error's own message with
    // the endpoint, status and backend code; the hand-off has to carry those, or the agent starts
    // from a sentence that names nothing. Pinned on the PAYLOAD rather than on which lookup ran:
    // the notice now reads the error's own pinned report first and the journal second, and either
    // route has to land here.
    recordError({
      source: 'api',
      message: 'boom',
      status: 500,
      code: 'tree_walk_failed',
      endpoint: '/api/project/tree',
    })
    H.api.projectTree.mockRejectedValue(new ApiError(500, 'boom'))
    const { qc } = mount()
    await waitFor(() =>
      expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('error'))
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent("Couldn't load the file tree — Refresh to retry")

    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('/api/project/tree')
    expect(prompt).toContain('500')
    expect(prompt).toContain('tree_walk_failed')
    expect(prompt).not.toContain("Couldn't load the file tree")
  })

  it('leaves the tree notice to the header Refresh, with no adjacent button', async () => {
    // The rail Retry only ever called the header Refresh above it, which stays mounted and
    // enabled beside the notice, so it was one action spelled twice.
    const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
    H.api.projectTree.mockRejectedValue(timeout)
    const { qc } = mount()
    await waitFor(() =>
      expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('error'))
    expect(await screen.findByText("Couldn't load the file tree — Refresh to retry")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Retry/ })).not.toBeInTheDocument()

    const refresh = screen.getByLabelText('Refresh')
    expect(refresh).toBeEnabled()
    const spy = vi.spyOn(qc, 'refetchQueries')
    fireEvent.click(refresh)
    expect(spy.mock.calls.map(c => (c[0] as { queryKey: unknown[] }).queryKey)).toEqual([
      ['project-tree', DIR],
      ['git-status', DIR],
      // Content results are cached, so the escape hatch has to reach them too:
      // a refresh that left a stale hit list beside a fresh tree would present
      // two different answers to one query.
      ['file-grep', DIR],
    ])
  })

  it('refetches both polling queries and locks the button until they land', async () => {
    const { qc } = mount()
    const releases: Array<() => void> = []
    const spy = vi
      .spyOn(qc, 'refetchQueries')
      .mockImplementation((() =>
        new Promise<void>(res => { releases.push(res) })) as typeof qc.refetchQueries)

    const btn = screen.getByLabelText('Refresh')
    fireEvent.click(btn)

    expect(spy.mock.calls.map(c => (c[0] as { queryKey: unknown[] }).queryKey)).toEqual([
      ['project-tree', DIR],
      ['git-status', DIR],
      // Content results are cached, so the escape hatch has to reach them too:
      // a refresh that left a stale hit list beside a fresh tree would present
      // two different answers to one query.
      ['file-grep', DIR],
    ])
    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'true'))
    expect(btn.querySelector('svg')?.getAttribute('class')).toContain('animate-spin')

    releases.forEach(r => r())
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled'))
    expect(btn.querySelector('svg')?.getAttribute('class')).not.toContain('animate-spin')
  })

  it('keeps Refresh in the tab order while its refresh is in flight', async () => {
    // The tree notice names this button as the remedy for a failing read, so a keyboard user
    // presses it exactly when the wait is longest. `disabled` leaves the tab order mid-press
    // and drops focus to <body> for the whole bounded wait; inertness is `aria-disabled` plus
    // the in-handler guard, the way FolderPanel's header Refresh and WorkspacePicker's Retry
    // already spell it.
    const { qc } = mount()
    const releases: Array<() => void> = []
    vi.spyOn(qc, 'refetchQueries').mockImplementation((() =>
      new Promise<void>(res => { releases.push(res) })) as typeof qc.refetchQueries)

    const btn = screen.getByLabelText('Refresh')
    btn.focus()
    expect(btn).toHaveFocus()
    fireEvent.click(btn)

    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'true'))
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveFocus()
    // Still a tab stop: tabbing away and back mid-flight lands on it again, which a
    // `disabled` control refuses.
    btn.blur()
    expect(btn).not.toHaveFocus()
    btn.focus()
    expect(btn).toHaveFocus()

    releases.forEach(r => r())
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled'))
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveFocus()
  })

  it('ignores a second press while a refresh is in flight, and takes the next one after it lands', async () => {
    // Without `disabled` the press reaches the handler, so the handler has to refuse it: a
    // restart would cancel the round trip under way and let the first press's `finally`
    // clear the flag under the second's work. The guard is on the press's own bracket, so
    // it lifts with the settle -- a press after that starts a fresh round.
    const { qc } = mount()
    const releases: Array<() => void> = []
    const spy = vi
      .spyOn(qc, 'refetchQueries')
      .mockImplementation((() =>
        new Promise<void>(res => { releases.push(res) })) as typeof qc.refetchQueries)

    const btn = screen.getByLabelText('Refresh')
    fireEvent.click(btn)
    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'true'))
    expect(spy).toHaveBeenCalledTimes(3)

    fireEvent.click(btn)
    fireEvent.click(btn)
    await new Promise(r => setTimeout(r, 20))
    expect(spy).toHaveBeenCalledTimes(3)

    releases.splice(0).forEach(r => r())
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled'))

    fireEvent.click(btn)
    expect(spy).toHaveBeenCalledTimes(6)
    releases.splice(0).forEach(r => r())
    await waitFor(() => expect(btn).not.toHaveAttribute('aria-disabled'))
  })
})

describe('FileBrowserRail resize grip', () => {
  it('grows leftward, clamps to both bounds, and persists the release width', () => {
    mount()
    const grip = screen.getByRole('separator')
    expect(grip).toHaveAttribute('aria-orientation', 'vertical')
    // First run has nothing stored, so the rail opens at its own minimum —
    // never below it, which would snap wider on the first drag.
    expect(rail().style.width).toBe('300px')

    fireEvent.pointerDown(grip, { clientX: 500, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    expect(document.body.style.userSelect).toBe('none')

    // The grip sits on the rail's LEFT edge, so a leftward drag grows it.
    fireEvent.pointerMove(grip, { clientX: 400, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('400px')

    fireEvent.pointerMove(grip, { clientX: 900, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('300px')

    fireEvent.pointerMove(grip, { clientX: 100, clientY: 0, pointerId: 1 })
    expect(rail().style.width).toBe('520px')

    fireEvent.pointerUp(grip, { clientX: 100, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
    expect(localStorage.getItem(RAIL_W_KEY)).toBe('520')
  })

  it('restores the body styles when unmounted mid-drag', () => {
    const { unmount } = mount()
    fireEvent.pointerDown(screen.getByRole('separator'), { clientX: 500, clientY: 0, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    unmount()
    expect(document.body.style.cursor).toBe('')
    expect(document.body.style.userSelect).toBe('')
  })

  it.each([
    ['9999', '520px'],
    ['10', '300px'],
    ['360', '360px'],
    ['not-a-number', '300px'],
  ])('seeds the width from a stored %s as %s', (stored, expected) => {
    localStorage.setItem(RAIL_W_KEY, stored)
    mount()
    expect(rail().style.width).toBe(expected)
  })
})

  it.each([
    ['a deadline', 'TimeoutError'],
    ['any other failure', 'Error'],
  ])('names the TREE, and Refresh as its remedy, when the bounded tree read fails with %s', async (_label, name) => {
    // Without this the rejected read painted an empty tree, which a reader takes as
    // "this project has no files" rather than as a gateway that never answered.
    H.api.projectTree.mockReset().mockRejectedValue(
      Object.assign(new Error('boom'), { name }))
    mount()

    // One failed ['project-tree'] read, named the same way on every surface that shows it --
    // INCLUDING the remedy: the rail only mounts while the failure is recoverable, and its
    // header Refresh refetches this key, so the suffix FolderPanel's sibling arm carries is
    // as truthful here. Without it the icon-only Refresh above was the undiscoverable fix.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load the file tree — Refresh to retry")
    expect(alert).not.toHaveTextContent(/Folder listing/)
  })

describe('FileBrowserRail tree notice by mode', () => {
  const TREE_NOTICE = "Couldn't load the file tree — Refresh to retry"
  const treeFailed = (qc: QueryClient) =>
    waitFor(() => expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('error'))

  it('stays silent in Changed mode, whose list the status read populates, and returns with All', async () => {
    // Changed mode lists ['git-status'], not ['project-tree'] (PierreWorkspaceTreeImpl:
    // `ready = mode === 'changed' ? status != null : tree != null`), so a failed tree
    // read there painted a persistent failure banner above a fully populated list --
    // the same mismatch the status notice above it is already gated against.
    H.api.projectTree.mockRejectedValue(Object.assign(new Error('boom'), { name: 'TimeoutError' }))
    H.api.projectGitStatus.mockResolvedValue({
      repo: true,
      files: [{ path: 'a.ts', status: 'M', staged: false }, { path: 'b.ts', status: 'M', staged: false }],
    })
    const { qc } = mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    await treeFailed(qc)
    // The Changed list has its payload: the badge counts it.
    expect(await screen.findByTestId('file-browser-rail-changed-count')).toHaveTextContent('2')
    expect(tree()).toHaveAttribute('data-mode', 'changed')
    expect(screen.queryByText(TREE_NOTICE)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    // Back in the tree-backed mode the same failure IS the body's failure, so it shows.
    fireEvent.click(screen.getByLabelText('All files'))
    expect(await screen.findByText(TREE_NOTICE)).toBeInTheDocument()
  })

  it('stays silent in Content mode, whose body is the grep results, not the tree', async () => {
    H.api.projectTree.mockRejectedValue(Object.assign(new Error('boom'), { name: 'TimeoutError' }))
    const { qc } = mount()
    await treeFailed(qc)
    expect(await screen.findByText(TREE_NOTICE)).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    await waitFor(() => expect(screen.queryByText(TREE_NOTICE)).toBeNull())
    fireEvent.click(screen.getByLabelText('Search file names'))
    expect(await screen.findByText(TREE_NOTICE)).toBeInTheDocument()
  })

  it('shows the Refresh label while the notice names it, and only then', async () => {
    // The notice says "Refresh to retry"; the control it names was a bare icon, so the
    // reader scanned for a word that was not rendered. Same reveal FolderPanel's header
    // Refresh makes while its notices name it.
    H.api.projectTree.mockRejectedValue(Object.assign(new Error('boom'), { name: 'TimeoutError' }))
    const { qc } = mount()
    await treeFailed(qc)
    await screen.findByText(TREE_NOTICE)
    const refresh = screen.getByLabelText('Refresh')
    expect(within(refresh).getByText('Refresh')).toBeInTheDocument()

    // Where the notice is not shown, nothing names the control, so the label goes too.
    fireEvent.click(screen.getByLabelText('Changed'))
    await waitFor(() => expect(screen.queryByText(TREE_NOTICE)).toBeNull())
    expect(within(screen.getByLabelText('Refresh')).queryByText('Refresh')).toBeNull()
  })

  it('keeps the header Refresh a bare icon while the tree read succeeds', async () => {
    const { qc } = mount()
    await waitFor(() => expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(within(screen.getByLabelText('Refresh')).queryByText('Refresh')).toBeNull()
  })

  it('renders no tree body under the notice, and brings it back once the read recovers', async () => {
    // The tree's own loading skeleton kept shimmering under a notice that said the read
    // had failed: two claims about one read. While the notice speaks for the body, the
    // body is empty; the header Refresh refetches the same key, and a success restores it.
    H.api.projectTree.mockRejectedValue(Object.assign(new Error('boom'), { name: 'TimeoutError' }))
    const { qc } = mount()
    await treeFailed(qc)
    expect(await screen.findByText(TREE_NOTICE)).toBeInTheDocument()
    expect(screen.queryByTestId('tree')).toBeNull()

    H.api.projectTree.mockResolvedValue({ root: DIR, paths: ['a.ts'], repo: true })
    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(screen.queryByText(TREE_NOTICE)).toBeNull()
    expect(tree()).toHaveAttribute('data-mode', 'all')
  })

  it('keeps the loaded tree under the notice when a later refetch fails', async () => {
    // react-query keeps the last listing through a failed refetch (a poll, a window focus,
    // the header Refresh), so `isError && data` is a real state of this rail. Dropping the
    // body there swapped the list the user was browsing for nothing on ONE failed poll,
    // until a Refresh succeeded; the rows stay usable under the notice instead, the way the
    // folder tab's search keeps its last rows on the same failure.
    const { qc } = mount()
    await waitFor(() => expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(tree()).toHaveAttribute('data-mode', 'all')
    expect(screen.queryByText(TREE_NOTICE)).toBeNull()

    H.api.projectTree.mockRejectedValue(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
    await qc.refetchQueries({ queryKey: ['project-tree', DIR] })
    await treeFailed(qc)
    // The failed refetch left the earlier listing in place -- that is the state under test.
    expect(qc.getQueryData(['project-tree', DIR])).toEqual({ root: DIR, paths: [], repo: true })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(TREE_NOTICE)
    expect(tree()).toHaveAttribute('data-mode', 'all')
    // The notice sits above the rows it speaks for, not in place of them.
    expect(alert.compareDocumentPosition(tree()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(screen.getByLabelText('Refresh')).getByText('Refresh')).toBeInTheDocument()

    // A Refresh that recovers clears the notice and leaves the tree where it was.
    H.api.projectTree.mockResolvedValue({ root: DIR, paths: ['a.ts'], repo: true })
    fireEvent.click(screen.getByLabelText('Refresh'))
    await waitFor(() => expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(screen.queryByText(TREE_NOTICE)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(tree()).toHaveAttribute('data-mode', 'all')
    expect(within(screen.getByLabelText('Refresh')).queryByText('Refresh')).toBeNull()
  })
})

describe('FileBrowserRail tree notice by cause', () => {
  const wrapper = (qc: QueryClient) => ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('reports no-dir without probing when there is no project directory', () => {
    const { result } = renderHook(() => useTreeState(null), { wrapper: wrapper(newClient()) })
    expect(result.current).toBe('no-dir')
    expect(H.api.projectTree).not.toHaveBeenCalled()
  })

  it.each([
    ['a deadline', Object.assign(new Error('slow'), { name: 'TimeoutError' }),
      "Couldn't load the file tree", true],
    ['a codeless failure', new Error('boom'), "Couldn't load the file tree", true],
    ['a refusal', new ApiError(403, 'no', JSON.stringify({ code: 'access_denied' })),
      'No access to this folder', false],
    ['a missing root', new ApiError(403, 'no', JSON.stringify({ code: 'unknown_project_dir' })),
      'Folder not found', false],
  ])('renders %s in the rail with only its truthful remedy', async (_label, err, copy, retryable) => {
    H.api.projectTree.mockRejectedValue(err)
    const { qc } = mount()
    await waitFor(() =>
      expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('error'))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(copy)
    const refresh = screen.getByLabelText('Refresh')
    if (retryable) {
      expect(alert).toHaveTextContent(`${copy} — Refresh to retry`)
      expect(within(refresh).getByText('Refresh')).toBeInTheDocument()
    } else {
      expect(alert).not.toHaveTextContent('Refresh to retry')
      expect(within(refresh).queryByText('Refresh')).toBeNull()
    }
    // With no earlier data, the notice speaks for the failed body; no loading
    // skeleton or empty tree renders beneath it.
    expect(screen.queryByTestId('tree')).toBeNull()
  })
})

/** A payload with one text hit and one document hit — the two row shapes. */
const GREP_PAYLOAD = {
  results: [
    { file: '/repo/src/a.ts', line: 12, preview: 'const NEEDLE = 1' },
    { file: '/repo/docs/spec.pptx', line: 0, preview: 'the needle roadmap', label: 'slide 7' },
  ],
  truncated: false,
  engine: 'rg' as const,
  skipped_docs: 0,
  root: DIR,
}

/** Switch to Content mode and type *query*, then wait for the debounced fetch. */
async function searchContents(query: string) {
  fireEvent.click(screen.getByLabelText('Search file contents'))
  fireEvent.change(screen.getByLabelText('Search in files…'), { target: { value: query } })
  await waitFor(() => expect(H.fileGrep).toHaveBeenCalled())
}

describe('FileBrowserRail positionless hits', () => {
  it('shows no tag for a hit with no location, rather than a line that does not exist', async () => {
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/docs/spec.docx', line: 0, preview: 'the needle decision', label: null }],
    })
    mount()
    await searchContents('needle')
    const row = await screen.findByText('docs/spec.docx')
    // A document hit carries line 0, so a `:line` fallback would render `:0`.
    expect(row.parentElement?.textContent).not.toContain(':0')
  })

  it('still explains that a document opens from the start when no hit carries a tag', async () => {
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/docs/spec.docx', line: 0, preview: 'the needle decision', label: null }],
    })
    mount()
    await searchContents('needle')
    // Gating the note on a TAG existing dropped it for a Word-only result, which is
    // the result that most needs telling where the file opens.
    expect(await screen.findByTestId('file-grep-doc-note')).toBeInTheDocument()
  })
})

describe('HighlightedPreview case folding', () => {
  it('marks the right characters when lowercasing changes length', async () => {
    // `İ`.toLowerCase() is TWO characters (`i` + U+0307), so an index into a
    // whole-string toLowerCase() does not address the original text and the mark
    // slides. Turkish is the common case; the bug is any length-changing fold.
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/src/tr.ts', line: 3, preview: 'İstanbul needle here', label: null }],
    })
    mount()
    await searchContents('needle')
    const mark = await waitFor(() => {
      const el = document.querySelector('mark')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(mark.textContent).toBe('needle')
  })
})

describe('FileBrowserRail tree-mode segment', () => {
  it('hides All/Changed in Content mode, because it scopes a tree that is not there', async () => {
    mount()
    // Present in Name mode, where it has a tree to scope.
    expect(screen.getByRole('group', { name: 'File list mode' })).toBeInTheDocument()

    await searchContents('needle')
    // Gone in Content mode. Left rendered it kept its highlight while the content
    // results ignore it -- the rail would claim a scope it does not apply -- and
    // clicking it did nothing a reader could see.
    expect(screen.queryByRole('group', { name: 'File list mode' })).toBeNull()

    // And it comes back with the tree rather than being dropped for the session.
    fireEvent.click(screen.getByLabelText('Search file names'))
    expect(screen.getByRole('group', { name: 'File list mode' })).toBeInTheDocument()
  })

  it('keeps a Changed selection to return to, rather than silently resetting it', async () => {
    mount()
    fireEvent.click(screen.getByLabelText('Changed'))
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
    await searchContents('needle')
    fireEvent.click(screen.getByLabelText('Search file names'))
    // Hiding the control must not decide for the user what its value becomes.
    expect(screen.getByLabelText('Changed')).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps the previous hits on screen while a refined query is in flight', async () => {
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/src/first.ts', line: 3, preview: 'needle one', label: null }],
    })
    mount()
    await searchContents('needle')
    await screen.findByText('src/first.ts')

    // A refined query changes the query key. Without placeholderData the list
    // blanked to "Searching..." for the debounce plus up to the whole 2s budget,
    // so typing cleared the answer instead of narrowing it.
    let release: (v: unknown) => void = () => {}
    H.fileGrep.mockImplementation(() => new Promise(r => { release = r }))
    fireEvent.change(screen.getByLabelText('Search in files…'), { target: { value: 'needles' } })
    await waitFor(() => expect(H.fileGrep).toHaveBeenCalledTimes(2))
    expect(screen.getByText('src/first.ts')).toBeInTheDocument()

    release({ ...GREP_PAYLOAD, results: [] })
  })

  it('drops the previous project\'s hits when the project changes', async () => {
    // The other side of placeholderData. The query key carries projectDir, so
    // keeping the previous answer across EVERY key change also keeps it across a
    // project switch -- and each row opens by absolute path, so those rows stay
    // clickable and open a file from the project the user has just left.
    // Narrowing a query is worth smoothing; a different project's answer is not
    // an approximation of this one.
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/src/first.ts', line: 3, preview: 'needle one', label: null }],
    })
    const { qc, rerender } = mount()
    await searchContents('needle')
    await screen.findByText('src/first.ts')

    // The next project's answer is still in flight, which is exactly the window
    // the placeholder used to fill with the old project's rows.
    let release: (v: unknown) => void = () => {}
    H.fileGrep.mockImplementation(() => new Promise(r => { release = r }))
    rerender(
      <QueryClientProvider client={qc}>
        <FileBrowserRail projectDir="/other-repo" onFileOpen={vi.fn()} selectedPath={null} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(H.fileGrep).toHaveBeenCalledWith('/other-repo', 'needle'))
    expect(screen.queryByText('src/first.ts')).not.toBeInTheDocument()

    release({ ...GREP_PAYLOAD, results: [] })
  })
})

describe('FileBrowserRail search-mode toggle', () => {
  it('starts on Name, where the query filters the tree and no content search runs', () => {
    mount()
    expect(screen.getByLabelText('Search file names')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('Search file contents')).toHaveAttribute('aria-pressed', 'false')
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'needle' } })
    expect(tree()).toHaveAttribute('data-query', 'needle')
    expect(H.fileGrep).not.toHaveBeenCalled()
  })

  it('replaces the tree with content results in Content mode', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    // The tree is not merely hidden: the rail has one body, and a stale tree
    // beside a hit list would leave two competing answers to one query.
    await waitFor(() => expect(screen.queryByTestId('tree')).toBeNull())
  })

  it('keeps Content mode across a remount', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    cleanup()
    mount()
    expect(screen.getByLabelText('Search file contents')).toHaveAttribute('aria-pressed', 'true')
  })

  it('asks for nothing below the two-character floor', async () => {
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    fireEvent.change(screen.getByLabelText('Search in files…'), { target: { value: 'n' } })
    // The hint stands in for the results list, so an empty rail never reads as
    // "searched and found nothing".
    expect(screen.getByText(/at least two characters/i)).toBeInTheDocument()
    await new Promise(r => setTimeout(r, 320))
    expect(H.fileGrep).not.toHaveBeenCalled()
  })

  it('debounces to ONE request for a burst of keystrokes', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    fireEvent.click(screen.getByLabelText('Search file contents'))
    const field = screen.getByLabelText('Search in files…')
    for (const value of ['ne', 'nee', 'need', 'needl', 'needle']) {
      fireEvent.change(field, { target: { value } })
    }
    await waitFor(() => expect(H.fileGrep).toHaveBeenCalledTimes(1))
    expect(H.fileGrep.mock.calls[0][0]).toBe(DIR)
    expect(H.fileGrep.mock.calls[0][1]).toBe('needle')
  })
})

describe('FileBrowserRail content results', () => {
  it('shortens each path against the searched root and marks the match', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    expect(await screen.findByText('src/a.ts')).toBeInTheDocument()
    expect(screen.getByText('docs/spec.pptx')).toBeInTheDocument()
    // The file's own casing survives: the run is located case-insensitively but
    // sliced out of the original text.
    expect(rail().querySelector('mark')?.textContent).toBe('NEEDLE')
  })

  it('shows :line for a text hit and the location badge for a document hit', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    expect(await screen.findByText(':12')).toBeInTheDocument()
    // A .pptx has no line to open at, so the row names the slide instead.
    expect(screen.getByText('slide 7')).toBeInTheDocument()
  })

  it('explains a document hit in the visible note, never in a hover-only tooltip', async () => {
    // A hover-only title would restate the visible note plus the tag beside it, and
    // gating one on the tag existing leaves a .docx hit -- the one row with no
    // location to name -- with neither. One always-visible explanation beats a hover
    // absent from the row that needs it most, so no row carries a title.
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [
        { file: '/repo/docs/deck.pptx', line: 0, preview: 'the needle plan', label: 'slide 7' },
        { file: '/repo/docs/spec.docx', line: 0, preview: 'the needle decision', label: null },
      ],
    })
    mount()
    await searchContents('needle')
    await screen.findByText('slide 7')
    const titled = Array.from(rail().querySelectorAll('[title]')).map(n => n.getAttribute('title'))
    expect(titled.some(t => (t ?? '').includes('Opening shows the document'))).toBe(false)
    // ... and the visible note is still there to carry the explanation.
    expect(screen.getByTestId('file-grep-doc-note')).toBeInTheDocument()
  })

  it('opens a text hit at its line', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('src/a.ts'))
    expect(onFileOpen).toHaveBeenCalledWith('/repo/src/a.ts', false, { line: 12 })
  })

  it('opens a document hit without a line to reveal', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('docs/spec.pptx'))
    // Line 0 is not a line: asking the panel to reveal it would scroll to a
    // position the file does not have.
    expect(onFileOpen).toHaveBeenCalledWith('/repo/docs/spec.pptx', false, undefined)
  })

  it('reports the count, and keeps the engine name out of the line', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    const status = await waitFor(() => {
      const el = screen.getByTestId('file-grep-status')
      expect(el.textContent).toMatch(/2 result/i)
      return el
    })
    // An engine name in the visible line reads as a FILTER -- "python" as "the
    // search was narrowed to Python files", i.e. a deliberately incomplete list.
    // So the name appears nowhere, and the fast path carries no tooltip either:
    // ripgrep is the expectation, and a gloss on the normal case is noise.
    expect(status.textContent).not.toContain('rg')
    expect(status).not.toHaveAttribute('title')
  })

  it('names the unit, because every engine returns one row per file', async () => {
    // "8 results" beside eight single-line rows leaves it open whether a result is
    // a file or one spot inside a file, and under the second reading the list looks
    // like it is hiding the other matches in each file. Every engine returns at
    // most one row per file, so the row says so.
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    const status = await screen.findByTestId('file-grep-status')
    await waitFor(() => expect(status.textContent).toMatch(/one row per file/i))
  })

  it('does not describe a list it has not got', async () => {
    // Nothing found is not "one row per file"; the empty state speaks for itself.
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, results: [] })
    mount()
    await searchContents('needle')
    await screen.findByText('No results')
    expect(screen.getByTestId('file-grep-status').textContent).not.toMatch(/one row per file/i)
  })

  it('says the list is partial rather than presenting it as complete', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, truncated: true, engine: 'python' })
    mount()
    await searchContents('needle')
    const status = await waitFor(() => {
      const el = screen.getByTestId('file-grep-status')
      // The same word the sentence below the row uses: a reader should not have
      // to wonder whether "capped" and "partial" are two different problems.
      expect(el.textContent).toContain('partial')
      return el
    })
    // The remedy is visible and keyboard-readable. The slow path DOES explain
    // itself, but by consequence rather than by name: "python" told the user
    // nothing they could act on, where "no ripgrep, slower scan" does.
    expect(await screen.findByText(/Narrow the query/i)).toBeVisible()
    expect(status).toHaveAttribute('title', expect.stringMatching(/slower/i))
    expect(status.getAttribute('title')).not.toContain('python')
  })

  it('names BOTH modes in words, whichever is active', async () => {
    mount()
    // An icon pair was misread; an icon pair with only the active word was
    // still misread. Two plain words on their own row: nothing left to guess,
    // and the search field keeps its full width for the query.
    expect(screen.getByLabelText('Search file names')).toHaveTextContent('Name')
    expect(screen.getByLabelText('Search file contents')).toHaveTextContent('Content')
    fireEvent.click(screen.getByLabelText('Search file contents'))
    expect(screen.getByLabelText('Search file names')).toHaveTextContent('Name')
    expect(screen.getByLabelText('Search file contents')).toHaveTextContent('Content')
    // The toggle sits OUTSIDE the search field, on its own row.
    const field = screen.getByLabelText('Search in files…').parentElement as HTMLElement
    expect(within(field).queryByLabelText('Search file names')).toBeNull()
  })

  it('says visibly, once, that document hits open from the start', async () => {
    H.fileGrep.mockResolvedValue(GREP_PAYLOAD)
    mount()
    await searchContents('needle')
    // A `slide 7` badge reads as a jump target the click cannot honour. The
    // correction is visible text above the list -- not a hover tooltip, which
    // has no affordance and no keyboard path.
    await screen.findByText('slide 7')
    expect(screen.getByTestId('file-grep-doc-note')).toHaveTextContent(/opens from the start/)
  })

  it('opens a document hit from the start, not at a line', async () => {
    H.fileGrep.mockResolvedValue({
      ...GREP_PAYLOAD,
      results: [{ file: '/repo/docs/spec.pptx', line: 0, preview: 'needle on slide three', label: 'slide 3' }],
    })
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    await searchContents('needle')
    fireEvent.click(await screen.findByText('docs/spec.pptx'))
    // Jumping a hit to its slide is out of scope, so `slide 3` is a label and
    // nothing more: the reveal slot stays a LINE, one meaning per channel. A
    // document hit's line is 0, so no target is passed and the file opens.
    expect(onFileOpen).toHaveBeenCalledWith('/repo/docs/spec.pptx', false, undefined)
    // The badge still needs the note, because it still reads as a jump target.
    expect(screen.getByTestId('file-grep-doc-note')).toHaveTextContent(
      /opens from the start/,
    )
  })

  it('names the documents the budget did not reach', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, skipped_docs: 4 })
    mount()
    await searchContents('needle')
    await waitFor(() =>
      expect(screen.getByTestId('file-grep-status').textContent)
        .toContain('4 or more documents not searched'))
  })

  it('reads correctly for a single skipped document', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, skipped_docs: 1 })
    mount()
    await searchContents('needle')
    // "1 or more documents" is deliberately accepted. The count is a FLOOR, so
    // stating it as one is what makes it honest -- a bare "1 document" invites the
    // reader to reconcile it against the rows, which is the confusion this
    // wording exists to end. No ICU plural is introduced: one phrasing that reads
    // for every count beats plural forms nobody here can verify in twelve
    // languages.
    await waitFor(() =>
      expect(screen.getByTestId('file-grep-status').textContent)
        .toContain('1 or more documents not searched'))
  })

  it('reports an empty answer as no matches, not as an empty rail', async () => {
    H.fileGrep.mockResolvedValue({ ...GREP_PAYLOAD, results: [] })
    mount()
    await searchContents('needle')
    expect(await screen.findByText('No results')).toBeInTheDocument()
  })

  it('says the search failed instead of showing zero results', async () => {
    H.fileGrep.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    mount()
    await searchContents('needle')
    // A refused root and an empty tree are different facts, and the second is
    // the one a user would act on by editing their query.
    const notice = await screen.findByTestId('file-grep-error')
    // Not "Content search failed": that reuses the toggle's own word as the
    // subject, so it reads as "the Content button is broken".
    expect(notice).toHaveTextContent("Couldn't search file contents")
    // Through ErrorNotice, not a hand-written red line: that is the one surface
    // carrying the agent hand-off for an error the user cannot fix by retyping.
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByTestId('file-grep-status')).not.toHaveTextContent('failed')
  })

  it('offers the agent hand-off on a failure, and no result rows', async () => {
    H.fileGrep.mockRejectedValue(Object.assign(new Error('nope'), { status: 503 }))
    mount()
    await searchContents('needle')
    await screen.findByTestId('file-grep-error')
    // An exhausted probe pool is the agent's problem, not the user's, so the
    // list stays empty rather than showing a stale hit set beside the error.
    expect(screen.queryByText('src/a.ts')).toBeNull()
    expect(screen.queryByText('No results')).toBeNull()
  })

  it('hands the agent the journaled report, not the generic sentence', async () => {
    // The notice shows a human line ("Couldn't search file contents"), which is
    // NOT the journal key. The API layer journals the failure under the error's
    // own message with the endpoint, status and backend code; the hand-off has
    // to carry those, or the agent starts from a sentence that names nothing.
    recordError({
      source: 'api',
      message: 'nope',
      status: 403,
      code: 'sensitive_path',
      endpoint: '/api/file-grep',
    })
    H.fileGrep.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    mount()
    await searchContents('needle')
    const notice = await screen.findByTestId('file-grep-error')
    fireEvent.click(within(notice).getByRole('button', { name: /ask the agent/i }))
    const prompt = consumeChatHandoff()
    expect(prompt).toContain('/api/file-grep')
    expect(prompt).toContain('403')
    expect(prompt).toContain('sensitive_path')
  })
})
