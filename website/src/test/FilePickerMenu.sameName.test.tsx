import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import FilePickerMenu, { disambiguatedPaths } from '../components/FilePickerMenu'

const ROOT = '/repo'
const NOW = Math.floor(Date.now() / 1000)

// Six rows sharing one basename under deep, largely-shared prefixes, the shape
// the issue describes: an end cut lands inside the common chain and the rows
// read identical. One unique-named row pins the unchanged behaviour beside it.
const SAME_NAME = [
  { path: '/repo/packages/core/src/README.md', name: 'README.md', size: 120, mtime: NOW - 60 },
  { path: '/repo/packages/website/src/README.md', name: 'README.md', size: 340, mtime: NOW - 120 },
  { path: '/repo/packages/cli/README.md', name: 'README.md', size: 80, mtime: NOW - 180 },
  { path: '/repo/docs/README.md', name: 'README.md', size: 900, mtime: NOW - 240 },
  { path: '/repo/README.md', name: 'README.md', size: 10, mtime: NOW - 300 },
  { path: '/repo/somewhere-else-entirely/README.md', name: 'README.md', size: 55, mtime: NOW - 360 },
]
const RESULTS = [
  ...SAME_NAME,
  { path: '/repo/notes.txt', name: 'notes.txt', size: 42, mtime: NOW - 90 },
]

function Harness({ query, open }: { query: string; open: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <FilePickerMenu query={query} anchorRef={ref} open={open} onSelect={vi.fn()} onClose={vi.fn()} />
      </div>
    </QueryClientProvider>
  )
}

/** The path sub-line is the second child of a row's text column. */
function subLineOf(row: HTMLElement): string {
  const col = row.querySelector('div.flex-1')
  expect(col).toBeTruthy()
  const lines = col!.querySelectorAll('div')
  expect(lines.length).toBe(2)
  return lines[1].textContent ?? ''
}

beforeEach(() => {
  vi.restoreAllMocks()
  mockApi.fileSearch.mockResolvedValue({ results: RESULTS, root: ROOT })
})

describe('disambiguatedPaths', () => {
  it('keeps the full path for rows whose name is unique in the set', () => {
    const rows = [
      { path: '/repo/a/one.ts', name: 'one.ts' },
      { path: '/repo/b/two.ts', name: 'two.ts' },
    ]
    const m = disambiguatedPaths(rows)
    expect(m.get('/repo/a/one.ts')).toBe('/repo/a/one.ts')
    expect(m.get('/repo/b/two.ts')).toBe('/repo/b/two.ts')
  })

  it('grows the parent suffix only until same-named rows differ', () => {
    // k=1 collides (`b`), k=2 distinguishes.
    const rows = [
      { path: '/repo/a/b/x.md', name: 'x.md' },
      { path: '/repo/c/b/x.md', name: 'x.md' },
    ]
    const m = disambiguatedPaths(rows)
    expect(m.get('/repo/a/b/x.md')).toBe('…/a/b/x.md')
    expect(m.get('/repo/c/b/x.md')).toBe('…/c/b/x.md')
  })

  it('keeps only the distinguishing tail once it differs', () => {
    const rows = [
      { path: '/repo/packages/core/README.md', name: 'README.md' },
      { path: '/repo/packages/web/README.md', name: 'README.md' },
    ]
    const m = disambiguatedPaths(rows)
    expect(m.get('/repo/packages/core/README.md')).toBe('…/core/README.md')
    expect(m.get('/repo/packages/web/README.md')).toBe('…/web/README.md')
  })

  it('distinguishes a root-level row from a nested same-named one', () => {
    const rows = [
      { path: '/repo/README.md', name: 'README.md' },
      { path: '/repo/docs/README.md', name: 'README.md' },
    ]
    const m = disambiguatedPaths(rows)
    expect(m.get('/repo/README.md')).toBe('…/repo/README.md')
    expect(m.get('/repo/docs/README.md')).toBe('…/docs/README.md')
  })

  it('handles a pair whose parents are a prefix of the other', () => {
    const rows = [
      { path: '/repo/a/README.md', name: 'README.md' },
      { path: '/repo/b/a/README.md', name: 'README.md' },
    ]
    const m = disambiguatedPaths(rows)
    // The last parent of BOTH rows is `a`, so k=1 collides and both grow.
    expect(m.get('/repo/a/README.md')).toBe('…/repo/a/README.md')
    expect(m.get('/repo/b/a/README.md')).toBe('…/b/a/README.md')
  })

  it('reuses the row separator on Windows-shaped paths', () => {
    const rows = [
      { path: 'C:\\repo\\packages\\core\\README.md', name: 'README.md' },
      { path: 'C:\\repo\\packages\\web\\README.md', name: 'README.md' },
    ]
    const m = disambiguatedPaths(rows)
    expect(m.get('C:\\repo\\packages\\core\\README.md')).toBe('…\\core\\README.md')
    expect(m.get('C:\\repo\\packages\\web\\README.md')).toBe('…\\web\\README.md')
  })
})

describe('FilePickerMenu same-named rows', () => {
  it('shows each same-named row enough of its parent to tell the rows apart', async () => {
    render(<Harness query="readme" open />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(RESULTS.length))
    const subLines = screen.getAllByRole('option').map(subLineOf)
    // Every row's visible path differs, same-named or not.
    expect(new Set(subLines).size).toBe(subLines.length)
  })

  it('leads the cut with the distinguishing parent segment', async () => {
    render(<Harness query="readme" open />)
    // The two `src` leaves collide at depth 1, so both grow one more segment;
    // the rest are told apart by their own parent.
    expect(await screen.findByText('…/core/src/README.md')).toBeInTheDocument()
    expect(screen.getByText('…/website/src/README.md')).toBeInTheDocument()
    expect(screen.getByText('…/cli/README.md')).toBeInTheDocument()
    expect(screen.getByText('…/docs/README.md')).toBeInTheDocument()
    expect(screen.getByText('…/somewhere-else-entirely/README.md')).toBeInTheDocument()
    expect(screen.getByText('…/repo/README.md')).toBeInTheDocument()
  })

  it('keeps the name line and the full-path tooltip untouched', async () => {
    render(<Harness query="readme" open />)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(RESULTS.length))
    // Six same-named rows all keep their basename in the name line.
    expect(screen.getAllByText('README.md')).toHaveLength(6)
    const rows = screen.getAllByRole('option')
    const readme = rows.find(r => r.getAttribute('title') === '/repo/packages/core/src/README.md')
    expect(readme).toBeTruthy()
  })

  it('leaves a unique-named row on its full path', async () => {
    render(<Harness query="readme" open />)
    expect(await screen.findByText('/repo/notes.txt')).toBeInTheDocument()
  })
})
