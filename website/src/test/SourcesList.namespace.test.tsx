import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return [] as unknown as never
    return { ok: true, id: 'new-id', status: 'pending_confirmation', file_count: 5 } as unknown as never
  })
})

describe('SourcesList — namespace in handleAdd', () => {
  it('includes properties.namespace for local_folder when uploadNamespace is set', async () => {
    render(
      <SourcesList
        onIngest={() => {}}
        uploadNamespace="my-ns"
        setUploadNamespace={() => {}}
        namespaces={['default', 'my-ns']}
        ingestionJobs={[]}
      />,
      { wrapper },
    )

    // Wait for loading to finish (sources query resolves)
    const addBtn = await screen.findByText('+ Add Source')
    fireEvent.click(addBtn)
    fireEvent.click(screen.getByText('Local Folder'))
    const input = screen.getByPlaceholderText(/Folder path/)
    fireEvent.change(input, { target: { value: '/tmp/docs' } })
    fireEvent.click(screen.getByText('Add Folder'))

    await waitFor(() =>
      expect(api.knowledgeApi).toHaveBeenCalledWith(
        '/sources',
        expect.objectContaining({
          method: 'POST',
          body: expect.stringContaining('"namespace":"my-ns"'),
        }),
      ),
    )
  })

  it('omits properties.namespace when uploadNamespace is default', async () => {
    render(
      <SourcesList
        onIngest={() => {}}
        uploadNamespace="default"
        setUploadNamespace={() => {}}
        namespaces={['default']}
        ingestionJobs={[]}
      />,
      { wrapper },
    )

    const addBtn = await screen.findByText('+ Add Source')
    fireEvent.click(addBtn)
    fireEvent.click(screen.getByText('Local Folder'))
    const input = screen.getByPlaceholderText(/Folder path/)
    fireEvent.change(input, { target: { value: '/tmp/docs' } })
    fireEvent.click(screen.getByText('Add Folder'))

    await waitFor(() => {
      const postCalls = vi.mocked(api.knowledgeApi).mock.calls.filter(
        ([path, opts]) => path === '/sources' && (opts as RequestInit | undefined)?.method === 'POST',
      )
      expect(postCalls.length).toBe(1)
      const body = JSON.parse((postCalls[0][1] as RequestInit).body as string)
      expect(body.properties).not.toHaveProperty('namespace')
    })
  })
})
