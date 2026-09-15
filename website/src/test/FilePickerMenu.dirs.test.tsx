import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu, { resultKind, selectionFor, makeRelative } from '../components/FilePickerMenu'

const ROOT = '/repo'
const NOW = Math.floor(Date.now() / 1000)

const RESULTS = [
  { path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: NOW - 120, kind: 'dir' as const },
  { path: '/repo/src/widgets.ts', name: 'widgets.ts', size: 2048, mtime: NOW - 120, kind: 'file' as const },
]

function Harness({
  query, open, onSelect = vi.fn(), onClose = vi.fn(), onFileOpen,
}: {
  query: string
  open: boolean
  onSelect?: (i: { path: string; relativePath: string; kind: 'file' | 'dir' }) => void
  onClose?: () => void
  onFileOpen?: (p: string) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <FilePickerMenu
          query={query}
          anchorRef={ref}
          open={open}
          onSelect={onSelect}
          onClose={onClose}
          onFileOpen={onFileOpen}
        />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.fileSearch.mockResolvedValue({ results: RESULTS, root: ROOT })
})

describe('selectionFor', () => {
  it('appends a trailing slash to directory relative paths', () => {
    expect(selectionFor(RESULTS[0], ROOT)).toEqual({
      path: '/repo/src/widgets',
      relativePath: 'src/widgets/',
      kind: 'dir',
    })
  })

  it('leaves file relative paths untouched', () => {
    expect(selectionFor(RESULTS[1], ROOT)).toEqual({
      path: '/repo/src/widgets.ts',
      relativePath: 'src/widgets.ts',
      kind: 'file',
    })
  })

  it('does not double up an existing trailing slash', () => {
    const withSlash = { ...RESULTS[0], path: '/repo/src/widgets/' }
    expect(selectionFor(withSlash, ROOT).relativePath).toBe('src/widgets/')
  })

  it('falls back to the absolute path when no root is known', () => {
    expect(selectionFor(RESULTS[0], '').relativePath).toBe('/repo/src/widgets/')
  })
})

describe('resultKind', () => {
  it('treats a missing kind as file for backward compatibility', () => {
    expect(resultKind({})).toBe('file')
  })

  it('reads an explicit dir kind', () => {
    expect(resultKind({ kind: 'dir' })).toBe('dir')
  })
})

describe('FilePickerMenu directory rows', () => {
  it('renders a directory name with a trailing slash', async () => {
    render(<Harness query="widgets" open />)
    expect(await screen.findByText('widgets/')).toBeInTheDocument()
    expect(screen.getByText('widgets.ts')).toBeInTheDocument()
  })

  it('marks rows with their kind', async () => {
    render(<Harness query="widgets" open />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    const kinds = screen.getAllByRole('option').map(o => o.getAttribute('data-kind'))
    expect(kinds).toContain('dir')
    expect(kinds).toContain('file')
  })

  it('shows a folder label instead of a byte size for directories', async () => {
    render(<Harness query="widgets" open />)
    expect(await screen.findByText(/^folder · /)).toBeInTheDocument()
    expect(screen.getByText(/^2kB · /)).toBeInTheDocument()
  })

  it('renders a Folder icon for directories', async () => {
    render(<Harness query="widgets" open />)
    expect(await screen.findByLabelText('Folder')).toBeInTheDocument()
  })

  it('suppresses the preview button on directory rows', async () => {
    const onFileOpen = vi.fn()
    render(<Harness query="widgets" open onFileOpen={onFileOpen} />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    // One eye button only: the file row. The dir row has nothing to preview.
    expect(screen.getAllByLabelText('Open in viewer')).toHaveLength(1)
  })

  it('emits kind=dir with a trailing-slash relative path on click', async () => {
    const onSelect = vi.fn()
    render(<Harness query="widgets" open onSelect={onSelect} />)
    const dirRow = await waitFor(() => {
      const row = screen.getAllByRole('option').find(o => o.getAttribute('data-kind') === 'dir')
      expect(row).toBeTruthy()
      return row!
    })
    fireEvent.mouseDown(dirRow)
    expect(onSelect).toHaveBeenCalledWith({
      path: '/repo/src/widgets',
      relativePath: 'src/widgets/',
      kind: 'dir',
    })
  })

  it('emits kind=file for file rows', async () => {
    const onSelect = vi.fn()
    render(<Harness query="widgets" open onSelect={onSelect} />)
    const fileRow = await waitFor(() => {
      const row = screen.getAllByRole('option').find(o => o.getAttribute('data-kind') === 'file')
      expect(row).toBeTruthy()
      return row!
    })
    fireEvent.mouseDown(fileRow)
    expect(onSelect).toHaveBeenCalledWith({
      path: '/repo/src/widgets.ts',
      relativePath: 'src/widgets.ts',
      kind: 'file',
    })
  })

  it('treats a result with no kind field as a file', async () => {
    mockApi.fileSearch.mockResolvedValue({
      results: [{ path: '/repo/legacy.ts', name: 'legacy.ts', size: 10, mtime: NOW }],
      root: ROOT,
    })
    const onSelect = vi.fn()
    render(<Harness query="legacy" open onSelect={onSelect} />)
    const row = await waitFor(() => {
      const r = screen.getAllByRole('option')[0]
      expect(r).toBeTruthy()
      return r
    })
    expect(row.getAttribute('data-kind')).toBe('file')
    fireEvent.mouseDown(row)
    expect(onSelect).toHaveBeenCalledWith({
      path: '/repo/legacy.ts',
      relativePath: 'legacy.ts',
      kind: 'file',
    })
  })
})

describe('makeRelative — separator awareness', () => {
  it('strips a POSIX root', () => {
    expect(makeRelative('/repo/src/pages', '/repo')).toBe('src/pages')
  })

  it('strips a Windows root', () => {
    // A '/'-only prefix check left the ABSOLUTE path here, which then failed to
    // resolve as a folder mention on send.
    expect(makeRelative('C:\\repo\\src\\pages', 'C:\\repo')).toBe('src\\pages')
  })

  it('strips a Windows root that already ends in a separator', () => {
    expect(makeRelative('C:\\repo\\src', 'C:\\repo\\')).toBe('src')
  })

  it('returns the path unchanged when the root does not match', () => {
    expect(makeRelative('/other/src', '/repo')).toBe('/other/src')
  })

  it('returns the path unchanged with an empty root', () => {
    expect(makeRelative('/repo/src', '')).toBe('/repo/src')
  })

  it('inserts a relative folder token for a Windows root', () => {
    const sel = selectionFor(
      { path: 'C:\\repo\\src\\pages', name: 'pages', size: 0, mtime: 0, kind: 'dir' },
      'C:\\repo',
    )
    expect(sel.relativePath).toBe('src\\pages/')
  })
})
