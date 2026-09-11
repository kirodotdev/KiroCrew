/**
 * The chat side panel's file-browser rail.
 *
 * Pins the parts of the rail that no other suite owns: the All/Changed segment
 * (whose mode is MODULE-level session state, so it must survive a remount), the
 * always-open search field feeding the tree's search session, the refresh
 * escape hatch, and the grip's clamp + persistence. The Pierre tree is replaced
 * by a probe that echoes the props it was handed, so "what the rail tells the
 * tree" is assertable without loading the trees runtime.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  render, screen, waitFor, fireEvent, within, cleanup, renderHook,
} from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const H = vi.hoisted(() => ({
  OPENED: '/repo/src/a.ts',
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    uploadToDirectory: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

vi.mock('../pierre/tree', () => ({
  TreeSkeleton: () => null,
  PierreWorkspaceTree: (p: {
    mode?: string
    projectDir: string
    searchQuery?: string | null
    selectedPath?: string | null
    onFileOpen?: (abs: string) => void
    onUploadRequest?: (absDir: string) => void
  }) => (
    <>
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
      {/* Stands in for a directory row's "Upload files…" context-menu action:
          PierreWorkspaceTreeImpl.test.tsx pins that click wired to this same
          callback, so this probe only needs to pin that FileBrowserRail
          forwards it and reacts correctly when it fires. */}
      <button
        data-testid="upload-request"
        onClick={() => p.onUploadRequest?.(`${p.projectDir}/target-dir`)}
      >
        upload-request
      </button>
    </>
  ),
}))

import FileBrowserRail, { useTreeAvailable } from '../pages/chat/FileBrowserRail'

const RAIL_W_KEY = 'mc-files-rail-w'
const DIR = '/repo'

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function mount(props: { onFileOpen?: (p: string, d: boolean) => void; selectedPath?: string | null } = {}) {
  const qc = newClient()
  const onFileOpen = props.onFileOpen ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <FileBrowserRail projectDir={DIR} onFileOpen={onFileOpen} selectedPath={props.selectedPath} />
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
  // The All/Changed mode lives in a module-level variable so in-place tab
  // navigation (which remounts the rail) keeps it — which also means a prior
  // test's toggle survives into this one. Drive it back to All explicitly.
  mount()
  fireEvent.click(screen.getByLabelText('All files'))
  cleanup()
  H.api.projectTree.mockClear()
  H.api.projectGitStatus.mockClear()
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
})

describe('FileBrowserRail file opens', () => {
  it('opens a plain file in All mode and drops the query', () => {
    const onFileOpen = vi.fn()
    mount({ onFileOpen })
    fireEvent.change(screen.getByLabelText('Filter files…'), { target: { value: 'a.ts' } })
    fireEvent.click(tree())
    expect(onFileOpen).toHaveBeenCalledWith(H.OPENED, false)
    expect(tree()).toHaveAttribute('data-query', '')
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

function fileTransfer(name = 'a.txt'): DataTransfer {
  const file = new File(['x'], name, { type: 'text/plain' })
  return {
    types: ['Files'],
    items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
    files: [file],
    dropEffect: 'none',
  } as unknown as DataTransfer
}

describe('FileBrowserRail upload', () => {
  beforeEach(() => {
    H.api.uploadToDirectory.mockReset().mockResolvedValue({ ok: true, path: `${DIR}/a.txt`, name: 'a.txt' })
  })

  const railEl = () => rail()

  it('shows the drop overlay for an OS file drag over the rail', () => {
    mount()
    fireEvent.dragEnter(railEl(), { dataTransfer: fileTransfer() })
    expect(screen.getByText('Drop files here to upload')).toBeInTheDocument()
  })

  it('uploads a pane-wide drop (no specific row) into the tree root', async () => {
    mount()
    fireEvent.drop(railEl(), { dataTransfer: fileTransfer() })

    await waitFor(() => expect(H.api.uploadToDirectory).toHaveBeenCalledWith(DIR, expect.any(File)))
  })

  it('resolves a drop landing on a real tree row to that row\'s directory', async () => {
    // The mocked tree in this file has no rows; this pins that the rail reads
    // whatever `data-item-path`/`data-item-type` attributes the ACTUAL event
    // target carries, regardless of what mounted it — PierreWorkspaceTreeImpl
    // renders those attributes for real (see resolveUploadTargetDir.test.ts).
    mount()
    const row = document.createElement('div')
    row.setAttribute('data-item-path', 'src')
    row.setAttribute('data-item-type', 'folder')
    tree().appendChild(row)

    fireEvent.drop(row, { dataTransfer: fileTransfer() })

    await waitFor(() => expect(H.api.uploadToDirectory).toHaveBeenCalledWith(`${DIR}/src`, expect.any(File)))
  })

  it('forwards onUploadRequest to the tree and uploads the picker selection into that directory', async () => {
    mount()
    fireEvent.click(screen.getByTestId('upload-request'))
    const input = screen.getByLabelText('Upload files…') as HTMLInputElement
    const file = new File(['y'], 'picked.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(H.api.uploadToDirectory).toHaveBeenCalledWith(`${DIR}/target-dir`, file))
  })

  it('shows an upload failure inline and lets it be dismissed', async () => {
    H.api.uploadToDirectory.mockResolvedValue({ ok: false, status: 400, code: 'unsupported_file_type', error: 'Unsupported file type: .exe' })
    mount()

    fireEvent.drop(railEl(), { dataTransfer: fileTransfer('a.exe') })

    const notice = await screen.findByTestId('file-browser-rail-upload-error')
    expect(notice).toHaveTextContent('Unsupported file type: .exe')

    fireEvent.click(within(notice).getByLabelText('Dismiss'))
    expect(screen.queryByTestId('file-browser-rail-upload-error')).toBeNull()
  })
})

describe('FileBrowserRail refresh', () => {
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
    ])
    await waitFor(() => expect(btn).toBeDisabled())
    expect(btn.querySelector('svg')?.getAttribute('class')).toContain('animate-spin')

    releases.forEach(r => r())
    await waitFor(() => expect(btn).not.toBeDisabled())
    expect(btn.querySelector('svg')?.getAttribute('class')).not.toContain('animate-spin')
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

describe('useTreeAvailable', () => {
  const wrapper = (qc: QueryClient) => ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )

  it('reports available while the tree endpoint answers', async () => {
    const qc = newClient()
    const { result } = renderHook(() => useTreeAvailable(DIR), { wrapper: wrapper(qc) })
    await waitFor(() =>
      expect(qc.getQueryState(['project-tree', DIR])?.status).toBe('success'))
    expect(result.current).toBe(true)
    expect(H.api.projectTree).toHaveBeenCalledWith(DIR)
  })

  it('reports unavailable once the tree endpoint errors', async () => {
    H.api.projectTree.mockRejectedValue(new Error('not a directory'))
    const { result } = renderHook(() => useTreeAvailable(DIR), { wrapper: wrapper(newClient()) })
    await waitFor(() => expect(result.current).toBe(false))
  })

  it('never probes without a project directory', () => {
    const { result } = renderHook(() => useTreeAvailable(null), { wrapper: wrapper(newClient()) })
    expect(result.current).toBe(false)
    expect(H.api.projectTree).not.toHaveBeenCalled()
  })
})
