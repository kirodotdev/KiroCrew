import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { useLayoutEffect, type ReactNode } from 'react'
import { renderWithProviders } from './helpers'

/**
 * A memory list that opens on answers already in the query cache shows its rows
 * in the first frame, with no loading rows before them. Without a cached answer
 * it opens on loading rows.
 */
const { api } = vi.hoisted(() => ({
  api: { memoryRecords: vi.fn(), memoryStores: vi.fn(), memoryRecordHistory: vi.fn() },
}))
vi.mock('../api/client', () => ({ api }))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')

const row = (i: number) => ({ kind: 'fact', id: `fact.${i}`, key: `fact.${i}`, value_json: JSON.stringify(`Value ${i}`), text: '', importance: 0.5, source: 'user_explicit', updated_at: '2026-11-20T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64) })

type Frame = { rows: number; placeholders: number }
/** Reads the page as the first commit left it: its layout effect runs after the
 *  list's own, before any render those effects ask for, so before the first paint. */
function FirstFrame({ onFrame, children }: { onFrame: (frame: Frame) => void; children: ReactNode }) {
  return <>{children}<Shot onFrame={onFrame} /></>
}
function Shot({ onFrame }: { onFrame: (frame: Frame) => void }) {
  useLayoutEffect(() => {
    onFrame({
      rows: [...document.querySelectorAll('li')].filter(item => /Value \d/.test(item.textContent || '')).length,
      placeholders: document.querySelectorAll('ul[role="status"] > li').length,
    })
  }, [onFrame])
  return null
}

const list = (key: string, onFrame: (frame: Frame) => void) => <FirstFrame key={key} onFrame={onFrame}>
  <MemoryList store="member-x" storeName="x" privateMemory selecting={false} onSelectingChange={() => {}} />
</FirstFrame>

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
  api.memoryRecords.mockResolvedValue({ entries: [row(1), row(2), row(3)], total: 3, has_more: false, facets: {} })
})

describe('opening the memory list', () => {
  it('shows cached rows in its first frame, with no loading rows', async () => {
    const frames: Frame[] = []
    const { rerender, queryClient } = renderWithProviders(list('first', frame => frames.push(frame)))
    await screen.findByText('Value 3')
    expect(queryClient.getQueryCache().findAll({ queryKey: ['memory-records', 'member-x'] }).length).toBeGreaterThan(0)
    // The page opens again on the same cache: a new list, a new store.
    const again: Frame[] = []
    rerender(list('second', frame => again.push(frame)))
    expect(again[0]).toEqual({ rows: 3, placeholders: 0 })
  })

  it('opens on loading rows when nothing is cached', () => {
    api.memoryRecords.mockReturnValue(new Promise(() => {}))
    const frames: Frame[] = []
    renderWithProviders(list('first', frame => frames.push(frame)))
    expect(frames[0]).toEqual({ rows: 0, placeholders: 3 })
  })
})
