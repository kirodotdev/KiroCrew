import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// A source whose last sync failed keeps its red Error badge and says so under the row.

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderWith(source: Pick<Source, 'sync_status'>) {
  const sources: Source[] = [{
    id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', item_count: 0, ...source,
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

describe('SourcesList — a source whose sync failed', () => {
  it('keeps the Error badge and shows the failure under the row', async () => {
    renderWith({ sync_status: 'error' })
    const notice = await screen.findByTestId('source-sync-error')
    expect(notice).toHaveTextContent('The last sync of this source failed.')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('shows no notice on a synced row', async () => {
    renderWith({ sync_status: 'synced' })
    await screen.findByText('notes.md')
    expect(screen.queryByTestId('source-sync-error')).toBeNull()
  })
})
