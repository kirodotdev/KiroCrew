import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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

function renderWith(source: Partial<Source>) {
  const sources: Source[] = [{
    id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', item_count: 0, sync_status: 'synced', ...source,
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

describe('SourcesList — a folder scan in which one file failed', () => {
  afterEach(() => { vi.useRealTimers() })

  const sourcesCalls = () => vi.mocked(api.knowledgeApi).mock.calls.filter(([p]) => p === '/sources').length

  it('shows no failure and keeps polling while the scan still runs', async () => {
    vi.useFakeTimers()
    renderWith({ source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'error', scanning: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(screen.getByText('notes.md')).toBeInTheDocument()
    expect(screen.queryByTestId('source-sync-error')).toBeNull()
    expect(screen.queryByText('Error')).toBeNull()
    const before = sourcesCalls()
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(sourcesCalls()).toBeGreaterThan(before)
  })

  it('labels the scanning row with the translated syncing status', async () => {
    renderWith({ source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'error', scanning: true })
    expect(await screen.findByText('Syncing')).toBeInTheDocument()
    expect(screen.queryByText('syncing')).toBeNull()
  })

  it('shows the failure once the scan has ended with an error', async () => {
    renderWith({ source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'error', scanning: false })
    expect(await screen.findByTestId('source-sync-error')).toHaveTextContent('The last sync of this source failed.')
    expect(screen.getByText('Error')).toBeInTheDocument()
  })
})
