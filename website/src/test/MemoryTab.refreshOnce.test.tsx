/**
 * One refresh re-reads each memory list once. The global view and the tab
 * around it both follow the trigger, and two invalidations in one commit
 * cancel the first read and send a second.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const { api } = vi.hoisted(() => ({
  api: {
    lessons: vi.fn(),
    memorySettings: vi.fn(),
    sessions: vi.fn(),
    memoryStores: vi.fn(),
    memoryRetired: vi.fn(),
    memoryBackups: vi.fn(),
    memoryCarve: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview/VectorMemoryCard', () => ({ SEMANTIC_CARD_ID: 'memory-semantic-card', default: () => <div /> }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryFailuresCard', () => ({ default: () => <div /> }))
const { default: MemoryTab } = await import('../pages/overview/MemoryTab')

beforeEach(() => {
  vi.clearAllMocks()
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [] })
  api.memoryStores.mockResolvedValue({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }], active: 'default' })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

describe('MemoryTab on a refresh', () => {
  it('re-reads the store list once, not once per component that follows the trigger', async () => {
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={1} />)
    await screen.findByText('No lessons yet')
    await waitFor(() => expect(api.memoryStores).toHaveBeenCalled())
    const before = api.memoryStores.mock.calls.length

    await act(async () => { rerender(<MemoryTab refreshTrigger={2} />) })
    expect(api.memoryStores.mock.calls.length - before).toBe(1)
  })

  it('re-reads the view once when a full refresh finds the store list changed', async () => {
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={1} liveRefreshTrigger={0} />)
    await screen.findByText('No lessons yet')
    await waitFor(() => expect(api.memoryStores).toHaveBeenCalled())
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalled())
    const backups = api.memoryBackups.mock.calls.length
    api.memoryStores.mockResolvedValue({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true, semantic_count: 9 }], active: 'default' })

    await act(async () => { rerender(<MemoryTab refreshTrigger={2} liveRefreshTrigger={1} />) })
    await waitFor(() => expect(api.memoryBackups.mock.calls.length).toBe(backups + 1))
    // A turn-end read after it finds nothing new: no second full re-read.
    await act(async () => { rerender(<MemoryTab refreshTrigger={2} liveRefreshTrigger={2} />) })
    await act(async () => {})
    expect(api.memoryBackups.mock.calls.length).toBe(backups + 1)
  })

  it('keeps a turn-end read asked during a slow full refresh, and acts on its changed answer', async () => {
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={1} liveRefreshTrigger={0} />)
    await screen.findByText('No lessons yet')
    await waitFor(() => expect(api.memoryStores).toHaveBeenCalled())
    await waitFor(() => expect(api.memoryBackups).toHaveBeenCalled())
    const unchanged = { stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }], active: 'default' }
    let land: (value: unknown) => void = () => {}
    let landOwed: (value: unknown) => void = () => {}
    // The full refresh's read is slow; the read the turn end owes finds memory written.
    // Each answer is its own network reply, so each lands in its own render.
    api.memoryStores.mockImplementationOnce(() => new Promise(resolve => { land = resolve }))
    api.memoryStores.mockImplementationOnce(() => new Promise(resolve => { landOwed = resolve }))

    await act(async () => { rerender(<MemoryTab refreshTrigger={2} liveRefreshTrigger={1} />) })
    // A turn that wrote memory ends while that read is out.
    await act(async () => { rerender(<MemoryTab refreshTrigger={2} liveRefreshTrigger={2} />) })
    const backups = api.memoryBackups.mock.calls.length
    const stores = api.memoryStores.mock.calls.length
    // The slow answer lands first and shows nothing new.
    await act(async () => { land(unchanged) })
    await waitFor(() => expect(api.memoryStores.mock.calls.length).toBeGreaterThan(stores))
    expect(api.memoryBackups.mock.calls.length).toBe(backups)
    // The owed read's changed answer re-reads the rest of the view.
    await act(async () => { landOwed({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true, semantic_count: 9 }], active: 'default' }) })
    await waitFor(() => expect(api.memoryBackups.mock.calls.length).toBeGreaterThan(backups))
  })

  it('lets a slow store-list read land, and folds the frames that arrive meanwhile into one more read', async () => {
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={1} liveRefreshTrigger={0} />)
    await screen.findByText('No lessons yet')
    await waitFor(() => expect(api.memoryStores).toHaveBeenCalled())
    const before = api.memoryStores.mock.calls.length
    let land: (value: unknown) => void = () => {}
    api.memoryStores.mockImplementationOnce(() => new Promise(resolve => { land = resolve }))
    // The read for the frames behind it stays out, as when frames keep coming.
    api.memoryStores.mockImplementationOnce(() => new Promise(() => {}))

    await act(async () => { rerender(<MemoryTab refreshTrigger={1} liveRefreshTrigger={1} />) })
    await act(async () => { rerender(<MemoryTab refreshTrigger={1} liveRefreshTrigger={2} />) })
    await act(async () => { rerender(<MemoryTab refreshTrigger={1} liveRefreshTrigger={3} />) })
    // Three frames during one slow read: still that one read.
    expect(api.memoryStores.mock.calls.length - before).toBe(1)

    const backups = api.memoryBackups.mock.calls.length
    await act(async () => { land({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true, semantic_count: 5 }], active: 'default' }) })
    // It landed, and its changed list re-reads the rest without waiting for the
    // read behind it; then one read for the three frames (and the full re-read's own).
    await waitFor(() => expect(api.memoryBackups.mock.calls.length).toBeGreaterThan(backups))
    expect(api.memoryStores.mock.calls.length - before).toBeGreaterThanOrEqual(2)
  })
})
