import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'

const api = vi.hoisted(() => ({
  lessons: vi.fn(), memorySettings: vi.fn(), sessions: vi.fn(), memoryStores: vi.fn(),
  memoryRetired: vi.fn(), memoryBackups: vi.fn(), memoryCarve: vi.fn(),
}))
vi.mock('./client', () => ({ api }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => null }))
vi.mock('../pages/overview/MemoryFailuresCard', () => ({ default: () => null }))
const { default: MemoryTab } = await import('../pages/overview/MemoryTab')

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset()
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [] })
  api.memoryStores.mockResolvedValue({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }], active: 'default' })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

describe('memory query invalidation', () => {
  it('invalidates cached audit pages and facets across stores on a memory-page refresh', async () => {
    const { queryClient, rerender } = renderWithProviders(<MemoryTab refreshTrigger={1} />, {
      queryDefaults: { staleTime: Infinity },
    })
    await screen.findByText('No lessons yet')
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalled())
    const query = { q: '', order: 'desc', filters: {} }
    const keys = ['default', 'reviewer'].flatMap(store => [
      ['memory-audit', store, query], ['memory-audit', store, query, 'counts'],
    ])
    for (const key of keys) {
      queryClient.setQueryData(key, { cached: true })
      expect(queryClient.getQueryState(key)?.isInvalidated).toBe(false)
    }
    queryClient.setQueryData(['unrelated'], { cached: true })

    await act(async () => { rerender(<MemoryTab refreshTrigger={2} />) })

    for (const key of keys) expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(['unrelated'])?.isInvalidated).toBe(false)
  })
})
