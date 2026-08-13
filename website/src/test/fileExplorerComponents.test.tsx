/**
 * Component rendering tests for the file-explorer builtin app.
 *
 * Follows the same pattern as ResearchLabPage.test.tsx:
 * mock the api client, wrap in QueryClient + MemoryRouter, render, assert.
 *
 * Note: FileViewer is NOT tested here because it imports MarkdownRenderer
 * which transitively loads highlight.js + mermaid (5s timeout in vitest).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('@radix-ui/react-context-menu', async () => await import('./__mocks__/@radix-ui/react-context-menu'))
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

// Polyfill ResizeObserver for jsdom (TabStrip uses it for scroll fade detection)
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

vi.mock('../apps/file-explorer/api', () => ({
  fileExplorerApi: {
    health: vi.fn(),
    tree: vi.fn(),
    read: vi.fn(),
    search: vi.fn(),
    gitStatus: vi.fn(),
    resolve: vi.fn(),
    complete: vi.fn(),
  },
}))

// Mock MarkdownRenderer to avoid heavy deps (highlight.js, mermaid)
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <pre data-testid="md-renderer">{content}</pre>,
}))

import { fileExplorerApi } from '../apps/file-explorer/api'
import FileExplorerPage from '../apps/file-explorer/FileExplorerPage'
import TreeNode from '../apps/file-explorer/TreeNode'
import SearchPanel from '../apps/file-explorer/SearchPanel'
import TabStrip from '../apps/file-explorer/TabStrip'
import PathBar from '../apps/file-explorer/PathBar'
import type { TreeEntry, FolderTab, FileTab, GitInfo } from '../apps/file-explorer/types'

function renderPage(store = createTestStore()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/file-explorer']}>
          <FileExplorerPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...utils, store }
}

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const TREE_ENTRIES: TreeEntry[] = [
  { name: 'src', path: '/home/user/src', type: 'dir', size: 0, mtime: 1700000000 },
  { name: 'README.md', path: '/home/user/README.md', type: 'file', size: 1024, mtime: 1700000000 },
  { name: 'package.json', path: '/home/user/package.json', type: 'file', size: 512, mtime: 1700000000 },
]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

// ─── FileExplorerPage integration ───────────────────────────────────────────

describe('FileExplorerPage', () => {
  it('shows loading skeleton initially', () => {
    vi.mocked(fileExplorerApi.health).mockReturnValue(new Promise(() => {})) // never resolves
    renderPage()
    expect(document.querySelector('.mc-fe-root')).toBeInTheDocument()
  })

  it('renders tree after health + tree data loads', async () => {
    vi.mocked(fileExplorerApi.health).mockResolvedValue({ allowedRoots: ['/home/user'] })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: TREE_ENTRIES })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    renderPage()
    await waitFor(() => expect(screen.getByText('src')).toBeInTheDocument())
    expect(screen.getByText('README.md')).toBeInTheDocument()
    expect(screen.getByText('package.json')).toBeInTheDocument()
  })

  it('"Chat about this file" hands the prompt to chat via Redux pendingInput', async () => {
    vi.mocked(fileExplorerApi.health).mockResolvedValue({ allowedRoots: ['/home/user'] })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: TREE_ENTRIES })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    const { store } = renderPage()
    await waitFor(() => expect(screen.getByText('README.md')).toBeInTheDocument())

    // Right-click the file to open the context menu, then click the chat row.
    fireEvent.contextMenu(screen.getByText('README.md'))
    const rows = screen.getAllByRole('menuitem')
    const chatRow = rows.find(r => r.textContent?.includes('Chat about this'))
    expect(chatRow).toBeTruthy()
    fireEvent.click(chatRow!)

    // The prompt is delivered through Redux pendingInput (ChatPage's prefill
    // contract), NOT an unconsumed ?message= URL param.
    const pending = store.getState().chat.pendingInput
    expect(pending).toContain('/home/user/README.md')
    expect(pending).toContain('file')
  })

  it('shows skeleton when health has not resolved', async () => {
    vi.mocked(fileExplorerApi.health).mockRejectedValue(new Error('connection refused'))
    renderPage()
    // Component stays in loading/uninitialized state — shows the root div but no tree
    await waitFor(() => expect(document.querySelector('.mc-fe-root')).toBeInTheDocument())
    expect(screen.queryByText('src')).not.toBeInTheDocument()
  })

  it('opens at the backend-reported home dir, not the shortest root', async () => {
    // macOS shape: /opt is the shortest allowed root; home must still win.
    vi.mocked(fileExplorerApi.health).mockResolvedValue({
      allowedRoots: ['/Users/u', '/private/tmp', '/home', '/opt'],
      home: '/Users/u',
    })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: [] })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    renderPage()
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/Users/u', 2))
  })

  it('recognizes a macOS /Users home from an older backend without `home`', async () => {
    // Regression: the old heuristic matched only '/home/' and fell back to
    // the shortest root, which opened /opt on macOS.
    vi.mocked(fileExplorerApi.health).mockResolvedValue({
      allowedRoots: ['/Users/u', '/private/tmp', '/home', '/opt'],
    })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: [] })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    renderPage()
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/Users/u', 2))
  })

  it('still opens the Linux /home/<user> root without `home`', async () => {
    vi.mocked(fileExplorerApi.health).mockResolvedValue({
      allowedRoots: ['/home/user', '/tmp', '/home', '/opt'],
    })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: [] })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    renderPage()
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user', 2))
  })

  it('ignores a `home` that is not an allowed root and uses roots[0]', async () => {
    vi.mocked(fileExplorerApi.health).mockResolvedValue({
      allowedRoots: ['/tmp', '/opt'],
      home: '/nonexistent',
    })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: [] })
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
    renderPage()
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/tmp', 2))
  })
})

// ─── TreeNode ───────────────────────────────────────────────────────────────

describe('TreeNode', () => {
  const dirNode: TreeEntry = { name: 'src', path: '/home/user/src', type: 'dir', children: [
    { name: 'index.ts', path: '/home/user/src/index.ts', type: 'file' },
  ]}
  const fileNode: TreeEntry = { name: 'app.py', path: '/home/user/app.py', type: 'file' }

  it('renders directory with chevron and folder icon', () => {
    renderWithQuery(
      <TreeNode node={dirNode} depth={0} expanded={{}} toggleExpand={vi.fn()} selectedPath="" onSelect={vi.fn()} gitMap={new Map()} />,
    )
    expect(screen.getByText('src')).toBeInTheDocument()
  })

  it('renders file without chevron', () => {
    renderWithQuery(
      <TreeNode node={fileNode} depth={0} expanded={{}} toggleExpand={vi.fn()} selectedPath="" onSelect={vi.fn()} gitMap={new Map()} />,
    )
    expect(screen.getByText('app.py')).toBeInTheDocument()
  })

  it('calls toggleExpand when directory is clicked', async () => {
    const toggle = vi.fn()
    renderWithQuery(
      <TreeNode node={dirNode} depth={0} expanded={{}} toggleExpand={toggle} selectedPath="" onSelect={vi.fn()} gitMap={new Map()} />,
    )
    await userEvent.click(screen.getByText('src'))
    expect(toggle).toHaveBeenCalledWith(dirNode)
  })

  it('calls onSelect when file is clicked', async () => {
    const select = vi.fn()
    renderWithQuery(
      <TreeNode node={fileNode} depth={0} expanded={{}} toggleExpand={vi.fn()} selectedPath="" onSelect={select} gitMap={new Map()} />,
    )
    await userEvent.click(screen.getByText('app.py'))
    expect(select).toHaveBeenCalledWith(fileNode)
  })

  it('shows children when expanded', () => {
    renderWithQuery(
      <TreeNode node={dirNode} depth={0} expanded={{ '/home/user/src': true }} toggleExpand={vi.fn()} selectedPath="" onSelect={vi.fn()} gitMap={new Map()} />,
    )
    expect(screen.getByText('index.ts')).toBeInTheDocument()
  })

  it('displays git badge when file is modified', () => {
    const gitMap = new Map<string, GitInfo>([
      ['/home/user', { repoRoot: '/home/user', branch: 'main', statuses: { 'app.py': 'M' } }],
    ])
    renderWithQuery(
      <TreeNode node={fileNode} depth={0} expanded={{}} toggleExpand={vi.fn()} selectedPath="" onSelect={vi.fn()} gitMap={gitMap} />,
    )
    expect(screen.getByTitle('git: M')).toBeInTheDocument()
  })

  it('highlights selected file', () => {
    renderWithQuery(
      <TreeNode node={fileNode} depth={0} expanded={{}} toggleExpand={vi.fn()} selectedPath="/home/user/app.py" onSelect={vi.fn()} gitMap={new Map()} />,
    )
    expect(document.querySelector('.is-selected')).toBeInTheDocument()
  })
})

// ─── SearchPanel ────────────────────────────────────────────────────────────

describe('SearchPanel', () => {
  it('renders search input and close button', () => {
    renderWithQuery(
      <SearchPanel rootPath="/home/user" onClose={vi.fn()} onJump={vi.fn()} />,
    )
    expect(screen.getByPlaceholderText(/Search in/)).toBeInTheDocument()
    expect(screen.getByLabelText('Close search')).toBeInTheDocument()
  })

  it('calls onClose when close button clicked', async () => {
    const close = vi.fn()
    renderWithQuery(<SearchPanel rootPath="/home/user" onClose={close} onJump={vi.fn()} />)
    await userEvent.click(screen.getByLabelText('Close search'))
    expect(close).toHaveBeenCalled()
  })

  it('shows results after search', async () => {
    vi.mocked(fileExplorerApi.search).mockResolvedValue({
      results: [{ file: '/home/user/src/main.ts', line: 10, col: 5, preview: 'const foo = bar' }],
      engine: 'rg',
      truncated: false,
    })
    renderWithQuery(<SearchPanel rootPath="/home/user" onClose={vi.fn()} onJump={vi.fn()} />)
    const input = screen.getByPlaceholderText(/Search in/)
    await userEvent.type(input, 'foo')
    await waitFor(() => expect(screen.getByText('const foo = bar')).toBeInTheDocument())
    expect(screen.getByText('src/main.ts')).toBeInTheDocument()
  })

  it('calls onJump when result clicked', async () => {
    vi.mocked(fileExplorerApi.search).mockResolvedValue({
      results: [{ file: '/home/user/test.py', line: 5, col: 1, preview: 'import os' }],
      engine: 'python',
      truncated: false,
    })
    const jump = vi.fn()
    renderWithQuery(<SearchPanel rootPath="/home/user" onClose={vi.fn()} onJump={jump} />)
    await userEvent.type(screen.getByPlaceholderText(/Search in/), 'import')
    await waitFor(() => expect(screen.getByText('import os')).toBeInTheDocument())
    await userEvent.click(screen.getByText('import os'))
    expect(jump).toHaveBeenCalledWith(expect.objectContaining({ file: '/home/user/test.py', line: 5 }))
  })
})

// ─── TabStrip ───────────────────────────────────────────────────────────────

describe('TabStrip', () => {
  const folders: FolderTab[] = [
    { id: 'ft-1', rootPath: '/home/user', label: 'Home', expanded: {}, showSearch: false },
    { id: 'ft-2', rootPath: '/tmp', label: '', expanded: {}, showSearch: false },
  ]
  const files: FileTab[] = [
    { id: 'of-1', path: '/home/user/main.ts', folderId: 'ft-1' },
  ]

  it('renders folder tabs with labels', () => {
    render(
      <TabStrip folderTabs={folders} fileTabs={files} activeFolderId="ft-1" activeFileId={null}
        onActivateFolder={vi.fn()} onActivateFile={vi.fn()} onCloseFolder={vi.fn()}
        onCloseFile={vi.fn()} onNewFolder={vi.fn()} onRenameFolder={vi.fn()} />,
    )
    expect(screen.getByText('Home')).toBeInTheDocument()
    expect(screen.getByText('tmp')).toBeInTheDocument() // basename of /tmp
  })

  it('renders file tabs', () => {
    render(
      <TabStrip folderTabs={folders} fileTabs={files} activeFolderId="ft-1" activeFileId="of-1"
        onActivateFolder={vi.fn()} onActivateFile={vi.fn()} onCloseFolder={vi.fn()}
        onCloseFile={vi.fn()} onNewFolder={vi.fn()} onRenameFolder={vi.fn()} />,
    )
    expect(screen.getByText('main.ts')).toBeInTheDocument()
  })

  it('calls onActivateFolder when folder tab clicked', async () => {
    const activate = vi.fn()
    render(
      <TabStrip folderTabs={folders} fileTabs={[]} activeFolderId="ft-1" activeFileId={null}
        onActivateFolder={activate} onActivateFile={vi.fn()} onCloseFolder={vi.fn()}
        onCloseFile={vi.fn()} onNewFolder={vi.fn()} onRenameFolder={vi.fn()} />,
    )
    await userEvent.click(screen.getByText('tmp'))
    expect(activate).toHaveBeenCalledWith('ft-2')
  })

  it('calls onNewFolder when + button clicked', async () => {
    const newFolder = vi.fn()
    render(
      <TabStrip folderTabs={folders} fileTabs={[]} activeFolderId="ft-1" activeFileId={null}
        onActivateFolder={vi.fn()} onActivateFile={vi.fn()} onCloseFolder={vi.fn()}
        onCloseFile={vi.fn()} onNewFolder={newFolder} onRenameFolder={vi.fn()} />,
    )
    await userEvent.click(screen.getByLabelText('New workspace tab'))
    expect(newFolder).toHaveBeenCalled()
  })
})

// ─── PathBar ────────────────────────────────────────────────────────────────

describe('PathBar', () => {
  it('renders breadcrumb segments', () => {
    renderWithQuery(
      <PathBar rootPath="/home/user/project" gitInfo={null} onChangeRoot={vi.fn()} onNavigate={vi.fn()} />,
    )
    expect(screen.getByText('home')).toBeInTheDocument()
    expect(screen.getByText('user')).toBeInTheDocument()
    expect(screen.getByText('project')).toBeInTheDocument()
  })

  it('shows git branch when gitInfo provided', () => {
    const gitInfo: GitInfo = { repoRoot: '/home/user/project', branch: 'feat/cool', statuses: { 'x.ts': 'M' } }
    renderWithQuery(
      <PathBar rootPath="/home/user/project" gitInfo={gitInfo} onChangeRoot={vi.fn()} onNavigate={vi.fn()} />,
    )
    expect(screen.getByText('feat/cool')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument() // change count
  })

  it('enters edit mode on click and shows input', async () => {
    renderWithQuery(
      <PathBar rootPath="/home/user" gitInfo={null} onChangeRoot={vi.fn()} onNavigate={vi.fn()} />,
    )
    await userEvent.click(screen.getByTitle('Click to edit path'))
    expect(screen.getByPlaceholderText('/path/to/folder')).toBeInTheDocument()
  })
})
