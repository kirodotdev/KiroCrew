import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { namedCeiling } from './namedCeiling'
// Every wait here spans a request chain, so each carries a named lost-run
// ceiling (website/docs/testing.md).
const PAGE_REQUEST_CHAIN = () => namedCeiling('PAGE_REQUEST_CHAIN', 5000)

/**
 * A Knowledge bulk action that fails part-way shows what the server holds.
 *
 * Archive and Delete send one request per selected item. When one is refused
 * and another goes through, the rollback puts every selected row back, and the
 * page must then refetch: the item the server did delete or archive is no
 * longer a live row, and leaving it on screen offers a retry that can only fail
 * on it again. The rollback must also wait for every request, so a request
 * still in flight cannot land after the refetch has read the list.
 *
 * The heavy children are stubbed, as in KnowledgeIndexErrors.test.tsx.
 */
const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))
vi.mock('../pages/knowledge/DetailView', () => ({ default: () => <div>detail</div> }))
vi.mock('../pages/knowledge/SourcesList', () => ({ default: () => <div>sources</div> }))
vi.mock('../pages/knowledge/KnowledgeGraph', () => ({ default: () => <div>graph</div> }))

const { default: KnowledgePage } = await import('../pages/knowledge/index')

const item = (id: string, title: string) => ({
  id, title, item_type: 'document', status: 'active', source_id: 's1',
  content: `${title} body`, created_at: '2026-01-01', updated_at: '2026-01-01',
})
const GOES_THROUGH = item('k1', 'Ledger alpha')
const REFUSED = item('k2', 'Ledger beta')

// What the server holds: the search answers from it, and a write that goes
// through changes it.
let live: Map<string, ReturnType<typeof item>>
// The id whose write the server refuses.
const REFUSED_ID = REFUSED.id
// Releases the write that goes through; held so it lands after the refusal.
let releaseHeld: (() => void) | null
// Once set, the next list read is held until `releaseRead` runs: the
// corrective refetch, so the test can look at the page while it is in flight.
let holdNextRead = false
let releaseRead: (() => void) | null

function api(path: string, opts?: RequestInit): Promise<unknown> {
  const p = String(path)
  if (p.startsWith('/items/')) {
    const id = p.slice('/items/'.length)
    if (id === REFUSED_ID) return Promise.reject(new Error('write refused'))
    return new Promise(resolve => {
      releaseHeld = () => {
        if (opts?.method === 'DELETE') live.delete(id)
        else {
          const cur = live.get(id)
          if (cur) live.set(id, { ...cur, status: JSON.parse(String(opts?.body)).status })
        }
        resolve({ ok: true })
      }
    })
  }
  if (p.startsWith('/items')) {
    const answer = () => {
      const items = [...live.values()].filter(i => i.status === 'active')
      return { items, total: items.length }
    }
    if (!holdNextRead) return Promise.resolve(answer())
    holdNextRead = false
    return new Promise(resolve => { releaseRead = () => resolve(answer()) })
  }
  if (p.startsWith('/source-counts')) return Promise.resolve({ counts: { s1: live.size }, total: live.size })
  if (p === '/sources') return Promise.resolve([{ id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/n', sync_status: 'synced', item_count: 2 }])
  if (p === '/stats') return Promise.resolve({ items: live.size, entities: 0, relations: 0, sources: 1 })
  if (p === '/config') return Promise.resolve({ enabled: true, supported_formats: ['.md'] })
  return Promise.resolve([])
}

let qc: QueryClient
function renderPage() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><KnowledgePage /></QueryClientProvider>
    </MemoryRouter>,
  )
}

async function searchAndSelectBoth() {
  await userEvent.type(await screen.findByPlaceholderText(/Search knowledge/i, {}, PAGE_REQUEST_CHAIN()), 'ledger{Enter}')
  await userEvent.click(await screen.findByLabelText(`Select ${GOES_THROUGH.title}`, {}, PAGE_REQUEST_CHAIN()))
  await userEvent.click(screen.getByLabelText(`Select ${REFUSED.title}`))
  expect(await screen.findByText('2 selected', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  live = new Map([[GOES_THROUGH.id, GOES_THROUGH], [REFUSED.id, REFUSED]])
  releaseHeld = null
  holdNextRead = false
  releaseRead = null
  mockKnowledgeApi.mockImplementation((path: string, opts?: RequestInit) => api(path, opts))
})

afterEach(() => {
  qc?.clear()
  vi.unstubAllGlobals()
})

describe('KnowledgePage — a bulk action that fails part-way', () => {
  for (const action of ['Delete', 'Archive'] as const) {
    it(`${action}: the item the server changed leaves the list, the refused one stays`, async () => {
      vi.stubGlobal('confirm', vi.fn(() => true))
      renderPage()
      await searchAndSelectBoth()
      await userEvent.click(screen.getByRole('button', { name: action }))
      // The refusal is already in; the other write is still in flight. No
      // failure is reported until every request has answered.
      await waitFor(() => expect(releaseHeld).not.toBeNull(), PAGE_REQUEST_CHAIN())
      expect(screen.queryByText(/write refused/)).not.toBeInTheDocument()
      holdNextRead = true
      await act(async () => { releaseHeld!() })
      // The corrective read is in flight: the action is still pending, so no
      // failure is shown yet and a retry cannot start and cancel that read.
      await waitFor(() => expect(releaseRead).not.toBeNull(), PAGE_REQUEST_CHAIN())
      expect(screen.queryByText(/write refused/)).not.toBeInTheDocument()
      await act(async () => { releaseRead!() })
      const notice = await screen.findByTestId('knowledge-bulk-error', {}, PAGE_REQUEST_CHAIN())
      expect(notice).toHaveTextContent('write refused')
      // The failure is reported only once the refetch has landed, so the list
      // already shows what the server holds: the changed item is gone, the
      // refused one is back as a live row. Until then the action stays pending
      // and a retry cannot cancel that refetch.
      expect(screen.queryByText(GOES_THROUGH.title)).not.toBeInTheDocument()
      expect(screen.getByText(REFUSED.title)).toBeInTheDocument()
    })
  }
})
