import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  fileSearch: vi.fn(),
  pathComplete: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu from '../components/FilePickerMenu'

/* `@.kiro/specs/<partial>` lists the spec FOLDERS of the project (one listing
   of .kiro/specs/), not a fuzzy search whose tasks.md files would bury them. */

const ROOT = '/repo'
const MTIME = 1_700_000_000

const LISTING = [
  { path: '/repo/.kiro/specs/login-flow', name: 'login-flow', size: 0, mtime: MTIME, kind: 'dir' as const },
  { path: '/repo/.kiro/specs/pod-cli', name: 'pod-cli', size: 0, mtime: MTIME, kind: 'dir' as const },
  // A loose file under .kiro/specs/ is not a spec, but it stays pickable.
  { path: '/repo/.kiro/specs/README.md', name: 'README.md', size: 10, mtime: MTIME, kind: 'file' as const },
]

function Harness({ query, project = ROOT, onSelect = vi.fn() }: {
  query: string
  project?: string
  onSelect?: (i: { path: string; relativePath: string; kind: 'file' | 'dir' }) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <FilePickerMenu query={query} anchorRef={ref} open project={project} onSelect={onSelect} onClose={vi.fn()} />
      </div>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.fileSearch.mockReset()
  mockApi.pathComplete.mockReset()
  mockApi.fileSearch.mockResolvedValue({ results: [], root: ROOT })
  mockApi.pathComplete.mockResolvedValue({ results: LISTING, root: ROOT })
})

describe('FilePickerMenu spec mode', () => {
  it('lists the spec folders from .kiro/specs/, not a fuzzy search', async () => {
    render(<Harness query=".kiro/specs/" />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3))
    expect(screen.getByText('login-flow/')).toBeInTheDocument()
    expect(screen.getByText('pod-cli/')).toBeInTheDocument()
    expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './.kiro/specs/', '', expect.anything())
    expect(mockApi.fileSearch).not.toHaveBeenCalled()
  })

  it('keeps a loose file under .kiro/specs/ pickable as a file', async () => {
    const onSelect = vi.fn()
    render(<Harness query=".kiro/specs/" onSelect={onSelect} />)
    fireEvent.mouseDown(await screen.findByText('README.md'))
    expect(onSelect).toHaveBeenCalledWith({
      path: '/repo/.kiro/specs/README.md',
      relativePath: '.kiro/specs/README.md',
      kind: 'file',
    })
  })

  it('names the project on each spec row, not the repeated path', async () => {
    render(<Harness query=".kiro/specs/" />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(3))
    const labels = screen.getAllByTestId('file-picker-row-project').map(el => el.textContent)
    expect(labels).toEqual(['repo', 'repo', 'repo'])
    expect(screen.queryByText('/repo/.kiro/specs/pod-cli')).not.toBeInTheDocument()
  })

  it('passes the typed spec-name prefix to the listing', async () => {
    render(<Harness query=".kiro/specs/po" />)
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './.kiro/specs/', 'po', expect.anything()))
  })

  it('picks a spec as a project-relative folder mention', async () => {
    const onSelect = vi.fn()
    render(<Harness query=".kiro/specs/" onSelect={onSelect} />)
    fireEvent.mouseDown(await screen.findByText('pod-cli/'))
    expect(onSelect).toHaveBeenCalledWith({
      path: '/repo/.kiro/specs/pod-cli',
      relativePath: '.kiro/specs/pod-cli/',
      kind: 'dir',
    })
  })

  it('falls back to the fuzzy search once the query goes inside a spec', async () => {
    render(<Harness query=".kiro/specs/pod-cli/tas" />)
    await waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalled())
    expect(mockApi.pathComplete).not.toHaveBeenCalled()
  })

  it('stays a fuzzy search without a project to list against', async () => {
    render(<Harness query=".kiro/specs/" project="" />)
    await waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalled())
    expect(mockApi.pathComplete).not.toHaveBeenCalled()
  })
})
