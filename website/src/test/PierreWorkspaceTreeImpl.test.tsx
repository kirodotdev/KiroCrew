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
 *
 * This file holds data loading and the state rows. The git status lanes and
 * Changed mode, the host wiring, the row context menu and folder expansion each
 * have a `PierreWorkspaceTreeImpl.<topic>.test.tsx` sibling, and the fixtures
 * they share live in `PierreWorkspaceTreeImplTestUtils.tsx`.
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

import { breakAtSlashes } from '../pierre/PierreWorkspaceTreeImpl'
import { STATE_ROW_DECORATION, STATE_ROW_ICON } from '../pierre/treeStateRows'
import { api } from '../api/client'
import { __resetTreeUnreadableDismissalsForTests, recallDismissedUnreadable, rememberDismissedUnreadable } from '../pierre/treeUnreadableDismissals'
import { MOVE_UNDO_MS } from '../components/MoveUndoBar'
import { treeMock } from './__mocks__/pierreTreesReact'
import type { MenuItem, MenuContext, VisibleRow } from './__mocks__/pierreTreesReact'
import {
  ROOT,
  PATHS,
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

describe('PierreWorkspaceTreeImpl — data loading', () => {
  it('stops polling the tree while its read is failing, and polls again once it answers', async () => {
    // `api.projectTree` runs under a deadline, so a wedged gateway turns every poll into a
    // rejection ~15s in; an un-gated 10s interval re-walked the server for as long as the
    // tree was mounted. The host's Refresh refetches this key and is the recovery path.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      vi.mocked(api.projectTree).mockRejectedValue(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
      const { qc } = renderTree()
      await waitFor(() => expect(qc.getQueryState(['project-tree', ROOT])?.status).toBe('error'))
      expect(api.projectTree).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(25_000) })
      expect(api.projectTree).toHaveBeenCalledTimes(1)

      vi.mocked(api.projectTree).mockResolvedValue(mkTree())
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      expect(api.projectTree).toHaveBeenCalledTimes(2)
      await act(async () => { await vi.advanceTimersByTimeAsync(10_500) })
      expect(api.projectTree).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

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

  it('drops a file row that shadows a directory of the same name (post-redaction collision)', async () => {
    // Egress redaction can flatten two distinct segments to one string, so a
    // FILE and an implied DIRECTORY end up sharing a name in the same parent.
    // @pierre/trees throws 'Path collides with an existing entry' on that; the
    // directory must win so its subtree still renders.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['a/[REDACTED: credential]', 'a/[REDACTED: credential]/x.txt', 'a/[REDACTED: credential]'],
    }))

    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([['a/[REDACTED: credential]/x.txt']])
  })

  it('keeps an explicit directory row over a same-named file', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['secrets', 'README.md'],
      directories: ['secrets/'],
    }))

    renderTree()
    await waitForTree()

    // A childless directory also gets its state row appended after the
    // listing; only the listing itself is under test here.
    const [fed] = treeMock.last().calls.resetPaths
    expect(fed.slice(0, 2)).toEqual(['README.md', 'secrets/'])
    expect(fed).not.toContain('secrets')
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

  it('keeps the truncation badge of a folder folded into a chain row', async () => {
    // `flattenEmptyDirectories` folds `p` -- left holding only `a` once the cap
    // fell after `p/a` -- into one `p/a` row whose path names its LAST folder.
    // The truncated `p` is asked through the row's segments, not lost.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['p', 'p/a'],
      truncatedDirectories: ['p'],
      truncated: true,
    }))
    renderTree()
    await waitForTree()
    const decorate = treeMock.last().options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => { text: string; title?: string } | null
    const chain = { kind: 'directory', name: 'p/a', path: 'p/a/' } as const
    const segments = [
      { isTerminal: false, name: 'p', path: 'p/' },
      { isTerminal: true, name: 'a', path: 'p/a/' },
    ]
    const badge = { text: 'some items not shown', title: 'some items not shown' }
    // `p/a` itself is childless and carries the empty state row, which speaks
    // for `a` only: the badge is `p`'s and stays whether the chain is open or not.
    for (const isExpanded of [false, true]) {
      expect(decorate({
        item: chain,
        row: { ...chain, isExpanded, isFlattened: true, flattenedSegments: segments } as unknown as VisibleRow,
      })).toEqual(badge)
    }
    // An unfolded row is asked about itself alone.
    expect(decorate({ item: chain, row: { ...chain, isExpanded: false } })).toBeNull()
  })

  it('names the limit, not an empty workspace, when a truncated payload has no row', async () => {
    // The server stopped reading before it reached anything it lists -- a root
    // whose first entries are all hidden folders, say. Nothing is known empty.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: [],
      directories: [],
      repo: false,
      truncated: true,
      truncatedDirectories: [''],
    }))
    renderTree()
    const notice = await screen.findByTestId('workspace-tree-root-truncated')
    expect(notice).toHaveTextContent('Workspace too large to list: limit of 10,000 items reached')
    expect(screen.queryByText('No files in this workspace yet')).not.toBeInTheDocument()
  })

  it("points the empty capped tree at the search box above it", async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: [],
      directories: [],
      repo: false,
      truncated: true,
      truncatedDirectories: [''],
    }))
    renderTree()
    await screen.findByTestId('workspace-tree-root-truncated')
    expect(screen.getByTestId('workspace-tree-root-truncated-hint'))
      .toHaveTextContent('Type a file name in the box above to find it.')
  })

  it('keeps explicit directory rows and marks directories whose files were sampled', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['alpha/a.ts'],
      directories: ['alpha', 'late', 'late/nested'],
      truncatedDirectories: ['late'],
      truncated: true,
    }))
    renderTree()
    await waitForTree()

    // `late/nested` holds nothing in this payload, so it carries the state row
    // every childless folder gets (see the state-row block below).
    expect(treeMock.last().calls.resetPaths).toEqual([
      ['alpha/a.ts', 'alpha/', 'late/', 'late/nested/', `late/nested/Empty folder${M}`],
    ])
    const decorate = treeMock.last().options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => { text: string; title?: string } | null
    // `late` still has `nested` beneath it, so its badge stays whether or not
    // it is expanded: no state row of its own says the same thing.
    const late = { kind: 'directory', name: 'late', path: 'late' } as const
    expect(decorate({ item: late, row: { ...late, isExpanded: false } })).toEqual({
      text: 'some items not shown',
      title: 'some items not shown',
    })
    expect(decorate({ item: late, row: { ...late, isExpanded: true } })).toEqual({
      text: 'some items not shown',
      title: 'some items not shown',
    })
    const alpha = { kind: 'directory', name: 'alpha', path: 'alpha' } as const
    expect(decorate({ item: alpha, row: { ...alpha, isExpanded: true } })).toBeNull()
    expect(screen.getByText(/limit of 10,000 items reached/i)).toBeInTheDocument()
  })

  it('reports an empty workspace instead of an empty tree', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: [] }))
    renderTree()

    await waitFor(() => expect(screen.getByText('No files in this workspace yet')).toBeInTheDocument())
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tree-root-unreadable')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tree-root-hidden-only')).not.toBeInTheDocument()
  })

  it('says which folder could not be read, with Refresh and the agent hand-off, instead of calling it empty', async () => {
    // The server names the project root as `.` when its own `scandir` failed:
    // the listing is empty because nothing READ it. One level down the state
    // row sits under the folder it qualifies; here no folder row exists, so
    // the notice names the folder itself and carries the same two actions as
    // the host's failed-listing notice: Refresh re-asks the listing, the
    // hand-off gives the agent the sentence with the path in it.
    const errorReport = await import('../utils/errorReport')
    errorReport.__resetErrorJournalForTests()
    errorReport.__resetNavSeamForTests()
    sessionStorage.clear()
    errorReport.installSoftNavigate(() => {})
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: [],
      directories: [],
      repo: false,
      unreadableDirectories: ['.'],
    }))
    try {
      renderTree()

      const state = await screen.findByTestId('workspace-tree-root-unreadable')
      const notice = within(state).getByRole('alert')
      // The sentence leads and the path is a span of its own; the path carries
      // a break hint after each `/` so a narrow notice wraps it at a segment
      // boundary, never mid-word. Hints are display only (stripped here).
      expect(notice).toHaveTextContent('No permission to read the workspace folder')
      const pathSpan = notice.querySelector('.font-mono')
      expect(pathSpan?.textContent).toBe(breakAtSlashes(ROOT))
      expect(pathSpan?.textContent?.replace(/\u200b/g, '')).toBe(ROOT)
      expect(breakAtSlashes('/a/b')).toBe('/\u200ba/\u200bb')
      // A Windows root keeps `\` separators, which would get no break point at
      // all: it is shown in forward-slash form, every segment breakable. A
      // POSIX name that merely contains a backslash is not Windows-shaped and
      // stays as it is.
      expect(breakAtSlashes('C:\\Users\\dev\\KiroCrew')).toBe('C:/\u200bUsers/\u200bdev/\u200bKiroCrew')
      expect(breakAtSlashes('\\\\share\\team\\repo')).toBe('/\u200b/\u200bshare/\u200bteam/\u200brepo')
      expect(breakAtSlashes('/home/dev/weird\\name')).toBe('/\u200bhome/\u200bdev/\u200bweird\\name')
      // Reading order: sentence, path, then the hand-off -- never the hand-off
      // between the sentence and its path.
      const handoff = within(notice).getByRole('button', { name: 'Ask the agent' })
      expect(pathSpan!.compareDocumentPosition(handoff) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(state).not.toHaveTextContent('Folder not readable')
      expect(screen.queryByText('No files in this workspace yet')).not.toBeInTheDocument()
      expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
      expect(api.projectTree).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
      await waitFor(() => expect(api.projectTree).toHaveBeenCalledTimes(2))

      // The hand-off carries the REAL path, not the display copy with hints.
      fireEvent.click(handoff)
      const prompt = errorReport.consumeChatHandoff()
      expect(prompt).toContain(`No permission to read the workspace folder ${ROOT}`)
      expect(prompt).not.toContain('\u200b')
    } finally {
      errorReport.__resetErrorJournalForTests()
      errorReport.__resetNavSeamForTests()
      sessionStorage.clear()
    }
  })

  it('says the root holds only skipped or hidden folders instead of calling the workspace empty', async () => {
    // `.` in `hiddenOnlyDirectories`: the project directory's top level holds
    // only folders the listing skips or hides (a `.kiro/`, a `node_modules/`),
    // so the payload is empty the same way an empty workspace's is -- and the
    // copy the state row uses one level down stands in for the empty notice.
    // Not an error: nothing to refresh, nothing to hand off.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: [],
      directories: [],
      repo: false,
      hiddenOnlyDirectories: ['.'],
    }))
    renderTree()

    const state = await screen.findByTestId('workspace-tree-root-hidden-only')
    expect(state).toHaveTextContent('This workspace contains only hidden items (skipped tool and cache folders, protected entries)')
    expect(screen.queryByText('No files in this workspace yet')).not.toBeInTheDocument()
    expect(screen.queryByTestId('workspace-tree-root-unreadable')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Refresh' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('file-tree')).not.toBeInTheDocument()
  })

  it('warns that a large workspace payload was truncated, in all mode only', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ truncated: true }))
    const { unmount } = renderTree()
    await waitForTree()
    expect(screen.getByText(/Large workspace/)).toBeInTheDocument()
    unmount()

    // Changed mode renders the git-status set, which carries no WORKSPACE-level
    // truncation notice -- that one is about the tree payload's own file budget.
    // The git-status set has its own cap and its own notice, asserted below.
    vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([mkFile('project/a.ts', 'M')]))
    renderTree({ mode: 'changed' })
    await waitForTree()
    expect(screen.queryByText(/Large workspace/)).not.toBeInTheDocument()
  })

  // The server caps the changed-file listing at 500 and reports it. Without a
  // notice of its own the tree simply ended at 500 rows, presenting a cut list
  // as a complete one.
  it('says the changed listing was cut when the git status was capped', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus(
        Array.from({ length: 500 }, (_, i) => mkFile(`project/f${i}.ts`, 'M')),
        { truncated: true },
      ),
    )
    renderTree({ mode: 'changed' })
    await waitForTree()
    const notice = await screen.findByTestId('workspace-tree-changed-truncated')
    // The count names the rows THIS surface shows, not the payload length: the
    // 500 listed files are filtered to those under the project root and
    // de-duplicated across staged/unstaged first.
    expect(notice).toHaveTextContent('first 500 shown')
  })

  it('leaves the changed tree unqualified when the git status was complete', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/a.ts', 'M')], { truncated: false }),
    )
    renderTree({ mode: 'changed' })
    await waitForTree()
    expect(screen.queryByTestId('workspace-tree-changed-truncated')).not.toBeInTheDocument()
  })

  // The notice is gated on the mode it describes: `all` renders the tree payload,
  // whose cap the workspace notice above already covers, so a capped git status
  // must not put a changed-list claim over a full-workspace tree.
  it('keeps the changed-listing notice out of all mode', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ truncated: false }))
    vi.mocked(api.projectGitStatus).mockResolvedValue(
      mkStatus([mkFile('project/a.ts', 'M')], { truncated: true }),
    )
    renderTree({ mode: 'all' })
    await waitForTree()
    // `all` mode gates the tree on the TREE payload, so it paints before the
    // status lands -- asserting the notice's absence straight after
    // `waitForTree` would pass with the mode gate deleted. Wait until the
    // capped status has been folded into the model, so the absence below is
    // about the gate rather than about timing.
    await waitFor(() =>
      expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([{ path: 'a.ts', status: 'modified' }]),
    )
    expect(screen.queryByTestId('workspace-tree-changed-truncated')).not.toBeInTheDocument()
  })
})

// The listing arrives whole -- there is no per-folder request -- so a folder
// with nothing beneath it is decided by the payload, never by a fetch in
// flight. Pierre has no slot for a status line under a row, so the wrapper
// feeds each childless folder ONE synthetic child whose basename is the
// label; these tests pin what reaches the model and how the wrapper keeps
// that row inert.
describe('PierreWorkspaceTreeImpl — state row under a childless folder', () => {
  it('puts an "Empty folder" row under a folder the payload lists with nothing in it', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['empty'],
    }))
    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([
      ['README.md', 'empty/', `empty/Empty folder${M}`],
    ])
  })

  it('says "Contains only hidden items (skipped tool and cache folders, protected entries)" when the folder holds only folders the listing skips or hides', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['HEARTBEAT.md'],
      directories: ['_bg'],
      hiddenOnlyDirectories: ['_bg'],
      repo: false,
    }))
    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([
      ['HEARTBEAT.md', '_bg/', `_bg/Contains only hidden items (skipped tool and cache folders, protected entries)${M}`],
    ])
  })

  it('says "Link to another folder: contents not listed" under a symlinked folder, and nothing under the folder holding it', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['HEARTBEAT.md'],
      directories: ['deploy', 'deploy/current'],
      linkedDirectories: ['deploy/current'],
      repo: false,
    }))
    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([
      ['HEARTBEAT.md', 'deploy/', 'deploy/current/', `deploy/current/Link to another folder: contents not listed${M}`],
    ])
  })

  it('marks the row of a folder the server could not read as pointing to the notice, puts no state row under it, and keeps its parent populated', async () => {
    // Nothing beneath the folder is KNOWN, so no row may make a claim about its
    // contents -- and a failed read is an error, which is REPORTED only through
    // the `ErrorNotice` above the tree (next test). The folder's own row is not
    // silent, though: without a marker the reader cannot tell from the tree
    // which of the folders that show nothing is the one the notice names. The
    // marker points at the notice (an icon whose accessible label says "see
    // the notice above"), it states no failure of its own, and it follows the
    // same payload list the notice reads. The folder is still an ordinary row
    // of its own, so its parent is not childless.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['vault', 'vault/locked'],
      unreadableDirectories: ['vault/locked'],
      repo: false,
    }))
    renderTree()
    await waitForTree()

    const model = treeMock.last()
    expect(model.calls.resetPaths).toEqual([['README.md', 'vault/', 'vault/locked/']])
    expect(model.calls.resetPaths[0].some(p => p.endsWith(M))).toBe(false)
    const decorate = model.options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => unknown
    const marker = { icon: { name: 'file-tree-icon-lock' }, title: 'Not readable — see the notice above' }
    const locked = { kind: 'directory', name: 'locked', path: 'vault/locked' } as const
    expect(decorate({ item: locked, row: { ...locked, isExpanded: false } })).toEqual(marker)
    expect(decorate({ item: locked, row: { ...locked, isExpanded: true } })).toEqual(marker)
    const vault = { kind: 'directory', name: 'vault', path: 'vault' } as const
    expect(decorate({ item: vault, row: { ...vault, isExpanded: true } })).toBeNull()
    // Pierre hands directory paths with the library's trailing slash too.
    expect(decorate({ item: { ...locked, path: 'vault/locked/' }, row: { ...locked, path: 'vault/locked/', isExpanded: false } })).toEqual(marker)
  })

  it('names the folders the server could not read in a notice above the tree, with the agent hand-off', async () => {
    // The row beneath the folder is a status line inside Pierre's shadow root:
    // it can carry no button. The failure itself -- a `scandir` the server
    // could not perform -- is an error the user may not be able to fix but the
    // agent often can, so it also surfaces through `ErrorNotice` above the
    // tree with the hand-off on (a listing holds no draft to lose). The root
    // marker `.` has its own whole-panel state and never appears here.
    const errorReport = await import('../utils/errorReport')
    errorReport.__resetErrorJournalForTests()
    errorReport.__resetNavSeamForTests()
    sessionStorage.clear()
    errorReport.installSoftNavigate(() => {})
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['vault', 'vault/locked', 'ops', 'ops/secrets'],
      unreadableDirectories: ['vault/locked', 'ops/secrets'],
      repo: false,
    }))
    try {
      const { qc } = renderTree()
      await waitForTree()

      const notice = await screen.findByTestId('workspace-tree-unreadable-notice')
      expect(notice).toHaveTextContent('Folders not readable: vault/locked, ops/secrets')
      expect(notice).toHaveAttribute('role', 'alert')
      fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
      expect(errorReport.consumeChatHandoff()).toContain('Folders not readable: vault/locked, ops/secrets')

      // The notice follows the payload: a poll that finds the folders readable
      // again takes it down with the rows.
      vi.mocked(api.projectTree).mockResolvedValue(mkTree({
        paths: ['README.md', 'vault/locked/inside.txt'],
        directories: ['vault', 'vault/locked', 'ops', 'ops/secrets'],
        unreadableDirectories: [],
        repo: false,
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument())
    } finally {
      errorReport.__resetErrorJournalForTests()
      errorReport.__resetNavSeamForTests()
      sessionStorage.clear()
    }
  })

  it('shows no not-readable notice when every folder was read', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['empty'],
      unreadableDirectories: [],
      repo: false,
    }))
    renderTree()
    await waitForTree()
    expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  describe('dismissing the not-readable notice', () => {
    const DISMISS = 'Dismiss: hide this notice until the unreadable folders change'
    const MARKER = 'Not readable — see the notice above'
    const MARKER_DISMISSED = 'Not readable — notice dismissed'
    const locked = (over: Partial<TreePayload> = {}) => mkTree({
      paths: ['README.md'],
      directories: ['vault', 'vault/locked'],
      unreadableDirectories: ['vault/locked'],
      repo: false,
      ...over,
    })
    const lockedRow = { kind: 'directory', name: 'locked', path: 'vault/locked' } as const
    const decorateLocked = () => {
      const decorate = treeMock.last().options.renderRowDecoration as (
        context: { item: MenuItem; row: VisibleRow },
      ) => unknown
      return decorate({ item: lockedRow, row: { ...lockedRow, isExpanded: true } })
    }

    beforeEach(() => {
      __resetTreeUnreadableDismissalsForTests()
      localStorage.removeItem('mc-files-tree-unreadable-dismissed')
    })

    it('hides the notice once dismissed and turns the row marker into "notice dismissed"', async () => {
      // A folder that stays unreadable by design (a root-owned cache) would
      // keep the red alert on every Files visit -- alarm as wallpaper. The
      // dismiss control does what a ✕ promises: the notice goes. Until then it
      // is the alert in the danger tone, never a toned-down status, and the
      // control names its promise for hover as well as for the accessibility
      // tree. The folder's row keeps its lock marker, whose label no longer
      // sends the reader to a notice that is not there.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const notice = await screen.findByTestId('workspace-tree-unreadable-notice')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice.className).toContain('text-danger')
      expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
      expect(decorateLocked()).toEqual({ icon: { name: 'file-tree-icon-lock' }, title: MARKER })
      const control = screen.getByRole('button', { name: DISMISS })
      expect(control).toHaveAttribute('title', DISMISS)

      fireEvent.click(control)

      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      // What stands in its place names the user's action, never the error: no
      // toned-down copy of the folder list survives the dismissal.
      expect(screen.getByRole('status')).not.toHaveTextContent(/vault\/locked|not readable/i)
      expect(screen.queryByRole('button', { name: 'Ask the agent' })).not.toBeInTheDocument()
      // The marker is fed by the payload, so it stays; its label follows the
      // dismissal.
      expect(decorateLocked()).toEqual({ icon: { name: 'file-tree-icon-lock' }, title: MARKER_DISMISSED })
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
    })

    it('leaves a one-line "Notice dismissed — Undo" where the notice stood; Undo brings the notice back and clears the remembered set', async () => {
      // The ✕ sits beside "Ask the agent" at the same weight, and a dismissal
      // is remembered per project across reloads -- so a mis-click, or a click
      // by a reader who did not take in the tooltip, needs a way back that is
      // visible, not hover-only. The line names the action, not the error, and
      // its Undo restores the alert and forgets the whole remembered set.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))

      const line = screen.getByTestId('workspace-tree-unreadable-dismissed')
      expect(line).toHaveAttribute('role', 'status')
      // The line says what the ✕ did and did not do -- hidden, not fixed: how
      // many folders are still unreadable (never which) and that the notice
      // returns when they change -- so a reader who did not hover the ✕ is not
      // left guessing whether the problem went away (UX lane).
      expect(line).toHaveTextContent('Notice hidden — 1 folder still unreadable (shown again when it changes)')
      expect(line.className).toContain('text-muted')
      const undo = within(line).getByRole('button', { name: 'Undo' })
      expect(undo).toHaveAccessibleDescription('Notice hidden — 1 folder still unreadable (shown again when it changes)')
      expect(decorateLocked()).toEqual({ icon: { name: 'file-tree-icon-lock' }, title: MARKER_DISMISSED })

      fireEvent.click(undo)

      const notice = screen.getByTestId('workspace-tree-unreadable-notice')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent('Folders not readable: vault/locked')
      expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: DISMISS })).toBeInTheDocument()
      expect(screen.queryByTestId('workspace-tree-unreadable-dismissed')).not.toBeInTheDocument()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(decorateLocked()).toEqual({ icon: { name: 'file-tree-icon-lock' }, title: MARKER })
      // Cleared, not merely hidden: the session map and the reload mirror both
      // hold nothing for the project.
      expect(recallDismissedUnreadable(ROOT)).toEqual([])
      expect(JSON.parse(localStorage.getItem('mc-files-tree-unreadable-dismissed') ?? '{}')).toEqual({ [ROOT]: [] })
    })

    it('keeps the Undo offer for the undo window only; expiry leaves the dismissal as it was', async () => {
      // Transient: the way back is offered while the mistake is fresh, on the
      // product's one undo clock (the session-move bar's horizon). Expiry
      // leaves the dismissal remembered and the notice hidden.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      vi.useFakeTimers()
      try {
        act(() => { fireEvent.click(dismiss) })
        expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1) })
        expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(2) })
        expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
        expect(screen.queryByRole('status')).not.toBeInTheDocument()
        expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('holds the Undo window open while the pointer is over the spot or focus is inside it, and resumes where it stopped', async () => {
      // The pointer is on the ✕ when the offer lands -- on the spot the line
      // takes over -- so the offer holds a FULL window until it leaves; a focus
      // inside it holds likewise, and the two are tracked apart so a pointer
      // passing through cannot release a hold keyboard focus still owns.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      const spot = screen.getByTestId('workspace-tree-unreadable-notice').parentElement as HTMLElement
      vi.useFakeTimers()
      try {
        act(() => { fireEvent.mouseEnter(spot) })
        act(() => { fireEvent.click(dismiss) })
        const undo = () => screen.queryByRole('button', { name: 'Undo' })
        expect(screen.getByTestId('workspace-tree-unreadable-dismissed').parentElement).toBe(spot)
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
        expect(undo()).toBeInTheDocument()
        act(() => { fireEvent.mouseLeave(spot) })
        // Part of the window runs, then a focus hold freezes the remainder...
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1000) })
        act(() => { fireEvent.focus(undo() as HTMLElement) })
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
        expect(undo()).toBeInTheDocument()
        act(() => { fireEvent.mouseEnter(spot) })
        act(() => { fireEvent.mouseLeave(spot) })
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
        expect(undo()).toBeInTheDocument()
        // ...and the remainder, not a fresh window, runs once the hold lifts.
        act(() => { fireEvent.blur(undo() as HTMLElement) })
        act(() => { vi.advanceTimersByTime(999) })
        expect(undo()).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(2) })
        expect(undo()).not.toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('starts a FULL window for a second dismissal in the same mount: the new offer never inherits the remainder the previous one had spent', async () => {
      // Every offer needs a key of its own. `undoOffer` is null between offers
      // (expiry, Undo, the notice returning), so a key derived from it would
      // come out the same each time, and the shared clock -- which tells a new
      // offer from the old one by its key alone -- would hand the second
      // dismissal the remainder frozen for the first (or, with the pointer
      // parked on the spot at the click, a deadline already past: a window of
      // nothing) and commit the remembered dismissal with no usable way back
      // (Opus lane on `9f52681b54`).
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      const spot = screen.getByTestId('workspace-tree-unreadable-notice').parentElement as HTMLElement
      vi.useFakeTimers()
      try {
        const undo = () => screen.queryByRole('button', { name: 'Undo' })
        // First offer: most of its window runs, a pointer passing over the spot
        // freezes the last second of it as the remainder on record, then Undo
        // takes the dismissal back.
        act(() => { fireEvent.click(dismiss) })
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1000) })
        act(() => { fireEvent.mouseEnter(spot) })
        act(() => { fireEvent.mouseLeave(spot) })
        act(() => { fireEvent.click(undo() as HTMLElement) })
        expect(screen.getByTestId('workspace-tree-unreadable-notice')).toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual([])
        // Second offer, pointer and focus elsewhere: a full window, not the one
        // second the first offer had left.
        act(() => { fireEvent.click(screen.getByRole('button', { name: DISMISS })) })
        expect(undo()).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(1001) })
        expect(undo()).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1001 - 1) })
        expect(undo()).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(2) })
        expect(undo()).not.toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
      } finally {
        vi.useRealTimers()
      }
    })

    it('lands a keyboard dismissal on Undo and lets the window run once focus leaves it: the hold the ✕ set is not stuck on a control that is gone', async () => {
      // Enter on the ✕ unmounts it. The spot's focus capture had set the hold
      // when the ✕ took focus, and a removed element fires no focusout, so
      // without the swap clearing it the offer would stay held for the life
      // of the mount: the Undo line above the tree for as long, and every
      // later offer born held (GPT and Opus lanes). Focus itself would fall to
      // <body>, with the window running while the keyboard user tabs back
      // from the top of the panel (UX lane) -- so the swap hands it to Undo,
      // the way back, where it holds the window as any focus inside the spot
      // does, and the window resumes the moment focus moves on.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      const elsewhere = document.createElement('button')
      document.body.appendChild(elsewhere)
      vi.useFakeTimers()
      try {
        // Enter on a focused button is a click in every browser; this DOM runs
        // no default actions, so the click Enter would dispatch is fired by
        // hand. What is under test is the FOCUSED control unmounting.
        act(() => { dismiss.focus() })
        expect(dismiss).toHaveFocus()
        fireEvent.keyDown(dismiss, { key: 'Enter' })
        act(() => { fireEvent.click(dismiss) })

        const undo = screen.getByRole('button', { name: 'Undo' })
        expect(undo).toHaveFocus()
        // Held while focus is on Undo, as any focus inside the spot holds...
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
        expect(undo).toBeInTheDocument()
        // ...and a full window runs once it leaves. Focus is moved by focusing
        // ANOTHER element, which blurs only what really had focus: a hold left
        // by the unmounted ✕ would get no focusout here and never lift.
        act(() => { elsewhere.focus() })
        expect(undo).not.toHaveFocus()
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS - 1) })
        expect(screen.queryByRole('button', { name: 'Undo' })).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(2) })
        expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
      } finally {
        vi.useRealTimers()
        elsewhere.remove()
      }
    })

    it('lands a keyboard Undo on the ✕, and the next dismissal is not born held by the hold Undo left behind', async () => {
      // The other direction of the same swap: Enter on Undo unmounts it while
      // it has focus. Focus goes to the control that reverses it, as the
      // Auto-title Undo's does; and the hold is cleared here too, or the offer
      // the NEXT ✕ creates would inherit a hold nothing is holding and never
      // run out.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      renderTree()
      await waitForTree()
      const elsewhere = document.createElement('button')
      document.body.appendChild(elsewhere)
      vi.useFakeTimers()
      try {
        const first = screen.getByRole('button', { name: DISMISS })
        act(() => { first.focus() })
        act(() => { fireEvent.click(first) })
        const undo = screen.getByRole('button', { name: 'Undo' })
        expect(undo).toHaveFocus()
        fireEvent.keyDown(undo, { key: 'Enter' })
        act(() => { fireEvent.click(undo) })

        const dismiss = screen.getByRole('button', { name: DISMISS })
        expect(dismiss).toHaveFocus()
        expect(screen.getByTestId('workspace-tree-unreadable-notice')).toHaveAttribute('role', 'alert')
        expect(recallDismissedUnreadable(ROOT)).toEqual([])
        // Focus moves on; a later click (a browser that focuses no pressed
        // button moves no focus) creates an offer that must run out on the
        // clock, not sit on a stale hold.
        act(() => { elsewhere.focus() })
        act(() => { fireEvent.click(dismiss) })
        expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Undo' })).not.toHaveFocus()
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 1) })
        expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
        expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
      } finally {
        vi.useRealTimers()
        elsewhere.remove()
      }
    })

    it('clears the focus hold when the payload swaps the spot under a focused Undo, so the next offer is not born held', async () => {
      // The swap is not always the user's: the notice comes back on its own
      // when a new folder fails, and Undo unmounts under the focus it holds.
      // No control was activated, so there is nothing to hand focus to -- but
      // the hold must still go with the element it described.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      const undo = screen.getByRole('button', { name: 'Undo' })
      act(() => { undo.focus() })
      expect(undo).toHaveFocus()

      vi.mocked(api.projectTree).mockResolvedValue(locked({
        directories: ['vault', 'vault/locked', 'ops', 'ops/secrets'],
        unreadableDirectories: ['vault/locked', 'ops/secrets'],
      }))
      await act(async () => { await qc.invalidateQueries() })
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      expect(undo).not.toBeInTheDocument()

      vi.useFakeTimers()
      try {
        act(() => { fireEvent.click(dismiss) })
        expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
        // Two folders remembered: the line counts them, in the plural.
        expect(screen.getByTestId('workspace-tree-unreadable-dismissed')).toHaveTextContent(
          'Notice hidden — 2 folders still unreadable (shown again when they change)',
        )
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS + 1) })
        expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
      } finally {
        vi.useRealTimers()
      }
    })

    it('retires the Undo offer for good once the notice has returned on its own, even if the payload settles again inside the window', async () => {
      // Gone is one-way, as for every undo offer: the alert came back with a
      // new folder, so the old dismissal is no longer what the user is looking
      // at, and when that folder reads again the remembered set hides the
      // notice WITHOUT resurrecting an Undo for a click made a payload ago.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()

      vi.mocked(api.projectTree).mockResolvedValue(locked({
        directories: ['vault', 'vault/locked', 'ops', 'ops/secrets'],
        unreadableDirectories: ['vault/locked', 'ops/secrets'],
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await screen.findByTestId('workspace-tree-unreadable-notice')

      vi.mocked(api.projectTree).mockResolvedValue(locked())
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument())
      expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
    })

    it('does not let a hold left by an unmounted spot pin a later offer open', async () => {
      // The pointer parked on the spot gets no leave event when the spot goes
      // (every folder read again). A later notice dismissed from the keyboard,
      // pointer elsewhere, must still run its window out.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      await screen.findByRole('button', { name: DISMISS })
      fireEvent.mouseEnter(screen.getByTestId('workspace-tree-unreadable-notice').parentElement as HTMLElement)

      vi.mocked(api.projectTree).mockResolvedValue(locked({ unreadableDirectories: [] }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument())

      vi.mocked(api.projectTree).mockResolvedValue(locked())
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      const dismiss = await screen.findByRole('button', { name: DISMISS })
      vi.useFakeTimers()
      try {
        act(() => { fireEvent.click(dismiss) })
        expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
        act(() => { vi.advanceTimersByTime(MOVE_UNDO_MS * 3) })
        expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
      } finally {
        vi.useRealTimers()
      }
    })

    it('does not carry the Undo offer across a remount: the dismissal is remembered, the offer is not', async () => {
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const first = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
      first.unmount()

      treeMock.reset()
      renderTree()
      await waitForTree()
      await waitFor(() => expect(decorateLocked()).toMatchObject({ title: MARKER_DISMISSED }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
    })

    it('remembers the dismissal across a remount and a page reload, per project', async () => {
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const first = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      first.unmount()

      // The Files tab remounts on in-place tab navigation: the module map
      // answers before the notice can flash red. The marker proves the payload
      // (and the folder) landed, so the absence is the dismissal, not a tree
      // that has not loaded yet.
      treeMock.reset()
      const second = renderTree()
      await waitForTree()
      await waitFor(() => expect(decorateLocked()).toMatchObject({ title: MARKER_DISMISSED }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      second.unmount()

      // A page reload drops the module map; the localStorage mirror answers.
      __resetTreeUnreadableDismissalsForTests()
      treeMock.reset()
      const third = renderTree()
      await waitForTree()
      await waitFor(() => expect(decorateLocked()).toMatchObject({ title: MARKER_DISMISSED }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      third.unmount()

      // Another project's identical folder was never dismissed.
      treeMock.reset()
      vi.mocked(api.projectTree).mockResolvedValue(locked({ root: '/repo/other' }))
      renderTree({ projectDir: '/repo/other' })
      await waitForTree()
      expect(await screen.findByTestId('workspace-tree-unreadable-notice')).toHaveAttribute('role', 'alert')
      expect(decorateLocked()).toMatchObject({ title: MARKER })
    })

    it('brings the notice back when a folder outside the dismissed set becomes unreadable', async () => {
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()

      vi.mocked(api.projectTree).mockResolvedValue(locked({
        directories: ['vault', 'vault/locked', 'ops', 'ops/secrets'],
        unreadableDirectories: ['vault/locked', 'ops/secrets'],
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      const notice = await screen.findByTestId('workspace-tree-unreadable-notice')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent('Folders not readable: vault/locked, ops/secrets')
      // The alert is back on its own, so the Undo offer is retired: one way
      // back at a time, never a live alert with an Undo line beside it.
      expect(screen.queryByTestId('workspace-tree-unreadable-dismissed')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
      // Both folders' markers point at the notice again.
      expect(decorateLocked()).toMatchObject({ title: MARKER })
      // Dismissing again covers the whole current list, with a fresh Undo.
      fireEvent.click(screen.getByRole('button', { name: DISMISS }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked', 'ops/secrets'])
    })

    it('forgets a dismissed folder once it reads again, so the same folder failing anew alerts again', async () => {
      // A dismissal covers the folder's CURRENT failure, not the folder
      // forever: a folder that recovers and later breaks again is a new
      // failure and must not inherit its old dismissal.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])

      // The folder reads again: nothing unreadable, and the dismissal is
      // pruned to the payload -- in the store too, so a reload does not
      // resurrect it.
      vi.mocked(api.projectTree).mockResolvedValue(locked({ unreadableDirectories: [] }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(recallDismissedUnreadable(ROOT)).toEqual([]))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      expect(decorateLocked()).toBeNull()
      expect(JSON.parse(localStorage.getItem('mc-files-tree-unreadable-dismissed') ?? '{}')).toEqual({ [ROOT]: [] })

      // The same folder fails again: the alert is back, marker pointing at it.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      const notice = await screen.findByTestId('workspace-tree-unreadable-notice')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent('Folders not readable: vault/locked')
      expect(decorateLocked()).toMatchObject({ title: MARKER })
    })

    it('keeps a dismissal while a truncated payload merely leaves the folder out', async () => {
      // A truncated payload is partial knowledge: a remembered folder absent
      // from it may only have fallen past the row cap, and forgetting it would
      // re-alert the very folder the user dismissed on the next full listing.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])

      vi.mocked(api.projectTree).mockResolvedValue(locked({
        directories: ['vault'],
        unreadableDirectories: [],
        truncated: true,
        truncatedDirectories: ['vault'],
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await act(async () => { await Promise.resolve() })
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])

      // Listed as a readable row, even in a truncated payload: it reads again.
      vi.mocked(api.projectTree).mockResolvedValue(locked({
        unreadableDirectories: [],
        truncated: true,
        truncatedDirectories: [''],
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(recallDismissedUnreadable(ROOT)).toEqual([]))
    })

    it('forgets a dismissal once a truncated payload lists the parent whole without the folder', async () => {
      // The cut did not reach `vault`: it is listed and not truncated, so a
      // `vault/locked` missing beneath it is gone, not hidden -- and the same
      // folder recreated unreadable later must alert again.
      vi.mocked(api.projectTree).mockResolvedValue(locked())
      const { qc } = renderTree()
      await waitForTree()
      fireEvent.click(await screen.findByRole('button', { name: DISMISS }))
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])

      vi.mocked(api.projectTree).mockResolvedValue(locked({
        directories: ['vault', 'other'],
        unreadableDirectories: [],
        truncated: true,
        truncatedDirectories: ['other'],
      }))
      await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })
      await waitFor(() => expect(recallDismissedUnreadable(ROOT)).toEqual([]))
    })

    it('does not forget a dismissal before the listing has answered', async () => {
      // A mount that has not heard from the server yet knows nothing about the
      // folders: pruning against that silence would forget every dismissal on
      // every reload and re-alert the very visit the dismissal was for.
      rememberDismissedUnreadable(ROOT, ['vault/locked'])
      let answer: (payload: TreePayload) => void = () => {}
      vi.mocked(api.projectTree).mockImplementation(() => new Promise(resolve => { answer = resolve }))
      renderTree()
      await act(async () => { await Promise.resolve() })
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])

      await act(async () => { answer(locked()) })
      await waitForTree()
      await waitFor(() => expect(decorateLocked()).toMatchObject({ title: MARKER_DISMISSED }))
      expect(screen.queryByTestId('workspace-tree-unreadable-notice')).not.toBeInTheDocument()
      expect(recallDismissedUnreadable(ROOT)).toEqual(['vault/locked'])
    })
  })

  it('says the files were not listed when a childless folder lost them to the file cap, once at a time', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['README.md'],
      directories: ['big'],
      truncatedDirectories: ['big'],
      truncated: true,
    }))
    renderTree()
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([
      ['README.md', 'big/', `big/Some items not shown: limit of 10,000 items reached${M}`],
    ])
    // The folder row's badge and the state row beneath it make the same claim,
    // so the badge yields while the row is showing (the folder is expanded) and
    // is back the moment the folder is collapsed and the row goes with it.
    const decorate = treeMock.last().options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => { text: string; title?: string } | null
    const big = { kind: 'directory', name: 'big', path: 'big/' } as const
    expect(decorate({ item: big, row: { ...big, isExpanded: true } })).toBeNull()
    expect(decorate({ item: big, row: { ...big, isExpanded: false } })).toEqual({
      text: 'some items not shown',
      title: 'some items not shown',
    })
  })

  it('adds no state row under a folder that has a file or a subfolder', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['src/a/b.ts', 'docs/readme.md'],
      // `src` holds a subfolder, `src/a` and `docs` hold a file, `pkg` holds a
      // subfolder that is itself childless: only that leaf gets a row.
      directories: ['src', 'src/a', 'docs', 'pkg', 'pkg/lib'],
    }))
    renderTree()
    await waitForTree()

    const [paths] = treeMock.last().calls.resetPaths
    const stateRows = paths.filter(p => p.endsWith(M))
    expect(stateRows).toEqual([`pkg/lib/Empty folder${M}`])
  })

  it('drops the row the moment a refetch brings real children', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md'], directories: ['empty'] }))
    const { qc } = renderTree()
    await waitForTree()
    expect(treeMock.last().calls.resetPaths.at(-1)).toContain(`empty/Empty folder${M}`)

    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md', 'empty/new.ts'], directories: ['empty'] }))
    await act(async () => { await qc.refetchQueries({ queryKey: ['project-tree', ROOT] }) })

    await waitFor(() => expect(treeMock.last().calls.resetPaths).toHaveLength(2))
    expect(treeMock.last().calls.resetPaths.at(-1)).toEqual(['README.md', 'empty/new.ts', 'empty/'])
  })

  it('never reports a state row as a file open, and leaves it unselected', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md'], directories: ['empty'] }))
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen })
    await waitForTree()
    const model = treeMock.last()

    act(() => { model.simulateSelection(`empty/Empty folder${M}`) })

    expect(onFileOpen).not.toHaveBeenCalled()
    expect(model.calls.deselect).toEqual([`empty/Empty folder${M}`])
    expect(model.getSelectedPaths()).toEqual([])
  })

  it('opens no context menu on a state row and closes the request Pierre already opened', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md'], directories: ['empty'] }))
    renderTree({ onAddToContext: vi.fn() })
    await waitForTree()

    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close: vi.fn(),
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(
      { kind: 'file', name: 'Empty folder', path: `empty/Empty folder${M}` },
      context,
    )
    expect(node).toBeNull()
    // Pierre flips into its menu-open state BEFORE asking for the content, and
    // while that state holds it swallows every key but Escape. A null render
    // alone would leave a keyboard user (Shift+F10 on the focused row) stuck
    // behind an invisible menu, so the rejection must also close it -- after
    // the render, since Pierre's close is a state update on its own root.
    expect(context.close).not.toHaveBeenCalled()
    await waitFor(() => expect(context.close).toHaveBeenCalledTimes(1))
    expect(context.close).toHaveBeenCalledWith()
  })

  it('styles the state rows as a status line through the shadow stylesheet, keyed on the decoration the plan emits', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md'], directories: ['empty'] }))
    renderTree()
    await waitForTree()

    const css = treeMock.last().options.unsafeCSS as string
    // Selected by the decoration the wrapper emits for a PLANNED row, never by
    // the marker suffix (a real file can end in it) and never by a label (the
    // sheet is fixed at model construction while the labels follow the
    // language, and a real file may carry a label's exact name).
    expect(css).toContain(`[data-icon-name="${STATE_ROW_ICON}"]`)
    expect(css).not.toContain('data-item-path')
    expect(css).not.toContain('Empty folder')
    expect(css).toContain('pointer-events:none')
    const decorate = treeMock.last().options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => unknown
    const planned = { kind: 'file', name: `Empty folder${M}`, path: `empty/Empty folder${M}` } as const
    expect(decorate({ item: planned, row: { ...planned, isExpanded: false } })).toEqual(STATE_ROW_DECORATION)
    const readme = { kind: 'file', name: 'README.md', path: 'README.md' } as const
    expect(decorate({ item: readme, row: { ...readme, isExpanded: false } })).toBeNull()
  })

  it('leaves a real file whose name ends in the marker character alone', async () => {
    // An agent or a cloned repository can write a file whose name ends in
    // U+200B -- the external-content boundary. The marker is the planned row's
    // identity in the path, not the styling hook: only a path in the plan gets
    // the decoration the sheet keys on, so this file keeps its icon, its
    // pointer and its open.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: [`notes/x${M}`, 'README.md'],
      directories: ['notes', 'empty'],
    }))
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen })
    await waitForTree()

    const model = treeMock.last()
    const [paths] = model.calls.resetPaths
    expect(paths).toContain(`notes/x${M}`)
    expect(paths).toContain(`empty/Empty folder${M}`)
    const decorate = model.options.renderRowDecoration as (
      context: { item: MenuItem; row: VisibleRow },
    ) => unknown
    const real = { kind: 'file', name: `x${M}`, path: `notes/x${M}` } as const
    expect(decorate({ item: real, row: { ...real, isExpanded: false } })).toBeNull()
    expect(model.options.unsafeCSS as string).not.toContain('data-item-path')
    // And the wrapper's guards do not treat it as a state row: selecting it opens it.
    act(() => { model.simulateSelection(`notes/x${M}`) })
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/notes/x${M}`)
    expect(model.calls.deselect).toEqual([])
  })

  it('leaves a real file that is named like a label alone', async () => {
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({
      paths: ['notes/Empty folder', 'README.md'],
      directories: ['notes', 'empty'],
    }))
    const onFileOpen = vi.fn()
    renderTree({ onFileOpen })
    await waitForTree()

    // The real file's path carries no marker, so the stylesheet leaves its icon
    // and pointer; only the synthetic row under `empty` ends in it.
    const [paths] = treeMock.last().calls.resetPaths
    expect(paths.filter(p => p.endsWith(M))).toEqual([`empty/Empty folder${M}`])
    expect(paths).toContain('notes/Empty folder')
    // And the wrapper's guards do not treat it as a state row: selecting it opens it.
    const model = treeMock.last()
    act(() => { model.simulateSelection('notes/Empty folder') })
    expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/notes/Empty folder`)
    expect(model.calls.deselect).toEqual([])
  })

  it('re-plans the rows in the new language when the UI language switches at runtime', async () => {
    // LanguageProvider repaints with cloneElement, so this component re-renders
    // WITHOUT remounting: labels read once per mount would leave the state row
    // as the one string in the panel still in the old language.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ paths: ['README.md'], directories: ['empty'] }))
    const { update } = renderTree()
    await waitForTree()
    expect(treeMock.last().calls.resetPaths.at(-1)).toContain(`empty/Empty folder${M}`)

    const { i18next } = await import('../i18n/all')
    try {
      await act(async () => { await i18next.changeLanguage('fr') })
      // The provider's repaint, which this harness has no provider to deliver.
      update()
      await waitFor(() =>
        expect(treeMock.last().calls.resetPaths.at(-1)).toContain(`empty/${i18next.t('components.workspaceTree.row_empty')}${M}`),
      )
      expect(i18next.t('components.workspaceTree.row_empty')).not.toBe('Empty folder')
    } finally {
      await act(async () => { await i18next.changeLanguage('en') })
    }
  })

  it('adds no state rows in changed mode, whose folders are all parents of a changed file', async () => {
    vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([mkFile('project/src/a.ts', 'M')]))
    renderTree({ mode: 'changed' })
    await waitForTree()

    expect(treeMock.last().calls.resetPaths).toEqual([['src/a.ts']])
  })
})
