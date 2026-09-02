// SessionArchive — the list/detail behaviour the empty-state suite never reaches:
// the fuzzy filter, the row formatting helpers, opening an archive (including the
// 200KB truncation and the error path), keyboard activation, and the abort of an
// in-flight read when another row is opened.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SessionArchive from '../pages/SessionArchive'

interface Entry { name: string; key: string; stamp: string; size: number; mtime: number }

const ENTRIES: Entry[] = [
  { name: 'aa.jsonl', key: 'slot-alpha', stamp: '20260101T091530', size: 512, mtime: 1 },
  { name: 'bb.jsonl', key: 'slot-beta', stamp: 'raw-stamp', size: 4096, mtime: 2 },
  { name: 'cc.jsonl', key: 'slot-gamma', stamp: '20260202T101112', size: 3 * 1024 * 1024, mtime: 3 },
]

/** Route /api/session/archive to the list, /api/session/archive/<name> to a body. */
function stubFetch(opts: { listFails?: boolean; body?: string; bodyFails?: boolean } = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === '/api/session/archive') {
      if (opts.listFails) return { ok: false, status: 500, text: async () => 'list-blew-up' }
      return { ok: true, status: 200, json: async () => ({ archives: ENTRIES }) }
    }
    if (opts.bodyFails) return { ok: false, status: 404, text: async () => 'read-blew-up' }
    return { ok: true, status: 200, text: async () => opts.body ?? 'archive-body' }
  })
  vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)
  return fetchMock
}

function renderArchive() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SessionArchive />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => { vi.unstubAllGlobals() })
afterEach(() => { vi.restoreAllMocks() })

describe('SessionArchive list', () => {
  it('lists every archive with a formatted stamp and size', async () => {
    stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    expect(screen.getByText('2026-01-01 09:15:30')).toBeInTheDocument()
    expect(screen.getByText('512B')).toBeInTheDocument()
    expect(screen.getByText('4.0KB')).toBeInTheDocument()
    expect(screen.getByText('3.00MB')).toBeInTheDocument()
    // A stamp that isn't the fixed 15-char form is shown verbatim.
    expect(screen.getByText('raw-stamp')).toBeInTheDocument()
  })

  it('surfaces a list failure and keeps the empty hint away', async () => {
    stubFetch({ listFails: true })
    renderArchive()
    await waitFor(() => expect(screen.getByText(/list-blew-up/)).toBeInTheDocument())
    expect(screen.queryByText('slot-alpha')).not.toBeInTheDocument()
  })

  it('refetches on the reload control', async () => {
    const fetchMock = stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    const listCalls = () => fetchMock.mock.calls.filter(c => c[0] === '/api/session/archive').length
    const before = listCalls()
    fireEvent.click(screen.getByText('Reload'))
    await waitFor(() => expect(listCalls()).toBe(before + 1))
  })

  it('filters on key or filename, case-insensitively', async () => {
    stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    const filter = screen.getByPlaceholderText(/fuzzy filter/i)
    fireEvent.change(filter, { target: { value: 'BETA' } })
    expect(screen.getByText('slot-beta')).toBeInTheDocument()
    expect(screen.queryByText('slot-alpha')).not.toBeInTheDocument()
    fireEvent.change(filter, { target: { value: 'cc.js' } })
    expect(screen.getByText('slot-gamma')).toBeInTheDocument()
  })

  it('explains an empty filter result instead of showing a blank list', async () => {
    stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText(/fuzzy filter/i), { target: { value: 'zzz-nothing' } })
    expect(screen.getByText(/zzz-nothing/)).toBeInTheDocument()
  })
})

describe('SessionArchive detail', () => {
  it('prompts for a selection before anything is opened', async () => {
    stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    expect(screen.getByText(/Select an archive/i)).toBeInTheDocument()
  })

  it('loads the archive body on click', async () => {
    const fetchMock = stubFetch({ body: 'transcript-line' })
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.click(screen.getByText('slot-alpha'))
    await waitFor(() => expect(screen.getByText('transcript-line')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith('/api/session/archive/aa.jsonl', expect.anything())
  })

  it('opens on Enter and on Space from the keyboard', async () => {
    stubFetch({ body: 'kbd-body' })
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    const rows = screen.getAllByRole('button').filter(b => b.textContent?.includes('slot-'))
    fireEvent.keyDown(rows[0], { key: 'Enter' })
    await waitFor(() => expect(screen.getByText('kbd-body')).toBeInTheDocument())
    fireEvent.keyDown(rows[1], { key: ' ' })
    await waitFor(() => expect(screen.getByText('bb.jsonl')).toBeInTheDocument())
  })

  it('ignores other keys on a row', async () => {
    const fetchMock = stubFetch()
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    const row = screen.getAllByRole('button').find(b => b.textContent?.includes('slot-alpha'))!
    fireEvent.keyDown(row, { key: 'ArrowDown' })
    expect(fetchMock.mock.calls.some(c => String(c[0]).includes('aa.jsonl'))).toBe(false)
  })

  it('truncates a body past the 200KB cap', async () => {
    stubFetch({ body: 'x'.repeat(200_001) })
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.click(screen.getByText('slot-alpha'))
    await waitFor(() => expect(screen.getByText(/truncated/i)).toBeInTheDocument())
  })

  it('surfaces a read failure without clearing the selection', async () => {
    stubFetch({ bodyFails: true })
    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.click(screen.getByText('slot-alpha'))
    await waitFor(() => expect(screen.getByText(/read-blew-up/)).toBeInTheDocument())
    expect(screen.getByText('aa.jsonl')).toBeInTheDocument()
  })

  it('aborts an in-flight read when another row is opened', async () => {
    const signals: (AbortSignal | undefined)[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { signal?: AbortSignal }) => {
      if (url === '/api/session/archive') {
        return { ok: true, status: 200, json: async () => ({ archives: ENTRIES }) }
      }
      signals.push(init?.signal)
      if (signals.length === 1) return new Promise(() => {}) as never
      return { ok: true, status: 200, text: async () => 'second-body' }
    }) as unknown as typeof fetch)

    renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.click(screen.getByText('slot-alpha'))
    await waitFor(() => expect(signals).toHaveLength(1))
    fireEvent.click(screen.getByText('slot-beta'))
    await waitFor(() => expect(screen.getByText('second-body')).toBeInTheDocument())
    expect(signals[0]?.aborted).toBe(true)
  })

  it('aborts the in-flight read on unmount', async () => {
    const signals: (AbortSignal | undefined)[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { signal?: AbortSignal }) => {
      if (url === '/api/session/archive') {
        return { ok: true, status: 200, json: async () => ({ archives: ENTRIES }) }
      }
      signals.push(init?.signal)
      return new Promise(() => {}) as never
    }) as unknown as typeof fetch)

    const { unmount } = renderArchive()
    await waitFor(() => expect(screen.getByText('slot-alpha')).toBeInTheDocument())
    fireEvent.click(screen.getByText('slot-alpha'))
    await waitFor(() => expect(signals).toHaveLength(1))
    unmount()
    expect(signals[0]?.aborted).toBe(true)
  })
})
