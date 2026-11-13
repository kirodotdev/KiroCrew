import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

// A rejected confirm, rename or Sync request must surface through ErrorNotice
// instead of silently clearing the busy state, the way add and delete do.

vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: vi.fn(),
  KnowledgeApiError: class KnowledgeApiError extends Error {
    code?: string
    constructor(message: string, code?: string) { super(message); this.name = 'KnowledgeApiError'; this.code = code }
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderList(source: Partial<Source>, handler?: (path: string, opts?: RequestInit) => Promise<unknown>) {
  const sources: Source[] = [{
    id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', item_count: 2, sync_status: 'synced', ...source,
  }]
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string, opts?: RequestInit) => {
    if (path === '/sources') return sources as unknown as never
    if (handler) return (await handler(path, opts)) as unknown as never
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

describe('SourcesList — a failed Sync request', () => {
  it('shows the failure under the row and clears the syncing state', async () => {
    renderList({ syncable: true }, async (path) => {
      if (path === '/sources/s1/sync') throw new Error('sync endpoint unreachable')
      return {}
    })
    fireEvent.click(await screen.findByLabelText('Sync source'))
    const notice = await screen.findByTestId('source-sync-action-error')
    expect(notice).toHaveTextContent('sync endpoint unreachable')
    expect(notice).toHaveAttribute('role', 'alert')
    // Busy state cleared: the Sync button is back and enabled.
    expect(screen.getByLabelText('Sync source')).not.toBeDisabled()
  })

  it('clears the notice when the same row starts a sync again', async () => {
    let fail = true
    renderList({ syncable: true }, async (path) => {
      if (path === '/sources/s1/sync') { if (fail) throw new Error('first sync failed'); return {} }
      return {}
    })
    fireEvent.click(await screen.findByLabelText('Sync source'))
    await screen.findByTestId('source-sync-action-error')
    fail = false
    fireEvent.click(screen.getByLabelText('Sync source'))
    await waitFor(() => expect(screen.queryByTestId('source-sync-action-error')).toBeNull())
  })
})

describe('SourcesList — a failed rename request', () => {
  it('surfaces the error and keeps the field open with the typed text', async () => {
    renderList({ name: 'Old Name' }, async (path, opts) => {
      if (path === '/sources/s1' && opts?.method === 'PATCH') throw new Error('rename was rejected')
      return {}
    })
    fireEvent.click(await screen.findByLabelText('Rename source'))
    const input = screen.getByLabelText('Source name')
    fireEvent.change(input, { target: { value: 'New Name' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const notice = await screen.findByTestId('source-rename-error')
    expect(notice).toHaveTextContent('rename was rejected')
    // The draft is kept so a retry needs no re-typing.
    expect(screen.getByLabelText('Source name')).toHaveValue('New Name')
    // An unsaved draft gets no agent hand-off (it would unmount and lose it).
    expect(screen.queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('does not surface an error after a successful rename closes the field', async () => {
    renderList({ name: 'Old Name' }, async () => ({ ok: true }))
    fireEvent.click(await screen.findByLabelText('Rename source'))
    const input = screen.getByLabelText('Source name')
    fireEvent.change(input, { target: { value: 'New Name' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByLabelText('Source name')).toBeNull())
    expect(screen.queryByTestId('source-rename-error')).toBeNull()
  })
})

describe('SourcesList — a failed folder-scan confirm request', () => {
  it('shows the failure and keeps the confirm dialog open', async () => {
    renderList(
      { source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'pending_confirmation' },
      async (path) => {
        if (path === '/sources/s1/confirm') throw new Error('confirm could not start the scan')
        return {}
      },
    )
    fireEvent.click(await screen.findByLabelText('Confirm scan'))
    const notice = await screen.findByTestId('source-confirm-error')
    expect(notice).toHaveTextContent('confirm could not start the scan')
    // The dialog's Confirm button is still there to retry.
    expect(screen.getByLabelText('Confirm scan')).toBeInTheDocument()
  })
})
