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
 * A row checked while a Knowledge bulk action runs stays checked when it lands.
 *
 * The optimistic update hides the rows the action was started on, which
 * empties the selection while the requests run. A row the user checks in that
 * window is a new pick, so the action's success must drop only its own rows
 * from the selection, not clear the whole thing.
 *
 * The heavy children are stubbed, as in KnowledgeBulkPartialFailure.test.tsx.
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
const ACTED_ON = item('k1', 'Ledger alpha')
const PICKED_LATER = item('k2', 'Ledger beta')

// What the server holds: the search answers from it, and a write changes it.
let live: Map<string, ReturnType<typeof item>>
// Releases the held write, so the test can pick a row while it is in flight.
let releaseHeld: (() => void) | null

function api(path: string, opts?: RequestInit): Promise<unknown> {
  const p = String(path)
  if (p.startsWith('/items/')) {
    const id = p.slice('/items/'.length)
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
    const items = [...live.values()].filter(i => i.status === 'active')
    return Promise.resolve({ items, total: items.length })
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

beforeEach(() => {
  vi.clearAllMocks()
  live = new Map([[ACTED_ON.id, ACTED_ON], [PICKED_LATER.id, PICKED_LATER]])
  releaseHeld = null
  mockKnowledgeApi.mockImplementation((path: string, opts?: RequestInit) => api(path, opts))
})

afterEach(() => {
  qc?.clear()
  vi.unstubAllGlobals()
})

describe('KnowledgePage — a row checked while a bulk action runs', () => {
  for (const action of ['Delete', 'Archive'] as const) {
    it(`${action}: the later pick is still selected when the action lands`, async () => {
      vi.stubGlobal('confirm', vi.fn(() => true))
      renderPage()
      await userEvent.type(await screen.findByPlaceholderText(/Search knowledge/i, {}, PAGE_REQUEST_CHAIN()), 'ledger{Enter}')
      await userEvent.click(await screen.findByLabelText(`Select ${ACTED_ON.title}`, {}, PAGE_REQUEST_CHAIN()))
      expect(await screen.findByText('1 selected', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: action }))
      await waitFor(() => expect(releaseHeld).not.toBeNull(), PAGE_REQUEST_CHAIN())

      // The acted-on row is hidden while the write runs; check the other one.
      await userEvent.click(screen.getByLabelText(`Select ${PICKED_LATER.title}`))
      expect(await screen.findByText('1 selected', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()

      await act(async () => { releaseHeld!() })
      // The action has settled once its button is enabled again.
      await waitFor(() => expect(screen.getByRole('button', { name: action })).toBeEnabled(), PAGE_REQUEST_CHAIN())
      expect(screen.getByText('1 selected')).toBeInTheDocument()
      expect(screen.getByLabelText(`Select ${PICKED_LATER.title}`)).toBeChecked()
    })
  }
})
