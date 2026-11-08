import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

const mockApi = vi.hoisted(() => ({ fileSearch: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu from '../components/FilePickerMenu'

// A fixed mtime: the row's age text is not under test.
const ROWS = [{ path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: 1_700_000_000, kind: 'file' as const }]

function Harness({ project, qc, onSelect, query = 'mai' }: { project: string; qc: QueryClient; onSelect: (i: { relativePath: string }) => void; query?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <QueryClientProvider client={qc}>
      <div ref={ref}>anchor</div>
      <FilePickerMenu query={query} anchorRef={ref} open project={project} onSelect={onSelect} onClose={() => {}} />
    </QueryClientProvider>
  )
}

beforeEach(() => { mockApi.fileSearch.mockReset() })

describe('FilePickerMenu placeholder rows across a project change', () => {
  // A row is relativized against its payload's root, so the previous project's
  // rows, kept on screen as a placeholder, would insert a token naming a
  // different file under the new project (#29596: `@src/main.ts` picked under
  // a `/repo/src` project, which resolves to /repo/src/src/main.ts).
  it('does not offer the previous project\'s rows while the new project\'s search is in flight', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const onSelect = vi.fn()
    mockApi.fileSearch.mockImplementation((_q: string, project?: string) => Promise.resolve({ root: project, results: ROWS }))
    const view = render(<Harness project="/repo" qc={qc} onSelect={onSelect} />)
    await screen.findByText('main.ts', undefined, { timeout: 3000 })

    let release!: () => void
    mockApi.fileSearch.mockImplementation((_q: string, project?: string) =>
      new Promise(resolve => { release = () => resolve({ root: project, results: ROWS }) }))
    view.rerender(<Harness project="/repo/src" qc={qc} onSelect={onSelect} />)

    // The old project's row is gone at once, not after the debounce or fetch.
    expect(screen.queryByRole('option')).toBeNull()

    await vi.waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalledWith('mai', '/repo/src', expect.anything()))
    expect(screen.queryByRole('option')).toBeNull()
    release()

    fireEvent.mouseDown(await screen.findByRole('option'))
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ relativePath: 'main.ts' }))
  })

  it('keeps the same project\'s rows on screen while its next query loads', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mockApi.fileSearch.mockImplementation((_q: string, project?: string) => Promise.resolve({ root: project, results: ROWS }))
    const view = render(<Harness project="/repo" qc={qc} onSelect={() => {}} />)
    await screen.findByText('main.ts', undefined, { timeout: 3000 })

    mockApi.fileSearch.mockImplementation(() => new Promise(() => {}))
    view.rerender(<Harness project="/repo" qc={qc} onSelect={() => {}} query="main" />)
    await vi.waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalledWith('main', '/repo', expect.anything()))
    expect(screen.getByRole('option')).toHaveTextContent('main.ts')
  })
})
