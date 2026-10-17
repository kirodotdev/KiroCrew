/**
 * KnowledgePage's `embedded` mode: inside Agent Capabilities the pane header
 * already shows the tab's label + description, so the page must not render its
 * own title block — but the Help affordance (onboarding + shortcuts dialog)
 * must survive the header's removal, docked at the end of the internal tab
 * strip. Standalone mode keeps the original chrome byte-for-byte.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

// Must import after the mock is registered
const { default: KnowledgePage } = await import('../pages/knowledge/index')

function defaultApi(path: string) {
  const p = String(path)
  if (p.startsWith('/source-counts')) return Promise.resolve({ counts: {}, total: 0 })
  if (p.startsWith('/items')) return Promise.resolve({ items: [], total: 0 })
  if (p.startsWith('/stats')) return Promise.resolve({ items: 0, entities: 0, relations: 0, sources: 0 })
  if (p.startsWith('/namespaces')) return Promise.resolve([])
  if (p.startsWith('/config')) return Promise.resolve({ enabled: true, supported_formats: ['.md'] })
  if (p.startsWith('/sources')) return Promise.resolve([])
  return Promise.resolve({})
}

beforeEach(() => {
  mockKnowledgeApi.mockReset()
  mockKnowledgeApi.mockImplementation(defaultApi)
})

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('KnowledgePage — embedded mode', () => {
  it('embedded: no page title, Help still present', () => {
    mount(<KnowledgePage embedded />)
    expect(screen.queryByText('Knowledge Library')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /help/i })).toBeInTheDocument()
  })

  it('standalone: title and Help render as before', () => {
    mount(<KnowledgePage />)
    expect(screen.getByText('Knowledge Library')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /help/i })).toBeInTheDocument()
  })
})
