/**
 * The pinned Files tab.
 *
 * The tab's whole job is conditional: it only shows the tree — and the hint
 * that points at it — when the tree endpoint actually answers for this
 * directory, and it never opens a file inline. Both halves are pinned here,
 * with the real FileBrowserRail mounted (only the Pierre tree is stubbed) so
 * the open handed up from a tree row is checked through the real wiring.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const H = vi.hoisted(() => ({
  OPENED: '/repo/src/a.ts',
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    revealPath: vi.fn(),
  },
}))

// `ApiError` is exported alongside `api` because the shared `revealOrOpen`
// helper (which the reveal button now routes through) branches on it.
vi.mock('../api/client', () => ({ api: H.api, ApiError: class ApiError extends Error {} }))

// The reveal-in-file-manager button is gated on branding.directLocal: a remote
// session degrades reveal to a clipboard copy, so the affordance is hidden
// there. Default to a local session so the existing header assertions hold; a
// dedicated case below flips it to pin the hidden (remote) state.
const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: (p: { onFileOpen?: (abs: string) => void }) => (
    <button data-testid="tree" onClick={() => p.onFileOpen?.(H.OPENED)}>tree</button>
  ),
}))

import FilesHomePanel from '../pages/chat/FilesHomePanel'

const DIR = '/repo/my-project'

/** The rail carries its own controls below this row, so header assertions are
 *  scoped to the header rather than the whole panel. */
const header = () => screen.getByText('Files').parentElement as HTMLElement

function mount(
  dir = DIR,
  onFileOpen: (p: string, d: boolean) => void = vi.fn(),
  onOpenTerminal?: () => void,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <FilesHomePanel projectDir={dir} onFileOpen={onFileOpen} onOpenTerminal={onOpenTerminal} />
    </QueryClientProvider>,
  )
  return { qc, onFileOpen, ...utils }
}

/** Open the header's `Project actions` overflow. Radix opens on keyboard
 *  activation — the path jsdom handles, unlike the PointerEvent mouse open. */
function openProjectActions() {
  const trigger = within(header()).getByLabelText('Project actions')
  fireEvent.keyDown(trigger, { key: 'Enter' })
  return trigger
}

beforeEach(() => {
  H.api.projectTree.mockReset().mockResolvedValue({ root: DIR, paths: [], repo: true })
  H.api.projectGitStatus.mockReset().mockResolvedValue({ repo: true, files: [] })
  H.api.revealPath.mockReset().mockResolvedValue(undefined)
  brandingEnv.directLocal = true
})

describe('FilesHomePanel header', () => {
  it('titles the tab and names the project by its last path segment', () => {
    mount()
    expect(screen.getByText('Files')).toBeInTheDocument()
    expect(screen.getByText('my-project')).toHaveAttribute('title', DIR)
  })

  it('ignores trailing slashes when naming the project', () => {
    mount('/repo/my-project///')
    expect(screen.getByText('my-project')).toBeInTheDocument()
  })

  it('falls back to the whole path when it has no name segment', () => {
    mount('/')
    expect(screen.getByTitle('/')).toHaveTextContent('/')
  })

  it('drops the project name and its actions when no directory is set', () => {
    mount('')
    expect(screen.getByText('Files')).toBeInTheDocument()
    expect(within(header()).queryByLabelText('Refresh')).toBeNull()
    expect(within(header()).queryByLabelText('Show in file manager')).toBeNull()
    expect(H.api.projectTree).not.toHaveBeenCalled()
  })

  it('invalidates both project queries from the refresh button', async () => {
    H.api.projectTree.mockRejectedValue(new Error('not a directory'))
    const { qc } = mount()
    // The retry sits with the error message rather than as a header icon, so the
    // remedy is next to the words describing the problem. Query it fresh at click
    // time: the settling query re-renders and detaches an earlier reference.
    await screen.findByText("Couldn't load the file tree")
    const spy = vi.spyOn(qc, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(spy.mock.calls.map(c => (c[0] as { queryKey: unknown[] }).queryKey)).toEqual([
      ['project-tree', DIR],
      ['git-status', DIR],
    ])
  })

  it('leaves exactly one Refresh control in the view once the rail mounts', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('tree')).toBeInTheDocument())
    expect(within(header()).queryByLabelText('Refresh')).toBeNull()
    expect(screen.getAllByLabelText('Refresh')).toHaveLength(1)
  })

  it('reveals the project directory in the file manager', () => {
    // The button routes through the shared revealOrOpen helper (so a failed
    // reveal surfaces the shared message instead of dead-clicking), which calls
    // api.revealPath with the explicit 'reveal' action. The label is the shared
    // platform-aware wording — generic here, with no gateway platform seeded.
    mount()
    fireEvent.click(within(header()).getByLabelText('Show in file manager'))
    expect(H.api.revealPath).toHaveBeenCalledWith(DIR, 'reveal')
  })

  it('hides the reveal action on a remote session', () => {
    // A remote session cannot drive the operator's file manager, so the button
    // is gated out entirely rather than firing a reveal that degrades to a copy.
    brandingEnv.directLocal = false
    mount()
    expect(within(header()).queryByLabelText('Show in file manager')).toBeNull()
  })
})

describe('FilesHomePanel per-project quick actions', () => {
  it('spawns a terminal in the project directory from the actions menu', async () => {
    // The point of the affordance: a shell already `cd`'d into the project,
    // reachable without knowing that a side-panel tab kind spawns one.
    const onOpenTerminal = vi.fn()
    mount(DIR, vi.fn(), onOpenTerminal)
    openProjectActions()
    fireEvent.click(await screen.findByText('Open terminal in the project directory'))
    expect(onOpenTerminal).toHaveBeenCalledTimes(1)
  })

  it('withholds the terminal action when the host serves no terminal', async () => {
    // No callback = the terminal feature is off or the host withdrew the view.
    // Offering the row anyway would promise a shell that never starts, and with
    // nothing left to hold the trigger goes too rather than opening an empty menu.
    mount(DIR)
    await waitFor(() => expect(screen.getByTestId('tree')).toBeInTheDocument())
    expect(within(header()).queryByLabelText('Project actions')).toBeNull()
  })

  it('says what the action IS, in the menu, not what it avoids', async () => {
    // A first-time reader identified the terminal row correctly and would not
    // click it, so the row has to answer "what is this" without being clicked.
    // The wording states the value: an earlier attempt reassured instead
    // ("Nothing runs until you type") and the same reader read the reassurance
    // as a warning, so the negative framing is pinned OUT here, not just the
    // positive one in.
    mount(DIR, vi.fn(), vi.fn())
    openProjectActions()
    expect(await screen.findByText('A command line that starts in this project’s folder.')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing runs until you type/)).toBeNull()
  })

  it('keeps the header at two controls, with the action inside the menu', async () => {
    // `max-two-buttons-per-row` caps the row. The action is a menu ROW rather
    // than a second button because it carries a one-line explanation, and a 26px
    // icon button has nowhere to put one. Held pending as well as settled, so the
    // assertion covers the in-flight state the old (unreachable) Refresh branch
    // claimed for itself.
    H.api.projectTree.mockReturnValue(new Promise(() => {}))
    mount(DIR, vi.fn(), vi.fn())
    expect(within(header()).getAllByRole('button').map(b => b.getAttribute('aria-label'))).toEqual([
      'Show in file manager', 'Project actions',
    ])
    openProjectActions()
    expect(await screen.findByText('Open terminal in the project directory')).toBeInTheDocument()
  })

  it('offers no header Refresh, whose branch could never render', async () => {
    // With a project directory set, `useTreeState` answers only `ready` or
    // `error` (`ready` covers in-flight on purpose), so the header's old
    // `!treeAvailable && treeState !== 'error'` Refresh was dead in every state.
    // The reachable refreshes are the rail's own and the tree-error state's.
    H.api.projectTree.mockReturnValue(new Promise(() => {}))
    mount(DIR, vi.fn(), vi.fn())
    expect(within(header()).queryByLabelText('Refresh')).toBeNull()
    openProjectActions()
    expect(await screen.findByText('Open terminal in the project directory')).toBeInTheDocument()
    expect(screen.queryByText('Refresh')).toBeNull()
  })

  it('drops the actions menu entirely when no directory is set', () => {
    mount('', vi.fn(), vi.fn())
    expect(within(header()).queryByLabelText('Project actions')).toBeNull()
  })
})

describe('FilesHomePanel tree availability', () => {
  it('mounts the rail and points at it while the tree endpoint answers', async () => {
    mount()
    expect(await screen.findByTestId('tree')).toBeInTheDocument()
    expect(screen.getByText('Select a file from the tree to open it in a new tab')).toBeInTheDocument()
    expect(screen.getByRole('separator')).toBeInTheDocument()
  })

  it('names the FETCH as the failure, not the setting, once the tree endpoint errors', async () => {
    // The directory IS set — the header is naming it — so blaming the setting
    // sends the user to fix something that is already correct. The endpoint
    // refuses for reasons the user cannot see (allow-list refusal, backend
    // hiccup), and retrying is the only remedy they have.
    H.api.projectTree.mockRejectedValue(new Error('not a directory'))
    mount()

    expect(await screen.findByText("Couldn't load the file tree")).toBeInTheDocument()
    expect(screen.queryByText('No project directory is set for this chat')).toBeNull()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(screen.queryByTestId('tree')).toBeNull()
    expect(screen.queryByRole('separator')).toBeNull()
  })

  it('still leaves exactly one Refresh control in the errored view', async () => {
    // The header icon yields to the error state's labelled button; two controls
    // with the same accessible name in one view is the collision this avoids.
    H.api.projectTree.mockRejectedValue(new Error('not a directory'))
    mount()
    await screen.findByText("Couldn't load the file tree")
    expect(screen.getAllByRole('button', { name: 'Refresh' })).toHaveLength(1)
  })

  it('shows no rail at all without a project directory', async () => {
    mount('')
    expect(await screen.findByText('No project directory is set for this chat')).toBeInTheDocument()
    expect(screen.queryByTestId('tree')).toBeNull()
  })

  it('spawns a tab rather than opening inline when a tree row is opened', async () => {
    const onFileOpen = vi.fn()
    mount(DIR, onFileOpen)
    fireEvent.click(await screen.findByTestId('tree'))
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, false)
    // Still the empty preview pane: this tab never renders the file itself.
    expect(screen.getByText('Select a file from the tree to open it in a new tab')).toBeInTheDocument()
  })

  it('probes under the tree component\'s own query key, so the probe costs no extra request', async () => {
    const { qc } = mount()
    await screen.findByTestId('tree')
    const keys = qc.getQueryCache().findAll({ queryKey: ['project-tree'] }).map(q => q.queryKey)
    expect(keys).toEqual([['project-tree', DIR]])
    expect(H.api.projectTree).toHaveBeenCalledTimes(1)
  })
})
