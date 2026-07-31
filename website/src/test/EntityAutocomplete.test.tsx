import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// The MemoryRouter wrapper is retained defensively for future routed children:
// since the always-on embeddings change, nothing in the KnowledgePage tree uses
// the router (EmbeddingStatus dropped its useNavigate; DetailView's Link2 is a
// lucide icon, not a router Link). If a routed child is added, this wrapper —
// which must come from 'react-router-dom' (v7 keeps its own context instance) —
// already provides the context.
import { MemoryRouter } from 'react-router-dom'

// Mock the knowledge API.
//
// /source-counts must report total > 0. With query === '' the page reads its
// total from that endpoint (index.tsx:204), and a total of 0 with pristine
// filters renders the onboarding empty state INSTEAD of the filter bar, so the
// search input this test types into would never mount. A non-empty library is
// the state a debounce test means to exercise anyway.
const mockKnowledgeApi = vi.fn().mockImplementation((path: string) => {
  if (path.startsWith('/source-counts')) {
    return Promise.resolve({ counts: { 'src-1': 1 }, total: 1 })
  }
  if (path.startsWith('/items')) {
    return Promise.resolve({ items: [], total: 1 })
  }
  return Promise.resolve([])
})
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

// Must import after mock
const { default: KnowledgePage } = await import('../pages/knowledge/index')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

function entityCalls() {
  return mockKnowledgeApi.mock.calls.filter(
    (c: unknown[]) => (c[0] as string).includes('/entities?q=')
  )
}

describe('EntityAutocomplete debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    qc.clear()
  })
  afterEach(() => { vi.useRealTimers() })

  it('debounces entity API calls (does not fire on every keystroke)', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })

    // Settle initial renders
    await act(async () => { vi.advanceTimersByTime(500) })
    mockKnowledgeApi.mockClear()

    const input = screen.getByPlaceholderText(/Search knowledge/)

    // Type "he" then immediately "hel" (within debounce window)
    await act(async () => { fireEvent.change(input, { target: { value: 'he' } }) })
    await act(async () => { fireEvent.change(input, { target: { value: 'hel' } }) })

    // Flush debounce
    await act(async () => { vi.advanceTimersByTime(300) })

    // Should NOT have called with "he" — only "hel" (debounce cancelled intermediate)
    const calls = entityCalls()
    expect(calls.every((c: unknown[]) => !(c[0] as string).includes('q=he&'))).toBe(true)
    expect(calls.some((c: unknown[]) => (c[0] as string).includes('q=hel'))).toBe(true)
  })

  it('fires entity API call after debounce settles', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await act(async () => { vi.advanceTimersByTime(500) })
    mockKnowledgeApi.mockClear()

    const input = screen.getByPlaceholderText(/Search knowledge/)

    await act(async () => { fireEvent.change(input, { target: { value: 'hello' } }) })
    await act(async () => { vi.advanceTimersByTime(300) })

    expect(entityCalls().length).toBeGreaterThanOrEqual(1)
    expect(entityCalls().at(-1)![0]).toContain('/entities?q=hello')
  })

})
