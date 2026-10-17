/**
 * EmbeddingStatus — the knowledge page's one-line smart-search banner.
 *
 * It is a four-way decision: render nothing until the vector status arrives,
 * render nothing when the library is empty, report the embedded fraction when
 * embeddings exist, and otherwise offer the Generate action (or say the engine
 * is down). The Generate path is the one with a side effect worth pinning — it
 * POSTs to /embedding/generate and then invalidates the counts query.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

const mocks = vi.hoisted(() => ({ vectorEmbeddingStatus: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import { EmbeddingStatus } from '../pages/knowledge/EmbeddingStatus'

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** Answer the two queries the banner issues: vector health and item counts. */
function withCounts(counts: { total_items: number; embedded_items: number }) {
  mockKnowledgeApi.mockImplementation((path: string) => {
    if (path === '/embedding/status') {
      return Promise.resolve({ enabled: true, available: true, model: 'zzq-model', ...counts })
    }
    return Promise.resolve({ ok: true })
  })
}

beforeEach(() => {
  mocks.vectorEmbeddingStatus.mockResolvedValue({ provider: 'zzq', server_healthy: true, model_available: false })
  withCounts({ total_items: 4, embedded_items: 2 })
})
afterEach(() => vi.clearAllMocks())

describe('EmbeddingStatus', () => {
  it('renders nothing while the vector status is still unknown', () => {
    mocks.vectorEmbeddingStatus.mockReturnValue(new Promise(() => {}))
    const { container } = wrap(<EmbeddingStatus />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the knowledge library is empty', async () => {
    withCounts({ total_items: 0, embedded_items: 0 })
    const { container } = wrap(<EmbeddingStatus />)
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalledWith('/embedding/status'))
    expect(container).toBeEmptyDOMElement()
  })

  it('reports the embedded fraction when embeddings exist', async () => {
    withCounts({ total_items: 4, embedded_items: 1 })
    wrap(<EmbeddingStatus />)
    await screen.findByText(i18nT('pages.knowledge.embeddingStatus.smart_search_active'))
    expect(screen.getByText(/1\/4/)).toBeInTheDocument()
    expect(screen.getByText(/25%/)).toBeInTheDocument()
  })

  it('offers Generate when the engine is up but nothing is embedded, and POSTs on click', async () => {
    withCounts({ total_items: 4, embedded_items: 0 })
    wrap(<EmbeddingStatus />)
    const btn = await screen.findByText(i18nT('pages.knowledge.embeddingStatus.generate_now'))
    expect(screen.getByText(i18nT('pages.knowledge.embeddingStatus.embedding_engine_ready_knowledge_items_need_embe'))).toBeInTheDocument()

    fireEvent.click(btn)
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalledWith('/embedding/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }))
  })

  it('counts model_available alone as an active engine', async () => {
    mocks.vectorEmbeddingStatus.mockResolvedValue({ server_healthy: false, model_available: true })
    withCounts({ total_items: 2, embedded_items: 2 })
    wrap(<EmbeddingStatus />)
    expect(await screen.findByText(i18nT('pages.knowledge.embeddingStatus.smart_search_active'))).toBeInTheDocument()
  })

  it('says smart search is unavailable when neither health signal is set', async () => {
    mocks.vectorEmbeddingStatus.mockResolvedValue({ server_healthy: false, model_available: false })
    wrap(<EmbeddingStatus />)
    expect(await screen.findByText(
      i18nT('pages.knowledge.embeddingStatus.smart_search_unavailable_embedding_model_is_down'),
    )).toBeInTheDocument()
  })
})
