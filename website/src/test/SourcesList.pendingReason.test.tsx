import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// A source the explicit-import budget deferred reads 'pending'; its row says why.

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderWith(source: Pick<Source, 'sync_status'>) {
  const sources: Source[] = [{
    id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', item_count: 0,
    properties: JSON.stringify({ pending_reason: 'import_budget' }), ...source,
  }]
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    return {} as unknown as never
  })
  return render(
    <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
    { wrapper },
  )
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
})

describe('SourcesList — why a source is pending', () => {
  it('names the import budget on a pending row it deferred', async () => {
    renderWith({ sync_status: 'pending' })
    expect(await screen.findByTestId('source-pending-reason')).toHaveTextContent('knowledge.import_chunk_budget')
  })

  it('shows no reason once the row has left pending', async () => {
    renderWith({ sync_status: 'synced' })
    await screen.findByText('notes.md')
    expect(screen.queryByTestId('source-pending-reason')).toBeNull()
  })
})
