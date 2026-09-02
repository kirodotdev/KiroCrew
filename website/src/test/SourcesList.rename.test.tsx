import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const sources: Source[] = [
  { id: 's1', name: 'Old Name', source_type: 'local_file', uri: '/tmp/doc.md', sync_status: 'synced', item_count: 3 },
]

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderList() {
  return render(
    <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
    { wrapper },
  )
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    return { ok: true } as unknown as never
  })
})

describe('SourcesList — rename source', () => {
  it('opens the editor and PATCHes the new name', async () => {
    renderList()
    await screen.findByText('Old Name')
    fireEvent.click(screen.getByLabelText('Rename source'))
    const input = screen.getByLabelText('Source name')
    fireEvent.change(input, { target: { value: 'New Name' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() =>
      expect(api.knowledgeApi).toHaveBeenCalledWith(
        '/sources/s1',
        expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: 'New Name' }) }),
      ),
    )
  })

  it('Escape cancels without a PATCH', async () => {
    renderList()
    await screen.findByText('Old Name')
    fireEvent.click(screen.getByLabelText('Rename source'))
    const input = screen.getByLabelText('Source name')
    fireEvent.change(input, { target: { value: 'Throwaway' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    await screen.findByText('Old Name')
    const patchCalls = vi.mocked(api.knowledgeApi).mock.calls.filter(
      ([, opts]) => (opts as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patchCalls).toHaveLength(0)
  })

  it('does not PATCH when the name is unchanged', async () => {
    renderList()
    await screen.findByText('Old Name')
    fireEvent.click(screen.getByLabelText('Rename source'))
    const input = screen.getByLabelText('Source name')
    fireEvent.keyDown(input, { key: 'Enter' })
    const patchCalls = vi.mocked(api.knowledgeApi).mock.calls.filter(
      ([, opts]) => (opts as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patchCalls).toHaveLength(0)
  })

  it('ignores a second Enter while a PATCH is in-flight', async () => {
    let resolvePatch: (v: unknown) => void = () => {}
    vi.mocked(api.knowledgeApi).mockImplementation(async (path: string, opts?: RequestInit) => {
      if (path === '/sources') return sources as unknown as never
      if (opts?.method === 'PATCH') return new Promise(res => { resolvePatch = res }) as unknown as never
      return { ok: true } as unknown as never
    })
    renderList()
    await screen.findByText('Old Name')
    fireEvent.click(screen.getByLabelText('Rename source'))
    fireEvent.change(screen.getByLabelText('Source name'), { target: { value: 'New Name' } })
    const patchCount = () =>
      vi.mocked(api.knowledgeApi).mock.calls.filter(
        ([, opts]) => (opts as RequestInit | undefined)?.method === 'PATCH',
      ).length
    fireEvent.keyDown(screen.getByLabelText('Source name'), { key: 'Enter' })
    await waitFor(() => expect(patchCount()).toBe(1))
    // Editor stays open while the mutation is pending; a second Enter must be a no-op.
    fireEvent.keyDown(screen.getByLabelText('Source name'), { key: 'Enter' })
    expect(patchCount()).toBe(1)
    resolvePatch({ ok: true })
    await waitFor(() => expect(screen.queryByLabelText('Source name')).toBeNull())
  })
})
