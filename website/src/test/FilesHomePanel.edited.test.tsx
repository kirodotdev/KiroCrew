/**
 * The Files tab's Edited view: the files this chat edited, read from the
 * server's session-files listing, with the tree read only in the Browse view.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const H = vi.hoisted(() => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    revealPath: vi.fn(),
    sessionFiles: vi.fn(),
    sessionFileDiff: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api, ApiError: class ApiError extends Error {} }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: false, desktopLauncher: false }),
}))
vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: () => <div data-testid="tree">tree</div>,
}))

import FilesHomePanel, { __resetFilesViewForTests } from '../pages/chat/FilesHomePanel'

const DIR = '/home/u/proj'

function mount(slot: string | null = 's1', onFileOpen = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <FilesHomePanel projectDir={DIR} slot={slot} onFileOpen={onFileOpen} />
    </QueryClientProvider>,
  )
  return { onFileOpen }
}

beforeEach(() => {
  __resetFilesViewForTests()
  H.api.projectTree.mockReset().mockResolvedValue({ root: DIR, paths: [], repo: false })
  H.api.projectGitStatus.mockReset().mockResolvedValue({ files: [] })
  H.api.sessionFiles.mockReset().mockResolvedValue({
    files: [
      { path: `${DIR}/src/new.ts`, ts: '2026-01-01T00:00:00Z', exists: true },
      { path: '/elsewhere/notes.md', ts: '2026-01-01T00:00:00Z', exists: true },
      { path: `${DIR}/old.txt`, ts: '2026-01-01T00:00:00Z', exists: false },
    ],
    truncated: false,
  })
})

describe('FilesHomePanel Edited view', () => {
  it('lists the edited files newest first and does not read the tree', async () => {
    mount()
    const rows = await screen.findAllByTestId('session-file-row')
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('new.ts'),
      expect.stringContaining('notes.md'),
      expect.stringContaining('old.txt'),
    ])
    // Inside the project the folder is relative; outside it is absolute.
    expect(rows[0]).toHaveTextContent('src')
    expect(rows[1]).toHaveTextContent('/elsewhere')
    expect(H.api.sessionFiles).toHaveBeenCalledWith('s1')
    expect(H.api.projectTree).not.toHaveBeenCalled()
    // The count sits on the view switch.
    expect(screen.getByTestId('files-view-edited')).toHaveTextContent('3')
  })

  it('opens a row, opens its diff, and marks a deleted file with no diff control', async () => {
    const { onFileOpen } = mount()
    const rows = await screen.findAllByTestId('session-file-row')
    fireEvent.click(within(rows[0]).getByTitle(`${DIR}/src/new.ts`))
    expect(onFileOpen).toHaveBeenLastCalledWith(`${DIR}/src/new.ts`, false)
    // The diff control carries a word, not only the glyph.
    expect(within(rows[0]).getByTestId('session-file-diff')).toHaveTextContent('Show changes')
    fireEvent.click(within(rows[0]).getByTestId('session-file-diff'))
    expect(onFileOpen).toHaveBeenLastCalledWith(`${DIR}/src/new.ts`, true)
    expect(rows[2]).toHaveTextContent('Deleted')
    expect(within(rows[2]).queryByTestId('session-file-diff')).toBeNull()
  })

  it('switches to Browse, which reads the tree, and back', async () => {
    mount()
    await screen.findAllByTestId('session-file-row')
    fireEvent.click(screen.getByTestId('files-view-browse'))
    await waitFor(() => expect(H.api.projectTree).toHaveBeenCalledWith(DIR))
    expect(await screen.findByTestId('tree')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('files-view-edited'))
    expect(await screen.findAllByTestId('session-file-row')).toHaveLength(3)
  })

  it('says how the list fills when nothing was edited, and offers Browse', async () => {
    H.api.sessionFiles.mockResolvedValue({ files: [], truncated: false })
    mount()
    expect(await screen.findByText('No files edited in this chat yet')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Browse the project folder'))
    expect(await screen.findByTestId('tree')).toBeInTheDocument()
  })

  it('says edits were withheld, not that none were made, when every one was', async () => {
    H.api.sessionFiles.mockResolvedValue({ files: [], truncated: false, withheld: true })
    mount()
    expect(await screen.findByTestId('session-files-withheld')).toHaveTextContent("Some edited files can't be listed here")
    expect(screen.queryByTestId('session-files-empty')).toBeNull()
  })

  it('says withheld edits apart from the cap', async () => {
    H.api.sessionFiles.mockResolvedValue({
      files: [{ path: `${DIR}/a.ts`, ts: '', exists: true }],
      truncated: false,
      withheld: true,
    })
    mount()
    expect(await screen.findByTestId('session-files-withheld-note')).toBeInTheDocument()
    expect(screen.queryByTestId('session-files-truncated')).toBeNull()
  })

  it('shows the capped note', async () => {
    H.api.sessionFiles.mockResolvedValue({
      files: [{ path: `${DIR}/a.ts`, ts: '', exists: true }],
      truncated: true,
    })
    mount()
    expect(await screen.findByTestId('session-files-truncated')).toBeInTheDocument()
  })

  it('without a slot shows only the tree', async () => {
    mount(null)
    expect(await screen.findByTestId('tree')).toBeInTheDocument()
    expect(screen.queryByTestId('files-view-edited')).toBeNull()
    expect(H.api.sessionFiles).not.toHaveBeenCalled()
  })
})

describe('FilesHomePanel view choice', () => {
  it('outlives a remount within the page session', async () => {
    const { unmount } = render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <FilesHomePanel projectDir={DIR} slot="s1" onFileOpen={vi.fn()} />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByTestId('files-view-browse'))
    unmount()
    mount()
    expect(await screen.findByTestId('tree')).toBeInTheDocument()
  })

  it('shows the load failure with a Retry instead of an empty list', async () => {
    H.api.sessionFiles.mockRejectedValue(new Error('503'))
    mount()
    expect(await screen.findByTestId('session-files-error')).toBeInTheDocument()
    expect(screen.queryByTestId('session-files-empty')).toBeNull()
  })
})

describe('FilesHomePanel Edited view: Show changes', () => {
  // The chat's own recorded edit, not git's HEAD: outside a repository, or
  // once the edit is committed, a working-tree diff has nothing to show.
  const FILE = `${DIR}/src/new.ts`
  function mountWithDiff() {
    const onFileOpen = vi.fn()
    const onOpenDiff = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <FilesHomePanel projectDir={DIR} slot="s1" onFileOpen={onFileOpen} onOpenDiff={onOpenDiff} />
      </QueryClientProvider>,
    )
    return { onFileOpen, onOpenDiff }
  }
  async function clickShowChanges() {
    const rows = await screen.findAllByTestId('session-file-row')
    // The click starts a request: settle it inside act, so the state it sets
    // lands before the test asserts.
    await act(async () => { fireEvent.click(within(rows[0]).getByTestId('session-file-diff')) })
  }

  it('opens the recorded before and after as a diff tab', async () => {
    H.api.sessionFileDiff.mockReset().mockResolvedValue({ path: FILE, available: true, before: 'a\n', after: 'b\n' })
    const { onFileOpen, onOpenDiff } = mountWithDiff()
    await clickShowChanges()
    await waitFor(() => expect(onOpenDiff).toHaveBeenCalledWith(FILE, 'b\n', 'a\n'))
    expect(H.api.sessionFileDiff).toHaveBeenCalledWith('s1', FILE)
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('opens a new or unchanged file as itself, as the transcript card does', async () => {
    H.api.sessionFileDiff.mockReset().mockResolvedValue({ path: FILE, available: true, before: '', after: 'new\n' })
    const { onFileOpen, onOpenDiff } = mountWithDiff()
    await clickShowChanges()
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledWith(FILE, false))
    expect(onOpenDiff).not.toHaveBeenCalled()
  })

  it('shows the working-tree diff when no whole pair is recorded', async () => {
    H.api.sessionFileDiff.mockReset().mockResolvedValue({ path: FILE, available: false, before: '', after: '' })
    const first = mountWithDiff()
    await clickShowChanges()
    await waitFor(() => expect(first.onFileOpen).toHaveBeenCalledWith(FILE, true))
    expect(first.onOpenDiff).not.toHaveBeenCalled()
  })

  it('says the record could not be read instead of opening a different diff', async () => {
    H.api.sessionFileDiff.mockReset().mockRejectedValue(new Error('503'))
    const { onFileOpen, onOpenDiff } = mountWithDiff()
    await clickShowChanges()
    const notice = await screen.findByTestId('session-file-diff-error')
    expect(notice).toHaveTextContent("Couldn't load this chat's change to new.ts")
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(onOpenDiff).not.toHaveBeenCalled()
    // A later click that succeeds clears it.
    H.api.sessionFileDiff.mockResolvedValue({ path: FILE, available: true, before: 'a\n', after: 'b\n' })
    await clickShowChanges()
    await waitFor(() => expect(onOpenDiff).toHaveBeenCalledWith(FILE, 'b\n', 'a\n'))
    await waitFor(() => expect(screen.queryByTestId('session-file-diff-error')).toBeNull())
  })

  it('ignores a failure that lands after a later click', async () => {
    let failFirst: (reason: unknown) => void = () => {}
    H.api.sessionFileDiff.mockReset()
      .mockReturnValueOnce(new Promise((_resolve, reject) => { failFirst = reject }))
      .mockResolvedValueOnce({ path: FILE, available: true, before: 'a\n', after: 'b\n' })
    const { onOpenDiff } = mountWithDiff()
    await clickShowChanges()
    await clickShowChanges()
    await waitFor(() => expect(onOpenDiff).toHaveBeenCalledWith(FILE, 'b\n', 'a\n'))
    failFirst(new Error('503'))
    await waitFor(() => expect(H.api.sessionFileDiff).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('session-file-diff-error')).toBeNull()
  })
})
