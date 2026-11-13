import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const sources: Source[] = [
  {
    id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/home/u/notes', sync_status: 'synced',
    item_count: 3, properties: JSON.stringify({ recursive: true, ignore_patterns: ['.trash/*'] }),
  },
]

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    if (path === '/sources/s1/files') return { files: [], total: 0, done: 0, failed: 0, skipped: 0 } as unknown as never
    return { ok: true } as unknown as never
  })
})

describe('SourcesList — edit a folder source\'s ignore patterns', () => {
  it('shows the saved patterns and PATCHes the edited list, trimmed and without blank lines', async () => {
    render(
      <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
      { wrapper },
    )
    await screen.findByText('Notes')
    fireEvent.click(screen.getByLabelText('Expand folder details'))
    const box = screen.getByLabelText('Ignore patterns') as HTMLTextAreaElement
    expect(box.value).toBe('.trash/*')
    // A visible label, bound to the box, that stays once patterns fill it.
    const label = screen.getByText('Ignore patterns', { selector: 'label' })
    expect(label).toBeVisible()
    expect(label).toHaveAttribute('for', box.id)
    expect(box).not.toHaveAttribute('aria-label')
    // The pattern format stays readable once saved patterns hide the placeholder.
    expect(box).toHaveAccessibleDescription(/One pattern per line/)
    const save = screen.getByRole('button', { name: 'Save patterns' })
    expect(save).toBeDisabled()

    fireEvent.change(box, { target: { value: '.trash/*\n  generated/**  \n\n' } })
    fireEvent.click(save)

    await waitFor(() =>
      expect(api.knowledgeApi).toHaveBeenCalledWith(
        '/sources/s1',
        expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ ignore_patterns: ['.trash/*', 'generated/**'] }) }),
      ),
    )
  })
})
