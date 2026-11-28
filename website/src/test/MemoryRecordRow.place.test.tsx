import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { MemoryRecord } from '../types/memoryEditing'

/**
 * A row's place in the windowed list (its index and set size) changes for every
 * row below one that comes or goes. The `<li>` takes the new place; the row's
 * content does not render again unless the record or its state changed.
 */
const { body } = vi.hoisted(() => ({ body: vi.fn() }))
vi.mock('../pages/overview/memoryRecordParts', async importOriginal => {
  const actual = await importOriginal<typeof import('../pages/overview/memoryRecordParts')>()
  return { ...actual, memoryRecordBody: (row: MemoryRecord) => { body(row.id); return actual.memoryRecordBody(row) } }
})
const { RecordRow } = await import('../pages/overview/memoryPage/MemoryListParts')

const record = (value: string): MemoryRecord => ({ kind: 'fact', id: 'fact.1', key: 'fact.1', value_json: JSON.stringify(value), text: '', importance: 0.5, source: 'user_explicit', updated_at: '2026-11-20T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64) })
const noop = () => {}

function row(item: MemoryRecord, place: { index: number; size: number }) {
  return <ul><RecordRow row={item} sort="updated" selecting={false} selected={false} selectDisabled={false} open={false}
    onSelect={noop} onOpen={noop} onEdit={noop} onForget={noop}
    data-index={place.index} data-item-index={place.index} aria-posinset={place.index + 1} aria-setsize={place.size} /></ul>
}

beforeEach(() => { body.mockClear() })

describe('a memory row moving in the windowed list', () => {
  it('takes its new place without rendering its content again', () => {
    const item = record('Value 1')
    const { rerender } = renderWithProviders(row(item, { index: 4, size: 40 }))
    expect(body).toHaveBeenCalledTimes(1)
    rerender(row(item, { index: 1, size: 31 }))
    const li = screen.getByRole('listitem')
    expect(li).toHaveAttribute('aria-posinset', '2')
    expect(li).toHaveAttribute('aria-setsize', '31')
    expect(li).toHaveAttribute('data-index', '1')
    expect(screen.getByText('Value 1')).toBeInTheDocument()
    expect(body).toHaveBeenCalledTimes(1)
  })

  it('renders its content again when the record changed', () => {
    const { rerender } = renderWithProviders(row(record('Value 1'), { index: 0, size: 2 }))
    rerender(row(record('Value 2'), { index: 0, size: 2 }))
    expect(screen.getByText('Value 2')).toBeInTheDocument()
    expect(body).toHaveBeenCalledTimes(2)
  })
})
